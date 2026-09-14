"""Retrieval helpers shared by every grounded skill.

These two functions were originally private to the Q&A skill. They moved here
when the Ship 30 essay skill needed both: a skill importing a private name from
a sibling skill couples the two, and the next skill would have copied them
instead -- at which point the passage numbering in a source card could silently
drift from the numbering in the prompt, which is precisely the bug that would
make a citation point at the wrong thing.
"""

from __future__ import annotations

import re

from app.llm.base import ChatMessage
from app.retrieval.retriever import RetrievalResult, RetrievedChunk

# Phrasings that only make sense relative to the previous turn. A follow-up like
# "explain that second point" or "turn that into an essay" retrieves nothing
# useful on its own, because the words that carry the topic are in the
# *previous* message.
_ANAPHORIC = re.compile(
    r"\b(that|this|those|these|it|they|them|he|she|his|her|the (?:first|second|third|last|next)"
    r"\s+(?:point|one|part|idea)|more|deeper|expand|elaborate|instead|why not)\b",
    re.IGNORECASE,
)
# Above this length a message carries enough of its own topic words that
# borrowing the previous question adds noise rather than signal.
_FOLLOW_UP_MAX_WORDS = 18

# Words that name an *output format* or a *command*, never a subject. For a
# generative request these are pure noise in a retrieval query, and measurably
# harmful noise: "How do I improve activation for a B2B SaaS product? Turn that
# into an HTML landing page with CSS." scored 0.4177 against the live index
# where the question alone scored 0.4976 -- across the 0.48 refusal threshold,
# so asking for a landing page about a topic made the topic unanswerable. The
# terms are strong lexical signals ("html", "css", "page") that pull the hybrid
# retriever toward genuinely unrelated engineering passages.
#
# They are stripped rather than the whole message being discarded, so a request
# that *does* add a subject ("turn that into a one-pager about onboarding")
# keeps "onboarding".
_FORMAT_TERMS = re.compile(
    r"\b(html|css|markdown|md|landing page|web ?page|one[- ]pager?|web ?site|"
    r"mock ?-?up|wireframe|checklist|template|essay|ship ?30(?: for 30)?|article|"
    r"blog post|newsletter|linkedin|thought leadership|\d{3,4}[- ]word|page|post|"
    r"doc|document|write|draft|create|build|make|generate|produce|turn|give me|"
    r"styled|for me|version of)\b",
    re.IGNORECASE,
)


def strip_format_terms(message: str) -> str:
    """Remove output-format and command words, leaving the subject matter."""
    remainder = _FORMAT_TERMS.sub(" ", message or "")
    return " ".join(remainder.split())


# Below this many *content* words, whatever survives `strip_format_terms` is
# connector debris ("that into an with"), not a subject -- see
# `build_retrieval_query`. Counted after dropping stopwords, because the
# residue of a pure format instruction is almost entirely stopwords ("that",
# "into", "an", "with") and a raw word count does not tell them apart from a
# real two-word subject ("about onboarding").
_MIN_RESIDUE_WORDS = 2
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
        "had", "has", "have", "i", "if", "in", "into", "is", "it", "its", "of",
        "on", "or", "that", "the", "their", "them", "then", "there", "these",
        "they", "this", "those", "to", "was", "were", "will", "with", "your", "you",
    }
)


def _content_words(text: str) -> list[str]:
    return [w for w in text.split() if w.strip(".,!?;:").lower() not in _STOPWORDS]


def build_retrieval_query(
    message: str, history: list[ChatMessage], *, drop_format_terms: bool = False
) -> str:
    """Resolve a follow-up against the previous user turn, when it needs it.

    Deliberately a heuristic and not a model call: query rewriting on a local 7B
    costs more latency than the retrieval it feeds, and gets the easy cases
    (which are most cases) no more right than concatenation does. When the
    heuristic is wrong the cost is a slightly noisier query, not a wrong answer.

    `drop_format_terms` is set by the generative skills (essay, artifact), whose
    requests are mostly instructions about *form*. Grounded Q&A leaves it off:
    there, every word the user typed is about the subject.
    """
    cleaned = strip_format_terms(message) if drop_format_terms else message
    if len(message.split()) > _FOLLOW_UP_MAX_WORDS or not _ANAPHORIC.search(message):
        # A standalone request still benefits from having its format words
        # removed -- "write a 1250 word essay on pricing power" retrieves on
        # "pricing power" -- but only if something is left to retrieve on.
        return cleaned or message
    previous = next((m.content for m in reversed(history) if m.role == "user"), None)
    if not previous:
        return cleaned or message
    # A pure format instruction ("Turn that into an HTML landing page with
    # CSS.") strips down to a handful of connector words -- "that into an
    # with" -- once the format terms are gone. Appending that residue to the
    # previous question is not neutral: it is off-topic noise the retriever
    # still has to explain, and it measurably moved a real query (0.4976)
    # below the refusal threshold (0.4177) on the live index. Below this
    # length the residue carries no subject of its own, so the previous
    # question alone is used undiluted.
    if len(_content_words(cleaned)) < _MIN_RESIDUE_WORDS:
        return previous
    return f"{previous} {cleaned}".strip()


def sources_payload(
    chunks: list[RetrievedChunk], retrieval: RetrievalResult, *, cited: bool = False
) -> list[dict[str, object]]:
    """Source cards, numbered to match the [S#] markers the model was given.

    The enumeration starting at 1 is load-bearing, not cosmetic: it is the same
    numbering `build_passage_block` writes into the prompt and the same one
    `validate_and_repair` checks markers against.
    """
    return [
        {
            "index": position,
            "label": f"S{position}",
            "cited": cited,
            "retrieval_method": retrieval.method,
            **chunk.as_dict(),
        }
        for position, chunk in enumerate(chunks, start=1)
    ]

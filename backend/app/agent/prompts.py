"""Prompt construction for grounded answering.

The rules below are written as hard constraints rather than suggestions because
a 7B model treats soft guidance as optional. Two of them exist specifically to
counter failure modes seen while building this:

* *"Every paragraph must carry a citation"* -- without it the model reliably
  cites its first paragraph and then continues from general knowledge.
* *"Do not cite a number you were not given"* -- the model would otherwise
  extrapolate `[S7]` and `[S8]` past the passages it was handed.

Both are also *checked* after generation (`app.agent.citations`), because a
prompt rule that is not verified is a hope.
"""

from __future__ import annotations

from app.llm.base import ChatMessage
from app.retrieval.retriever import RetrievedChunk

GROUNDED_SYSTEM = """You are a product and growth assistant. You answer ONLY \
from the numbered transcript passages supplied with the question. They come from \
Lenny's Podcast interviews with operators.

Rules, all mandatory:
1. Use only the supplied passages. If they do not support a claim, do not make it.
2. Cite with square-bracket markers: [S1], [S2]. Cite the passage that supports \
the specific claim, not a general list at the end.
3. EVERY paragraph and EVERY bullet must contain at least one citation marker.
4. Only cite marker numbers that appear in the passages below. Never invent one.
5. Name the speaker when their view matters ("Brian Chesky argues... [S2]").
6. If the passages disagree, say so and cite both sides.
7. If the passages do not answer the question, say plainly what is missing \
rather than filling the gap from general knowledge.
8. Be concrete and specific. No throat-clearing, no "it depends" without saying \
what it depends on. Aim for 150-350 words unless the question needs more."""

REFUSAL_SYSTEM = """You are a product and growth assistant whose only knowledge \
source is a set of Lenny's Podcast transcripts. The search found nothing that \
supports an answer to the user's question.

Say so directly, in two or three sentences. State what the corpus does cover \
(product, growth, and startup operating topics) and, if the closest matches \
listed below are related but insufficient, mention what they were about. Do NOT \
answer the question from general knowledge, and do not use citation markers."""


def estimate_tokens(text: str) -> int:
    """Cheap character-based estimate.

    A real tokenizer would be more accurate but would tie the budget to one
    model's vocabulary, and the budget only needs to be conservative -- it is
    used to decide how many passages fit, never to bill anything.
    """
    return max(1, len(text) // 4)


def format_passage(index: int, chunk: RetrievedChunk) -> str:
    speakers = ", ".join(chunk.speakers) if chunk.speakers else (chunk.guest or "Unknown")
    stamp = f" at {chunk.timestamp_label}" if chunk.timestamp_label else ""
    return (
        f"[S{index}] {chunk.episode_title} - {speakers}{stamp}\n"
        f"{chunk.content.strip()}"
    )


def build_passage_block(chunks: list[RetrievedChunk], token_budget: int) -> tuple[str, int]:
    """Format passages in rank order until the budget is spent.

    Returns the block and how many passages actually fit, because the citation
    validator must be told the real number -- telling it 6 when only 4 were sent
    would make a hallucinated [S5] look legitimate.
    """
    parts: list[str] = []
    used = 0
    for position, chunk in enumerate(chunks, start=1):
        formatted = format_passage(position, chunk)
        cost = estimate_tokens(formatted)
        if parts and used + cost > token_budget:
            break
        parts.append(formatted)
        used += cost
    return "\n\n".join(parts), len(parts)


def build_grounded_messages(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[ChatMessage],
    *,
    context_tokens: int,
    max_output_tokens: int,
) -> tuple[list[ChatMessage], int]:
    """Assemble the full prompt within the model's real context window.

    Order of eviction when space is tight is deliberate: conversation history
    goes first, passages last. An answer without history is merely less
    conversational; an answer without passages is ungrounded, which is the one
    failure this product cannot ship.
    """
    reserve = max_output_tokens + estimate_tokens(GROUNDED_SYSTEM) + 256
    available = max(512, context_tokens - reserve)

    history_budget = min(available // 4, 2000)
    trimmed_history = _trim_history(history, history_budget)
    history_cost = sum(estimate_tokens(m.content) for m in trimmed_history)

    passage_budget = available - history_cost - estimate_tokens(question)
    block, used_count = build_passage_block(chunks, passage_budget)

    user_content = (
        f"Transcript passages:\n\n{block}\n\n"
        f"---\nQuestion: {question}\n\n"
        f"Answer using only the passages above, citing [S1]-[S{used_count}]."
    )
    messages = [
        ChatMessage(role="system", content=GROUNDED_SYSTEM),
        *trimmed_history,
        ChatMessage(role="user", content=user_content),
    ]
    return messages, used_count


def build_refusal_messages(
    question: str, chunks: list[RetrievedChunk], confidence: float
) -> list[ChatMessage]:
    if chunks:
        nearest = "\n".join(
            f"- {c.episode_title}: {c.content.strip()[:160]}..." for c in chunks[:3]
        )
        context = f"Closest matches found (all too weak to rely on):\n{nearest}"
    else:
        context = "The search returned no passages at all."
    return [
        ChatMessage(role="system", content=REFUSAL_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                f"Question: {question}\n\n{context}\n\n"
                f"(Retrieval confidence was {confidence:.2f}.)"
            ),
        ),
    ]


def _trim_history(history: list[ChatMessage], budget: int) -> list[ChatMessage]:
    """Keep the most recent turns that fit, oldest dropped first."""
    kept: list[ChatMessage] = []
    used = 0
    for message in reversed(history):
        cost = estimate_tokens(message.content)
        if used + cost > budget:
            break
        kept.append(message)
        used += cost
    return list(reversed(kept))

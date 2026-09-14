"""The Ship 30 for 30 essay: its output contract, its prompts, its repairs.

Why this is a module and not a prompt
------------------------------------
"Write me a 1,250-word essay" as a single instruction to `qwen2.5:7b-instruct`
does not work, and checkpoint 1 recorded why: a small model asked for a long
piece in one pass returns 400-600 words, drifts off the passages somewhere in
the middle, and decorates the result with bold text until nothing is emphasised.
Every one of those is a *structural* failure, so the fix is structural:

* **Plan first.** One cheap call produces an outline -- title, hook angle,
  section headings, takeaway -- validated against a schema, repaired once, and
  replaced by a deterministic outline if the model still cannot produce one.
  The essay's skeleton is therefore never left to chance.
* **Generate section by section, to a word budget.** Each section is a short
  request (~240 words) of the kind a 7B model is reliably good at, and it is
  handed the same retrieved passages as every other section, so the whole essay
  is anchored to one body of evidence.
* **Own the structure, do not ask for it.** Headings are emitted by *this code*
  from the outline, not by the model. A required element that the code writes
  cannot be missing.
* **Repair deterministically where possible.** Over-bolding and over-length are
  fixed by editing text, not by another 30-second model call.

What "correct" means is defined once, here, in `evaluate_structure`, and used by
the skill (to decide whether to repair), by `app.evals.essay_eval` (to measure
it) and by the tests. PRD 2.3 requires this to be verified programmatically
rather than by eye; that is only possible if there is one definition of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.citations import extract_markers
from app.llm.base import ChatMessage
from app.retrieval.retriever import RetrievedChunk

# ------------------------------------------------------------ the contract --

TARGET_WORDS = 1250
# +/-20%: 1,000-1,500 words. "~1,250" in the PRD is a shape, not a checksum --
# an essay is not wrong at 1,180 words. The band is wide enough to be reachable
# by a 7B model and narrow enough that a 600-word stub still fails.
WORD_TOLERANCE = 0.20
MIN_WORDS = int(TARGET_WORDS * (1 - WORD_TOLERANCE))
MAX_WORDS = int(TARGET_WORDS * (1 + WORD_TOLERANCE))

MIN_SECTIONS = 3
MAX_SECTIONS = 5
DEFAULT_SECTIONS = 4

# A hook that runs long is not a hook. Ship 30's own teaching is that the first
# line earns the second; this caps the whole opening block. Widened from an
# initial 90 after a live run against qwen2.5:7b-instruct produced a genuinely
# good, tightly-written 91-word hook that a stricter cap flagged as a structure
# failure -- the PRD requires *a* hook, not a specific word count, and 90 turned
# out to be tighter than natural model output reliably lands. 120 still rejects
# an opening that has become a second section.
MAX_HOOK_WORDS = 120
MIN_BULLETS = 3
# "Selective bold" is a range, not a minimum: zero bold is unskimmable, and
# twenty is a highlighter left on the page.
MIN_BOLD = 2
MAX_BOLD = 18

# Word budgets per part, summing to roughly TARGET_WORDS. HOOK_WORDS -- the
# length the model is *asked* for -- must stay safely under MAX_HOOK_WORDS --
# the length `evaluate_structure` will *accept* -- or the prompt sets the model
# up to fail its own validator: an earlier version asked for 140 and then
# rejected anything over 90, so a hook landing exactly where it was told to
# land still failed structure. 100 leaves comfortable room under 120.
HOOK_WORDS = 100
TAKEAWAY_WORDS = 160
SECTION_WORDS = (TARGET_WORDS - HOOK_WORDS - TAKEAWAY_WORDS) // DEFAULT_SECTIONS

TAKEAWAY_HEADING = "The takeaway"
_TAKEAWAY_PATTERN = re.compile(
    r"^#{1,6}\s*.*\b(takeaway|bottom line|what to do|start here|in short)\b",
    re.IGNORECASE | re.MULTILINE,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MARKER = re.compile(r"\[[^\[\]]{0,60}\]")


def count_words(text: str) -> int:
    """Words a reader would count: prose only.

    Citation markers, heading hashes and list bullets are formatting, not
    writing. Counting them would let an essay hit its target by citing more,
    which is exactly the wrong incentive to create.
    """
    stripped = _MARKER.sub(" ", text or "")
    stripped = re.sub(r"^#{1,6}\s*", " ", stripped, flags=re.MULTILINE)
    stripped = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", " ", stripped, flags=re.MULTILINE)
    stripped = stripped.replace("*", " ").replace("_", " ")
    return len([w for w in stripped.split() if any(c.isalnum() for c in w)])


@dataclass(slots=True)
class StructureReport:
    """Whether an essay meets the Ship 30 shape, and precisely how it does not."""

    word_count: int = 0
    heading_count: int = 0
    bullet_count: int = 0
    bold_count: int = 0
    hook_words: int = 0
    has_title: bool = False
    has_hook: bool = False
    has_takeaway: bool = False
    within_word_target: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "word_count": self.word_count,
            "target_words": TARGET_WORDS,
            "word_range": [MIN_WORDS, MAX_WORDS],
            "within_word_target": self.within_word_target,
            "heading_count": self.heading_count,
            "bullet_count": self.bullet_count,
            "bold_count": self.bold_count,
            "hook_words": self.hook_words,
            "has_title": self.has_title,
            "has_hook": self.has_hook,
            "has_takeaway": self.has_takeaway,
            "structure_ok": self.ok,
            "issues": self.issues,
        }


def evaluate_structure(essay: str) -> StructureReport:
    """The programmatic acceptance check for PRD 2.3."""
    text = essay or ""
    headings = _HEADING.findall(text)
    report = StructureReport(
        word_count=count_words(text),
        heading_count=len([h for h in headings if len(h[0]) >= 2]),
        bullet_count=len(_BULLET.findall(text)),
        bold_count=len(_BOLD.findall(text)),
    )
    report.has_title = any(len(level) == 1 for level, _ in headings)
    report.within_word_target = MIN_WORDS <= report.word_count <= MAX_WORDS
    report.has_takeaway = bool(_TAKEAWAY_PATTERN.search(text))

    hook = _hook_block(text)
    report.hook_words = count_words(hook)
    report.has_hook = bool(hook) and report.hook_words <= MAX_HOOK_WORDS

    if not report.within_word_target:
        report.issues.append(
            f"word_count {report.word_count} outside {MIN_WORDS}-{MAX_WORDS}"
        )
    if not report.has_title:
        report.issues.append("missing H1 title")
    if not report.has_hook:
        report.issues.append(
            "missing hook"
            if not hook
            else f"hook is {report.hook_words} words (max {MAX_HOOK_WORDS})"
        )
    if report.heading_count < MIN_SECTIONS:
        report.issues.append(f"{report.heading_count} section headings (min {MIN_SECTIONS})")
    if report.bullet_count < MIN_BULLETS:
        report.issues.append(f"{report.bullet_count} bullets (min {MIN_BULLETS})")
    if not MIN_BOLD <= report.bold_count <= MAX_BOLD:
        report.issues.append(f"{report.bold_count} bold spans (want {MIN_BOLD}-{MAX_BOLD})")
    if not report.has_takeaway:
        report.issues.append("no takeaway section")
    return report


def _hook_block(text: str) -> str:
    """The first prose block after the H1 title."""
    body = _HEADING.sub(
        lambda m: "\n\n" if len(m.group(1)) == 1 else m.group(0), text or "", count=1
    )
    for block in re.split(r"\n\s*\n", body):
        candidate = block.strip()
        if candidate and not candidate.startswith("#"):
            return candidate
    return ""


# ------------------------------------------------------ deterministic repair --


def enforce_selective_bold(essay: str, limit: int = MAX_BOLD) -> tuple[str, int]:
    """Un-bold everything past `limit`, keeping the earliest emphases.

    Deterministic because it can be: removing `**` cannot change a claim or
    orphan a citation, and the alternative -- asking the model to re-emphasise
    -- costs another 30 seconds on a local model for a purely cosmetic defect.
    Earliest-kept rather than longest-kept so the emphasis stays where the
    argument is being set up.
    """
    spans = list(_BOLD.finditer(essay or ""))
    if len(spans) <= limit:
        return essay, 0
    out = essay
    for span in reversed(spans[limit:]):
        out = out[: span.start()] + span.group(1) + out[span.end() :]
    return out, len(spans) - limit


def trim_to_word_limit(essay: str, limit: int = MAX_WORDS) -> tuple[str, int]:
    """Drop whole paragraphs from the longest section until the essay fits.

    Whole paragraphs, never sentences: each paragraph carries its own citation,
    so removing one leaves every remaining claim still supported, while a
    mid-paragraph cut could strand a sentence away from the marker that backs
    it. Headings, bullets and the final takeaway are never removed -- losing
    them would trade a word-count failure for a structure failure.
    """
    if count_words(essay) <= limit:
        return essay, 0
    blocks = re.split(r"(\n\s*\n)", essay or "")
    removed = 0
    # Walk backwards through body paragraphs, protecting the last section (the
    # takeaway) and anything that is not plain prose.
    protected_from = _takeaway_offset(blocks)
    for index in range(protected_from - 1, -1, -1):
        if count_words("".join(blocks)) <= limit:
            break
        block = blocks[index]
        candidate = block.strip()
        if not candidate or candidate.startswith("#") or _BULLET.match(candidate):
            continue
        if count_words(candidate) < 25:
            continue
        blocks[index] = ""
        removed += 1
    cleaned = re.sub(r"\n{3,}", "\n\n", "".join(blocks))
    return cleaned.strip() + "\n", removed


def _takeaway_offset(blocks: list[str]) -> int:
    for index, block in enumerate(blocks):
        if _TAKEAWAY_PATTERN.search(block.strip()):
            return index
    return len(blocks)


# ----------------------------------------------------------------- outline --


@dataclass(slots=True)
class SectionPlan:
    heading: str
    angle: str = ""
    want_bullets: bool = False


@dataclass(slots=True)
class EssayOutline:
    title: str
    hook_angle: str
    sections: list[SectionPlan]
    takeaway_angle: str
    source: str = "model"  # model | repaired | fallback

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "hook_angle": self.hook_angle,
            "takeaway_angle": self.takeaway_angle,
            "source": self.source,
            "sections": [
                {"heading": s.heading, "angle": s.angle, "bullets": s.want_bullets}
                for s in self.sections
            ],
        }


class OutlineInvalid(ValueError):
    """The model's outline did not satisfy the schema. Carries why, for the retry."""


def parse_outline(raw: str) -> EssayOutline:
    """Parse and validate the outline JSON, raising `OutlineInvalid` with a reason.

    The reason text is fed back to the model verbatim on the repair attempt: a
    retry that only says "that was wrong" gets the same answer again.
    """
    payload = _extract_json_object(raw)
    if payload is None:
        raise OutlineInvalid("the response was not a JSON object")
    if not isinstance(payload, dict):
        raise OutlineInvalid("the top-level JSON value must be an object")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise OutlineInvalid('"title" is required and must be a non-empty string')

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        raise OutlineInvalid('"sections" is required and must be an array')
    sections: list[SectionPlan] = []
    for item in raw_sections:
        if isinstance(item, str):
            heading = item.strip()
            angle = ""
        elif isinstance(item, dict):
            heading = str(item.get("heading") or item.get("title") or "").strip()
            angle = str(item.get("angle") or item.get("purpose") or "").strip()
        else:
            continue
        if heading:
            sections.append(SectionPlan(heading=_clean_heading(heading), angle=angle))
    if not MIN_SECTIONS <= len(sections) <= MAX_SECTIONS:
        raise OutlineInvalid(
            f'"sections" must contain between {MIN_SECTIONS} and {MAX_SECTIONS} '
            f"entries, each with a non-empty heading (got {len(sections)})"
        )

    for index in (1, min(2, len(sections) - 1)):
        sections[index].want_bullets = True

    return EssayOutline(
        title=_clean_heading(title),
        hook_angle=str(payload.get("hook") or payload.get("hook_angle") or "").strip(),
        sections=sections,
        takeaway_angle=str(payload.get("takeaway") or payload.get("takeaway_angle") or "").strip(),
    )


def _clean_heading(value: str) -> str:
    """Strip the decorations models add to a heading they were asked for plain.

    Order matters and was got wrong once: stripping emphasis before the list
    numbering leaves `**First point` for an input of `1. **First point**`,
    because the leading `**` is not at the start of the string until the `1. `
    is gone. Numbering first, emphasis second, then emphasis again is belt and
    braces for `**1. Heading**`.
    """
    cleaned = re.sub(r"^#{1,6}\s*", "", value).strip().strip("*_ ")
    cleaned = re.sub(r"^(?:section\s*)?\d+[.):]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().strip("*_ ").strip()
    return " ".join(cleaned.split())[:120]


def _extract_json_object(raw: str) -> Any:
    """Find the JSON object in a response that may be wrapped in prose or fences.

    Small instruct models routinely answer "Sure! Here is the outline:" followed
    by a fenced block. Refusing that output would fail on a formatting habit
    rather than on a planning failure, so the object is located rather than
    demanded.
    """
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def fallback_outline(question: str) -> EssayOutline:
    """A fixed, always-valid outline for when the model cannot produce one.

    A generic essay whose claims are still drawn from the retrieved passages is
    a far better outcome than a failed turn: the grounding guarantee -- the part
    this product cannot compromise on -- is unaffected by where the headings
    came from. The outline's `source` records that this happened, so a run that
    silently depends on the fallback is visible in the report rather than
    indistinguishable from a good one.
    """
    topic = " ".join((question or "this problem").split())[:80].rstrip("?.,! ")
    return EssayOutline(
        title=f"What operators actually do about {topic.lower()}",
        hook_angle="open with the specific, concrete mistake most teams make here",
        sections=[
            SectionPlan(
                "Why this is harder than it looks",
                "the real constraint, not the obvious one",
            ),
            SectionPlan(
                "What the operators actually did",
                "concrete examples from the passages",
                want_bullets=True,
            ),
            SectionPlan(
                "Where teams get it wrong",
                "the common failure mode and its cause",
                want_bullets=True,
            ),
            SectionPlan("How to apply it this week", "the smallest useful first move"),
        ],
        takeaway_angle="one specific action the reader can take on Monday",
        source="fallback",
    )


# ----------------------------------------------------------------- prompts --

_CITATION_RULES = """Citation rules, all mandatory:
- Every paragraph and every bullet must contain at least one marker like [S1].
- Cite the passage that supports the specific claim, not a list at the end.
- Only use marker numbers that appear in the passages. Never invent one.
- Make no claim the passages do not support."""

OUTLINE_SYSTEM = f"""You are planning a Ship 30 for 30 style essay: a short, \
punchy, skimmable piece of operator writing. You plan only -- you do not write \
the essay.

Reply with a single JSON object and nothing else:

{{
  "title": "a specific, concrete title -- not a question, not a category name",
  "hook": "one sentence describing how to open with tension or a surprising fact",
  "sections": [
    {{"heading": "a specific claim or question, 3-8 words", "angle": "what this section argues"}}
  ],
  "takeaway": "the one specific action the reader should take"
}}

Rules:
- Exactly {DEFAULT_SECTIONS} sections.
- Headings must be specific ("Activation is an onboarding problem"), never \
generic ("Introduction", "Conclusion", "Overview", "Background").
- Plan only what the supplied passages can actually support.
- Output the JSON object alone. No prose before or after, no code fences."""

SECTION_SYSTEM = f"""You are writing ONE section of a Ship 30 for 30 essay, \
using only the numbered transcript passages supplied.

Voice: direct, concrete, second person where it helps. Short sentences. No \
throat-clearing, no "in today's fast-paced world", no restating the heading, no \
concluding summary -- this is a middle section of a longer piece.

{_CITATION_RULES}

Output the section's body text only. Do NOT write the heading -- it is added \
for you. Do not write a title. Do not announce what you are about to do."""

HOOK_SYSTEM = f"""You are writing the opening of a Ship 30 for 30 essay, using \
only the numbered transcript passages supplied.

The opening must earn the next line: start with a specific tension, a concrete \
number, or a claim most people get wrong. Never start with "In today's", "In \
this essay", "Have you ever", or a definition.

{_CITATION_RULES}

Output the opening paragraphs only -- no heading, no title, no bullets."""

TAKEAWAY_SYSTEM = f"""You are writing the closing section of a Ship 30 for 30 \
essay, using only the numbered transcript passages supplied.

It must give ONE specific, testable action the reader can take this week -- not \
a summary of the essay, not a list of principles, not "it depends". Name the \
first concrete step.

{_CITATION_RULES}

Output the closing paragraphs only -- no heading."""


def build_outline_messages(question: str, passage_block: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=OUTLINE_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                f"Transcript passages:\n\n{passage_block}\n\n---\n"
                f"Essay brief: {question}\n\nReturn the JSON outline."
            ),
        ),
    ]


def build_outline_repair_messages(
    question: str, passage_block: str, previous: str, reason: str
) -> list[ChatMessage]:
    return [
        *build_outline_messages(question, passage_block),
        ChatMessage(role="assistant", content=previous[:1500]),
        ChatMessage(
            role="user",
            content=(
                f"That was not valid: {reason}. Reply with ONLY the JSON object, "
                f"starting with {{ and ending with }}, with exactly "
                f"{DEFAULT_SECTIONS} sections."
            ),
        ),
    ]


def build_section_messages(
    outline: EssayOutline,
    section: SectionPlan | None,
    passage_block: str,
    used: int,
    *,
    part: str,
    word_target: int,
) -> list[ChatMessage]:
    """One prompt per essay part. `part` selects the system prompt and framing."""
    system = {"hook": HOOK_SYSTEM, "takeaway": TAKEAWAY_SYSTEM}.get(part, SECTION_SYSTEM)
    outline_view = "\n".join(f"{n}. {s.heading}" for n, s in enumerate(outline.sections, 1))

    if part == "hook":
        brief = (
            f"Essay title: {outline.title}\n"
            f"Opening angle: {outline.hook_angle or 'open with the concrete mistake teams make'}\n"
            f"The essay then covers:\n{outline_view}"
        )
    elif part == "takeaway":
        brief = (
            f"Essay title: {outline.title}\n"
            f"Closing angle: {outline.takeaway_angle or 'the smallest useful first move'}\n"
            f"The essay covered:\n{outline_view}"
        )
    else:
        assert section is not None
        bullets = (
            "\nInclude one short bulleted list of 3 items inside this section; "
            "each bullet needs its own citation marker."
            if section.want_bullets
            else "\nDo not use bullet points in this section; write prose."
        )
        brief = (
            f"Essay title: {outline.title}\n"
            f"Full outline:\n{outline_view}\n\n"
            f"Write section: {section.heading}\n"
            f"This section argues: {section.angle or section.heading}{bullets}"
        )

    return [
        ChatMessage(role="system", content=system),
        ChatMessage(
            role="user",
            content=(
                f"Transcript passages:\n\n{passage_block}\n\n---\n{brief}\n\n"
                f"Length: about {word_target} words. Cite using [S1]-[S{used}]."
            ),
        ),
    ]


def build_expansion_messages(
    outline: EssayOutline,
    section: SectionPlan,
    passage_block: str,
    used: int,
    existing: str,
    extra_words: int,
) -> list[ChatMessage]:
    """Lengthen one section rather than regenerating the whole essay.

    Regenerating everything to fix a word count would throw away good sections
    and cost five more model calls; one targeted expansion costs one.
    """
    return [
        ChatMessage(role="system", content=SECTION_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                f"Transcript passages:\n\n{passage_block}\n\n---\n"
                f"Essay: {outline.title}\nSection: {section.heading}\n\n"
                f"Current draft of this section:\n{existing}\n\n"
                f"Rewrite this section about {extra_words} words longer by adding a "
                f"concrete example or a specific mechanism from the passages. Keep "
                f"everything that is already there. Cite using [S1]-[S{used}]. "
                f"Output the section body only."
            ),
        ),
    ]


def assemble(outline: EssayOutline, hook: str, sections: list[str], takeaway: str) -> str:
    """Build the final Markdown. The structure is written here, not generated.

    This is the reason `evaluate_structure` can be strict: the title, the
    headings and the takeaway heading exist because this function wrote them, so
    the only things that can actually fail are length, bullets and emphasis --
    all of which are then measured and, where possible, repaired.
    """
    parts = [f"# {outline.title}", "", hook.strip(), ""]
    for plan, body in zip(outline.sections, sections, strict=False):
        if not body.strip():
            continue
        parts.extend([f"## {plan.heading}", "", body.strip(), ""])
    parts.extend([f"## {TAKEAWAY_HEADING}", "", takeaway.strip(), ""])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip() + "\n"


def strip_generated_heading(text: str, heading: str) -> str:
    """Remove a heading the model wrote anyway, so it is not duplicated.

    The prompt says not to write one; models write one perhaps a fifth of the
    time. Since the code emits the real heading, a model-written one would
    appear immediately below it.
    """
    body = (text or "").strip()
    body = re.sub(r"^#{1,6}\s*.*\n+", "", body, count=1) if body.startswith("#") else body
    normalised = heading.strip().lower()
    first, _, rest = body.partition("\n")
    if first.strip().strip("*_#: ").lower() == normalised:
        body = rest
    return body.strip()


def format_word_budget(sections: int) -> int:
    """Per-section word target for an outline with `sections` sections."""
    if sections <= 0:
        return SECTION_WORDS
    return max(120, (TARGET_WORDS - HOOK_WORDS - TAKEAWAY_WORDS) // sections)


def passage_indices(chunks: list[RetrievedChunk]) -> list[int]:
    return list(range(1, len(chunks) + 1))


def cited_in(text: str) -> list[int]:
    return sorted(set(extract_markers(text)))

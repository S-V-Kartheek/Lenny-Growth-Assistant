"""Citation extraction, validation and repair.

This module is the mechanical half of the product's central promise: *if it
answers, the answer is supported*. Prompting a model to cite is necessary but
not sufficient -- a 7B model will occasionally emit `[S9]` when it was given six
passages, and a citation that points at nothing is worse than no citation,
because it reads as verified.

So every answer is checked against the chunk IDs that were **actually
retrieved** for that turn, and markers that do not resolve are removed rather
than displayed. The removal is recorded, not hidden: `GroundingReport` is
persisted with the message and returned to the client.

Definition of "grounded", used by metric M1 and enforced by the prompt:

* at least one citation marker that resolves to a retrieved passage, **and**
* no marker that fails to resolve, **and**
* no substantive paragraph without a citation.

The last clause is the one that catches the common failure: a well-cited first
paragraph followed by three paragraphs of confident, uncited model opinion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# A citation group is any bracketed run containing at least one S-number:
# [S1], [S1, S3], [S2; S4] and the sloppy [S 1] all parse. Bracketed text with
# no S-number (a markdown link label, for instance) is left untouched.
_BRACKET_GROUP = re.compile(r"\[[^\[\]]{0,60}\]")
_S_NUMBER = re.compile(r"S\s*(\d+)", re.IGNORECASE)

# Lines that make no claim of their own and so need no citation.
_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$|-{3,}\s*$)")
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Below this length a paragraph is a transition or a heading-like fragment, not
# a substantive claim. Chosen so "Here is what operators say:" is not flagged
# while a real two-sentence assertion is.
MIN_CLAIM_WORDS = 12


@dataclass(slots=True)
class GroundingReport:
    """Everything known about how well an answer is supported."""

    cited_indices: list[int] = field(default_factory=list)
    invalid_markers: list[int] = field(default_factory=list)
    uncited_paragraphs: int = 0
    repaired: bool = False
    citation_count: int = 0
    source_count: int = 0

    @property
    def has_valid_citation(self) -> bool:
        return bool(self.cited_indices)

    @property
    def is_grounded(self) -> bool:
        """Metric M1's per-answer predicate, evaluated on the *displayed* text.

        `invalid_markers` deliberately does not gate this: it records markers
        that were found and stripped, but by the time this is evaluated they
        are already gone from the text the user sees. Requiring it to be empty
        would mark a successfully repaired answer as ungrounded forever, which
        defeats the purpose of repairing it. `repaired` carries that history
        for observability instead.
        """
        return self.has_valid_citation and self.uncited_paragraphs == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cited_indices": self.cited_indices,
            "invalid_markers": self.invalid_markers,
            "uncited_paragraphs": self.uncited_paragraphs,
            "repaired": self.repaired,
            "citation_count": self.citation_count,
            "source_count": self.source_count,
            "grounded": self.is_grounded,
        }


def extract_markers(text: str) -> list[int]:
    """Every S-number referenced, in order of appearance, including duplicates."""
    found: list[int] = []
    for group in _BRACKET_GROUP.finditer(text):
        found.extend(int(n) for n in _S_NUMBER.findall(group.group(0)))
    return found


def _claim_blocks(text: str) -> list[str]:
    """Split into units that can each be expected to carry a citation.

    Bullets are separated from one another rather than treated as one block:
    a five-bullet list with one citation on the last bullet is four uncited
    claims, and collapsing them would score it as fully grounded.
    """
    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        lines = [ln for ln in paragraph.splitlines() if ln.strip()]
        if any(_BULLET_PREFIX.match(ln) for ln in lines):
            blocks.extend(lines)
        elif lines:
            blocks.append(" ".join(lines))
    return blocks


def count_uncited_paragraphs(text: str) -> int:
    uncited = 0
    for block in _claim_blocks(text):
        stripped = block.strip()
        if _HEADING.match(stripped):
            continue
        body = _BULLET_PREFIX.sub("", stripped)
        if len(body.split()) < MIN_CLAIM_WORDS:
            continue
        if not extract_markers(body):
            uncited += 1
    return uncited


def validate_and_repair(text: str, source_count: int) -> tuple[str, GroundingReport]:
    """Strip markers that do not resolve, and report what the answer looks like.

    Repair is deterministic deletion rather than a second model call. On a local
    7B model a repair round-trip costs another 20-40 seconds and frequently
    reintroduces the same fault; removing the unresolvable marker is immediate,
    cannot make the text less true, and leaves the surrounding sentence intact.
    Whether the *stripped* answer is still acceptable is then decided by the
    caller from `has_valid_citation` -- see the knowledge_qa skill.
    """
    valid_range = range(1, source_count + 1)
    invalid: list[int] = []
    cited: list[int] = []
    repaired = False

    def rewrite(match: re.Match[str]) -> str:
        nonlocal repaired
        group = match.group(0)
        numbers = [int(n) for n in _S_NUMBER.findall(group)]
        if not numbers:
            return group
        keep = [n for n in numbers if n in valid_range]
        dropped = [n for n in numbers if n not in valid_range]
        invalid.extend(dropped)
        cited.extend(keep)
        if not dropped:
            return group
        repaired = True
        if not keep:
            return ""
        return "[" + ", ".join(f"S{n}" for n in keep) + "]"

    cleaned = _BRACKET_GROUP.sub(rewrite, text)
    if repaired:
        # Deleting a marker can leave " ." or a double space behind.
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)

    report = GroundingReport(
        cited_indices=sorted(set(cited)),
        invalid_markers=sorted(set(invalid)),
        uncited_paragraphs=count_uncited_paragraphs(cleaned),
        repaired=repaired,
        citation_count=len(cited),
        source_count=source_count,
    )
    return cleaned, report

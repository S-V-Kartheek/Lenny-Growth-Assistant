"""Turn speaker turns into retrieval chunks.

Chunking strategy and why
-------------------------
Podcast transcripts are conversational: a single idea is usually developed over
several consecutive turns, and a fixed character window would cut mid-sentence
and destroy the speaker/timestamp attribution that makes a citation verifiable.

So chunks are built by **accumulating whole turns** up to a token budget, never
splitting a turn unless it alone exceeds the budget (in which case it is split
on sentence boundaries). Each chunk records:
  * the speakers it contains -> "who said this"
  * the start second of its first turn -> a deep-link into the episode
  * the end second of its last turn  -> the span the claim came from

A configurable overlap of whole turns keeps context continuous across chunk
boundaries so an answer is not cut off from its own set-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ingestion.parse import Turn

# 1 token ~= 4 characters of English prose. Good enough for budgeting; the exact
# tokenizer differs per model and we deliberately do not couple to one.
CHARS_PER_TOKEN = 4

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Chunk:
    chunk_index: int
    content: str
    speakers: list[str]
    # None when the source transcript carries no timestamps (see parse.Turn).
    start_seconds: int | None
    end_seconds: int | None
    token_estimate: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _split_long_turn(turn: Turn, max_tokens: int) -> list[Turn]:
    """Split one oversized turn on sentence boundaries, keeping its timestamp."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    sentences = _SENTENCE_RE.split(turn.text)
    pieces: list[Turn] = []
    buf = ""
    for sentence in sentences:
        if buf and len(buf) + len(sentence) + 1 > max_chars:
            pieces.append(Turn(turn.speaker, turn.start_seconds, buf.strip()))
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf.strip():
        pieces.append(Turn(turn.speaker, turn.start_seconds, buf.strip()))
    return pieces or [turn]


def chunk_turns(
    turns: list[Turn],
    *,
    target_tokens: int = 320,
    overlap_turns: int = 1,
) -> list[Chunk]:
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if overlap_turns < 0:
        raise ValueError("overlap_turns must be non-negative")

    # Normalise oversized turns first so the accumulator only sees fitting units.
    units: list[Turn] = []
    for turn in turns:
        if estimate_tokens(turn.text) > target_tokens:
            units.extend(_split_long_turn(turn, target_tokens))
        else:
            units.append(turn)

    chunks: list[Chunk] = []
    window: list[Turn] = []
    budget = 0

    def emit() -> None:
        if not window:
            return
        content = "\n\n".join(f"{t.speaker}: {t.text}" for t in window)
        speakers: list[str] = []
        for t in window:
            if t.speaker not in speakers:
                speakers.append(t.speaker)
        offsets = [t.start_seconds for t in window if t.start_seconds is not None]
        chunks.append(
            Chunk(
                chunk_index=len(chunks),
                content=content,
                speakers=speakers,
                start_seconds=min(offsets) if offsets else None,
                end_seconds=max(offsets) if offsets else None,
                token_estimate=estimate_tokens(content),
            )
        )

    for turn in units:
        cost = estimate_tokens(turn.text)
        if window and budget + cost > target_tokens:
            emit()
            # Carry the tail of the previous window forward as overlap.
            window = window[-overlap_turns:] if overlap_turns else []
            budget = sum(estimate_tokens(t.text) for t in window)
        window.append(turn)
        budget += cost

    emit()
    return chunks

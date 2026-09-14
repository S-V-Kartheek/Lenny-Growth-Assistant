"""Parse an upstream transcript.md into structured, citable units.

Upstream format (pinned repo ChatPRD/lennys-podcast-transcripts):

    ---
    guest: Ada Chen Rekhi
    title: ...
    youtube_url: https://www.youtube.com/watch?v=...
    ...
    ---

    # Title

    ## Transcript

    Ada Chen Rekhi (00:00:00):
    It's a terrible outcome to ...

    (00:01:21):
    We do a live exercise ...

A bare `(HH:MM:SS):` line is a continuation of the previous speaker, so the
speaker is carried forward. Timestamps are preserved because they are what makes
a citation verifiable: they become `?t=<seconds>` on the YouTube URL.

The archive is not uniform -- three transcript layouts occur in practice. See
the regex block below for the list and how they are disambiguated.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# The upstream archive is not one format. Three genuinely occur, and supporting
# only the first silently dropped ~10% of episodes from the knowledge base:
#
#   A  "Speaker Name (00:12:34):"  header line, text on following lines
#      -- also the MM:SS variant, and a bare "(00:12:34):" continuation.
#   B  "[00:00:00] Speaker: text"  timestamp, speaker and text on one line.
#   C  "Speaker:"                  header line with no timestamp at all.
#
# They are tried in order of precision, because A and B carry the second-offsets
# that make a citation deep-linkable. C still yields citable text, just at
# episode granularity rather than to-the-moment.
_HEADER_RE = re.compile(
    r"^(?P<speaker>[^\n(]{0,80}?)\s*\((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\):\s*$"
)
_INLINE_RE = re.compile(
    r"^\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:]{1,80}?):\s*(?P<text>.+)$"
)
_BARE_SPEAKER_RE = re.compile(r"^(?P<speaker>[A-Z][\w.'-]*(?:\s+[\w.'-]+){0,4}):\s*$")


@dataclass(slots=True)
class Turn:
    """One speaker turn, at a known offset into the episode when available.

    start_seconds is None for transcripts that carry no timestamps at all; such
    a turn is still citable, just at episode rather than moment granularity.
    """

    speaker: str
    start_seconds: int | None
    text: str


@dataclass(slots=True)
class EpisodeDocument:
    slug: str
    title: str
    guest: str | None
    youtube_url: str | None
    video_id: str | None
    publish_date: date | None
    duration_seconds: int | None
    view_count: int | None
    keywords: list[str]
    description: str | None
    source_path: str
    content_hash: str
    turns: list[Turn] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(t.text.split()) for t in self.turns)


class TranscriptParseError(ValueError):
    """Raised when a file does not look like an upstream transcript."""


def _timestamp_to_seconds(value: str) -> int:
    parts = [int(p) for p in value.split(":")]
    hours, minutes, seconds = ([0] * (3 - len(parts))) + parts
    return hours * 3600 + minutes * 60 + seconds


def _coerce_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _video_id(meta: dict[str, Any]) -> str | None:
    if vid := meta.get("video_id"):
        return str(vid)
    url = str(meta.get("youtube_url") or "")
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
    return match.group(1) if match else None


def parse_transcript(raw: str, *, slug: str, source_path: str) -> EpisodeDocument:
    """Parse raw markdown into an EpisodeDocument.

    Raises TranscriptParseError when frontmatter is absent or unparseable, so a
    single malformed upstream file is skipped rather than corrupting the index.
    """
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise TranscriptParseError(f"{source_path}: missing YAML frontmatter")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise TranscriptParseError(f"{source_path}: invalid frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise TranscriptParseError(f"{source_path}: frontmatter is not a mapping")

    body = raw[match.end() :]
    turns = _parse_turns(body)
    if not turns:
        raise TranscriptParseError(f"{source_path}: no speaker turns found")

    keywords = meta.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []

    title = str(meta.get("title") or slug.replace("-", " ").title()).strip()

    return EpisodeDocument(
        slug=slug,
        title=title,
        guest=str(meta["guest"]).strip() if meta.get("guest") else None,
        youtube_url=str(meta["youtube_url"]).strip() if meta.get("youtube_url") else None,
        video_id=_video_id(meta),
        publish_date=_coerce_date(meta.get("publish_date")),
        duration_seconds=_coerce_int(meta.get("duration_seconds")),
        view_count=_coerce_int(meta.get("view_count")),
        keywords=[str(k).strip() for k in keywords if str(k).strip()],
        description=str(meta["description"]).strip() if meta.get("description") else None,
        source_path=source_path,
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        turns=turns,
    )


def _parse_turns(body: str) -> list[Turn]:
    """Parse speaker turns, trying each known layout in order of precision."""
    for parser in (_parse_header_turns, _parse_inline_turns, _parse_bare_speaker_turns):
        turns = parser(body)
        if turns:
            return turns
    return []


def _parse_header_turns(body: str) -> list[Turn]:
    """Format A: a header line, then the text on the lines beneath it."""
    turns: list[Turn] = []
    current_speaker = "Unknown"
    pending: Turn | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal pending, buffer
        if pending is not None:
            text = " ".join(line.strip() for line in buffer if line.strip()).strip()
            if text:
                pending.text = text
                turns.append(pending)
        pending = None
        buffer = []

    for line in body.splitlines():
        header = _HEADER_RE.match(line.strip())
        if header:
            flush()
            speaker = header.group("speaker").strip()
            if speaker:
                current_speaker = speaker
            pending = Turn(
                speaker=current_speaker,
                start_seconds=_timestamp_to_seconds(header.group("ts")),
                text="",
            )
            continue
        if pending is not None:
            buffer.append(line)

    flush()
    return turns


def _parse_inline_turns(body: str) -> list[Turn]:
    """Format B: "[00:00:00] Speaker: text" all on one line."""
    turns: list[Turn] = []
    for line in body.splitlines():
        match = _INLINE_RE.match(line.strip())
        if not match:
            # Wrapped continuation of the previous line.
            if turns and line.strip() and not line.lstrip().startswith("#"):
                turns[-1].text = f"{turns[-1].text} {line.strip()}".strip()
            continue
        turns.append(
            Turn(
                speaker=match.group("speaker").strip(),
                start_seconds=_timestamp_to_seconds(match.group("ts")),
                text=match.group("text").strip(),
            )
        )
    return turns


def _parse_bare_speaker_turns(body: str) -> list[Turn]:
    """Format C: a "Speaker:" header with no timestamp.

    Offsets are unknown, so start_seconds is None and citations for these
    episodes link to the episode rather than to a moment within it.
    """
    turns: list[Turn] = []
    pending_speaker: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal pending_speaker, buffer
        if pending_speaker is not None:
            text = " ".join(line.strip() for line in buffer if line.strip()).strip()
            if text:
                turns.append(Turn(speaker=pending_speaker, start_seconds=None, text=text))
        pending_speaker = None
        buffer = []

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _BARE_SPEAKER_RE.match(stripped)
        if match:
            flush()
            pending_speaker = match.group("speaker").strip()
            continue
        if pending_speaker is not None:
            buffer.append(line)

    flush()
    # Require several turns: a stray "Note:" line must not be mistaken for a
    # whole transcript in this, the loosest, format.
    return turns if len(turns) >= 5 else []

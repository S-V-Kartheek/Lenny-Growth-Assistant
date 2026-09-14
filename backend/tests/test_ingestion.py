"""Ingestion correctness: parsing, chunking and selection.

These are the tests that protect grounding. If parsing silently loses speaker
attribution or timestamps, every citation the product shows becomes unverifiable
while still *looking* correct -- the most dangerous failure mode in the system.
"""

from __future__ import annotations

import pytest

from app.ingestion.chunk import chunk_turns, estimate_tokens
from app.ingestion.parse import (
    EpisodeDocument,
    TranscriptParseError,
    Turn,
    parse_transcript,
)
from app.ingestion.select import select_episodes

# ------------------------------------------------------------------ parsing --

def test_parses_frontmatter_metadata(sample_transcript: str) -> None:
    doc = parse_transcript(
        sample_transcript, slug="ada-chen-rekhi", source_path="episodes/ada/transcript.md"
    )
    assert doc.guest == "Ada Chen Rekhi"
    assert doc.video_id == "l-T8sNRcWQk"
    assert doc.publish_date is not None and doc.publish_date.year == 2023
    assert "growth" in doc.keywords
    assert doc.content_hash and len(doc.content_hash) == 64


def test_extracts_timestamped_speaker_turns(sample_transcript: str) -> None:
    doc = parse_transcript(sample_transcript, slug="ada", source_path="p.md")
    assert len(doc.turns) > 5
    first = doc.turns[0]
    assert first.speaker == "Ada Chen Rekhi"
    assert first.start_seconds == 0
    assert first.text
    # Offsets must be monotonic; a regression here breaks deep-linked citations.
    offsets = [t.start_seconds for t in doc.turns]
    assert offsets == sorted(offsets)


def test_bare_timestamp_continues_previous_speaker() -> None:
    raw = (
        "---\ntitle: T\n---\n\n"
        "## Transcript\n\n"
        "Lenny (00:00:10):\nFirst part of the thought.\n\n"
        "(00:00:40):\nSecond part of the same thought.\n\n"
        "Guest (00:01:00):\nA reply.\n"
    )
    doc = parse_transcript(raw, slug="s", source_path="p.md")
    assert [t.speaker for t in doc.turns] == ["Lenny", "Lenny", "Guest"]
    assert [t.start_seconds for t in doc.turns] == [10, 40, 60]


def test_mm_ss_timestamps_are_supported() -> None:
    # Regression: ~10% of upstream episodes (Seth Godin, Teresa Torres, ...) use
    # MM:SS rather than HH:MM:SS. Accepting only HH:MM:SS dropped them from the
    # knowledge base while logging nothing louder than a warning.
    raw = (
        "---\ntitle: T\n---\n\n"
        "Lenny Rachitsky (00:00):\nOpening line.\n\n"
        "Seth Godin (01:30):\nA reply.\n\n"
        "(12:05):\nStill Seth.\n"
    )
    doc = parse_transcript(raw, slug="s", source_path="p.md")
    assert [t.start_seconds for t in doc.turns] == [0, 90, 725]
    assert [t.speaker for t in doc.turns] == ["Lenny Rachitsky", "Seth Godin", "Seth Godin"]


def test_hh_mm_ss_and_mm_ss_agree_on_the_same_instant() -> None:
    hh = parse_transcript(
        "---\ntitle: T\n---\n\nS (00:01:30):\nx\n", slug="s", source_path="p.md"
    )
    mm = parse_transcript("---\ntitle: T\n---\n\nS (01:30):\nx\n", slug="s", source_path="p.md")
    assert hh.turns[0].start_seconds == mm.turns[0].start_seconds == 90


def test_inline_bracketed_timestamp_format_is_supported() -> None:
    # Format B, e.g. episodes/ryan-hoover: "[00:00:00] Ryan: text".
    raw = (
        "---\ntitle: T\n---\n\n"
        "[00:00:00] Ryan: I don't know how to articulate that feeling.\n"
        "[00:00:28] Lenny: Ryan Hoover is the founder of Product Hunt.\n"
    )
    doc = parse_transcript(raw, slug="ryan", source_path="p.md")
    assert [t.speaker for t in doc.turns] == ["Ryan", "Lenny"]
    assert [t.start_seconds for t in doc.turns] == [0, 28]
    assert "Product Hunt" in doc.turns[1].text


def test_untimestamped_speaker_format_is_supported_without_offsets() -> None:
    # Format C, e.g. episodes/adriel-frederick. The text is still citable; only
    # the moment-level deep link is unavailable.
    body = "".join(f"Speaker {i % 2}:\nLine number {i} of the conversation.\n\n" for i in range(8))
    doc = parse_transcript(f"---\ntitle: T\n---\n\n{body}", slug="a", source_path="p.md")
    assert len(doc.turns) == 8
    assert all(t.start_seconds is None for t in doc.turns)
    assert doc.turns[0].speaker == "Speaker 0"


def test_stray_colon_lines_do_not_masquerade_as_a_transcript() -> None:
    # The untimestamped format is the loosest matcher, so it must not fire on a
    # document that merely contains a "Note:" line.
    raw = "---\ntitle: T\n---\n\nNote:\nSomething.\n\nSummary:\nElse.\n"
    with pytest.raises(TranscriptParseError):
        parse_transcript(raw, slug="s", source_path="p.md")


def test_timestamped_format_wins_over_looser_matchers() -> None:
    raw = (
        "---\ntitle: T\n---\n\n"
        "Lenny (00:00:05):\nProper turn.\n\n"
        "Note:\nA stray colon line.\n"
    )
    doc = parse_transcript(raw, slug="s", source_path="p.md")
    assert doc.turns[0].start_seconds == 5


def test_chunks_of_untimestamped_turns_have_no_offsets() -> None:
    turns = [Turn("S", None, "some words here") for _ in range(6)]
    chunks = chunk_turns(turns, target_tokens=20, overlap_turns=0)
    assert chunks
    assert all(c.start_seconds is None and c.end_seconds is None for c in chunks)


def test_video_id_is_derived_from_url_when_absent() -> None:
    raw = (
        "---\ntitle: T\nyoutube_url: https://www.youtube.com/watch?v=ABC123xyz\n---\n\n"
        "Lenny (00:00:01):\nHello.\n"
    )
    assert parse_transcript(raw, slug="s", source_path="p.md").video_id == "ABC123xyz"


@pytest.mark.parametrize(
    "raw",
    [
        "no frontmatter at all",
        "---\n: : bad yaml [\n---\nLenny (00:00:01):\nHi.\n",
        "---\ntitle: T\n---\n\nJust prose with no speaker turns.\n",
    ],
)
def test_malformed_transcripts_are_rejected_not_silently_accepted(raw: str) -> None:
    # A malformed file must raise so the pipeline skips it; accepting it would
    # put uncitable text into the index.
    with pytest.raises(TranscriptParseError):
        parse_transcript(raw, slug="s", source_path="p.md")


# ----------------------------------------------------------------- chunking --

def _turns(n: int, words: int = 40) -> list[Turn]:
    return [Turn(f"S{i % 2}", i * 30, " ".join(["word"] * words)) for i in range(n)]


def test_chunks_respect_token_budget() -> None:
    chunks = chunk_turns(_turns(20), target_tokens=200, overlap_turns=0)
    assert chunks
    # Allow one turn of slack: a turn is never split unless it alone overflows.
    assert all(c.token_estimate <= 200 + estimate_tokens("word " * 40) for c in chunks)


def test_chunks_carry_speakers_and_time_span() -> None:
    chunks = chunk_turns(_turns(12), target_tokens=150, overlap_turns=0)
    for chunk in chunks:
        assert chunk.speakers
        assert chunk.start_seconds <= chunk.end_seconds
        assert ":" in chunk.content  # "Speaker: text" formatting preserved


def test_overlap_repeats_boundary_turn() -> None:
    no_overlap = chunk_turns(_turns(12), target_tokens=120, overlap_turns=0)
    overlapped = chunk_turns(_turns(12), target_tokens=120, overlap_turns=1)
    assert len(overlapped) >= len(no_overlap)
    # The last turn of chunk N must reappear at the start of chunk N+1.
    for a, b in zip(overlapped, overlapped[1:], strict=False):
        assert a.content.split("\n\n")[-1] == b.content.split("\n\n")[0]


def test_oversized_single_turn_is_split_on_sentences() -> None:
    long_turn = [Turn("S", 0, "This is a sentence. " * 400)]
    chunks = chunk_turns(long_turn, target_tokens=200, overlap_turns=0)
    assert len(chunks) > 1
    assert all(c.token_estimate <= 260 for c in chunks)
    assert all(c.start_seconds == 0 for c in chunks)  # timestamp preserved


def test_chunking_is_deterministic(sample_transcript: str) -> None:
    doc = parse_transcript(sample_transcript, slug="s", source_path="p.md")
    a = chunk_turns(doc.turns, target_tokens=320, overlap_turns=1)
    b = chunk_turns(doc.turns, target_tokens=320, overlap_turns=1)
    assert [c.content for c in a] == [c.content for c in b]


def test_empty_input_yields_no_chunks() -> None:
    assert chunk_turns([], target_tokens=100) == []


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_chunk_budget_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        chunk_turns(_turns(3), target_tokens=bad)


# ---------------------------------------------------------------- selection --

def _doc(slug: str, duration: int | None, views: int | None) -> EpisodeDocument:
    return EpisodeDocument(
        slug=slug,
        title=slug,
        guest=None,
        youtube_url=None,
        video_id=None,
        publish_date=None,
        duration_seconds=duration,
        view_count=views,
        keywords=[],
        description=None,
        source_path=f"episodes/{slug}/transcript.md",
        content_hash="x" * 64,
        turns=[Turn("S", 0, "hello")],
    )


def test_short_clips_are_excluded() -> None:
    docs = [_doc("clip", 230, 9_000_000), _doc("full", 3600, 10)]
    result = select_episodes(docs, min_duration_seconds=1200, max_episodes=10)
    assert [d.slug for d in result.selected] == ["full"]
    assert result.skipped_short == 1


def test_selection_is_deterministic_on_view_count_ties() -> None:
    docs = [_doc("b", 3600, 100), _doc("a", 3600, 100), _doc("c", 3600, 500)]
    order = [d.slug for d in select_episodes(docs, max_episodes=10).selected]
    assert order == ["c", "a", "b"]  # views desc, then slug asc


def test_max_episodes_caps_selection() -> None:
    docs = [_doc(f"e{i}", 3600, i) for i in range(10)]
    result = select_episodes(docs, max_episodes=3)
    assert len(result.selected) == 3
    assert result.skipped_over_limit == 7


def test_allow_list_overrides_all_other_rules() -> None:
    docs = [_doc("clip", 60, 1), _doc("full", 3600, 999)]
    result = select_episodes(docs, allow_list=["clip"], max_episodes=1)
    assert [d.slug for d in result.selected] == ["clip"]


def test_ingest_all_ignores_the_cap() -> None:
    docs = [_doc(f"e{i}", 3600, i) for i in range(10)]
    assert len(select_episodes(docs, max_episodes=3, ingest_all=True).selected) == 10


def test_episodes_with_unknown_duration_are_kept() -> None:
    # Missing metadata must not silently drop content from the knowledge base.
    result = select_episodes([_doc("unknown", None, 5)], min_duration_seconds=1200)
    assert [d.slug for d in result.selected] == ["unknown"]

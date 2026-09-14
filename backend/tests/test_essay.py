"""The essay output contract: structure evaluation, outline parsing, repairs.

These are the checks PRD 2.3 requires to be programmatic rather than by eye, so
they are tested against constructed essays whose properties are known exactly --
an essay built to be 1,240 words must be measured as 1,240 words, and one built
to be 600 must fail. `app.evals.essay_eval` then applies the same function to
real model output.
"""

from __future__ import annotations

import pytest

from app.agent.essay import (
    MAX_BOLD,
    MAX_WORDS,
    MIN_WORDS,
    TAKEAWAY_HEADING,
    EssayOutline,
    OutlineInvalid,
    SectionPlan,
    assemble,
    count_words,
    enforce_selective_bold,
    evaluate_structure,
    fallback_outline,
    parse_outline,
    strip_generated_heading,
    trim_to_word_limit,
)

WORD = "insight"


def paragraph(words: int, marker: str = "[S1]") -> str:
    return " ".join([WORD] * words) + f" {marker}"


def build_essay(
    *,
    section_words: int = 240,
    sections: int = 4,
    bullets: int = 3,
    bold: int = 4,
    hook_words: int = 60,
    takeaway: bool = True,
    title: bool = True,
) -> str:
    parts = []
    if title:
        parts.append("# A specific, concrete title\n")
    parts.append(paragraph(hook_words) + "\n")
    for index in range(sections):
        parts.append(f"## Section heading {index}\n")
        parts.append(paragraph(section_words) + "\n")
    for _index in range(bullets):
        parts.append(f"- {paragraph(14)}\n")
    parts.append("\n")
    for index in range(bold):
        parts.append(f"**emphasis {index}** {paragraph(13)}\n\n")
    if takeaway:
        parts.append(f"## {TAKEAWAY_HEADING}\n")
        parts.append(paragraph(120) + "\n")
    return "\n".join(parts)


# ------------------------------------------------------------ word counting --


def test_word_count_ignores_citation_markers() -> None:
    """Counting markers would reward citing more to hit the target."""
    assert count_words("one two three [S1] [S2, S3]") == 3


def test_word_count_ignores_markdown_syntax() -> None:
    assert count_words("## Heading here") == 2
    assert count_words("- **bold** item here") == 3


# ------------------------------------------------------- structure contract --


def test_a_well_formed_essay_passes_every_check() -> None:
    report = evaluate_structure(build_essay())
    assert report.ok, report.issues
    assert MIN_WORDS <= report.word_count <= MAX_WORDS
    assert report.has_title and report.has_hook and report.has_takeaway


def test_a_short_essay_fails_on_word_count() -> None:
    report = evaluate_structure(build_essay(section_words=60))
    assert not report.ok
    assert any("word_count" in issue for issue in report.issues)


def test_an_overlong_essay_fails_on_word_count() -> None:
    report = evaluate_structure(build_essay(section_words=450))
    assert not report.ok
    assert any("word_count" in issue for issue in report.issues)


def test_missing_takeaway_is_an_issue() -> None:
    report = evaluate_structure(build_essay(takeaway=False))
    assert "no takeaway section" in report.issues


def test_missing_title_is_an_issue() -> None:
    assert "missing H1 title" in evaluate_structure(build_essay(title=False)).issues


def test_too_few_bullets_is_an_issue() -> None:
    report = evaluate_structure(build_essay(bullets=1))
    assert any("bullets" in issue for issue in report.issues)


def test_too_few_and_too_many_bold_spans_both_fail() -> None:
    """Selective bold is a range: none is unskimmable, twenty is a highlighter."""
    assert any("bold" in i for i in evaluate_structure(build_essay(bold=0)).issues)
    assert any("bold" in i for i in evaluate_structure(build_essay(bold=30)).issues)


def test_an_overlong_hook_is_not_a_hook() -> None:
    report = evaluate_structure(build_essay(hook_words=200))
    assert any("hook" in issue for issue in report.issues)


@pytest.mark.parametrize(
    "heading",
    ["## The takeaway", "## Bottom line", "## What to do on Monday", "## Start here"],
)
def test_takeaway_is_recognised_by_several_headings(heading: str) -> None:
    essay = f"# T\n\nhook words here\n\n{heading}\n\nDo the thing [S1]."
    assert evaluate_structure(essay).has_takeaway


# ------------------------------------------------------- deterministic repair --


def test_excess_bold_is_removed_keeping_the_earliest() -> None:
    essay = " ".join(f"**bold {i}** filler" for i in range(25))
    repaired, removed = enforce_selective_bold(essay)
    assert removed == 25 - MAX_BOLD
    assert repaired.count("**") == MAX_BOLD * 2
    assert "**bold 0**" in repaired
    assert "**bold 24**" not in repaired
    # The words themselves survive -- only the emphasis is dropped.
    assert "bold 24" in repaired


def test_bold_within_the_limit_is_left_alone() -> None:
    essay = "**a** x **b** y"
    assert enforce_selective_bold(essay) == (essay, 0)


def test_trimming_removes_whole_paragraphs_not_sentences() -> None:
    essay = build_essay(section_words=500)
    assert count_words(essay) > MAX_WORDS
    trimmed, removed = trim_to_word_limit(essay)
    assert removed > 0
    assert count_words(trimmed) <= MAX_WORDS
    # Every remaining paragraph still carries its citation.
    assert "[S1]" in trimmed


def test_trimming_preserves_headings_and_the_takeaway() -> None:
    trimmed, _ = trim_to_word_limit(build_essay(section_words=500))
    assert f"## {TAKEAWAY_HEADING}" in trimmed
    assert "# A specific, concrete title" in trimmed


def test_trimming_does_nothing_to_an_essay_already_in_range() -> None:
    essay = build_essay()
    assert trim_to_word_limit(essay) == (essay, 0)


# -------------------------------------------------------------- the outline --


def test_parses_a_well_formed_outline() -> None:
    outline = parse_outline(
        '{"title": "Activation is an onboarding problem", "hook": "open with the drop-off", '
        '"sections": [{"heading": "A", "angle": "a"}, {"heading": "B", "angle": "b"}, '
        '{"heading": "C", "angle": "c"}, {"heading": "D", "angle": "d"}], '
        '"takeaway": "instrument the first session"}'
    )
    assert outline.title == "Activation is an onboarding problem"
    assert len(outline.sections) == 4
    assert outline.source == "model"


def test_outline_survives_code_fences_and_preamble() -> None:
    """Small instruct models habitually wrap JSON in prose; that is a formatting
    habit, not a planning failure, so it must not fail the essay."""
    raw = (
        "Sure! Here is the outline:\n```json\n"
        '{"title": "T", "sections": ["One", "Two", "Three"]}\n```\nHope that helps!'
    )
    outline = parse_outline(raw)
    assert outline.title == "T"
    assert [s.heading for s in outline.sections] == ["One", "Two", "Three"]


def test_outline_headings_are_cleaned_of_numbering_and_markup() -> None:
    outline = parse_outline(
        '{"title": "## T", "sections": ["1. **First point**", "2) Second", "Third"]}'
    )
    assert outline.title == "T"
    assert outline.sections[0].heading == "First point"
    assert outline.sections[1].heading == "Second"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json at all", "not a JSON object"),
        ('{"sections": ["a", "b", "c"]}', "title"),
        ('{"title": "T"}', "sections"),
        ('{"title": "T", "sections": ["only one"]}', "between"),
        ('{"title": "T", "sections": ["a","b","c","d","e","f","g"]}', "between"),
    ],
)
def test_invalid_outline_raises_with_a_reason_the_retry_can_use(
    raw: str, fragment: str
) -> None:
    """The reason is fed back to the model verbatim; "that was wrong" gets the
    same answer again."""
    with pytest.raises(OutlineInvalid) as excinfo:
        parse_outline(raw)
    assert fragment in str(excinfo.value)


def test_fallback_outline_is_always_valid() -> None:
    outline = fallback_outline("How do I improve activation?")
    assert outline.source == "fallback"
    assert 3 <= len(outline.sections) <= 5
    assert all(s.heading for s in outline.sections)
    assert any(s.want_bullets for s in outline.sections)


def test_some_sections_are_marked_for_bullets() -> None:
    """The bullet requirement is assigned at plan time, not hoped for at write
    time -- it is the only way `MIN_BULLETS` is reliably met."""
    outline = parse_outline('{"title": "T", "sections": ["a", "b", "c", "d"]}')
    assert any(s.want_bullets for s in outline.sections)


# ------------------------------------------------------------------ assembly --


def test_assembly_writes_the_structure_itself() -> None:
    """Headings come from code, so they cannot be missing however the model
    behaved."""
    outline = EssayOutline(
        title="Real title",
        hook_angle="",
        sections=[SectionPlan("First"), SectionPlan("Second"), SectionPlan("Third")],
        takeaway_angle="",
    )
    essay = assemble(outline, "A hook [S1].", ["one [S1]", "two [S2]", "three [S1]"], "Do it [S1].")
    assert essay.startswith("# Real title")
    assert "## First" in essay and "## Second" in essay and "## Third" in essay
    assert f"## {TAKEAWAY_HEADING}" in essay


def test_assembly_skips_a_section_that_failed_to_generate() -> None:
    """One failed model call must not produce a heading with nothing under it."""
    outline = EssayOutline("T", "", [SectionPlan("A"), SectionPlan("B")], "")
    essay = assemble(outline, "hook [S1]", ["body [S1]", ""], "takeaway [S1]")
    assert "## A" in essay
    assert "## B" not in essay


def test_model_written_heading_is_not_duplicated() -> None:
    assert strip_generated_heading("## My heading\n\nBody text", "My heading") == "Body text"
    assert strip_generated_heading("**My heading**\nBody", "My heading") == "Body"
    assert strip_generated_heading("Body text only", "My heading") == "Body text only"

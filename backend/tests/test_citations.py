"""Citation extraction, validation and repair.

This is the mechanism behind M1 (grounded answer rate): if these tests are
wrong, an ungrounded answer could still be reported as grounded.
"""

from __future__ import annotations

from app.agent.citations import (
    GroundingReport,
    count_uncited_paragraphs,
    extract_markers,
    validate_and_repair,
)


def test_extract_markers_finds_single_and_grouped_citations() -> None:
    text = "Retention matters [S1]. Both agree [S2, S3] on this point."
    assert extract_markers(text) == [1, 2, 3]


def test_extract_markers_ignores_brackets_with_no_s_number() -> None:
    text = "See the [appendix] for detail, not [S1] alone."
    assert extract_markers(text) == [1]


def test_validate_and_repair_keeps_valid_markers_untouched() -> None:
    text = "Activation matters [S1]. Retention too [S2]."
    cleaned, report = validate_and_repair(text, source_count=2)
    assert cleaned == text
    assert not report.repaired
    assert report.cited_indices == [1, 2]
    assert report.invalid_markers == []
    assert report.is_grounded


def test_validate_and_repair_strips_a_hallucinated_marker() -> None:
    text = "This is well supported [S1, S9]. Nothing else to add here at all really."
    cleaned, report = validate_and_repair(text, source_count=3)
    assert "[S1]" in cleaned
    assert "S9" not in cleaned
    assert report.repaired
    assert report.invalid_markers == [9]
    assert report.cited_indices == [1]


def test_validate_and_repair_removes_bracket_entirely_when_every_number_is_invalid() -> None:
    text = "A claim with no real support [S9]."
    cleaned, report = validate_and_repair(text, source_count=2)
    assert "[S9]" not in cleaned
    assert "S9" not in cleaned
    assert report.invalid_markers == [9]
    assert not report.has_valid_citation


def test_answer_with_no_citation_at_all_is_not_grounded() -> None:
    text = "This is a long uncited paragraph making a real claim about retention."
    _, report = validate_and_repair(text, source_count=3)
    assert not report.has_valid_citation
    assert not report.is_grounded


def test_uncited_paragraph_after_a_cited_one_fails_grounding() -> None:
    """The common failure mode: cited opener, then unsupported model opinion."""
    text = (
        "Activation is the first thing to fix, according to the transcripts [S1].\n\n"
        "Beyond that, you should also rebuild your entire onboarding flow from "
        "scratch and focus on habit loops, which tend to work well in general."
    )
    _, report = validate_and_repair(text, source_count=2)
    assert report.cited_indices == [1]
    assert report.uncited_paragraphs == 1
    assert not report.is_grounded


def test_short_transition_lines_do_not_count_as_uncited_claims() -> None:
    text = "Here is what the transcripts say:\n\nRetention drives growth [S1]."
    assert count_uncited_paragraphs(text) == 0


def test_heading_lines_are_not_treated_as_claims() -> None:
    text = "## Key takeaway\n\nActivation matters most [S1]."
    assert count_uncited_paragraphs(text) == 0


def test_each_bullet_is_checked_independently() -> None:
    text = (
        "- Activation matters a great deal for early retention according to guests [S1]\n"
        "- Pricing should be revisited constantly based on real customer feedback and "
        "competitor signals, according to several operators interviewed on the show"
    )
    assert count_uncited_paragraphs(text) == 1


def test_grounding_report_as_dict_round_trips_the_predicate() -> None:
    report = GroundingReport(cited_indices=[1], invalid_markers=[], uncited_paragraphs=0)
    assert report.as_dict()["grounded"] is True

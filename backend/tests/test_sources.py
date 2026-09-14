"""Retrieval query construction shared by every grounded/generative skill.

`build_retrieval_query` is covered for the plain Q&A case in
`test_knowledge_qa.py`. This file covers `drop_format_terms`, added after a
live-verification run of the artifact skill found it moving a real query
across the refusal threshold -- see the "format-instruction dilution" note in
`agent-transcripts/checkpoint-3-*.md`.
"""

from __future__ import annotations

import pytest

from app.agent.sources import build_retrieval_query, strip_format_terms
from app.llm.base import ChatMessage

TOPIC = "How do I improve activation for a B2B SaaS product?"


def _history() -> list[ChatMessage]:
    return [ChatMessage(role="user", content=TOPIC)]


@pytest.mark.parametrize(
    "message",
    [
        "Turn that into an HTML landing page with CSS.",
        "Turn that into a Ship 30 essay.",
        "Write a 1250 word essay about this.",
        "Turn that into a markdown one-pager.",
    ],
)
def test_a_pure_format_instruction_falls_back_to_the_previous_question_untouched(
    message: str,
) -> None:
    """These strip down to connector debris ("that into an with") once format
    words are gone. Appending that debris to a good query is not neutral -- it
    is exactly what moved a real retrieval from 0.4976 to 0.4177 confidence on
    the live index, crossing the 0.48 refusal threshold. The fix is to notice
    there is no real subject left and use the previous question alone."""
    assert build_retrieval_query(message, _history(), drop_format_terms=True) == TOPIC


def test_a_standalone_request_with_no_anaphora_retrieves_on_its_own_words() -> None:
    """"Give me a launch checklist" carries no pronoun or "that/this" -- it is
    not recognised as a follow-up at all, so the previous topic is correctly
    irrelevant and it retrieves on what is left after stripping "give me" and
    "checklist": "launch"."""
    query = build_retrieval_query("Give me a launch checklist.", _history(), drop_format_terms=True)
    assert query != TOPIC
    assert "launch" in query


def test_a_follow_up_that_adds_a_real_subject_keeps_it() -> None:
    query = build_retrieval_query(
        "Turn that into a one-pager about onboarding friction.", _history(), drop_format_terms=True
    )
    assert TOPIC in query
    assert "onboarding friction" in query


def test_a_standalone_request_retrieves_on_its_subject_not_its_format() -> None:
    """No history to fall back to: the format words are still noise, so they
    are dropped, leaving the subject to retrieve on."""
    query = build_retrieval_query(
        "Write a 1250 word Ship 30 essay about pricing power.", [], drop_format_terms=True
    )
    assert "pricing power" in query
    assert "1250" not in query
    assert "essay" not in query.lower()


def test_grounded_qa_does_not_strip_format_terms_by_default() -> None:
    """Q&A requests are never pure format instructions -- every word is about
    the subject, so nothing here is dropped."""
    query = build_retrieval_query("How should I write my pricing page copy?", [])
    assert "pricing page" in query


def test_strip_format_terms_removes_command_and_format_vocabulary() -> None:
    stripped = strip_format_terms("Create an HTML landing page with CSS about pricing")
    assert "html" not in stripped.lower()
    assert "css" not in stripped.lower()
    assert "landing page" not in stripped.lower()
    assert "pricing" in stripped


def test_strip_format_terms_on_empty_input() -> None:
    assert strip_format_terms("") == ""
    assert strip_format_terms(None) == ""

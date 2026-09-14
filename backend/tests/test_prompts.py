"""Prompt assembly and context budgeting.

The scenario that matters here is a small context window (assumption A7): the
passage block must be built to fit whatever is left after history and the
system prompt, and it must report truthfully how many passages actually made
it in, because that count is what the citation validator trusts.
"""

from __future__ import annotations

from app.agent.prompts import build_grounded_messages, build_passage_block, estimate_tokens
from app.llm.base import ChatMessage
from tests.conftest import make_chunk as _chunk


def test_build_passage_block_includes_everything_within_budget() -> None:
    chunks = [_chunk(i, "short passage") for i in range(1, 4)]
    block, used = build_passage_block(chunks, token_budget=10_000)
    assert used == 3
    assert "[S1]" in block and "[S3]" in block


def test_build_passage_block_stops_when_budget_is_exhausted() -> None:
    long_content = "word " * 400  # deliberately large relative to the budget
    chunks = [_chunk(i, long_content) for i in range(1, 5)]
    block, used = build_passage_block(chunks, token_budget=estimate_tokens(long_content) * 2)
    assert 0 < used < 4
    # Numbering is by *position among retrieved chunks*, not by how many fit --
    # so a truncated block still numbers from S1 upward with no gaps.
    for i in range(1, used + 1):
        assert f"[S{i}]" in block


def test_build_passage_block_always_includes_at_least_one_even_over_budget() -> None:
    """A single oversized passage must not be dropped entirely -- a partial answer beats none."""
    huge = "word " * 5000
    chunks = [_chunk(1, huge)]
    block, used = build_passage_block(chunks, token_budget=10)
    assert used == 1
    assert "[S1]" in block


def test_build_grounded_messages_evicts_history_before_passages_when_tight() -> None:
    """History goes first when space is short (module docstring rule)."""
    chunks = [_chunk(i, "a supporting passage with real content") for i in range(1, 7)]
    long_history = [
        ChatMessage(role="user", content="word " * 300),
        ChatMessage(role="assistant", content="word " * 300),
    ]
    messages, used = build_grounded_messages(
        "What should I prioritise?",
        chunks,
        long_history,
        context_tokens=600,  # deliberately small: assumption A7
        max_output_tokens=200,
    )
    # The passages still made it into the final user message even though the
    # context window is tiny and history was long.
    assert used >= 1
    assert any("[S1]" in m.content for m in messages)


def test_build_grounded_messages_reports_the_true_passage_count() -> None:
    chunks = [_chunk(i, "passage " * 20) for i in range(1, 7)]
    messages, used = build_grounded_messages(
        "question", chunks, [], context_tokens=4000, max_output_tokens=256
    )
    user_message = messages[-1].content
    assert f"[S1]-[S{used}]" in user_message
    # No marker beyond `used` should be dangled in the instruction text.
    assert f"S{used + 1}" not in user_message

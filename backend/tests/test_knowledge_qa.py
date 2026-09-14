"""The grounded Q&A skill's decision logic, with a fake retriever and a fake
model so the three gates (confidence, prompt contract, post-hoc validation)
can be tested deterministically and without a live database or model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agent.contracts import Intent, SkillContext
from app.agent.skills.knowledge_qa import KnowledgeQASkill, build_retrieval_query
from app.config import Settings
from app.llm.base import ChatMessage, StreamEvent
from app.retrieval.retriever import RetrievalResult
from tests.conftest import make_chunk as _chunk


class ScriptedGateway:
    """Replays a fixed sequence of answers, one per call to `.stream()`."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls: list[list[ChatMessage]] = []
        self.last_provider = _FakeProvider()
        self.last_fallback_from = None
        self.primary = self.last_provider

    async def stream(self, messages, **kwargs) -> AsyncIterator[StreamEvent]:
        self.calls.append(messages)
        text = self._answers.pop(0) if self._answers else ""
        if text:
            yield StreamEvent(text=text)
        yield StreamEvent(done=True)

    async def complete(self, messages, **kwargs):
        from app.llm.base import Completion

        parts = []
        async for event in self.stream(messages, **kwargs):
            if event.text:
                parts.append(event.text)
        return Completion(text="".join(parts), provider="fake", model="fake-model")


class _FakeProvider:
    name = "fake"
    model = "fake-model"
    context_tokens = 8192


class FakeRetriever:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result

    async def retrieve(self, query: str, **kwargs) -> RetrievalResult:
        return self._result


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _result(confidence: float, n_chunks: int = 2) -> RetrievalResult:
    chunks = [_chunk(i, f"passage {i}") for i in range(1, n_chunks + 1)]
    return RetrievalResult(chunks=chunks, method="hybrid", confidence=confidence, query="q")


async def _run(skill: KnowledgeQASkill, message: str = "How do I improve activation?"):
    events = [e async for e in skill.run(SkillContext(message=message))]
    result = next(e.result for e in events if e.kind == "result")
    return events, result


async def test_refuses_below_the_confidence_threshold_without_calling_the_model() -> None:
    gateway = ScriptedGateway(["should never be used"])
    retriever = FakeRetriever(_result(confidence=0.1))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert result.refused
    assert result.grounding["reason"] == "insufficient_evidence"
    # gate 1 fires before generation, but a refusal explanation is still one call.
    assert len(gateway.calls) == 1


async def test_answers_above_threshold_with_valid_citations() -> None:
    gateway = ScriptedGateway(["Activation matters most [S1], and retention follows [S2]."])
    retriever = FakeRetriever(_result(confidence=0.8))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert not result.refused
    assert result.grounding["grounded"] is True
    assert result.sources[0]["cited"] is True
    assert result.sources[1]["cited"] is True


async def test_hallucinated_citation_is_stripped_but_answer_still_grounded() -> None:
    gateway = ScriptedGateway(["Activation matters most [S1, S9]."])
    retriever = FakeRetriever(_result(confidence=0.8, n_chunks=2))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert not result.refused
    assert "S9" not in result.content
    assert result.grounding["invalid_markers"] == [9]
    assert result.grounding["repaired"] is True


async def test_uncited_trailing_paragraph_triggers_a_retry_that_fixes_it() -> None:
    """Regression: the dominant M1 failure was a well-cited answer with an
    uncited summary paragraph tacked on. Gate 3 must retry on that, not just
    on a total absence of citations."""
    gateway = ScriptedGateway(
        [
            "Activation drives retention, according to the operators interviewed [S1].\n\n"
            "In summary, this is the most important lever founders should focus on "
            "early, before anything else really matters at all for growth.",
            "Activation drives retention, according to the operators interviewed [S1]. "
            "In summary, this is the most important lever, as covered above [S2].",
        ]
    )
    retriever = FakeRetriever(_result(confidence=0.8))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert len(gateway.calls) == 2  # the retry fired
    assert not result.refused
    assert result.grounding["grounded"] is True


async def test_uncited_trailing_paragraph_keeps_first_answer_if_retry_does_not_help() -> None:
    """A retry that does not improve grounding must not throw away a usable first answer."""
    first = (
        "Activation drives retention, according to the operators interviewed [S1].\n\n"
        "In summary, this is the most important lever founders should focus on "
        "early, before anything else really matters at all for growth."
    )
    gateway = ScriptedGateway([first, first])  # retry produces the identical, still-uncited text
    retriever = FakeRetriever(_result(confidence=0.8))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert len(gateway.calls) == 2
    assert not result.refused  # it still has a valid citation, so it is shown, not refused
    assert result.content.startswith("Activation drives retention")


async def test_no_valid_citation_triggers_one_retry_then_refuses() -> None:
    # First attempt has no citation at all; retry also fails -> ungrounded refusal.
    gateway = ScriptedGateway(
        ["Just an opinion with no citation at all, unfortunately for everyone."] * 2
    )
    retriever = FakeRetriever(_result(confidence=0.8))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert result.refused
    assert result.grounding["reason"] == "no_valid_citation_after_retry"
    # one generation call + one retry call = 2 stream() invocations
    assert len(gateway.calls) == 2


async def test_retry_that_succeeds_produces_a_grounded_answer() -> None:
    gateway = ScriptedGateway(
        [
            "No citation here, unfortunately, despite a fairly long claim being made.",
            "Retention is the key lever according to the operators [S1].",
        ]
    )
    retriever = FakeRetriever(_result(confidence=0.8))
    settings = _settings(retrieval_min_confidence=0.48)
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)

    assert not result.refused
    assert result.grounding["grounded"] is True


def test_build_retrieval_query_resolves_a_short_follow_up() -> None:
    history = [
        ChatMessage(role="user", content="How do I improve activation?"),
        ChatMessage(role="assistant", content="Focus on the aha moment [S1]."),
    ]
    query = build_retrieval_query("Can you expand on that?", history)
    assert "activation" in query.lower()


def test_build_retrieval_query_leaves_a_self_contained_question_alone() -> None:
    history = [ChatMessage(role="user", content="How do I improve activation?")]
    query = build_retrieval_query("What does the podcast say about pricing strategy?", history)
    assert query == "What does the podcast say about pricing strategy?"


async def test_empty_retrieval_result_refuses_cleanly() -> None:
    gateway = ScriptedGateway(["closest matches were unrelated"])
    retriever = FakeRetriever(
        RetrievalResult(chunks=[], method="hybrid", confidence=0.0, query="q")
    )
    settings = _settings()
    skill = KnowledgeQASkill(settings, gateway, retriever)

    _, result = await _run(skill)
    assert result.refused
    assert result.intent == Intent.KNOWLEDGE_QA

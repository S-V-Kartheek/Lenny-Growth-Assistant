"""DirectRuntime: normalising a skill's failures into a well-formed event stream.

The API layer trusts that every stream ends in exactly one terminal event
(`result` or `error`) so it never has to guess whether an SSE connection ended
cleanly. These tests are what makes that trust justified.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agent.contracts import Intent, Phase, Skill, SkillContext, SkillEvent, SkillResult
from app.agent.runtime import DirectRuntime
from app.errors import ProviderUnavailable


class _WellBehavedSkill(Skill):
    intent = Intent.KNOWLEDGE_QA
    name = "ok"
    description = ""

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        yield SkillEvent(kind="delta", text="hi")
        yield SkillEvent(kind="result", result=SkillResult(content="hi", intent=self.intent))


class _AppErrorSkill(Skill):
    intent = Intent.KNOWLEDGE_QA
    name = "broken"
    description = ""

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        yield SkillEvent(kind="phase", phase=Phase.GENERATING)
        raise ProviderUnavailable("model is down", remediation="start ollama")


class _CrashingSkill(Skill):
    intent = Intent.KNOWLEDGE_QA
    name = "crash"
    description = ""

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        yield SkillEvent(kind="phase", phase=Phase.GENERATING)
        raise ValueError("boom")


class _SilentSkill(Skill):
    """Never yields a result event -- a bug the runtime must catch, not propagate."""

    intent = Intent.KNOWLEDGE_QA
    name = "silent"
    description = ""

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        yield SkillEvent(kind="phase", phase=Phase.GENERATING)


async def _events(skill: Skill) -> list[SkillEvent]:
    runtime = DirectRuntime()
    return [e async for e in runtime.execute(skill, SkillContext(message="hi"))]


async def test_successful_skill_ends_with_result_then_done() -> None:
    events = await _events(_WellBehavedSkill())
    kinds = [e.kind for e in events]
    assert kinds[-2:] == ["result", "phase"]
    assert events[-1].phase == Phase.DONE


async def test_app_error_becomes_a_terminal_error_event_with_the_stable_code() -> None:
    events = await _events(_AppErrorSkill())
    assert events[-1].kind == "error"
    assert events[-1].data["code"] == "provider_unavailable"
    assert events[-1].data["remediation"] == "start ollama"


async def test_unexpected_exception_does_not_propagate_and_ends_the_stream_cleanly() -> None:
    events = await _events(_CrashingSkill())
    assert events[-1].kind == "error"
    assert events[-1].data["code"] == "internal_error"


async def test_a_skill_that_never_yields_a_result_still_produces_one() -> None:
    events = await _events(_SilentSkill())
    result_events = [e for e in events if e.kind == "result"]
    assert len(result_events) == 1
    assert result_events[0].result.refused is True

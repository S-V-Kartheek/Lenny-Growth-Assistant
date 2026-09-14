"""Wires router, skill registry and runtime into one entry point.

This is what `app.services.chat` (the sessions/streaming layer) calls. It knows
nothing about HTTP, SSE or persistence -- those are the caller's concerns; this
module's only job is: given a message and history, decide the intent and run
the right skill through the selected runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agent.contracts import Phase, Skill, SkillContext, SkillEvent
from app.agent.router import IntentRouter
from app.agent.runtime import select_runtime
from app.agent.skills.knowledge_qa import KnowledgeQASkill
from app.agent.skills.planned import artifact_placeholder, ship30_placeholder
from app.config import Settings
from app.llm.registry import LLMGateway
from app.retrieval.retriever import Retriever


class Agent:
    """The conversational agent: route, then run."""

    def __init__(self, settings: Settings, gateway: LLMGateway, retriever: Retriever) -> None:
        self._settings = settings
        self._gateway = gateway
        self._router = IntentRouter(gateway)
        self._runtime = select_runtime(settings)
        self._skills: dict[str, Skill] = {
            str(s.intent): s
            for s in (
                KnowledgeQASkill(settings, gateway, retriever),
                ship30_placeholder(),
                artifact_placeholder(),
            )
        }

    def describe_skills(self) -> list[dict[str, object]]:
        return [skill.describe() for skill in self._skills.values()]

    @property
    def gateway(self) -> LLMGateway:
        return self._gateway

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        yield SkillEvent(kind="phase", phase=Phase.ROUTING)
        decision = await self._router.route(ctx.message)
        ctx.intent = decision.intent
        ctx.routing = decision.as_dict()
        yield SkillEvent(kind="routing", data=decision.as_dict())

        skill = self._skills[str(decision.intent)]
        async for event in self._runtime.execute(skill, ctx):
            yield event

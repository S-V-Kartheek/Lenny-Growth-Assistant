"""Placeholder for skills that are routed now and implemented in checkpoint 3.

Routing and implementation are separate concerns, and they were built in that
order on purpose: the router is only measurable if every intent it can emit has
somewhere to go. Registering a truthful placeholder keeps the routing evaluation
honest (a request for an essay really is classified as an essay) without
pretending the capability exists.

The alternative -- routing essay and artifact requests into grounded Q&A -- was
rejected because it produces something plausible instead of something correct,
which is precisely the behaviour this system is built to avoid.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from app.agent.contracts import Intent, Phase, Skill, SkillContext, SkillEvent, SkillResult


class PlannedSkill(Skill):
    """A registered, routable capability that is not implemented yet."""

    def __init__(
        self,
        intent: Intent,
        name: str,
        description: str,
        examples: tuple[str, ...],
        checkpoint: str,
    ) -> None:
        self.intent = intent
        self.name = name
        self.description = description
        self.examples = examples
        self._checkpoint = checkpoint

    async def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        started = time.perf_counter()
        yield SkillEvent(kind="phase", phase=Phase.GENERATING)
        content = (
            f"That request routed to the **{self.name}** skill, which lands in "
            f"{self._checkpoint}. Ask a product or growth question and I will "
            f"answer it from the transcripts with citations."
        )
        yield SkillEvent(kind="delta", text=content)
        yield SkillEvent(
            kind="result",
            result=SkillResult(
                content=content,
                intent=self.intent,
                refused=True,
                grounding={"reason": "skill_not_implemented", "checkpoint": self._checkpoint},
                latency_ms=(time.perf_counter() - started) * 1000,
            ),
        )


def ship30_placeholder() -> PlannedSkill:
    return PlannedSkill(
        intent=Intent.SHIP30_ESSAY,
        name="Ship 30 for 30 essay",
        description=(
            "Turns a grounded answer into a ~1,250-word essay with a hook, "
            "skimmable structure and a specific takeaway."
        ),
        examples=("Turn that into a Ship 30 essay.", "Write a 1250 word essay about this."),
        checkpoint="checkpoint 3",
    )


def artifact_placeholder() -> PlannedSkill:
    return PlannedSkill(
        intent=Intent.ARTIFACT,
        name="Artifact generator",
        description=(
            "Produces a markdown or sanitised HTML/CSS artifact -- a checklist, "
            "one-pager or landing page -- from the conversation."
        ),
        examples=("Create an HTML landing page for this.", "Give me a launch checklist."),
        checkpoint="checkpoint 3",
    )

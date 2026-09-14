"""The shared skill contract.

A *skill* is a named capability with a defined input, a defined output and its
own validation rules -- not a prompt string. Grounded Q&A, the Ship 30 essay and
artifact generation are all skills, and they are declared **once** here so that
the runtime, the router, the API and the evaluation harness all agree on what a
skill is and what it returns.

The alternative -- inlining prompts at the call site -- was rejected because the
output contract is where quality is actually enforced on a small local model
(PRD 1.6, "Local model quality"). A skill that cannot state what valid output
looks like cannot be evaluated, repaired or refused.

Skills emit **events**, not return values, because the API streams. A skill that
only returned a final object would force the streaming layer to re-implement it,
and the two would drift.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.llm.base import ChatMessage


class Intent(StrEnum):
    """What the user is asking for. One intent maps to exactly one skill."""

    KNOWLEDGE_QA = "knowledge_qa"
    SHIP30_ESSAY = "ship30_essay"
    ARTIFACT = "artifact"


class Phase(StrEnum):
    """Coarse progress states, surfaced to the UI.

    A local 7B model can take 30+ seconds to produce its first token. Without
    phase feedback the interface is indistinguishable from a hang, so these are
    a product requirement rather than debug output (PRD 1.6, "Latency").
    """

    ROUTING = "routing"
    RETRIEVING = "retrieving"
    GENERATING = "generating"
    VALIDATING = "validating"
    DONE = "done"


@dataclass(slots=True)
class SkillContext:
    """Everything a skill is allowed to depend on.

    Passing this rather than letting skills reach for globals is what makes
    them testable with a fake provider and a fake retriever.
    """

    message: str
    session_id: str | None = None
    history: list[ChatMessage] = field(default_factory=list)
    intent: Intent = Intent.KNOWLEDGE_QA
    routing: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillResult:
    """The final, validated output of a skill."""

    content: str
    intent: Intent
    refused: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    grounding: dict[str, Any] = field(default_factory=dict)
    # Set only by the artifact skill. Carries `raw_content` internally so the
    # persistence layer can store the unsanitised output for debugging, but
    # `as_dict()` strips it: the wire contract is that a client is only ever
    # handed the sanitised `content` (PRD 2.4, docs/architecture.md#artifacts).
    artifact: dict[str, Any] | None = None
    provider: str | None = None
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0

    @property
    def artifact_for_client(self) -> dict[str, Any] | None:
        """The artifact minus anything a viewer must never receive."""
        if self.artifact is None:
            return None
        return {k: v for k, v in self.artifact.items() if k != "raw_content"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "intent": str(self.intent),
            "refused": self.refused,
            "sources": self.sources,
            "grounding": self.grounding,
            "artifact": self.artifact_for_client,
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass(slots=True)
class SkillEvent:
    """One streamed unit of skill progress.

    `kind` is the wire-level SSE event name, so the API layer is a transport and
    holds no knowledge of skill internals.
    """

    kind: str  # phase | delta | sources | result | error
    phase: Phase | None = None
    text: str = ""
    data: dict[str, Any] | None = None
    result: SkillResult | None = None


class Skill(ABC):
    """Base class for every skill.

    `intent` is the routing key; `describe()` feeds both the API's capability
    listing and the router's LLM fallback prompt, so a new skill becomes
    routable by being registered rather than by editing the router.
    """

    intent: Intent
    name: str
    description: str
    # Short phrases the router's LLM fallback is shown as examples. Kept with
    # the skill so routing knowledge lives next to the capability it selects.
    examples: tuple[str, ...] = ()

    @abstractmethod
    def run(self, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        """Execute, yielding progress events and finally one `result` event."""

    def describe(self) -> dict[str, Any]:
        return {
            "intent": str(self.intent),
            "name": self.name,
            "description": self.description,
            "examples": list(self.examples),
        }

"""Agent runtime: the seam between a skill and the machinery that executes it.

Why there is a seam at all
--------------------------
Skills are defined once (`app.agent.contracts`) and could in principle be driven
by more than one execution engine -- an in-process loop, or Anthropic's Claude
Agent SDK. Keeping that choice behind `AgentRuntime.execute()` means the API,
the router and the skills themselves are unaware of which engine ran, and
`select_runtime()` is the single place a per-provider decision would be made.

Why there is only one implementation today
------------------------------------------
The Claude Agent SDK was evaluated and rejected for this system. The evidence,
from the published package rather than from assumption:

* Its only transport is `_internal/transport/subprocess_cli.py` -- it spawns the
  **Claude Code CLI** as a child process and speaks a JSON control protocol to
  it. It is not an HTTP client for the Messages API.
* Because the CLI binary is vendored, the wheels are platform-specific and
  large: 91.6 MB for `manylinux_2_17_x86_64`, against a 0.33 MB sdist. That is
  the weight it would add to the API image.
* It is Claude-only. The demo's default provider is Ollama (assumption A7), so
  it could not run the primary path at all, and a second runtime would have to
  exist regardless.
* Its value is agentic tool use -- filesystem, bash, MCP servers, permission
  prompts. This system has one capability surface (retrieval over a pinned
  corpus) and deliberately does *not* want a model choosing tools: the grounding
  guarantee comes from a fixed retrieve -> generate -> validate pipeline.

So the trade-off is stated plainly: adopting the SDK would buy multi-turn tool
orchestration this product does not use, at the cost of a subprocess per turn, a
~90 MB image increase, Claude-only operation and losing token-level streaming
control. `DirectRuntime` is ~60 lines and does exactly what is needed. If a
later checkpoint needs genuine tool-using agency on Claude, a `ClaudeSDKRuntime`
implements this same interface and `select_runtime()` returns it for
`Provider.ANTHROPIC` -- no skill or API change.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.agent.contracts import Phase, Skill, SkillContext, SkillEvent, SkillResult
from app.config import Provider, Settings
from app.errors import AppError

log = logging.getLogger("app.agent.runtime")


class AgentRuntime(ABC):
    """Executes a skill and normalises its event stream."""

    name: str

    @abstractmethod
    def execute(self, skill: Skill, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        ...


class DirectRuntime(AgentRuntime):
    """In-process execution: the skill drives the provider directly.

    Its job beyond calling `skill.run()` is to make the event stream trustworthy
    for the transport layer above it:

    * expected failures become a terminal `error` event with a stable code,
      instead of tearing down a half-written SSE response;
    * exactly one terminal event is emitted, so the API never has to guess
      whether a stream ended cleanly.
    """

    name = "direct"

    async def execute(self, skill: Skill, ctx: SkillContext) -> AsyncIterator[SkillEvent]:
        saw_result = False
        try:
            async for event in skill.run(ctx):
                if event.kind == "result":
                    saw_result = True
                yield event
        except AppError as exc:
            log.warning(
                "skill_failed",
                extra={
                    "skill": skill.name,
                    "intent": str(skill.intent),
                    "code": exc.code,
                },
            )
            yield SkillEvent(
                kind="error",
                data={
                    "code": exc.code,
                    "message": exc.message,
                    "remediation": exc.remediation,
                },
            )
            return
        except Exception as exc:  # noqa: BLE001 - must not kill the SSE connection
            log.exception("skill_crashed", extra={"skill": skill.name})
            yield SkillEvent(
                kind="error",
                data={
                    "code": "internal_error",
                    "message": "The assistant failed while answering.",
                    "remediation": "Retry; if it persists check the logs for this request_id.",
                    "error_type": type(exc).__name__,
                },
            )
            return

        if not saw_result:
            # A skill that streams text but never yields a result would leave
            # nothing to persist. Fail loudly here rather than silently storing
            # an empty assistant message.
            log.error("skill_produced_no_result", extra={"skill": skill.name})
            yield SkillEvent(
                kind="result",
                result=SkillResult(
                    content="",
                    intent=skill.intent,
                    refused=True,
                    grounding={"reason": "skill_produced_no_result"},
                ),
            )
        yield SkillEvent(kind="phase", phase=Phase.DONE)


def select_runtime(settings: Settings, provider: Provider | None = None) -> AgentRuntime:
    """Pick the runtime for a provider.

    Every provider resolves to `DirectRuntime` today; the function exists so
    that decision is written down in one place and can change without touching
    call sites. See the module docstring for why the alternative was rejected.
    """
    _ = provider or settings.llm_provider
    return DirectRuntime()

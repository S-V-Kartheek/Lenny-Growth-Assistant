"""Turns a posted message into a persisted, streamed conversation turn.

This is the seam between HTTP/SSE (app.api.chat) and the agent
(app.agent.orchestrator). It owns exactly three responsibilities: load this
session's history (and only this session's), persist the user's message before
running anything, and persist the assistant's result plus its sources after the
agent finishes -- whether that finish was an answer, a refusal, or an error.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.agent.contracts import SkillContext, SkillEvent
from app.agent.orchestrator import Agent
from app.llm.base import ChatMessage
from app.services import artifacts, sessions

log = logging.getLogger("app.services.chat")

# History budget in *turns*, not tokens -- fine token budgeting happens in
# app.agent.prompts, which trims further against the model's real context
# window. This just bounds how much is even loaded from the database.
MAX_HISTORY_MESSAGES = 20


async def _load_history(session_id: str) -> list[ChatMessage]:
    rows = await sessions.get_history(session_id)
    recent = rows[-MAX_HISTORY_MESSAGES:]
    return [
        ChatMessage(role=r.role, content=r.content)  # type: ignore[arg-type]
        for r in recent
        if r.role in ("user", "assistant")
    ]


async def stream_turn(
    agent: Agent, session_id: str, user_message: str
) -> AsyncIterator[SkillEvent]:
    """Run one conversational turn against `session_id`, persisting as it goes.

    The user message is written to the database *before* the agent runs, so a
    crash mid-generation still leaves an accurate record of what was asked.
    """
    history = await _load_history(session_id)
    await sessions.append_message(session_id, "user", user_message)

    ctx = SkillContext(message=user_message, session_id=session_id, history=history)

    async for event in agent.run(ctx):
        yield event
        if event.kind == "result" and event.result is not None:
            result = event.result
            message_id = await sessions.append_message(
                session_id,
                "assistant",
                result.content,
                intent=str(result.intent),
                provider=result.provider,
                model=result.model,
                latency_ms=result.latency_ms,
                token_usage=result.usage or None,
                grounding=result.grounding or None,
                sources=result.sources or None,
            )
            if result.artifact:
                # Persisted here rather than inside the skill because this is the
                # first moment `message_id` exists -- and because a skill that
                # opened its own transaction could not be run in a test without
                # PostgreSQL. A failure to store the artifact must not lose the
                # message that was already written, so it is caught and logged.
                try:
                    row = await artifacts.create_artifact(
                        session_id,
                        kind=result.artifact["kind"],
                        title=result.artifact["title"],
                        content=result.artifact["content"],
                        raw_content=result.artifact.get("raw_content"),
                        sanitization=result.artifact.get("sanitization"),
                        message_id=message_id,
                    )
                    yield SkillEvent(
                        kind="artifact_saved",
                        data={
                            "id": row.id,
                            "kind": row.kind,
                            "title": row.title,
                            "version": row.version,
                            "document_url": (
                                f"/api/artifacts/{row.id}/document"
                                if row.kind == "html"
                                else None
                            ),
                        },
                    )
                except Exception:  # noqa: BLE001 - the turn itself already succeeded
                    log.exception(
                        "artifact_persist_failed", extra={"session_id": session_id}
                    )
        elif event.kind == "error" and event.data is not None:
            await sessions.append_message(
                session_id,
                "assistant",
                "",
                error=event.data,
            )

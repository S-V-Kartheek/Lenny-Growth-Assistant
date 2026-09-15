"""Sessions, message history, and SSE streaming chat.

Isolation guarantee (assumption A4's companion requirement, PRD 3 "Sessions"):
every route that touches history is scoped by `session_id`, and
`get_session()` additionally checks `user_id` so one caller cannot address
another's session by id even without a real auth boundary. `test_sessions.py`
exercises this directly: two sessions, overlapping topics, and an assertion
that neither's history leaks into the other.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import AgentDep, SettingsDep, UserIdDep
from app.llm.registry import build_provider
from app.schemas.chat import (
    CreateSessionRequest,
    MessageRecord,
    PostMessageRequest,
    ProviderInfo,
    SessionHistoryResponse,
    SessionListResponse,
    SessionSummary,
    SetProviderRequest,
)
from app.services import sessions
from app.services.chat import stream_turn

log = logging.getLogger("app.api.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _summary(row: sessions.SessionRow) -> SessionSummary:
    return SessionSummary(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        message_count=row.message_count,
    )


def _record(row: sessions.MessageRow) -> MessageRecord:
    return MessageRecord(
        id=row.id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        intent=row.intent,
        provider=row.provider,
        model=row.model,
        latency_ms=row.latency_ms,
        token_usage=row.token_usage,
        grounding=row.grounding,
        sources=row.sources,
        created_at=row.created_at,
    )


@router.post("", response_model=SessionSummary, status_code=201)
async def create_session(body: CreateSessionRequest, user_id: UserIdDep) -> SessionSummary:
    row = await sessions.create_session(user_id, body.title)
    return _summary(row)


@router.get("", response_model=SessionListResponse)
async def list_sessions(user_id: UserIdDep) -> SessionListResponse:
    rows = await sessions.list_sessions(user_id)
    return SessionListResponse(sessions=[_summary(r) for r in rows])


@router.get("/{session_id}", response_model=SessionHistoryResponse)
async def get_session_history(session_id: str, user_id: UserIdDep) -> SessionHistoryResponse:
    session = await sessions.get_session(session_id, user_id)
    history = await sessions.get_history(session_id)
    return SessionHistoryResponse(
        session=_summary(session), messages=[_record(m) for m in history]
    )


@router.post("/{session_id}/messages")
async def post_message(
    session_id: str,
    body: PostMessageRequest,
    user_id: UserIdDep,
    agent: AgentDep,
) -> EventSourceResponse:
    """Post a message and stream the response as Server-Sent Events.

    Event names on the wire: `phase`, `routing`, `sources`, `delta`, `result`,
    `error`. A client that only wants the final text can ignore everything but
    `delta`/`result`; a client building a rich UI (retrieving -> generating
    progress, source cards as soon as they are known) has every phase to hook.
    """
    # Ownership check happens before any streaming starts, so a bad session id
    # surfaces as a normal 404 rather than an SSE stream that opens and errors.
    await sessions.get_session(session_id, user_id)

    async def event_stream():
        try:
            async for event in stream_turn(agent, session_id, body.content):
                payload: dict[str, object] = {"kind": event.kind}
                if event.phase is not None:
                    payload["phase"] = str(event.phase)
                if event.text:
                    payload["text"] = event.text
                if event.data is not None:
                    payload["data"] = event.data
                if event.result is not None:
                    payload["result"] = event.result.as_dict()
                yield {"event": event.kind, "data": json.dumps(payload)}
        except Exception as exc:  # noqa: BLE001 - the stream must end cleanly either way
            log.exception("stream_failed", extra={"session_id": session_id})
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "kind": "error",
                        "data": {
                            "code": "internal_error",
                            "message": "The stream failed unexpectedly.",
                            "error_type": type(exc).__name__,
                        },
                    }
                ),
            }

    return EventSourceResponse(event_stream())


provider_router = APIRouter(prefix="/api", tags=["provider"])


@provider_router.get("/provider", response_model=ProviderInfo)
async def provider_info(settings: SettingsDep, agent: AgentDep) -> ProviderInfo:
    """Which provider and model are actually serving requests right now.

    PRD 2.5: the UI header must reflect the active provider without a code
    change when it is switched. This is what it reads.
    """
    described = agent.gateway.describe
    return ProviderInfo(
        provider=described["provider"],
        model=described["model"],
        context_tokens=described["context_tokens"],
        fallback_provider=described["fallback_provider"],
    )


@provider_router.post("/provider", response_model=ProviderInfo)
async def switch_provider(
    body: SetProviderRequest, settings: SettingsDep, agent: AgentDep
) -> ProviderInfo:
    """Switch the active model for every subsequent request on this deployment.

    A process-wide switch, not per-session: the assignment's model toggle is a
    single header control (PRD 2.5), not a per-conversation setting, so there
    is one gateway to redirect rather than one per session. Swapping isn't
    gated on health() -- an unconfigured provider (e.g. no GEMINI_API_KEY yet)
    is allowed to become primary, and the next message fails loudly with
    `provider_unavailable` and its remediation, the same way an unconfigured
    provider already behaves via LLM_PROVIDER at startup. Silently refusing
    the switch would hide that the key still needs to be set.
    """
    agent.gateway.primary = build_provider(settings, body.provider)
    described = agent.gateway.describe
    return ProviderInfo(
        provider=described["provider"],
        model=described["model"],
        context_tokens=described["context_tokens"],
        fallback_provider=described["fallback_provider"],
    )

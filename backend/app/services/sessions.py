"""Session and message persistence.

Explicit SQL through a thin repository, matching the rest of the project (no
ORM -- see checkpoint 1's "deliberately not built" notes). The one property
this module exists to guarantee: **two sessions never share context.** Every
read here is scoped by `session_id`, and history for a chat turn is always
loaded by joining through `messages.session_id`, never by a query that could
accidentally span sessions (e.g. "the last N messages for this user").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.db.engine import connection
from app.errors import NotFoundError

log = logging.getLogger("app.services.sessions")

# Single-tenant, no authentication (assumption A4). Every caller is folded onto
# one user row so sessions and messages still have a foreign key to satisfy,
# without building auth that demonstrates nothing the brief asks for.
ANONYMOUS_EXTERNAL_ID = "anonymous"


@dataclass(slots=True)
class SessionRow:
    id: str
    title: str
    created_at: Any
    updated_at: Any
    message_count: int = 0


@dataclass(slots=True)
class MessageRow:
    id: str
    session_id: str
    role: str
    content: str
    intent: str | None
    provider: str | None
    model: str | None
    latency_ms: int | None
    token_usage: dict[str, Any] | None
    grounding: dict[str, Any] | None
    created_at: Any
    sources: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None


async def get_or_create_anonymous_user() -> str:
    async with connection() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE external_id = :ext"),
                {"ext": ANONYMOUS_EXTERNAL_ID},
            )
        ).first()
        if row:
            return str(row[0])
        row = (
            await conn.execute(
                text(
                    "INSERT INTO users (external_id, display_name) "
                    "VALUES (:ext, 'Anonymous') RETURNING id"
                ),
                {"ext": ANONYMOUS_EXTERNAL_ID},
            )
        ).first()
        return str(row[0])


async def create_session(user_id: str, title: str | None = None) -> SessionRow:
    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (user_id, title) VALUES (:uid, :title) "
                    "RETURNING id, title, created_at, updated_at"
                ),
                {"uid": user_id, "title": title or "New chat"},
            )
        ).mappings().first()
    return SessionRow(
        id=str(row["id"]),
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=0,
    )


async def list_sessions(user_id: str) -> list[SessionRow]:
    async with connection() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT s.id, s.title, s.created_at, s.updated_at, "
                    "COUNT(m.id) AS message_count "
                    "FROM sessions s "
                    "LEFT JOIN messages m ON m.session_id = s.id "
                    "WHERE s.user_id = :uid AND s.archived = FALSE "
                    "GROUP BY s.id "
                    "ORDER BY s.updated_at DESC"
                ),
                {"uid": user_id},
            )
        ).mappings().all()
    return [
        SessionRow(
            id=str(r["id"]),
            title=r["title"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            message_count=r["message_count"],
        )
        for r in rows
    ]


async def get_session(session_id: str, user_id: str) -> SessionRow:
    """Scoped by user_id so one caller can never fetch another's session by guessing an id."""
    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, title, created_at, updated_at FROM sessions "
                    "WHERE id = :sid AND user_id = :uid AND archived = FALSE"
                ),
                {"sid": session_id, "uid": user_id},
            )
        ).mappings().first()
    if row is None:
        raise NotFoundError(f"Session {session_id} was not found.")
    return SessionRow(
        id=str(row["id"]), title=row["title"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def update_session_title(session_id: str, user_id: str, title: str) -> SessionRow:
    await get_session(session_id, user_id)
    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE sessions SET title = :title, updated_at = now() "
                    "WHERE id = :sid AND user_id = :uid AND archived = FALSE "
                    "RETURNING id, title, created_at, updated_at"
                ),
                {"sid": session_id, "uid": user_id, "title": title.strip()},
            )
        ).mappings().first()
    if row is None:
        raise NotFoundError(f"Session {session_id} was not found.")
    return SessionRow(
        id=str(row["id"]),
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def archive_session(session_id: str, user_id: str) -> None:
    await get_session(session_id, user_id)
    async with connection() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET archived = TRUE, updated_at = now() "
                "WHERE id = :sid AND user_id = :uid AND archived = FALSE"
            ),
            {"sid": session_id, "uid": user_id},
        )


async def get_history(session_id: str) -> list[MessageRow]:
    """All messages for one session, in order, with their sources attached.

    Sources are fetched in a second query keyed by message id and merged in
    Python; a LEFT JOIN would multiply message rows per source and complicate
    the JSON fields, for no benefit at this scale.
    """
    async with connection() as conn:
        messages = (
            await conn.execute(
                text(
                    "SELECT id, session_id, role, content, intent, provider, model, "
                    "latency_ms, token_usage, grounding, error, created_at "
                    "FROM messages WHERE session_id = :sid ORDER BY created_at ASC"
                ),
                {"sid": session_id},
            )
        ).mappings().all()
        if not messages:
            return []
        message_ids = [m["id"] for m in messages]
        sources = (
            await conn.execute(
                text(
                    "SELECT message_id, snapshot, cited, rank FROM message_sources "
                    "WHERE message_id = ANY(:ids) ORDER BY message_id, rank"
                ),
                {"ids": message_ids},
            )
        ).mappings().all()

    by_message: dict[str, list[dict[str, Any]]] = {}
    for s in sources:
        by_message.setdefault(str(s["message_id"]), []).append(
            {**dict(s["snapshot"]), "cited": s["cited"]}
        )

    return [
        MessageRow(
            id=str(m["id"]),
            session_id=str(m["session_id"]),
            role=m["role"],
            content=m["content"],
            intent=m["intent"],
            provider=m["provider"],
            model=m["model"],
            latency_ms=m["latency_ms"],
            token_usage=m["token_usage"],
            grounding=m["grounding"],
            created_at=m["created_at"],
            sources=by_message.get(str(m["id"]), []),
            error=m["error"],
        )
        for m in messages
    ]


async def append_message(
    session_id: str,
    role: str,
    content: str,
    *,
    intent: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    latency_ms: float | None = None,
    token_usage: dict[str, Any] | None = None,
    grounding: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> str:
    """Insert one message and its source snapshots, in one transaction.

    Sources are snapshotted (not just chunk-id references) so that a message's
    citations remain reconstructable even if the underlying chunk is later
    re-ingested or deleted -- the same provenance argument as the corpus pin.
    """
    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO messages (session_id, role, content, intent, provider, "
                    "model, latency_ms, token_usage, grounding, error) "
                    "VALUES (:sid, :role, :content, :intent, :provider, :model, "
                    ":latency_ms, CAST(:token_usage AS jsonb), CAST(:grounding AS jsonb), "
                    "CAST(:error AS jsonb)) RETURNING id"
                ),
                {
                    "sid": session_id,
                    "role": role,
                    "content": content,
                    "intent": intent,
                    "provider": provider,
                    "model": model,
                    "latency_ms": int(latency_ms) if latency_ms is not None else None,
                    "token_usage": _to_json(token_usage),
                    "grounding": _to_json(grounding),
                    "error": _to_json(error),
                },
            )
        ).first()
        message_id = str(row[0])

        for source in sources or []:
            await conn.execute(
                text(
                    "INSERT INTO message_sources "
                    "(message_id, chunk_id, rank, score, retrieval_method, cited, snapshot) "
                    "VALUES (:mid, :chunk_id, :rank, :score, :method, :cited, "
                    "CAST(:snapshot AS jsonb))"
                ),
                {
                    "mid": message_id,
                    "chunk_id": source.get("chunk_id"),
                    "rank": source.get("index", 0),
                    "score": source.get("score", 0.0),
                    "method": source.get("retrieval_method", "hybrid"),
                    "cited": bool(source.get("cited", False)),
                    "snapshot": _to_json(source),
                },
            )
    return message_id


def _to_json(value: dict[str, Any] | None) -> str | None:
    import json

    return json.dumps(value) if value is not None else None

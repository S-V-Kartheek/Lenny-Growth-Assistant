"""Artifact persistence.

One invariant governs this module, and it is enforced by types rather than by
care: **`content` is sanitised and servable; `raw_content` is not, and never
leaves the server.** `ArtifactRow` therefore does not carry `raw_content` at
all. Reading it requires calling `get_raw_for_debugging()` explicitly, which no
API route does -- so an accidental "just include everything" serialisation
cannot leak the unsanitised model output.

`version` supports regenerating an artifact in place later (checkpoint 4's
"regenerate" affordance): a new row with the same `session_id` and an
incremented version, rather than an update, so the earlier version is still
auditable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.db.engine import connection
from app.errors import NotFoundError

log = logging.getLogger("app.services.artifacts")

# Guards the database against an artifact large enough to be a denial-of-service
# on the viewer rather than a document. Generous: a real landing page with
# inline CSS runs 10-30 KB.
MAX_ARTIFACT_CHARS = 400_000


@dataclass(slots=True)
class ArtifactRow:
    """An artifact as a client may see it. Deliberately has no `raw_content`."""

    id: str
    session_id: str
    message_id: str | None
    kind: str
    title: str
    content: str
    version: int
    sanitization: dict[str, Any]
    created_at: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "version": self.version,
            "sanitization": self.sanitization,
            "created_at": self.created_at,
        }


def _row(mapping: Any) -> ArtifactRow:
    return ArtifactRow(
        id=str(mapping["id"]),
        session_id=str(mapping["session_id"]),
        message_id=str(mapping["message_id"]) if mapping["message_id"] else None,
        kind=mapping["kind"],
        title=mapping["title"],
        content=mapping["content"],
        version=mapping["version"],
        sanitization=dict(mapping["sanitization"] or {}),
        created_at=mapping["created_at"],
    )


_SELECT = (
    "SELECT id, session_id, message_id, kind, title, content, version, "
    "sanitization, created_at FROM artifacts"
)


async def create_artifact(
    session_id: str,
    *,
    kind: str,
    title: str,
    content: str,
    raw_content: str | None = None,
    sanitization: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> ArtifactRow:
    """Persist one artifact, versioned per session.

    The version is computed in the same statement that inserts, so two
    concurrent turns in one session cannot both claim the same version number by
    reading first and writing second.
    """
    if len(content) > MAX_ARTIFACT_CHARS:
        log.warning(
            "artifact_truncated",
            extra={"session_id": session_id, "length": len(content)},
        )
        content = content[:MAX_ARTIFACT_CHARS]

    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO artifacts "
                    "(session_id, message_id, kind, title, content, raw_content, "
                    " version, sanitization) "
                    "SELECT :sid, :mid, :kind, :title, :content, :raw, "
                    "       COALESCE(MAX(version), 0) + 1, CAST(:san AS jsonb) "
                    "FROM artifacts WHERE session_id = :sid "
                    "RETURNING id, session_id, message_id, kind, title, content, "
                    "          version, sanitization, created_at"
                ),
                {
                    "sid": session_id,
                    "mid": message_id,
                    "kind": kind,
                    "title": title[:200] or "Artifact",
                    "content": content,
                    "raw": raw_content,
                    "san": json.dumps(sanitization or {}),
                },
            )
        ).mappings().first()
    log.info(
        "artifact_created",
        extra={
            "artifact_id": str(row["id"]),
            "session_id": session_id,
            "kind": kind,
            "version": row["version"],
            "sanitized": bool((sanitization or {}).get("modified")),
        },
    )
    return _row(row)


async def list_artifacts(session_id: str) -> list[ArtifactRow]:
    async with connection() as conn:
        rows = (
            await conn.execute(
                text(f"{_SELECT} WHERE session_id = :sid ORDER BY created_at DESC"),
                {"sid": session_id},
            )
        ).mappings().all()
    return [_row(r) for r in rows]


async def get_artifact(artifact_id: str, session_id: str | None = None) -> ArtifactRow:
    """Fetch one artifact, optionally scoped to a session.

    The optional scope exists so a route that already knows which session the
    caller is addressing can require the artifact to belong to it, rather than
    trusting an id alone -- the same pattern `sessions.get_session` uses.
    """
    clause = " WHERE id = :aid" + (" AND session_id = :sid" if session_id else "")
    params: dict[str, Any] = {"aid": artifact_id}
    if session_id:
        params["sid"] = session_id
    async with connection() as conn:
        row = (await conn.execute(text(_SELECT + clause), params)).mappings().first()
    if row is None:
        raise NotFoundError(f"Artifact {artifact_id} was not found.")
    return _row(row)


async def get_raw_for_debugging(artifact_id: str) -> str | None:
    """The unsanitised model output. **Never** serve this to a browser.

    Exists for operators diagnosing a sanitisation decision ("why did my page
    lose its layout?"). Kept as an explicit, awkwardly-named function rather
    than a field on `ArtifactRow` so that reaching for it is always a decision
    somebody made on purpose.
    """
    async with connection() as conn:
        row = (
            await conn.execute(
                text("SELECT raw_content FROM artifacts WHERE id = :aid"),
                {"aid": artifact_id},
            )
        ).first()
    return row[0] if row else None

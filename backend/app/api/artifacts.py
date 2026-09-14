"""Artifact endpoints, including the isolated viewer document.

    GET /api/sessions/{id}/artifacts     list a session's artifacts
    GET /api/artifacts/{id}              one artifact, as JSON
    GET /api/artifacts/{id}/document     the HTML document, for an iframe

The isolation contract (PRD 2.4, acceptance criterion "the viewer cannot reach
the parent page, cookies or storage") is implemented here and not deferred to
the frontend, because it is a *server* guarantee. A frontend that forgot the
right iframe attributes would silently lose it; served this way, the document
defends itself:

* `Content-Security-Policy` on the response **and** inside the document, with no
  `script-src` under `default-src 'none'` -- so no script runs even if one
  survived sanitisation.
* `Content-Security-Policy: sandbox` on the response, which applies sandbox
  semantics to the document regardless of how it was framed. The frontend also
  sets `sandbox` on the iframe; either alone is sufficient, and the combination
  means neither side is the single point of failure.
* `X-Frame-Options`/`frame-ancestors` limited to this origin, so the artifact
  cannot be framed by an unrelated site and used for clickjacking.
* `Content-Disposition: inline` with an explicit `charset`, so the browser
  cannot be talked into a different content type by the document's own bytes.

Markdown artifacts are **not** served by `/document`. Markdown is rendered by
the client, and handing a browser a `text/markdown` response it might sniff as
HTML would undo the point; the sanitised source is returned as JSON instead.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from app.agent.sanitize import ARTIFACT_CSP, ARTIFACT_SANDBOX
from app.api.deps import UserIdDep
from app.errors import NotFoundError
from app.schemas.artifact import ArtifactListResponse, ArtifactRecord
from app.services import artifacts, sessions

router = APIRouter(prefix="/api", tags=["artifacts"])


def _record(row: artifacts.ArtifactRow) -> ArtifactRecord:
    return ArtifactRecord(
        id=row.id,
        session_id=row.session_id,
        message_id=row.message_id,
        kind=row.kind,
        title=row.title,
        content=row.content,
        version=row.version,
        sanitization=row.sanitization,
        created_at=row.created_at,
        sandbox=ARTIFACT_SANDBOX if row.kind == "html" else None,
        document_url=f"/api/artifacts/{row.id}/document" if row.kind == "html" else None,
    )


@router.get("/sessions/{session_id}/artifacts", response_model=ArtifactListResponse)
async def list_session_artifacts(session_id: str, user_id: UserIdDep) -> ArtifactListResponse:
    # Ownership is checked on the session, not the artifact: an artifact is only
    # reachable through a session the caller can already address.
    await sessions.get_session(session_id, user_id)
    rows = await artifacts.list_artifacts(session_id)
    return ArtifactListResponse(artifacts=[_record(r) for r in rows])


@router.get("/artifacts/{artifact_id}", response_model=ArtifactRecord)
async def get_artifact(artifact_id: str, user_id: UserIdDep) -> ArtifactRecord:
    row = await artifacts.get_artifact(artifact_id)
    await sessions.get_session(row.session_id, user_id)
    return _record(row)


@router.get("/artifacts/{artifact_id}/document", response_class=Response)
async def get_artifact_document(artifact_id: str, user_id: UserIdDep) -> Response:
    """Serve the sanitised HTML for embedding in a sandboxed iframe."""
    row = await artifacts.get_artifact(artifact_id)
    await sessions.get_session(row.session_id, user_id)
    if row.kind != "html":
        raise NotFoundError(
            f"Artifact {artifact_id} is {row.kind}, not html, and has no document view.",
            remediation="Read the sanitised source from GET /api/artifacts/{id} instead.",
        )
    return Response(
        content=row.content,
        media_type="text/html; charset=utf-8",
        headers={
            # The same policy the document carries in its own <meta>, sent as a
            # header too: a header cannot be dislodged by anything in the body.
            "Content-Security-Policy": ARTIFACT_CSP,
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Content-Disposition": "inline",
        },
    )

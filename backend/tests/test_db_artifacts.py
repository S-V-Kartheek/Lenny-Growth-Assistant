"""Artifact persistence and the artifact API, against a live PostgreSQL.

The properties proved here are the ones that cannot be proved in a unit test:
that versioning is computed atomically by the database, that artifacts are
scoped to their session, and -- the one that matters most -- that `raw_content`
is unreachable through every route the API exposes.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agent.sanitize import ARTIFACT_CSP
from app.config import Settings
from app.db.engine import connection, dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.errors import NotFoundError
from app.main import create_app
from app.services import artifacts, sessions
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.db

HOSTILE_RAW = "<h1>Page</h1><script>alert('pwned')</script><p>Body</p>"
SAFE_DOCUMENT = (
    "<!doctype html>\n<html lang=\"en\">\n<head>\n"
    f'<meta http-equiv="Content-Security-Policy" content="{ARTIFACT_CSP}">\n'
    "<title>Page</title>\n</head>\n<body>\n<h1>Page</h1><p>Body</p>\n</body>\n</html>\n"
)


@pytest.fixture
async def settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL)


@pytest.fixture
async def db(settings: Settings) -> None:
    init_engine(settings)
    await run_migrations()
    async with connection() as conn:
        await conn.execute(text("TRUNCATE episodes, users RESTART IDENTITY CASCADE"))
    yield
    await dispose_engine()


@pytest.fixture
async def session_id(db: None) -> str:
    user_id = await sessions.get_or_create_anonymous_user()
    row = await sessions.create_session(user_id, title="Artifacts")
    return row.id


@pytest.fixture
async def client(db: None, settings: Settings) -> AsyncClient:
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _make(session_id: str, **overrides) -> artifacts.ArtifactRow:
    payload = {
        "kind": "html",
        "title": "Page",
        "content": SAFE_DOCUMENT,
        "raw_content": HOSTILE_RAW,
        "sanitization": {"engine": "nh3", "removed_tags": ["script"], "verified": True},
    }
    payload.update(overrides)
    return await artifacts.create_artifact(session_id, **payload)


# ------------------------------------------------------------- persistence --


async def test_creates_and_reads_back_an_artifact(session_id: str) -> None:
    created = await _make(session_id)
    fetched = await artifacts.get_artifact(created.id)

    assert fetched.id == created.id
    assert fetched.kind == "html"
    assert fetched.content == SAFE_DOCUMENT
    assert fetched.sanitization["removed_tags"] == ["script"]


async def test_versions_increment_per_session(session_id: str) -> None:
    """Computed inside the INSERT, so two concurrent turns cannot both read
    version 1 and both write version 2."""
    first = await _make(session_id, title="v1")
    second = await _make(session_id, title="v2")
    assert (first.version, second.version) == (1, 2)


async def test_versions_are_independent_between_sessions(db: None) -> None:
    user_id = await sessions.get_or_create_anonymous_user()
    one = (await sessions.create_session(user_id)).id
    two = (await sessions.create_session(user_id)).id
    await _make(one)
    other = await _make(two)
    assert other.version == 1


async def test_listing_is_scoped_to_one_session(db: None) -> None:
    user_id = await sessions.get_or_create_anonymous_user()
    mine = (await sessions.create_session(user_id)).id
    theirs = (await sessions.create_session(user_id)).id
    await _make(mine, title="Mine")
    await _make(theirs, title="Theirs")

    titles = [row.title for row in await artifacts.list_artifacts(mine)]
    assert titles == ["Mine"]


async def test_get_scoped_to_the_wrong_session_is_not_found(db: None) -> None:
    user_id = await sessions.get_or_create_anonymous_user()
    mine = (await sessions.create_session(user_id)).id
    theirs = (await sessions.create_session(user_id)).id
    created = await _make(mine)
    with pytest.raises(NotFoundError):
        await artifacts.get_artifact(created.id, session_id=theirs)


async def test_an_oversized_artifact_is_truncated_not_rejected(session_id: str) -> None:
    """A 1 MB page is a bad artifact, not a failed turn."""
    row = await _make(session_id, content="x" * (artifacts.MAX_ARTIFACT_CHARS + 5000))
    assert len(row.content) == artifacts.MAX_ARTIFACT_CHARS


async def test_deleting_a_session_deletes_its_artifacts(session_id: str) -> None:
    created = await _make(session_id)
    async with connection() as conn:
        await conn.execute(
            text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id}
        )
    with pytest.raises(NotFoundError):
        await artifacts.get_artifact(created.id)


# ---------------------------------------------------- the raw_content rule --


async def test_raw_content_is_stored_but_absent_from_the_row_type(session_id: str) -> None:
    created = await _make(session_id)
    assert not hasattr(created, "raw_content")
    assert "raw_content" not in created.as_dict()
    # It really is in the database -- retrievable only by asking for it by name.
    assert await artifacts.get_raw_for_debugging(created.id) == HOSTILE_RAW


# --------------------------------------------------------------------- API --


async def test_api_lists_and_fetches_artifacts(
    client: AsyncClient, session_id: str
) -> None:
    created = await _make(session_id)

    listing = await client.get(f"/api/sessions/{session_id}/artifacts")
    assert listing.status_code == 200
    assert [a["id"] for a in listing.json()["artifacts"]] == [created.id]

    single = await client.get(f"/api/artifacts/{created.id}")
    assert single.status_code == 200
    body = single.json()
    assert body["kind"] == "html"
    assert body["document_url"] == f"/api/artifacts/{created.id}/document"


async def test_api_never_returns_raw_content(client: AsyncClient, session_id: str) -> None:
    """The single most important assertion about this API surface."""
    created = await _make(session_id)
    for url in (
        f"/api/sessions/{session_id}/artifacts",
        f"/api/artifacts/{created.id}",
    ):
        text_body = (await client.get(url)).text
        assert "raw_content" not in text_body
        assert "alert('pwned')" not in text_body
        assert "<script>" not in text_body


async def test_api_advertises_a_sandbox_without_same_origin(
    client: AsyncClient, session_id: str
) -> None:
    created = await _make(session_id)
    body = (await client.get(f"/api/artifacts/{created.id}")).json()
    assert "allow-same-origin" not in body["sandbox"]
    assert "allow-scripts" not in body["sandbox"]


async def test_markdown_artifacts_have_no_document_url(
    client: AsyncClient, session_id: str
) -> None:
    created = await _make(session_id, kind="markdown", content="# Doc\n\nBody")
    body = (await client.get(f"/api/artifacts/{created.id}")).json()
    assert body["document_url"] is None
    assert body["sandbox"] is None


async def test_unknown_artifact_returns_the_standard_error_envelope(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/artifacts/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------- the document view --


async def test_document_is_served_with_isolating_headers(
    client: AsyncClient, session_id: str
) -> None:
    created = await _make(session_id)
    response = await client.get(f"/api/artifacts/{created.id}/document")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp  # nothing grants script; default-src denies it
    assert "allow-same-origin" not in csp
    # frame-ancestors lists 'self' plus the configured frontend origin(s)
    # (CORS_ORIGINS) -- not just 'self', which would block the frontend's own
    # separate origin from ever embedding the sandboxed iframe (found live,
    # not by a unit test: see checkpoint 4's transcript). No X-Frame-Options
    # header is sent -- it cannot express "allow this other origin" the way
    # CSP's frame-ancestors can, and every evergreen browser honours
    # frame-ancestors over it when both are present.
    assert (
        "frame-ancestors 'self' http://localhost:5173 http://localhost:4173"
        " http://localhost:3000;" in csp
    )
    assert "x-frame-options" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_document_body_is_the_sanitised_content_only(
    client: AsyncClient, session_id: str
) -> None:
    created = await _make(session_id)
    body = (await client.get(f"/api/artifacts/{created.id}/document")).text
    assert body == SAFE_DOCUMENT
    assert "alert('pwned')" not in body


async def test_markdown_has_no_document_view(client: AsyncClient, session_id: str) -> None:
    """Serving Markdown as a document would invite a browser to sniff it as HTML."""
    created = await _make(session_id, kind="markdown", content="# Doc")
    response = await client.get(f"/api/artifacts/{created.id}/document")
    assert response.status_code == 404

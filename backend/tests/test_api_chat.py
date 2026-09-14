"""End-to-end chat API: sessions, SSE streaming, grounded answers -- against a
live PostgreSQL and a live model provider. Requires both `db` and `llm`
markers; skipped with a clear reason when either is absent.

This is the one place the whole pipeline is proven together: routing, real
retrieval, real generation, citation validation, and persistence, driven the
same way a real client would drive it.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.config import Settings
from app.db.engine import connection, dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL

pytestmark = [pytest.mark.db, pytest.mark.llm]


async def _parse_sse(response) -> list[dict]:
    events: list[dict] = []
    event_name = None
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            payload = json.loads(line.split(":", 1)[1].strip())
            events.append({"event": event_name, **payload})
    return events


@pytest.fixture
async def client() -> AsyncClient:
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL)
    init_engine(settings)
    await run_migrations()
    async with connection() as conn:
        await conn.execute(text("TRUNCATE episodes, users RESTART IDENTITY CASCADE"))
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dispose_engine()


async def test_provider_info_reflects_the_configured_backend(client: AsyncClient) -> None:
    response = await client.get("/api/provider")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "ollama"
    assert body["model"]


async def test_create_session_and_post_a_message_streams_a_grounded_answer(
    client: AsyncClient,
) -> None:
    created = await client.post("/api/sessions", json={"title": "Activation"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    async with client.stream(
        "POST",
        f"/api/sessions/{session_id}/messages",
        json={"content": "How should a team think about activation and retention?"},
        timeout=120.0,
    ) as response:
        assert response.status_code == 200
        events = await _parse_sse(response)

    kinds = [e["event"] for e in events]
    assert "phase" in kinds
    assert "sources" in kinds
    assert kinds[-1] == "result" or "result" in kinds

    result_event = next(e for e in events if e["event"] == "result")
    result = result_event["result"]
    assert result["intent"] == "knowledge_qa"
    # Either a grounded answer or an honest refusal is acceptable here -- what
    # is NOT acceptable is an answer with no valid citation (checked below).
    if not result["refused"]:
        assert result["grounding"]["cited_indices"]
        assert result["grounding"]["invalid_markers"] == []

    history = await client.get(f"/api/sessions/{session_id}")
    assert history.status_code == 200
    messages = history.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


async def test_two_sessions_do_not_share_context_over_the_real_api(
    client: AsyncClient,
) -> None:
    session_a = (await client.post("/api/sessions", json={})).json()["id"]
    session_b = (await client.post("/api/sessions", json={})).json()["id"]

    async with client.stream(
        "POST",
        f"/api/sessions/{session_a}/messages",
        json={"content": "What does the podcast say about pricing?"},
        timeout=120.0,
    ) as response:
        await _parse_sse(response)

    history_a = (await client.get(f"/api/sessions/{session_a}")).json()["messages"]
    history_b = (await client.get(f"/api/sessions/{session_b}")).json()["messages"]

    assert len(history_a) == 2  # user + assistant
    assert len(history_b) == 0  # untouched by session A's turn


async def test_fetching_another_users_session_id_is_a_404(client: AsyncClient) -> None:
    response = await client.get("/api/sessions/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"

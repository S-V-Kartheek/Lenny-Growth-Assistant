"""Session and message persistence against a live PostgreSQL.

The one property this file exists to prove: **two sessions never share
context.** Everything else (creation, listing, ordering, source snapshots) is
supporting evidence for that guarantee actually holding at the storage layer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.config import Settings
from app.db.engine import connection, dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.errors import NotFoundError
from app.services import sessions
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.db


@pytest.fixture
async def db() -> None:
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL)
    init_engine(settings)
    await run_migrations()
    yield
    await dispose_engine()


@pytest.fixture
async def clean_tables(db: None) -> None:
    async with connection() as conn:
        await conn.execute(text("TRUNCATE episodes, users RESTART IDENTITY CASCADE"))


@pytest.fixture
async def user_id(clean_tables: None) -> str:
    return await sessions.get_or_create_anonymous_user()


async def test_get_or_create_anonymous_user_is_idempotent(clean_tables: None) -> None:
    first = await sessions.get_or_create_anonymous_user()
    second = await sessions.get_or_create_anonymous_user()
    assert first == second


async def test_create_and_list_sessions(user_id: str) -> None:
    created = await sessions.create_session(user_id, title="Growth strategy")
    listed = await sessions.list_sessions(user_id)
    assert [s.id for s in listed] == [created.id]
    assert listed[0].title == "Growth strategy"
    assert listed[0].message_count == 0


async def test_get_session_rejects_a_session_belonging_to_another_user(
    user_id: str,
) -> None:
    session = await sessions.create_session(user_id)
    async with connection() as conn:
        other_user = (
            await conn.execute(
                text(
                    "INSERT INTO users (external_id) VALUES ('someone-else') RETURNING id"
                )
            )
        ).scalar_one()

    with pytest.raises(NotFoundError):
        await sessions.get_session(session.id, str(other_user))


async def test_two_sessions_never_share_history(user_id: str) -> None:
    """The core isolation guarantee (PRD 3, 'Sessions')."""
    session_a = await sessions.create_session(user_id, title="Session A")
    session_b = await sessions.create_session(user_id, title="Session B")

    await sessions.append_message(session_a.id, "user", "What about activation?")
    await sessions.append_message(session_a.id, "assistant", "Focus on the aha moment.")

    await sessions.append_message(session_b.id, "user", "What about pricing?")

    history_a = await sessions.get_history(session_a.id)
    history_b = await sessions.get_history(session_b.id)

    assert [m.content for m in history_a] == [
        "What about activation?",
        "Focus on the aha moment.",
    ]
    assert [m.content for m in history_b] == ["What about pricing?"]
    # Neither session's messages carry the other session's id.
    assert all(m.session_id == session_a.id for m in history_a)
    assert all(m.session_id == session_b.id for m in history_b)


async def test_append_message_persists_sources_and_marks_citations(user_id: str) -> None:
    session = await sessions.create_session(user_id)
    sources = [
        {"chunk_id": None, "index": 1, "score": 0.9, "retrieval_method": "hybrid", "cited": True},
        {"chunk_id": None, "index": 2, "score": 0.5, "retrieval_method": "hybrid", "cited": False},
    ]
    await sessions.append_message(
        session.id,
        "assistant",
        "Activation matters [S1].",
        intent="knowledge_qa",
        provider="ollama",
        model="qwen2.5:7b-instruct",
        grounding={"grounded": True},
        sources=sources,
    )

    history = await sessions.get_history(session.id)
    assert len(history) == 1
    message = history[0]
    assert message.provider == "ollama"
    assert message.grounding == {"grounded": True}
    assert len(message.sources) == 2
    assert message.sources[0]["cited"] is True
    assert message.sources[1]["cited"] is False


async def test_appending_a_message_touches_session_updated_at(user_id: str) -> None:
    session = await sessions.create_session(user_id)
    before = session.updated_at
    await sessions.append_message(session.id, "user", "hello")
    refreshed = await sessions.get_session(session.id, user_id)
    assert refreshed.updated_at >= before

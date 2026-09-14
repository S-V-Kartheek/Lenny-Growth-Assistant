"""Database-backed tests: schema, persistence and real retrieval ranking.

These need PostgreSQL and are skipped with a clear reason when it is absent
(see conftest). They exercise the parts that cannot be proven with unit tests:
that the schema behaves as designed, and that retrieval actually ranks a
relevant passage above an irrelevant one against a live index.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.engine import connection, dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.retrieval.retriever import Retriever, knowledge_base_stats
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.db


@pytest.fixture
async def db() -> None:
    """Fresh engine per test.

    Deliberately function-scoped: a module-scoped async fixture binds its
    connections to a different event loop than the one each test runs in, which
    surfaces as "attached to a different loop" rather than as a real failure.
    Engine construction is cheap and migrations are idempotent, so the isolation
    is worth more than the milliseconds.
    """
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL)
    init_engine(settings)
    await run_migrations()
    yield
    await dispose_engine()


@pytest.fixture
async def clean_tables(db: None) -> None:
    async with connection() as conn:
        await conn.execute(text("TRUNCATE episodes, users RESTART IDENTITY CASCADE"))


async def _seed_episode(slug: str, title: str, chunks: list[str]) -> str:
    async with connection() as conn:
        episode_id = (
            await conn.execute(
                text(
                    "INSERT INTO episodes (slug, title, video_id, source_repo, "
                    "source_commit, source_path, content_hash) VALUES "
                    "(:slug, :title, 'VID123', 'repo', 'abc123', 'p.md', :hash) RETURNING id"
                ),
                {"slug": slug, "title": title, "hash": uuid.uuid4().hex},
            )
        ).scalar_one()
        for index, content in enumerate(chunks):
            await conn.execute(
                text(
                    "INSERT INTO chunks (episode_id, chunk_index, content, speakers, "
                    "start_seconds, end_seconds, token_estimate) VALUES "
                    "(:e, :i, :c, ARRAY['Lenny'], :s, :s, 50)"
                ),
                {"e": episode_id, "i": index, "c": content, "s": index * 60},
            )
    return str(episode_id)


# ------------------------------------------------------------------- schema --

async def test_migrations_are_idempotent(db: None) -> None:
    # Startup runs migrations every boot; a second run must be a no-op. The db
    # fixture has already applied them once.
    assert await run_migrations() == {"applied": [], "skipped": []}


async def test_deleting_an_episode_cascades_to_its_chunks(clean_tables: None) -> None:
    episode_id = await _seed_episode("e1", "T", ["alpha", "beta"])
    async with connection() as conn:
        await conn.execute(text("DELETE FROM episodes WHERE id = :i"), {"i": episode_id})
        remaining = (
            await conn.execute(
                text("SELECT count(*) FROM chunks WHERE episode_id = :i"), {"i": episode_id}
            )
        ).scalar_one()
    assert remaining == 0


async def test_chunk_index_is_unique_per_episode(clean_tables: None) -> None:
    episode_id = await _seed_episode("e1", "T", ["alpha"])
    with pytest.raises(IntegrityError):
        async with connection() as conn:
            await conn.execute(
                text(
                    "INSERT INTO chunks (episode_id, chunk_index, content) "
                    "VALUES (:e, 0, 'duplicate')"
                ),
                {"e": episode_id},
            )


async def test_tsvector_is_generated_from_content(clean_tables: None) -> None:
    await _seed_episode("e1", "T", ["retention cohorts and activation"])
    async with connection() as conn:
        row = (
            await conn.execute(
                text("SELECT tsv @@ websearch_to_tsquery('english', 'retention') FROM chunks")
            )
        ).scalar_one()
    assert row is True


async def test_message_role_constraint_rejects_unknown_roles(clean_tables: None) -> None:
    async with connection() as conn:
        user_id = (
            await conn.execute(
                text("INSERT INTO users (external_id) VALUES ('u1') RETURNING id")
            )
        ).scalar_one()
        session_id = (
            await conn.execute(
                text("INSERT INTO sessions (user_id) VALUES (:u) RETURNING id"), {"u": user_id}
            )
        ).scalar_one()
    with pytest.raises(IntegrityError):
        async with connection() as conn:
            await conn.execute(
                text("INSERT INTO messages (session_id, role, content) VALUES (:s, 'robot', 'x')"),
                {"s": session_id},
            )


async def test_inserting_a_message_touches_the_session(clean_tables: None) -> None:
    async with connection() as conn:
        user_id = (
            await conn.execute(
                text("INSERT INTO users (external_id) VALUES ('u2') RETURNING id")
            )
        ).scalar_one()
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (user_id, created_at, updated_at) "
                    "VALUES (:u, now() - interval '1 hour', now() - interval '1 hour') "
                    "RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
    async with connection() as conn:
        await conn.execute(
            text("INSERT INTO messages (session_id, role, content) VALUES (:s, 'user', 'hi')"),
            {"s": session_id},
        )
    async with connection() as conn:
        updated = (
            await conn.execute(
                text("SELECT updated_at > created_at FROM sessions WHERE id = :s"),
                {"s": session_id},
            )
        ).scalar_one()
    assert updated is True


# ---------------------------------------------------------------- retrieval --

async def test_lexical_retrieval_ranks_the_relevant_chunk_first(clean_tables: None) -> None:
    await _seed_episode(
        "growth",
        "Growth loops",
        [
            "A growth loop compounds when each new user brings another user.",
            "My favourite restaurant in San Francisco serves excellent pasta.",
        ],
    )
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, embedding_provider="none")
    result = await Retriever(settings).retrieve("how do growth loops compound?", top_k=2)

    assert not result.is_empty
    assert "growth loop" in result.chunks[0].content.lower()
    assert result.method == "lexical"


async def test_retrieval_returns_nothing_for_an_off_corpus_question(clean_tables: None) -> None:
    await _seed_episode("growth", "Growth loops", ["A growth loop compounds over time."])
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, embedding_provider="none")
    result = await Retriever(settings).retrieve("quantum chromodynamics lattice gauge theory")

    # Empty retrieval is the signal the assistant uses to refuse rather than
    # invent an answer, so it must genuinely come back empty.
    assert result.is_empty
    assert result.confidence == 0.0


async def test_retrieved_chunks_carry_a_deep_linked_citation(clean_tables: None) -> None:
    await _seed_episode("growth", "Growth loops", ["x", "A growth loop compounds over time."])
    settings = Settings(_env_file=None, database_url=TEST_DATABASE_URL, embedding_provider="none")
    result = await Retriever(settings).retrieve("growth loop compounds")

    top = result.chunks[0]
    assert top.citation_url is not None
    assert "VID123" in top.citation_url and "t=" in top.citation_url
    assert top.episode_title == "Growth loops"
    assert top.source_commit == "abc123"  # provenance survives to the citation


async def test_retrieval_survives_a_missing_embedding_backend(clean_tables: None) -> None:
    await _seed_episode("growth", "Growth loops", ["A growth loop compounds over time."])
    settings = Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        embedding_provider="ollama",
        ollama_base_url="http://127.0.0.1:1",  # nothing listening
    )
    result = await Retriever(settings).retrieve("growth loop")

    # Degrades to lexical instead of failing the request, and says so.
    assert not result.is_empty
    assert result.method == "lexical"
    assert result.degraded_reason == "embedding_unavailable"


async def test_knowledge_base_stats_reflect_the_index(clean_tables: None) -> None:
    await _seed_episode("e1", "T", ["alpha", "beta", "gamma"])
    stats = await knowledge_base_stats()
    assert stats["episodes"] == 1
    assert stats["chunks"] == 3

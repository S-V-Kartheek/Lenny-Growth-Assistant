"""Shared test fixtures.

Tests are split into three tiers so the suite is useful on any machine:

* **Pure tests** (default) exercise parsing, chunking, selection, fusion,
  routing and sanitisation with no external dependency. They must always pass.
* **`@pytest.mark.db` tests** need a live PostgreSQL. They are skipped
  automatically -- with a visible reason -- when one is not reachable, rather
  than failing and hiding real regressions in the pure tier.
* **`@pytest.mark.llm` tests** need a live model provider (Ollama by default).
  Same treatment: skipped with a reason, never failed, when it is absent.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "WARNING")

FIXTURES = Path(__file__).parent / "fixtures"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny_test"
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


@pytest.fixture(scope="session")
def sample_transcript() -> str:
    return (FIXTURES / "sample_transcript.md").read_text(encoding="utf-8")


def make_chunk(index: int, content: str = "a supporting passage"):
    """A retrieved chunk with plausible values, for tests that do not need a database.

    Shared here so `test_prompts.py` and `test_knowledge_qa.py` build the same
    shape of fixture rather than each inventing a slightly different one.
    """
    from app.retrieval.retriever import RetrievedChunk

    return RetrievedChunk(
        chunk_id=f"c{index}",
        episode_id=f"e{index}",
        episode_slug=f"slug-{index}",
        episode_title=f"Episode {index}",
        guest="Guest",
        content=content,
        speakers=["Guest"],
        start_seconds=10 * index,
        end_seconds=10 * index + 30,
        chunk_index=0,
        youtube_url=None,
        video_id="vid123",
        publish_date=None,
        source_commit="abc",
        source_path="p.md",
        score=1.0 / index,
        lexical_rank=index,
        semantic_rank=index,
    )


def _database_reachable() -> bool:
    """Probe once per session; used to skip the db tier cleanly."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def probe() -> bool:
        engine = create_async_engine(TEST_DATABASE_URL, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(probe())
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def database_available() -> bool:
    return _database_reachable()


def _llm_reachable() -> bool:
    try:
        response = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def llm_available() -> bool:
    return _llm_reachable()


@pytest.fixture(autouse=True)
def _skip_db_tests(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("db") and not request.getfixturevalue(
        "database_available"
    ):
        pytest.skip(
            f"PostgreSQL not reachable at {TEST_DATABASE_URL}. "
            "Start it with `docker compose up -d db` and create the lenny_test database."
        )


@pytest.fixture(autouse=True)
def _skip_llm_tests(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("llm") and not request.getfixturevalue(
        "llm_available"
    ):
        pytest.skip(
            f"No model provider reachable at {OLLAMA_BASE_URL}. "
            "Start it with `ollama serve` and `ollama pull qwen2.5:7b-instruct`."
        )

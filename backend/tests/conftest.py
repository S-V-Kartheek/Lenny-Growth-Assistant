"""Shared test fixtures.

Tests are split into two tiers so the suite is useful on any machine:

* **Pure tests** (default) exercise parsing, chunking, selection, fusion,
  routing and sanitisation with no external dependency. They must always pass.
* **`@pytest.mark.db` tests** need a live PostgreSQL. They are skipped
  automatically -- with a visible reason -- when one is not reachable, rather
  than failing and hiding real regressions in the pure tier.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "WARNING")

FIXTURES = Path(__file__).parent / "fixtures"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny_test"
)


@pytest.fixture(scope="session")
def sample_transcript() -> str:
    return (FIXTURES / "sample_transcript.md").read_text(encoding="utf-8")


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


@pytest.fixture(autouse=True)
def _skip_db_tests(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("db") and not request.getfixturevalue(
        "database_available"
    ):
        pytest.skip(
            f"PostgreSQL not reachable at {TEST_DATABASE_URL}. "
            "Start it with `docker compose up -d db` and create the lenny_test database."
        )

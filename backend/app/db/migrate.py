"""Minimal forward-only migration runner.

A full migration framework is more machinery than this project needs: the
schema is small and ships as a handful of idempotent SQL files. Applied
filenames are recorded in `schema_migrations` so re-running is a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text

from app.db.engine import get_engine

log = logging.getLogger("app.db.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Migrations that may fail without failing startup, because they enable an
# optional capability. 002 needs the pgvector extension, which is present in the
# project's Docker image but not in a stock postgres install.
OPTIONAL = {"002_vector.sql"}


async def run_migrations() -> dict[str, list[str]]:
    applied: list[str] = []
    skipped: list[str] = []
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.execute(text(_BOOTSTRAP))
        rows = await conn.execute(text("SELECT filename FROM schema_migrations"))
        done = {r[0] for r in rows}

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            async with engine.begin() as conn:
                # asyncpg PREPAREs every statement, and a prepared statement may
                # hold only one command -- so a multi-statement migration script
                # must go through the driver's simple-query protocol directly.
                raw = await conn.get_raw_connection()
                await raw.driver_connection.execute(sql)
                await conn.execute(
                    text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                    {"f": path.name},
                )
            applied.append(path.name)
            log.info("migration_applied", extra={"migration": path.name})
        except Exception as exc:  # noqa: BLE001
            if path.name in OPTIONAL:
                skipped.append(path.name)
                log.warning(
                    "migration_skipped_optional",
                    extra={"migration": path.name, "error_type": type(exc).__name__},
                )
                continue
            log.error("migration_failed", extra={"migration": path.name})
            raise

    return {"applied": applied, "skipped": skipped}

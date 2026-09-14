"""Async database engine, session helper and health probe."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import Settings
from app.errors import DatabaseUnavailable

log = logging.getLogger("app.db")

_engine: AsyncEngine | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args={"timeout": settings.db_connect_timeout_seconds},
            echo=False,
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised; call init_engine() first.")
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """Yield a connection inside a transaction, translating *connectivity* failures.

    Only errors that mean "the database cannot be reached" become
    DatabaseUnavailable, so the UI can show a truthful "database is down"
    state. Constraint violations and malformed SQL are programming errors, not
    outages: they are logged and re-raised, surfacing as a 500 with a
    request_id. Collapsing both into one code was actively misleading -- it
    would have told an operator to check their database connection when the
    real fault was in a query.
    """
    try:
        async with get_engine().begin() as conn:
            yield conn
    except (OperationalError, InterfaceError, DisconnectionError) as exc:
        log.warning("database_unreachable", extra={"error_type": type(exc).__name__})
        raise DatabaseUnavailable("The database is currently unreachable.") from exc
    except SQLAlchemyError as exc:
        log.error("database_query_failed", extra={"error_type": type(exc).__name__})
        raise


async def check_database() -> dict[str, object]:
    """Health probe. Never raises; returns a status document."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            has_vector = bool(
                (
                    await conn.execute(
                        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                    )
                ).first()
            )
        return {"status": "ok", "pgvector": has_vector}
    except Exception as exc:  # noqa: BLE001 - health must not propagate
        return {"status": "unavailable", "error_type": type(exc).__name__}

"""Health and readiness endpoints.

Three levels, because they answer different operational questions:

* ``/health``  -- is the process up? (container liveness; never touches deps)
* ``/health/ready`` -- can it actually serve traffic? (db + knowledge base)
* ``/health/detail`` -- what exactly is degraded? (per-dependency breakdown,
  used by the UI status badge and by a human debugging a failed demo)

None of them raise: a health endpoint that 500s tells you less than one that
reports which dependency is down.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response

from app.api.deps import EmbedderDep, SettingsDep
from app.db.engine import check_database
from app.retrieval.retriever import knowledge_base_stats

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health(settings: SettingsDep) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


async def _knowledge_status() -> dict[str, Any]:
    try:
        stats = await knowledge_base_stats()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "error_type": type(exc).__name__}
    status = "ok" if stats.get("chunks", 0) > 0 else "empty"
    return {"status": status, **stats}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, Any]:
    database = await check_database()
    knowledge = await _knowledge_status()
    ok = database["status"] == "ok" and knowledge["status"] == "ok"
    if not ok:
        response.status_code = 503
    return {
        "status": "ready" if ok else "not_ready",
        "database": database,
        "knowledge_base": knowledge,
    }


@router.get("/health/detail", summary="Per-dependency diagnostics")
async def detail(settings: SettingsDep, embedder: EmbedderDep) -> dict[str, Any]:
    embeddings_ok = await embedder.available()
    database = await check_database()
    knowledge = await _knowledge_status()

    return {
        "service": settings.app_name,
        "environment": settings.environment,
        "database": database,
        "knowledge_base": knowledge,
        "embeddings": {
            "status": "ok" if embeddings_ok else "unavailable",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "impact": None
            if embeddings_ok
            else "Semantic retrieval is disabled; search falls back to lexical only.",
        },
        "corpus": {
            "repo": settings.corpus_repo,
            "commit": settings.corpus_commit,
        },
    }

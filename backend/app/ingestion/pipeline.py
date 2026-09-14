"""End-to-end ingestion: pinned corpus -> parsed episodes -> chunks -> index.

Runnable as ``python -m app.ingestion.pipeline`` (see the ``ingest`` service in
docker-compose.yml).

Idempotency: an episode whose content_hash is unchanged is skipped entirely.
Changing CORPUS_COMMIT to a newer revision therefore re-indexes only what moved.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import text

from app.config import Settings, get_settings
from app.db.engine import connection, dispose_engine, init_engine
from app.db.migrate import run_migrations
from app.ingestion.chunk import chunk_turns
from app.ingestion.fetch import ensure_corpus, iter_transcripts
from app.ingestion.parse import TranscriptParseError, parse_transcript
from app.ingestion.select import select_episodes
from app.observability import configure_logging
from app.retrieval.embeddings import EmbeddingClient, EmbeddingUnavailable

log = logging.getLogger("app.ingestion")

# Above this share of unparseable files, ingestion escalates from warnings to an
# error: it almost certainly means the upstream format changed.
PARSE_FAILURE_ALERT_RATIO = 0.05

_INSERT_EPISODE = """
INSERT INTO episodes (
    slug, guest, title, youtube_url, video_id, publish_date,
    duration_seconds, view_count, keywords, description,
    source_repo, source_commit, source_path, content_hash, word_count
) VALUES (
    :slug, :guest, :title, :youtube_url, :video_id, :publish_date,
    :duration_seconds, :view_count, :keywords, :description,
    :source_repo, :source_commit, :source_path, :content_hash, :word_count
)
ON CONFLICT (slug) DO UPDATE SET
    guest = EXCLUDED.guest,
    title = EXCLUDED.title,
    youtube_url = EXCLUDED.youtube_url,
    video_id = EXCLUDED.video_id,
    publish_date = EXCLUDED.publish_date,
    duration_seconds = EXCLUDED.duration_seconds,
    view_count = EXCLUDED.view_count,
    keywords = EXCLUDED.keywords,
    description = EXCLUDED.description,
    source_commit = EXCLUDED.source_commit,
    source_path = EXCLUDED.source_path,
    content_hash = EXCLUDED.content_hash,
    word_count = EXCLUDED.word_count,
    ingested_at = now()
RETURNING id
"""

_INSERT_CHUNK = """
INSERT INTO chunks (
    episode_id, chunk_index, content, speakers,
    start_seconds, end_seconds, token_estimate
) VALUES (
    :episode_id, :chunk_index, :content, :speakers,
    :start_seconds, :end_seconds, :token_estimate
)
"""


@dataclass
class IngestionReport:
    episodes_ingested: int = 0
    episodes_skipped: int = 0
    episodes_unchanged: int = 0
    chunks_created: int = 0
    chunks_embedded: int = 0
    parse_failures: list[str] = field(default_factory=list)
    embedding_model: str | None = None
    semantic_enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "episodes_ingested": self.episodes_ingested,
            "episodes_skipped": self.episodes_skipped,
            "episodes_unchanged": self.episodes_unchanged,
            "chunks_created": self.chunks_created,
            "chunks_embedded": self.chunks_embedded,
            "parse_failures": len(self.parse_failures),
            "embedding_model": self.embedding_model,
            "semantic_enabled": self.semantic_enabled,
        }


async def ingest(settings: Settings | None = None, *, force: bool = False) -> IngestionReport:
    settings = settings or get_settings()
    report = IngestionReport()

    root = ensure_corpus(
        repo=settings.corpus_repo,
        commit=settings.corpus_commit,
        cache_dir=settings.corpus_cache_dir,
    )

    documents = []
    for raw in iter_transcripts(root):
        try:
            documents.append(
                parse_transcript(raw.text, slug=raw.slug, source_path=raw.source_path)
            )
        except TranscriptParseError as exc:
            report.parse_failures.append(str(exc))
            log.warning("transcript_parse_failed", extra={"slug": raw.slug})

    # A handful of malformed upstream files is normal; a large fraction means the
    # upstream format changed and the knowledge base is quietly losing coverage.
    # That must be an ERROR an operator sees, not a pile of warnings.
    total_files = len(documents) + len(report.parse_failures)
    if total_files and len(report.parse_failures) / total_files > PARSE_FAILURE_ALERT_RATIO:
        log.error(
            "transcript_parse_failure_rate_high",
            extra={
                "failed": len(report.parse_failures),
                "total": total_files,
                "sample": report.parse_failures[:3],
                "hint": "Upstream transcript format may have changed; "
                "check app/ingestion/parse.py.",
            },
        )

    selection = select_episodes(
        documents,
        min_duration_seconds=settings.ingest_min_duration_seconds,
        max_episodes=settings.ingest_max_episodes,
        ingest_all=settings.ingest_all,
        allow_list=settings.ingest_episodes,
    )
    report.episodes_skipped = selection.skipped_short + selection.skipped_over_limit
    log.info(
        "ingestion_selection",
        extra={
            "available": len(documents),
            "selected": len(selection.selected),
            "skipped_short": selection.skipped_short,
            "skipped_over_limit": selection.skipped_over_limit,
        },
    )

    embedder = EmbeddingClient(settings)
    semantic = await embedder.available()
    report.semantic_enabled = semantic
    report.embedding_model = embedder.model if semantic else None
    if not semantic and embedder.enabled:
        log.warning("embeddings_unavailable_lexical_only", extra={"model": embedder.model})

    async with connection() as conn:
        run_id = (
            await conn.execute(
                text(
                    "INSERT INTO ingestion_runs (source_commit, embedding_model) "
                    "VALUES (:c, :m) RETURNING id"
                ),
                {"c": settings.corpus_commit, "m": report.embedding_model},
            )
        ).scalar_one()

    try:
        for document in selection.selected:
            async with connection() as conn:
                existing = (
                    await conn.execute(
                        text("SELECT content_hash FROM episodes WHERE slug = :s"),
                        {"s": document.slug},
                    )
                ).first()
                if existing and existing[0] == document.content_hash and not force:
                    report.episodes_unchanged += 1
                    continue

                episode_id = (
                    await conn.execute(
                        text(_INSERT_EPISODE),
                        {
                            "slug": document.slug,
                            "guest": document.guest,
                            "title": document.title,
                            "youtube_url": document.youtube_url,
                            "video_id": document.video_id,
                            "publish_date": document.publish_date,
                            "duration_seconds": document.duration_seconds,
                            "view_count": document.view_count,
                            "keywords": document.keywords,
                            "description": document.description,
                            "source_repo": settings.corpus_repo,
                            "source_commit": settings.corpus_commit,
                            "source_path": document.source_path,
                            "content_hash": document.content_hash,
                            "word_count": document.word_count,
                        },
                    )
                ).scalar_one()

                # Replace chunks wholesale: simpler and safer than diffing, and
                # the cascade keeps message_sources referentially honest.
                await conn.execute(
                    text("DELETE FROM chunks WHERE episode_id = :e"), {"e": episode_id}
                )

                chunks = chunk_turns(
                    document.turns,
                    target_tokens=settings.chunk_target_tokens,
                    overlap_turns=settings.chunk_overlap_turns,
                )
                for chunk in chunks:
                    await conn.execute(
                        text(_INSERT_CHUNK),
                        {
                            "episode_id": episode_id,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            "speakers": chunk.speakers,
                            "start_seconds": chunk.start_seconds,
                            "end_seconds": chunk.end_seconds,
                            "token_estimate": chunk.token_estimate,
                        },
                    )
                report.episodes_ingested += 1
                report.chunks_created += len(chunks)
                log.info("episode_indexed", extra={"slug": document.slug, "chunks": len(chunks)})

        if semantic:
            report.chunks_embedded = await _embed_pending(embedder, settings)

    except Exception as exc:  # noqa: BLE001 - record the failure, then re-raise
        async with connection() as conn:
            await conn.execute(
                text(
                    "UPDATE ingestion_runs SET status='failed', error=:e, finished_at=now() "
                    "WHERE id=:id"
                ),
                {"e": f"{type(exc).__name__}: {exc}"[:2000], "id": run_id},
            )
        raise

    async with connection() as conn:
        await conn.execute(
            text(
                "UPDATE ingestion_runs SET status='succeeded', finished_at=now(), "
                "episodes_ingested=:ei, episodes_skipped=:es, chunks_created=:cc, "
                "chunks_embedded=:ce WHERE id=:id"
            ),
            {
                "ei": report.episodes_ingested,
                "es": report.episodes_skipped,
                "cc": report.chunks_created,
                "ce": report.chunks_embedded,
                "id": run_id,
            },
        )

    log.info("ingestion_complete", extra=report.as_dict())
    return report


async def _embed_pending(embedder: EmbeddingClient, settings: Settings) -> int:
    """Embed chunks that have no vector yet, in batches, resumable on failure."""
    embedded = 0
    while True:
        async with connection() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, content FROM chunks WHERE embedding IS NULL "
                        "ORDER BY created_at LIMIT :n"
                    ),
                    {"n": settings.embedding_batch_size},
                )
            ).all()
        if not rows:
            break
        try:
            vectors = await embedder.embed([r[1] for r in rows])
        except EmbeddingUnavailable as exc:
            # Partial embedding is a valid state: retrieval falls back to lexical
            # for un-embedded chunks and a re-run resumes exactly here.
            log.warning("embedding_batch_failed", extra={"error": str(exc)[:200]})
            break
        async with connection() as conn:
            for (chunk_id, _), vector in zip(rows, vectors, strict=True):
                await conn.execute(
                    text(
                        "UPDATE chunks SET embedding = CAST(:v AS vector), "
                        "embedding_model = :m WHERE id = :id"
                    ),
                    {"v": str(vector), "m": embedder.model, "id": chunk_id},
                )
        embedded += len(rows)
        if embedded % 200 == 0:
            log.info("embedding_progress", extra={"embedded": embedded})
    return embedded


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Lenny's Podcast transcripts.")
    parser.add_argument("--force", action="store_true", help="Re-index unchanged episodes.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    init_engine(settings)
    try:
        await run_migrations()
        report = await ingest(settings, force=args.force)
        print(report.as_dict())
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())

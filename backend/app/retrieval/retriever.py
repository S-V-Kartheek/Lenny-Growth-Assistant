"""Hybrid retrieval over the transcript index.

Why hybrid
----------
Neither strategy alone is good enough on conversational transcripts:

* **Lexical** (Postgres full-text) nails proper nouns, product names and exact
  jargon ("PLG", "North Star metric") that an embedding model blurs together.
  It fails when the user's words differ from the speaker's.
* **Semantic** (pgvector cosine) handles paraphrase -- "how do I get people to
  stick around" finds a discussion of retention -- but happily returns
  topically-adjacent passages that do not answer the question.

The two ranked lists are fused with Reciprocal Rank Fusion. RRF is used instead
of a weighted score blend because ts_rank and cosine similarity are on
incomparable scales; RRF only needs the *ordering* from each, so no fragile
per-strategy normalisation constant has to be tuned.

Degraded mode: when pgvector or the embedding model is unavailable, the semantic
leg is skipped and `method` reports "lexical". Retrieval still works; the API
says so, and the UI shows it, rather than pretending nothing changed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.config import Settings
from app.db.engine import connection
from app.observability import timed
from app.retrieval.embeddings import EmbeddingClient, EmbeddingUnavailable

log = logging.getLogger("app.retrieval")

# Domain filler that Postgres' English stopword dictionary cannot know about.
# Ordinary stopwords ("what", "is", "the") are deliberately NOT listed here --
# to_tsvector already drops them, and duplicating that logic risks removing a
# word that matters in a different phrasing.
_QUERY_NOISE = re.compile(
    r"\b(please|can you|could you|tell me|i want|i would like|help me|"
    r"lenny|podcast|transcript|episode)\b",
    re.IGNORECASE,
)

_LEXICAL_SQL = """
SELECT c.id, ts_rank_cd(c.tsv, q) AS score
FROM chunks c, websearch_to_tsquery('english', :query) q
WHERE c.tsv @@ q
ORDER BY score DESC
LIMIT :k
"""

_SEMANTIC_SQL = """
SELECT c.id, 1 - (c.embedding <=> CAST(:vector AS vector)) AS score
FROM chunks c
WHERE c.embedding IS NOT NULL
ORDER BY c.embedding <=> CAST(:vector AS vector)
LIMIT :k
"""

_HYDRATE_SQL = """
SELECT
    c.id, c.content, c.speakers, c.start_seconds, c.end_seconds, c.chunk_index,
    e.id AS episode_id, e.slug, e.title, e.guest, e.youtube_url, e.video_id,
    e.publish_date, e.source_commit, e.source_path
FROM chunks c
JOIN episodes e ON e.id = c.episode_id
WHERE c.id = ANY(:ids)
"""


@dataclass(slots=True)
class RetrievedChunk:
    """One citable passage, carrying everything needed to verify it."""

    chunk_id: str
    episode_id: str
    episode_slug: str
    episode_title: str
    guest: str | None
    content: str
    speakers: list[str]
    start_seconds: int | None
    end_seconds: int | None
    chunk_index: int
    youtube_url: str | None
    video_id: str | None
    publish_date: str | None
    source_commit: str
    source_path: str
    score: float
    lexical_rank: int | None
    semantic_rank: int | None

    @property
    def citation_url(self) -> str | None:
        """Deep-link to the exact moment the passage was spoken."""
        if not self.video_id:
            return self.youtube_url
        start = max(0, (self.start_seconds or 0) - 5)
        return f"https://www.youtube.com/watch?v={self.video_id}&t={start}s"

    @property
    def timestamp_label(self) -> str | None:
        if self.start_seconds is None:
            return None
        seconds = int(self.start_seconds)
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "episode_slug": self.episode_slug,
            "episode_title": self.episode_title,
            "guest": self.guest,
            "speakers": self.speakers,
            "timestamp": self.timestamp_label,
            "start_seconds": self.start_seconds,
            "citation_url": self.citation_url,
            "publish_date": self.publish_date,
            "source_commit": self.source_commit,
            "source_path": self.source_path,
            "score": round(self.score, 5),
            "excerpt": self.content[:400],
        }


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    method: str
    confidence: float
    query: str
    degraded_reason: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.chunks


def normalise_query(query: str) -> str:
    """Strip conversational filler so the search terms dominate the query."""
    cleaned = _QUERY_NOISE.sub(" ", query)
    cleaned = re.sub(r"[^\w\s'-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # If filtering removed everything meaningful, fall back to the raw query.
    return cleaned if len(cleaned) >= 3 else re.sub(r"\s+", " ", query).strip()


def _rrf(rank: int, k: int) -> float:
    return 1.0 / (k + rank)


class Retriever:
    def __init__(self, settings: Settings, embedder: EmbeddingClient | None = None) -> None:
        self._settings = settings
        self._embedder = embedder or EmbeddingClient(settings)

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        candidate_k: int | None = None,
    ) -> RetrievalResult:
        settings = self._settings
        top_k = top_k or settings.retrieval_top_k
        candidate_k = candidate_k or settings.retrieval_candidate_k
        search_query = normalise_query(query)

        with timed(log, "retrieval", query_chars=len(query), top_k=top_k) as fields:
            lexical = await self._lexical(search_query, candidate_k)
            semantic, degraded_reason = await self._semantic(search_query, candidate_k)

            method = "hybrid" if semantic else "lexical"
            if semantic is None and self._embedder.enabled:
                method = "lexical"

            fused = self._fuse(lexical, semantic or [], settings.retrieval_rrf_k)
            # Hydrate a wider slice than needed so the diversity cap has
            # something to fall back to when one episode dominates.
            pool_ids = [cid for cid, _ in fused[: top_k * 4]]
            pool = await self._hydrate(pool_ids, fused, lexical, semantic or [])
            chunks = self._diversify(pool, top_k, settings.retrieval_max_per_episode)

            confidence = self._confidence(chunks, lexical, semantic)
            fields.update(
                {
                    "method": method,
                    "lexical_hits": len(lexical),
                    "semantic_hits": len(semantic or []),
                    "returned": len(chunks),
                    "confidence": round(confidence, 4),
                }
            )

        return RetrievalResult(
            chunks=chunks,
            method=method,
            confidence=confidence,
            query=search_query,
            degraded_reason=degraded_reason,
        )

    # -------------------------------------------------------------- legs ---
    async def _lexical(self, query: str, k: int) -> list[tuple[str, float]]:
        rows = await self._lexical_query(query, k)
        if rows:
            return rows
        # websearch_to_tsquery ANDs every term: a five-concept question ("B2B
        # SaaS activation improve product") needs one chunk containing every
        # word, and often none exists even though several chunks answer it
        # well on a subset of those terms. OR-ing the same words trades
        # precision for the recall that keeps a good question from being
        # refused outright -- RRF fusion and diversification downstream still
        # weed out the resulting weak matches.
        words = query.split()
        if len(words) < 2:
            return rows
        return await self._lexical_query(" OR ".join(words), k)

    async def _lexical_query(self, query: str, k: int) -> list[tuple[str, float]]:
        async with connection() as conn:
            rows = (await conn.execute(text(_LEXICAL_SQL), {"query": query, "k": k})).all()
        return [(str(r[0]), float(r[1])) for r in rows]

    async def _semantic(
        self, query: str, k: int
    ) -> tuple[list[tuple[str, float]] | None, str | None]:
        if not self._embedder.enabled:
            return None, "semantic_disabled"
        try:
            vector = await self._embedder.embed_one(query)
        except EmbeddingUnavailable as exc:
            log.warning("semantic_leg_unavailable", extra={"error": str(exc)[:200]})
            return None, "embedding_unavailable"
        try:
            async with connection() as conn:
                rows = (
                    await conn.execute(text(_SEMANTIC_SQL), {"vector": str(vector), "k": k})
                ).all()
        except Exception as exc:  # noqa: BLE001 - pgvector may not be installed
            log.warning("semantic_query_failed", extra={"error_type": type(exc).__name__})
            return None, "pgvector_unavailable"
        return [(str(r[0]), float(r[1])) for r in rows], None

    # ------------------------------------------------------------- fusion ---
    @staticmethod
    def _fuse(
        lexical: list[tuple[str, float]],
        semantic: list[tuple[str, float]],
        rrf_k: int,
    ) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for rank, (chunk_id, _) in enumerate(lexical, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + _rrf(rank, rrf_k)
        for rank, (chunk_id, _) in enumerate(semantic, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + _rrf(rank, rrf_k)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    @staticmethod
    def _diversify(
        pool: list[RetrievedChunk], top_k: int, max_per_episode: int
    ) -> list[RetrievedChunk]:
        """Cap how many passages one episode may contribute.

        Without this, a single long episode that matches well tends to occupy
        every slot: the answer then rests on one guest's opinion while looking
        as though it were broadly sourced. Capping per episode makes the
        citation list genuinely varied, which is the honest representation of
        what the corpus supports.

        Passages beyond the cap are not discarded outright -- they backfill any
        remaining slots, so a narrow corpus still returns a full result set.
        """
        if max_per_episode <= 0:
            return pool[:top_k]

        selected: list[RetrievedChunk] = []
        overflow: list[RetrievedChunk] = []
        per_episode: dict[str, int] = {}

        for chunk in pool:  # pool is already in fused-score order
            count = per_episode.get(chunk.episode_id, 0)
            if count < max_per_episode and len(selected) < top_k:
                selected.append(chunk)
                per_episode[chunk.episode_id] = count + 1
            else:
                overflow.append(chunk)

        for chunk in overflow:
            if len(selected) >= top_k:
                break
            selected.append(chunk)
        return selected

    @staticmethod
    def _confidence(
        chunks: list[RetrievedChunk],
        lexical: list[tuple[str, float]],
        semantic: list[tuple[str, float]] | None,
    ) -> float:
        """A 0-1 signal used to decide whether to answer or to refuse.

        Cosine similarity from the embedding model is the better-behaved signal,
        so it is preferred when available. ts_rank_cd is unbounded, so it is
        squashed rather than normalised against an arbitrary maximum. Two weak
        agreeing legs should not look like one strong one, hence `max`, not sum.

        The threshold that consumes this (RETRIEVAL_MIN_CONFIDENCE) is
        calibrated against the golden set in backend/tests/golden.
        """
        if not chunks:
            return 0.0
        best = 0.0
        if semantic:
            # Cosine similarity on nomic-embed-text sits ~0.4-0.8 for relevant
            # passages; rescale so 0.35 -> 0 and 0.85 -> 1.
            best = max(best, (semantic[0][1] - 0.35) / 0.5)
        if lexical:
            top = lexical[0][1]
            best = max(best, top / (top + 0.08))
        return max(0.0, min(1.0, best))

    # ------------------------------------------------------------ hydrate ---
    async def _hydrate(
        self,
        ids: list[str],
        fused: list[tuple[str, float]],
        lexical: list[tuple[str, float]],
        semantic: list[tuple[str, float]],
    ) -> list[RetrievedChunk]:
        if not ids:
            return []
        async with connection() as conn:
            rows = (await conn.execute(text(_HYDRATE_SQL), {"ids": ids})).mappings().all()

        fused_scores = dict(fused)
        lexical_ranks = {cid: i for i, (cid, _) in enumerate(lexical, start=1)}
        semantic_ranks = {cid: i for i, (cid, _) in enumerate(semantic, start=1)}
        by_id = {str(row["id"]): row for row in rows}

        out: list[RetrievedChunk] = []
        for chunk_id in ids:  # preserve fused ordering
            row = by_id.get(chunk_id)
            if row is None:
                continue
            publish_date = row["publish_date"]
            out.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    episode_id=str(row["episode_id"]),
                    episode_slug=row["slug"],
                    episode_title=row["title"],
                    guest=row["guest"],
                    content=row["content"],
                    speakers=list(row["speakers"] or []),
                    start_seconds=row["start_seconds"],
                    end_seconds=row["end_seconds"],
                    chunk_index=row["chunk_index"],
                    youtube_url=row["youtube_url"],
                    video_id=row["video_id"],
                    publish_date=publish_date.isoformat() if publish_date else None,
                    source_commit=row["source_commit"],
                    source_path=row["source_path"],
                    score=fused_scores.get(chunk_id, 0.0),
                    lexical_rank=lexical_ranks.get(chunk_id),
                    semantic_rank=semantic_ranks.get(chunk_id),
                )
            )
        return out


async def knowledge_base_stats() -> dict[str, Any]:
    """Counts used by /health and by the empty-index guard."""
    async with connection() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM episodes) AS episodes, "
                    "(SELECT count(*) FROM chunks) AS chunks, "
                    "(SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS embedded"
                )
            )
        ).mappings().first()
    return dict(row or {"episodes": 0, "chunks": 0, "embedded": 0})

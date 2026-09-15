"""Embedding client.

Semantic retrieval is an *optional* capability. If the configured embedding
provider is unreachable (Ollama down, Gemini API error), `available()` returns
False and the retriever falls back to lexical-only search rather than failing
the request. Degraded retrieval is surfaced in the API response so the UI can
say so plainly.

Two backends are supported:

* **ollama** -- local, used in dev/Docker Compose where a model can be pulled.
* **gemini** -- hosted, used on Render where there is no GPU/persistent disk
  for Ollama. Uses `models/gemini-embedding-001` at `outputDimensionality=768`
  dimensions -- matching the `vector(768)` column so no schema migration is
  needed when switching between the two providers.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("app.retrieval.embeddings")

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class EmbeddingUnavailable(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._provider = settings.embedding_provider
        self._enabled = self._provider in ("ollama", "gemini")
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._gemini_api_key = settings.gemini_api_key
        # Free-tier Gemini quota is EmbedContentRequestsPerMinutePerProjectPer
        # Model = 100 (confirmed from the API's own 429 body), and each text
        # in one batchEmbedContents call counts as one request against it --
        # so a single full batch (_GEMINI_MAX_BATCH below) already spends the
        # *entire* per-minute budget, and the very next batch fails
        # immediately no matter how it's retried. Paced here instead of
        # reactively retried: one instance is reused for a whole ingestion
        # run, so tracking the last call time here (rather than per-request)
        # correctly paces every batch that instance sends, from any caller.
        self._last_gemini_call_monotonic: float | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def available(self, timeout: float = 5.0) -> bool:
        """True when the configured embedding backend is actually usable."""
        if not self._enabled:
            return False
        if self._provider == "gemini":
            return bool(self._gemini_api_key)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                names = {m.get("name", "") for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("embedding_probe_failed", extra={"error_type": type(exc).__name__})
            return False
        # Ollama reports "nomic-embed-text:latest" for "nomic-embed-text".
        return any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)

    async def embed(
        self,
        texts: list[str],
        *,
        timeout: float = 120.0,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Embed passages for the index. `task_type` only affects Gemini,
        which produces measurably better retrieval when queries and documents
        are embedded asymmetrically -- see `embed_one` for the query side."""
        if not self._enabled:
            raise EmbeddingUnavailable("Embedding provider is disabled (EMBEDDING_PROVIDER=none).")
        if not texts:
            return []
        if self._provider == "gemini":
            vectors = await self._embed_gemini(texts, timeout=timeout, task_type=task_type)
        else:
            vectors = await self._embed_ollama(texts, timeout=timeout)

        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                f"Embedding backend returned {len(vectors)} vectors for {len(texts)} inputs."
            )
        if vectors and len(vectors[0]) != self.dimensions:
            raise EmbeddingUnavailable(
                f"Embedding model '{self.model}' returned {len(vectors[0])} dimensions, "
                f"but the schema expects {self.dimensions}. Update EMBEDDING_DIMENSIONS "
                "and re-run migrations if you changed the model."
            )
        return vectors

    async def embed_one(self, text: str, *, timeout: float = 60.0) -> list[float]:
        """Embed a search query -- asymmetric task_type vs. `embed`."""
        result = await self.embed([text], timeout=timeout, task_type="RETRIEVAL_QUERY")
        return result[0]

    # ------------------------------------------------------------ backends ---
    async def _embed_ollama(self, texts: list[str], *, timeout: float) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                return response.json().get("embeddings") or []
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Embedding request to Ollama failed: {exc}") from exc

    # Gemini's batchEmbedContents rejects more than 100 requests in one call,
    # independent of EMBEDDING_BATCH_SIZE (which is sized for Ollama). Chunk
    # here so callers can keep using one config value for either backend.
    # 100 also happens to equal the free-tier per-minute quota itself (see
    # __init__'s comment), so this is the largest batch that can ever
    # succeed on that tier, not just an API ceiling.
    _GEMINI_MAX_BATCH = 100
    # Spacing between successive batches so a full-size batch (which alone
    # can spend the entire per-minute quota) never overlaps the next one.
    # Slightly over 60s for margin against clock/window-alignment slop --
    # Google's own 429 body suggested a ~46-47s retry-after in practice, so
    # this leaves real headroom rather than racing the reset exactly.
    _GEMINI_MIN_SECONDS_BETWEEN_BATCHES = 61.0
    _GEMINI_RATE_LIMIT_RETRIES = 3

    async def _embed_gemini(
        self, texts: list[str], *, timeout: float, task_type: str
    ) -> list[list[float]]:
        if not self._gemini_api_key:
            raise EmbeddingUnavailable("GEMINI_API_KEY is not set.")
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"{_GEMINI_BASE_URL}/{model}:batchEmbedContents"

        vectors: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for start in range(0, len(texts), self._GEMINI_MAX_BATCH):
                    batch = texts[start : start + self._GEMINI_MAX_BATCH]
                    requests = [
                        {
                            "model": model,
                            "content": {"parts": [{"text": t}]},
                            "taskType": task_type,
                            "outputDimensionality": self.dimensions,
                        }
                        for t in batch
                    ]
                    response = await self._post_paced(client, url, requests)
                    response.raise_for_status()
                    embeddings = response.json().get("embeddings") or []
                    vectors.extend(e.get("values", []) for e in embeddings)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Embedding request to Gemini failed: {exc}") from exc
        return vectors

    async def _post_paced(
        self, client: httpx.AsyncClient, url: str, requests: list[dict]
    ) -> httpx.Response:
        """Send one batch, spaced from the previous one, retrying a 429 as a
        last resort (proactive pacing is the real fix; reactive retry only
        covers a window-alignment edge the pacing itself can't guarantee)."""
        import asyncio
        import time

        for attempt in range(self._GEMINI_RATE_LIMIT_RETRIES + 1):
            if self._last_gemini_call_monotonic is not None:
                elapsed = time.monotonic() - self._last_gemini_call_monotonic
                remaining = self._GEMINI_MIN_SECONDS_BETWEEN_BATCHES - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_gemini_call_monotonic = time.monotonic()
            response = await client.post(
                url, params={"key": self._gemini_api_key}, json={"requests": requests}
            )
            if response.status_code != 429 or attempt == self._GEMINI_RATE_LIMIT_RETRIES:
                return response
            log.warning(
                "gemini_embedding_rate_limited",
                extra={"attempt": attempt + 1},
            )
        return response  # pragma: no cover - loop always returns above

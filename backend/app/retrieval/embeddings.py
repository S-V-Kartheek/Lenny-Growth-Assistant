"""Embedding client.

Semantic retrieval is an *optional* capability. If the embedding model is not
pulled, or Ollama is down, `available()` returns False and the retriever falls
back to lexical-only search rather than failing the request. Degraded retrieval
is surfaced in the API response so the UI can say so plainly.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("app.retrieval.embeddings")


class EmbeddingUnavailable(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._enabled = settings.embedding_provider == "ollama"
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._base_url = settings.ollama_base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def available(self, timeout: float = 5.0) -> bool:
        """True when the configured embedding model is actually loadable."""
        if not self._enabled:
            return False
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

    async def embed(self, texts: list[str], *, timeout: float = 120.0) -> list[list[float]]:
        if not self._enabled:
            raise EmbeddingUnavailable("Embedding provider is disabled (EMBEDDING_PROVIDER=none).")
        if not texts:
            return []
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self.model, "input": texts},
                )
                response.raise_for_status()
                vectors = response.json().get("embeddings") or []
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(
                f"Embedding request to Ollama failed: {exc}"
            ) from exc

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
        result = await self.embed([text], timeout=timeout)
        return result[0]

"""Shared FastAPI dependencies.

Settings are resolved from `app.state` rather than the module-level
`get_settings()` singleton. The singleton is fine for CLI entry points, but a
request handler that reaches for a global cannot be exercised against a
different configuration -- which is exactly what the tests need to do to prove
the API behaves correctly when a dependency is unavailable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.config import Settings
from app.retrieval.embeddings import EmbeddingClient
from app.retrieval.retriever import Retriever


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_embedder(settings: SettingsDep) -> EmbeddingClient:
    return EmbeddingClient(settings)


EmbedderDep = Annotated[EmbeddingClient, Depends(get_embedder)]


def get_retriever(settings: SettingsDep, embedder: EmbedderDep) -> Retriever:
    return Retriever(settings, embedder)


RetrieverDep = Annotated[Retriever, Depends(get_retriever)]

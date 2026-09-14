"""Shared FastAPI dependencies.

Settings are resolved from `app.state` rather than the module-level
`get_settings()` singleton. The singleton is fine for CLI entry points, but a
request handler that reaches for a global cannot be exercised against a
different configuration -- which is exactly what the tests need to do to prove
the API behaves correctly when a dependency is unavailable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from app.agent.orchestrator import Agent
from app.config import Settings
from app.llm.registry import LLMGateway
from app.retrieval.embeddings import EmbeddingClient
from app.retrieval.retriever import Retriever
from app.services import sessions


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_embedder(settings: SettingsDep) -> EmbeddingClient:
    return EmbeddingClient(settings)


EmbedderDep = Annotated[EmbeddingClient, Depends(get_embedder)]


def get_retriever(settings: SettingsDep, embedder: EmbedderDep) -> Retriever:
    return Retriever(settings, embedder)


RetrieverDep = Annotated[Retriever, Depends(get_retriever)]


def get_gateway(request: Request, settings: SettingsDep) -> LLMGateway:
    """One gateway per application, cached on app.state.

    Constructed lazily rather than at startup so the health check, config
    validation and other paths that only need `Settings` are unaffected by
    whichever provider is configured -- and so tests can swap `app.state`
    without touching the lifespan.
    """
    gateway = getattr(request.app.state, "llm_gateway", None)
    if gateway is None:
        gateway = LLMGateway(settings)
        request.app.state.llm_gateway = gateway
    return gateway


GatewayDep = Annotated[LLMGateway, Depends(get_gateway)]


def get_agent(settings: SettingsDep, gateway: GatewayDep, retriever: RetrieverDep) -> Agent:
    return Agent(settings, gateway, retriever)


AgentDep = Annotated[Agent, Depends(get_agent)]


async def get_current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    """Resolve the caller to a user row.

    No authentication (assumption A4): `X-User-Id` is an optional client-chosen
    label for multi-browser testing, folded to the single anonymous user when
    absent. It is not a security boundary -- see docs/architecture.md.
    """
    _ = x_user_id  # reserved for a future multi-user mode; not yet load-bearing
    return await sessions.get_or_create_anonymous_user()


UserIdDep = Annotated[str, Depends(get_current_user_id)]

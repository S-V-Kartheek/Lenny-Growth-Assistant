"""Request/response models for the conversational API.

These are the wire contract, kept separate from `app.agent.contracts` (the
internal skill contract) so that changing the internal event shape does not
silently change the public API, and vice versa.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.config import Provider


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class UpdateSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SessionSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class MessageRecord(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    intent: str | None = None
    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    token_usage: dict[str, Any] | None = None
    grounding: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class SessionHistoryResponse(BaseModel):
    session: SessionSummary
    messages: list[MessageRecord]


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ProviderInfo(BaseModel):
    provider: str
    model: str
    context_tokens: int
    fallback_provider: str | None = None


class SetProviderRequest(BaseModel):
    provider: Provider

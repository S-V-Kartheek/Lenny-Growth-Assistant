"""Typed application configuration.

Every runtime knob lives here and is sourced from the environment, so an
evaluator can change model, provider, retrieval behaviour or ingestion scope
without touching application code (assignment section 3.2).
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Provider(StrEnum):
    """Supported LLM backends.

    StrEnum so a provider compares and serialises as its own name, which keeps
    config values, log fields and API responses identical strings.
    """

    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "Lenny Growth Assistant"
    environment: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    api_prefix: str = "/api"

    # CORS origins allowed to call the API. Kept explicit (never "*") because
    # the API is session-bearing; see docs/architecture.md#security.
    # NoDecode: without it pydantic-settings JSON-decodes list fields straight
    # from .env and a plain comma-separated value raises before any validator
    # runs. NoDecode hands the raw string to _split_csv instead.
    # :5173 is the Vite dev server; :4173 is the built `web` Compose service
    # (frontend/Dockerfile's `serve` stage); :3000 covers a common alternate
    # dev port. Missing one of these here is not just a CORS nuisance -- the
    # artifact document endpoint's frame-ancestors is also derived from this
    # list (app.agent.sanitize.response_csp), so an origin left out here
    # cannot embed a sandboxed artifact either. Found live, against the
    # actual containerised `web` service, in checkpoint 4.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://localhost:4173",
            "http://localhost:3000",
        ]
    )

    # --------------------------------------------------------------- data ---
    database_url: str = "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_seconds: float = 5.0
    db_statement_timeout_seconds: float = 30.0

    # ----------------------------------------------------------- provider ---
    llm_provider: Provider = Provider.OLLAMA
    llm_temperature: float = 0.3
    llm_timeout_seconds: float = 180.0
    llm_max_output_tokens: int = 2048

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    # Context window of the configured local model. Used for context budgeting;
    # a wrong value here degrades answers silently, so it is explicit config.
    ollama_context_tokens: int = 32768

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    anthropic_context_tokens: int = 200000

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_context_tokens: int = 128000

    # If the selected provider is unreachable, fall back to this one when it is
    # configured. Empty string disables fallback (fail loudly instead).
    llm_fallback_provider: Provider | None = None

    # ---------------------------------------------------------- embedding ---
    embedding_provider: Literal["ollama", "none"] = "ollama"
    embedding_model: str = "nomic-embed-text"
    embedding_dimensions: int = 768
    # Measured on nomic-embed-text: 16/batch ~3.8 chunks/s, 128/batch ~8.7.
    # Bigger batches amortise model-load and HTTP overhead; beyond ~256 the
    # gain flattens while a single failed batch costs more work to redo.
    embedding_batch_size: int = 128

    # ---------------------------------------------------------- retrieval ---
    retrieval_top_k: int = 6
    retrieval_candidate_k: int = 30
    # Below this fused-confidence score the assistant refuses rather than
    # guessing. Calibrated against the golden set (see docs/architecture.md).
    retrieval_min_confidence: float = 0.48
    retrieval_rrf_k: int = 60
    # Maximum passages one episode may contribute to a single answer, so the
    # citation list reflects the corpus rather than one dominant episode.
    retrieval_max_per_episode: int = 2

    # ---------------------------------------------------------- ingestion ---
    corpus_repo: str = "ChatPRD/lennys-podcast-transcripts"
    # Pinned so ingestion is reproducible and every citation is traceable to an
    # exact upstream revision.
    corpus_commit: str = "be8ab89a890a833cbba2c892178f823fff178c65"
    corpus_cache_dir: str = "data/corpus"
    # Clips and trailers are excluded; only full-length episodes carry the
    # depth a growth question needs.
    ingest_min_duration_seconds: int = 1200
    ingest_max_episodes: int = 40
    ingest_all: bool = False
    ingest_episodes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    chunk_target_tokens: int = 320
    chunk_overlap_turns: int = 1

    # ------------------------------------------------------------ limits ---
    max_message_chars: int = 4000
    max_sessions_per_user: int = 200
    rate_limit_per_minute: int = 60

    @field_validator("cors_origins", "ingest_episodes", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept either a comma-separated string or a JSON array from .env.

        NoDecode disables pydantic-settings' own JSON decoding for these fields,
        so both shapes have to be handled here. CSV is the documented form
        because it is what people actually type into a .env file.
        """
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Expected a comma-separated list or valid JSON array, got: {stripped!r}"
                    ) from exc
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    @field_validator("llm_fallback_provider", "anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_fallback(self) -> Settings:
        if self.llm_fallback_provider == self.llm_provider:
            self.llm_fallback_provider = None
        return self

    # ------------------------------------------------------------ helpers ---
    @property
    def sync_database_url(self) -> str:
        """psycopg-style URL, used by tooling that cannot speak asyncpg."""
        return self.database_url.replace("+asyncpg", "")

    def model_for(self, provider: Provider) -> str:
        return {
            Provider.OLLAMA: self.ollama_model,
            Provider.ANTHROPIC: self.anthropic_model,
            Provider.OPENAI: self.openai_model,
        }[provider]

    def context_tokens_for(self, provider: Provider) -> int:
        return {
            Provider.OLLAMA: self.ollama_context_tokens,
            Provider.ANTHROPIC: self.anthropic_context_tokens,
            Provider.OPENAI: self.openai_context_tokens,
        }[provider]

    def api_key_for(self, provider: Provider) -> str | None:
        return {
            Provider.OLLAMA: None,
            Provider.ANTHROPIC: self.anthropic_api_key,
            Provider.OPENAI: self.openai_api_key,
        }[provider]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

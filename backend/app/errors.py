"""Structured application errors and their HTTP representation.

Every failure the client can observe is one of these, so the frontend can react
to a stable machine-readable `code` instead of parsing prose. This is what lets
the UI show "Ollama is not running" rather than a generic 500.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorCode:
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    DATABASE_UNAVAILABLE = "database_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    KNOWLEDGE_BASE_EMPTY = "knowledge_base_empty"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ARTIFACT_REJECTED = "artifact_rejected"
    INTERNAL_ERROR = "internal_error"


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error identifier.")
    message: str = Field(description="Human-readable explanation, safe to display.")
    remediation: str | None = Field(
        default=None, description="What the operator or user can do about it."
    )
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base class for expected failures with a defined client contract."""

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR
    remediation: str | None = None

    def __init__(
        self,
        message: str,
        *,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if remediation is not None:
            self.remediation = remediation
        self.details = details

    def to_body(self, request_id: str | None = None) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            remediation=self.remediation,
            details=self.details,
            request_id=request_id,
        )


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class ValidationFailure(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR


class RateLimited(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    remediation = "Slow down and retry in a few seconds."


class DatabaseUnavailable(AppError):
    status_code = 503
    code = ErrorCode.DATABASE_UNAVAILABLE
    remediation = (
        "PostgreSQL is unreachable. Check `docker compose ps db` and DATABASE_URL."
    )


class ProviderUnavailable(AppError):
    status_code = 503
    code = ErrorCode.PROVIDER_UNAVAILABLE
    remediation = (
        "The configured model backend is unreachable. For Ollama, run `ollama serve` "
        "and `ollama pull <model>`; for cloud providers, check the API key."
    )


class ProviderTimeout(AppError):
    status_code = 504
    code = ErrorCode.PROVIDER_TIMEOUT
    remediation = (
        "The model took too long. Increase LLM_TIMEOUT_SECONDS or use a smaller model."
    )


class ProviderError(AppError):
    status_code = 502
    code = ErrorCode.PROVIDER_ERROR


class ModelOutputInvalid(AppError):
    status_code = 502
    code = ErrorCode.MODEL_OUTPUT_INVALID
    remediation = "The model returned output that failed schema validation after retry."


class KnowledgeBaseEmpty(AppError):
    status_code = 503
    code = ErrorCode.KNOWLEDGE_BASE_EMPTY
    remediation = "Run `make ingest` (or `docker compose run --rm ingest`) to index transcripts."


class ArtifactRejected(AppError):
    status_code = 422
    code = ErrorCode.ARTIFACT_REJECTED

"""Shared HTTP plumbing for the provider implementations.

Why one transport rather than three vendor SDKs
-----------------------------------------------
`anthropic` and `openai` both ship capable official SDKs. They were not used,
for three reasons specific to this system:

1. **The abstraction is the deliverable.** Three SDKs means three retry
   policies, three exception hierarchies and three streaming shapes to
   normalise. The normalising code is the risky part, and writing it against
   one HTTP client instead of three makes it a third of the size.
2. **Testability without credentials.** `httpx.MockTransport` lets the
   Anthropic and OpenAI providers be unit-tested end to end -- request shape,
   SSE parsing, error translation -- on a machine with no API keys. That is the
   only way these paths get tested at all in this engagement.
3. **Image size and supply chain.** The container already carries httpx for
   embeddings. Both wire protocols used here are small and stable.

The trade-off, recorded honestly: the SDKs would give us automatic retries with
jitter, request-id capture and forward compatibility with new response fields
for free. If cloud providers became the primary path, that would be worth the
swap -- and it would be a change inside `app/llm/`, nowhere else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.errors import ProviderError, ProviderTimeout, ProviderUnavailable

log = logging.getLogger("app.llm")


def translate_http_error(provider: str, exc: Exception) -> Exception:
    """Map transport failures onto the API's stable error codes.

    The distinction matters to the caller: a timeout is worth retrying or
    worth a smaller model, an unreachable host is an operator problem, and a
    4xx is a configuration problem. Collapsing them into one code would send
    an operator to check the wrong thing.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeout(
            f"The {provider} model did not respond in time.",
            details={"provider": provider},
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        body = _safe_body(exc.response)
        if status in (401, 403):
            return ProviderUnavailable(
                f"{provider} rejected the credentials.",
                remediation=f"Check the API key for {provider} in .env.",
                details={"provider": provider, "status": status},
            )
        if status == 404:
            return ProviderUnavailable(
                f"{provider} does not have the requested model.",
                remediation=(
                    "For Ollama run `ollama pull <model>`; for cloud providers check "
                    "the model name is spelled exactly as the vendor documents it."
                ),
                details={"provider": provider, "status": status, "body": body},
            )
        if status == 429:
            return ProviderError(
                f"{provider} rate-limited the request.",
                remediation="Wait and retry, or lower request volume.",
                details={"provider": provider, "status": status},
            )
        return ProviderError(
            f"{provider} returned HTTP {status}.",
            details={"provider": provider, "status": status, "body": body},
        )
    if isinstance(exc, httpx.HTTPError):
        return ProviderUnavailable(
            f"{provider} is unreachable.",
            details={"provider": provider, "error_type": type(exc).__name__},
        )
    return exc


def _safe_body(response: httpx.Response) -> str:
    """Truncated error body. Useful for diagnosis, bounded so it cannot flood logs."""
    try:
        return response.text[:300]
    except Exception:  # noqa: BLE001 - a streaming response may not have been read
        return ""


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON payloads from a `data:` SSE stream.

    `[DONE]` sentinels and non-JSON keep-alive lines are dropped rather than
    raising: a provider inserting a comment line must not kill a live answer.
    """
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            log.warning("sse_line_not_json", extra={"line": payload[:120]})


async def iter_ndjson(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON objects from a newline-delimited stream (Ollama)."""
    async for line in response.aiter_lines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            log.warning("ndjson_line_not_json", extra={"line": line[:120]})

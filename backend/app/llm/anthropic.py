"""Anthropic provider (Messages API, server-sent events).

Two shape differences from the other providers are handled here so that no
caller ever has to know about them:

* Anthropic takes the system prompt as a **top-level parameter**, not as a
  message with `role: "system"`. Passing one in the message list is a 400.
* `max_tokens` is **required**, not optional.

Health check: there is no free "ping" endpoint, so `health()` issues a
deliberately minimal one-token request. That costs a fraction of a cent and
verifies the three things that actually break -- key validity, model name, and
reachability -- which a key-presence check cannot.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.transport import iter_sse_data, translate_http_error

log = logging.getLogger("app.llm.anthropic")

API_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.anthropic_model
        self.context_tokens = settings.anthropic_context_tokens
        self._api_key = settings.anthropic_api_key
        self._timeout = settings.llm_timeout_seconds
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_output_tokens
        self._base_url = "https://api.anthropic.com/v1"
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key or "",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _split(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict[str, str]]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        turns = [m.as_dict() for m in messages if m.role != "system"]
        return system, turns

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="not_configured",
                reason="ANTHROPIC_API_KEY is not set.",
                remediation="Set ANTHROPIC_API_KEY in .env to use this provider.",
            )
        try:
            client = self._client or httpx.AsyncClient(timeout=15.0)
            try:
                response = await client.post(
                    f"{self._base_url}/messages",
                    headers=self._headers,
                    json={
                        "model": self.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
                response.raise_for_status()
            finally:
                if self._client is None:
                    await client.aclose()
        except httpx.HTTPError as exc:
            translated = translate_http_error(self.name, exc)
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="unavailable",
                reason=str(translated),
                remediation=getattr(translated, "remediation", None),
            )
        return ProviderHealth(provider=self.name, model=self.model, status="ok")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._api_key:
            from app.errors import ProviderUnavailable

            raise ProviderUnavailable(
                "Anthropic is selected but ANTHROPIC_API_KEY is not set.",
                remediation="Set ANTHROPIC_API_KEY in .env, or switch LLM_PROVIDER back to ollama.",
            )

        system, turns = self._split(messages)
        payload: dict[str, object] = {
            "model": self.model,
            "messages": turns,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": self._temperature if temperature is None else temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        usage = TokenUsage()
        try:
            async with client.stream(
                "POST", f"{self._base_url}/messages", headers=self._headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    # The body has to be read before it can be inspected; without
                    # this the error carries an empty body and says nothing useful.
                    await response.aread()
                response.raise_for_status()
                finish_reason: str | None = None
                async for event in iter_sse_data(response):
                    kind = event.get("type")
                    if kind == "message_start":
                        start_usage = (event.get("message") or {}).get("usage") or {}
                        usage.input_tokens = int(start_usage.get("input_tokens") or 0)
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if text := delta.get("text"):
                            yield StreamEvent(text=text)
                    elif kind == "message_delta":
                        finish_reason = (event.get("delta") or {}).get("stop_reason")
                        usage.output_tokens = int(
                            (event.get("usage") or {}).get("output_tokens") or 0
                        )
                    elif kind == "error":
                        raise translate_http_error(
                            self.name,
                            httpx.HTTPError(str((event.get("error") or {}).get("message"))),
                        ) from None
                yield StreamEvent(done=True, usage=usage, finish_reason=finish_reason)
        except httpx.HTTPError as exc:
            raise translate_http_error(self.name, exc) from exc
        finally:
            if owns_client:
                await client.aclose()

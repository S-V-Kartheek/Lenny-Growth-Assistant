"""Grok provider (xAI's Chat Completions API -- OpenAI-compatible).

Kept as its own `LLMProvider` (rather than pointing `OPENAI_BASE_URL` at xAI)
so it can carry its own API key, model and context-window settings and sit
alongside Gemini in the fallback chain without displacing the OpenAI slot.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.transport import iter_sse_data, translate_http_error

log = logging.getLogger("app.llm.grok")


class GrokProvider(LLMProvider):
    name = "grok"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.grok_model
        self.context_tokens = settings.grok_context_tokens
        self._api_key = settings.grok_api_key
        self._base_url = settings.grok_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_output_tokens
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key or ''}",
            "content-type": "application/json",
        }

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="not_configured",
                reason="GROK_API_KEY is not set.",
                remediation="Set GROK_API_KEY in .env to use this provider.",
            )
        try:
            client = self._client or httpx.AsyncClient(timeout=15.0)
            try:
                response = await client.get(
                    f"{self._base_url}/models/{self.model}", headers=self._headers
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
                "Grok is selected but GROK_API_KEY is not set.",
                remediation="Set GROK_API_KEY in .env, or switch LLM_PROVIDER back to ollama.",
            )

        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "temperature": self._temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        usage = TokenUsage()
        finish_reason: str | None = None
        try:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions", headers=self._headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                response.raise_for_status()
                async for event in iter_sse_data(response):
                    if reported := event.get("usage"):
                        usage = TokenUsage(
                            input_tokens=int(reported.get("prompt_tokens") or 0),
                            output_tokens=int(reported.get("completion_tokens") or 0),
                        )
                    for choice in event.get("choices") or []:
                        if text := (choice.get("delta") or {}).get("content"):
                            yield StreamEvent(text=text)
                        if reason := choice.get("finish_reason"):
                            finish_reason = reason
                yield StreamEvent(done=True, usage=usage, finish_reason=finish_reason)
        except httpx.HTTPError as exc:
            raise translate_http_error(self.name, exc) from exc
        finally:
            if owns_client:
                await client.aclose()

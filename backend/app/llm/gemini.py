"""Gemini provider (Generative Language API, server-sent events).

Two shape differences from the other providers are handled here so no caller
has to know about them:

* Gemini's chat roles are `user`/`model`, not `user`/`assistant`, and the
  system prompt is a separate `systemInstruction` field -- not a message with
  `role: "system"` (same restriction as Anthropic, different field name).
* The API key is sent as the `x-goog-api-key` header rather than a query
  parameter, so it never ends up in a URL that could be logged.

Health check hits `GET /v1beta/models/{model}` -- free, and confirms both the
key and the model name exist, the same trade Anthropic's design note explains
for why OpenAI/Ollama-style "list models" checks are preferred over a
token-spending probe where the vendor offers one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.transport import iter_sse_data, translate_http_error

log = logging.getLogger("app.llm.gemini")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.gemini_model
        self.context_tokens = settings.gemini_context_tokens
        self._api_key = settings.gemini_api_key
        self._timeout = settings.llm_timeout_seconds
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_output_tokens
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key or "", "content-type": "application/json"}

    def _split(self, messages: list[ChatMessage]) -> tuple[dict | None, list[dict[str, object]]]:
        system_text = "\n\n".join(m.content for m in messages if m.role == "system")
        system = {"parts": [{"text": system_text}]} if system_text else None
        turns = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        return system, turns

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="not_configured",
                reason="GEMINI_API_KEY is not set.",
                remediation="Set GEMINI_API_KEY in .env to use this provider.",
            )
        try:
            client = self._client or httpx.AsyncClient(timeout=15.0)
            try:
                response = await client.get(
                    f"{_BASE_URL}/models/{self.model}", headers=self._headers
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
                "Gemini is selected but GEMINI_API_KEY is not set.",
                remediation="Set GEMINI_API_KEY in .env, or switch LLM_PROVIDER back to ollama.",
            )

        system, turns = self._split(messages)
        payload: dict[str, object] = {
            "contents": turns,
            "generationConfig": {
                "temperature": self._temperature if temperature is None else temperature,
                "maxOutputTokens": max_tokens or self._max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = system

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        usage = TokenUsage()
        finish_reason: str | None = None
        url = f"{_BASE_URL}/models/{self.model}:streamGenerateContent?alt=sse"
        try:
            async with client.stream(
                "POST", url, headers=self._headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                response.raise_for_status()
                async for event in iter_sse_data(response):
                    for candidate in event.get("candidates") or []:
                        parts = (candidate.get("content") or {}).get("parts") or []
                        for part in parts:
                            if text := part.get("text"):
                                yield StreamEvent(text=text)
                        if reason := candidate.get("finishReason"):
                            finish_reason = reason
                    if reported := event.get("usageMetadata"):
                        usage = TokenUsage(
                            input_tokens=int(reported.get("promptTokenCount") or 0),
                            output_tokens=int(reported.get("candidatesTokenCount") or 0),
                        )
                yield StreamEvent(done=True, usage=usage, finish_reason=finish_reason)
        except httpx.HTTPError as exc:
            raise translate_http_error(self.name, exc) from exc
        finally:
            if owns_client:
                await client.aclose()

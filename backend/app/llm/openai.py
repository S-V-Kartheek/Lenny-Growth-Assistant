"""OpenAI provider (Chat Completions API, server-sent events).

Chat Completions rather than the Responses API: it is the endpoint that every
OpenAI-compatible gateway (vLLM, LiteLLM, Together, Groq, llama.cpp's server)
also implements, so `OPENAI_BASE_URL` turns this one class into a provider for
any of them without further code. That reach is worth more here than the newer
API's features, none of which this system uses.

`stream_options.include_usage` is requested explicitly -- without it a streamed
response reports no token counts at all, and the per-message usage record would
be silently empty for this provider only.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.transport import iter_sse_data, translate_http_error

log = logging.getLogger("app.llm.openai")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.openai_model
        self.context_tokens = settings.openai_context_tokens
        self._api_key = settings.openai_api_key
        self._base_url = settings.openai_base_url.rstrip("/")
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
                reason="OPENAI_API_KEY is not set.",
                remediation="Set OPENAI_API_KEY in .env to use this provider.",
            )
        try:
            client = self._client or httpx.AsyncClient(timeout=15.0)
            try:
                # /models is free and confirms both the key and the model name,
                # so unlike Anthropic no token-spending probe is needed.
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
                "OpenAI is selected but OPENAI_API_KEY is not set.",
                remediation="Set OPENAI_API_KEY in .env, or switch LLM_PROVIDER back to ollama.",
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

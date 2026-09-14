"""Ollama provider -- the default, and the one the demo is designed around.

Assumption A7: the local model is the constraint to design for. Everything in
this file assumes a 7B model on consumer hardware: generous timeouts, streaming
by default, and `num_ctx` pinned to the configured context window because
Ollama otherwise silently truncates to its own default (2048 on many builds),
which would drop the retrieved passages out of the prompt and turn a grounded
answer into an ungrounded one with no error anywhere.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from app.config import Settings
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.transport import iter_ndjson, translate_http_error

log = logging.getLogger("app.llm.ollama")


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.ollama_model
        self.context_tokens = settings.ollama_context_tokens
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_seconds
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_output_tokens
        self._client = client

    def _open(self, timeout: float) -> httpx.AsyncClient:
        # An injected client (tests, or a future shared pool) is reused as-is;
        # otherwise a short-lived client per call keeps connection lifetime
        # bounded to the request, which matters when Ollama restarts.
        return self._client or httpx.AsyncClient(timeout=timeout)

    async def health(self) -> ProviderHealth:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                names = {m.get("name", "") for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError) as exc:
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="unavailable",
                reason=f"Ollama is unreachable at {self._base_url} ({type(exc).__name__}).",
                remediation="Start it with `ollama serve`, or correct OLLAMA_BASE_URL.",
            )
        # Ollama reports "qwen2.5:7b-instruct" but also answers to the bare name;
        # compare on the tag-stripped form so either spelling in .env works.
        if not any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names):
            return ProviderHealth(
                provider=self.name,
                model=self.model,
                status="unavailable",
                reason=f"Ollama is running but '{self.model}' is not pulled.",
                remediation=f"Run `ollama pull {self.model}`.",
            )
        return ProviderHealth(provider=self.name, model=self.model, status="ok")

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        payload = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "stream": True,
            "options": {
                "temperature": self._temperature if temperature is None else temperature,
                "num_predict": max_tokens or self._max_tokens,
                # Explicit, not inherited: see the module docstring.
                "num_ctx": self.context_tokens,
            },
        }
        client = self._open(self._timeout)
        owns_client = self._client is None
        try:
            async with client.stream(
                "POST", f"{self._base_url}/api/chat", json=payload
            ) as response:
                response.raise_for_status()
                async for chunk in iter_ndjson(response):
                    if error := chunk.get("error"):
                        raise translate_http_error(
                            self.name, httpx.HTTPError(str(error))
                        ) from None
                    text = (chunk.get("message") or {}).get("content", "")
                    if text:
                        yield StreamEvent(text=text)
                    if chunk.get("done"):
                        yield StreamEvent(
                            done=True,
                            usage=TokenUsage(
                                input_tokens=int(chunk.get("prompt_eval_count") or 0),
                                output_tokens=int(chunk.get("eval_count") or 0),
                            ),
                            finish_reason=chunk.get("done_reason"),
                        )
        except httpx.HTTPError as exc:
            raise translate_http_error(self.name, exc) from exc
        finally:
            if owns_client:
                await client.aclose()

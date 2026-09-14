"""Provider construction and the documented fallback policy.

Fallback rules, stated precisely because "it falls back" is not a specification
-- an operator needs to know exactly when their configured model is *not* the
one that answered:

1. Fallback happens **only** when `LLM_FALLBACK_PROVIDER` is set, names a
   different provider, and that provider is itself configured.
2. It triggers **only** on `ProviderUnavailable` or `ProviderTimeout` -- the
   provider could not be reached or did not respond. A `ProviderError` (a 4xx
   or a rejected request) is a configuration fault and is raised, because
   retrying it on another model hides a bug the operator must fix.
3. It triggers **only before the first token has been emitted.** Once text has
   streamed to the user, switching models would splice two models' prose into
   one answer. After that point the failure is surfaced, not papered over.
4. The `Completion` and every API response carry the provider that *actually*
   answered, plus `fallback_from`, so a switch is visible rather than silent.

With no fallback configured the system fails loudly. That is the default, and
it is the right default for a tool whose value is knowing where an answer came
from.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.config import Provider, Settings
from app.errors import ProviderTimeout, ProviderUnavailable
from app.llm.anthropic import AnthropicProvider
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent
from app.llm.ollama import OllamaProvider
from app.llm.openai import OpenAIProvider

log = logging.getLogger("app.llm.registry")

_BUILDERS = {
    Provider.OLLAMA: OllamaProvider,
    Provider.ANTHROPIC: AnthropicProvider,
    Provider.OPENAI: OpenAIProvider,
}

# Errors that mean "this backend could not serve the request", as opposed to
# "this request was wrong" -- only the former is worth retrying elsewhere.
_FALLBACK_TRIGGERS = (ProviderUnavailable, ProviderTimeout)


def build_provider(settings: Settings, provider: Provider | None = None) -> LLMProvider:
    return _BUILDERS[provider or settings.llm_provider](settings)


def all_providers(settings: Settings) -> list[LLMProvider]:
    """Every provider, for the diagnostics endpoint -- including unconfigured ones.

    Showing a provider as `not_configured` is more useful than omitting it: the
    operator can see the switch exists and what it needs before it will work.
    """
    return [build_provider(settings, p) for p in Provider]


class LLMGateway:
    """The single object the rest of the application talks to about models."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.primary = build_provider(settings)
        self.fallback: LLMProvider | None = (
            build_provider(settings, settings.llm_fallback_provider)
            if settings.llm_fallback_provider
            else None
        )
        # Which provider served the most recent stream. Read by the caller after
        # the stream finishes so the persisted message records the truth.
        self.last_provider: LLMProvider = self.primary
        self.last_fallback_from: str | None = None

    @property
    def describe(self) -> dict[str, object]:
        return {
            **self.primary.describe(),
            "fallback_provider": self.fallback.name if self.fallback else None,
        }

    async def health(self) -> ProviderHealth:
        return await self.primary.health()

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.last_provider = self.primary
        self.last_fallback_from = None
        emitted = False
        try:
            async for event in self.primary.stream(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                emitted = emitted or bool(event.text)
                yield event
            return
        except _FALLBACK_TRIGGERS as exc:
            if self.fallback is None or emitted:
                # Rule 3: past the first token, a switch would splice two models.
                if emitted:
                    log.error(
                        "provider_failed_mid_stream_no_fallback",
                        extra={"provider": self.primary.name, "error": str(exc)[:200]},
                    )
                raise
            log.warning(
                "provider_fallback",
                extra={
                    "from_provider": self.primary.name,
                    "to_provider": self.fallback.name,
                    "reason": type(exc).__name__,
                },
            )

        self.last_provider = self.fallback
        self.last_fallback_from = self.primary.name
        async for event in self.fallback.stream(
            messages, temperature=temperature, max_tokens=max_tokens
        ):
            yield event

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """Non-streaming convenience with the same fallback semantics."""
        import time

        from app.llm.base import Completion, TokenUsage

        started = time.perf_counter()
        parts: list[str] = []
        usage = TokenUsage()
        finish_reason: str | None = None
        async for event in self.stream(
            messages, temperature=temperature, max_tokens=max_tokens
        ):
            if event.text:
                parts.append(event.text)
            if event.usage:
                usage = event.usage
            if event.finish_reason:
                finish_reason = event.finish_reason
        return Completion(
            text="".join(parts),
            provider=self.last_provider.name,
            model=self.last_provider.model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
            finish_reason=finish_reason,
        )

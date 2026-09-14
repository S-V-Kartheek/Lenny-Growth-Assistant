"""The provider-agnostic contract every LLM backend implements.

Why this exists
---------------
The demo runs on Ollama, but the same code has to run against Anthropic or
OpenAI by changing `LLM_PROVIDER` alone (PRD 2.5). That is only true if every
caller in the codebase talks to *this* interface and nothing else -- no
provider-specific branches in the skills, the router or the API layer.

Three deliberate properties:

* **Chat, not completion.** All three backends expose a message-list API, and
  collapsing to a single prompt string would throw away the system/user
  separation that grounding depends on.
* **Streaming is first-class.** A 7B model on CPU takes tens of seconds. The
  non-streaming `complete()` is implemented *on top of* `stream()` so there is
  exactly one code path per provider to get wrong.
* **Every response carries its provenance.** `provider` and `model` travel with
  the text, so they can be persisted per message and shown in the UI rather
  than inferred from configuration that may since have changed.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class TokenUsage:
    """Reported by the provider where available; zeros mean 'not reported'.

    Ollama reports prompt/eval counts, the cloud providers report input/output
    tokens. Normalising them here keeps the message record comparable across
    providers instead of storing three different shapes.
    """

    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(slots=True)
class Completion:
    text: str
    provider: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    finish_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": self.usage.as_dict(),
            "latency_ms": round(self.latency_ms, 1),
            "finish_reason": self.finish_reason,
        }


@dataclass(slots=True)
class ProviderHealth:
    """A health answer specific enough to act on.

    "unavailable" is not useful on its own: an operator needs to know whether
    Ollama is down, the model was never pulled, or an API key is missing --
    three different fixes. `reason` and `remediation` carry that.
    """

    provider: str
    model: str
    status: Literal["ok", "unavailable", "not_configured"]
    reason: str | None = None
    remediation: str | None = None
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "reason": self.reason,
            "remediation": self.remediation,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
        }


@dataclass(slots=True)
class StreamEvent:
    """One unit of streamed output.

    `text` carries incremental content; the final event has `done=True` and
    carries usage/finish metadata. Providers emit deltas in their own shapes;
    this is the single shape the rest of the system consumes.
    """

    text: str = ""
    done: bool = False
    usage: TokenUsage | None = None
    finish_reason: str | None = None


class LLMProvider(ABC):
    """What every backend must implement. Four methods, no optional extras."""

    name: str
    model: str
    context_tokens: int

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Probe reachability *and* that the configured model actually exists.

        Checking only reachability was not enough in practice: Ollama answers
        `/api/tags` happily while the requested model has never been pulled,
        which then fails at generation time with an opaque 404.
        """

    @abstractmethod
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield incremental output. Must raise ProviderUnavailable/Timeout/Error."""

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Collect a full response. Deliberately built on `stream()`.

        A separate non-streaming implementation per provider would double the
        surface where request shaping, error translation and usage accounting
        can diverge between the streaming and non-streaming paths -- a class of
        bug that only shows up in whichever path the tests happen not to cover.
        """
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
            provider=self.name,
            model=self.model,
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
            finish_reason=finish_reason,
        )

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "context_tokens": self.context_tokens}

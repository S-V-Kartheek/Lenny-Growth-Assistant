"""Provider abstraction: the base contract, and the fallback policy in LLMGateway.

These use a fake in-process provider rather than mocking httpx, because the
behaviour under test (complete-built-on-stream, and the fallback rules) lives
above the HTTP layer and should not need a network shape to exercise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.errors import ProviderError, ProviderTimeout, ProviderUnavailable
from app.llm.base import ChatMessage, LLMProvider, ProviderHealth, StreamEvent, TokenUsage
from app.llm.registry import LLMGateway


class FakeProvider(LLMProvider):
    """Emits a scripted sequence of events, or raises a scripted exception."""

    def __init__(
        self,
        name: str,
        *,
        chunks: list[str] | None = None,
        raise_after: int = -1,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.context_tokens = 8192
        self._chunks = chunks or ["hello", " world"]
        self._raise_after = raise_after
        self._error = error
        self.calls: list[list[ChatMessage]] = []

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, model=self.model, status="ok")

    async def stream(
        self, messages: list[ChatMessage], *, temperature=None, max_tokens=None
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(messages)
        for i, chunk in enumerate(self._chunks):
            if i == self._raise_after:
                raise self._error or ProviderUnavailable("fake failure")
            yield StreamEvent(text=chunk)
        yield StreamEvent(done=True, usage=TokenUsage(1, 2), finish_reason="stop")


def _gateway(primary: LLMProvider, fallback: LLMProvider | None = None) -> LLMGateway:
    gateway = LLMGateway.__new__(LLMGateway)
    gateway._settings = None  # not touched by the code paths under test
    gateway.primary = primary
    gateway.fallback = fallback
    gateway.last_provider = primary
    gateway.last_fallback_from = None
    return gateway


async def test_complete_concatenates_stream_and_reports_usage() -> None:
    provider = FakeProvider("primary")
    completion = await provider.complete([ChatMessage(role="user", content="hi")])
    assert completion.text == "hello world"
    assert completion.provider == "primary"
    assert completion.usage.input_tokens == 1
    assert completion.usage.output_tokens == 2
    assert completion.finish_reason == "stop"


async def test_gateway_uses_primary_when_healthy() -> None:
    gateway = _gateway(FakeProvider("primary"))
    completion = await gateway.complete([ChatMessage(role="user", content="hi")])
    assert completion.provider == "primary"
    assert gateway.last_fallback_from is None


async def test_gateway_falls_back_before_any_token_is_emitted() -> None:
    primary = FakeProvider("primary", chunks=[], raise_after=0, error=ProviderUnavailable("down"))
    fallback = FakeProvider("fallback")
    gateway = _gateway(primary, fallback)

    completion = await gateway.complete([ChatMessage(role="user", content="hi")])

    assert completion.provider == "fallback"
    assert gateway.last_fallback_from == "primary"


async def test_gateway_does_not_fall_back_once_a_token_was_emitted() -> None:
    """Rule 3: switching mid-stream would splice two models' prose together."""
    primary = FakeProvider(
        "primary",
        chunks=["partial answer ", "more that never arrives"],
        raise_after=1,
        error=ProviderTimeout("slow"),
    )
    fallback = FakeProvider("fallback")
    gateway = _gateway(primary, fallback)

    with pytest.raises(ProviderTimeout):
        await gateway.complete([ChatMessage(role="user", content="hi")])
    assert fallback.calls == []


async def test_gateway_does_not_fall_back_on_a_configuration_error() -> None:
    """A ProviderError (4xx, rejected request) is a bug to fix, not a reason to retry elsewhere."""
    primary = FakeProvider(
        "primary", chunks=[], raise_after=0, error=ProviderError("bad request")
    )
    fallback = FakeProvider("fallback")
    gateway = _gateway(primary, fallback)

    with pytest.raises(ProviderError):
        await gateway.complete([ChatMessage(role="user", content="hi")])
    assert fallback.calls == []


async def test_gateway_without_a_configured_fallback_fails_loudly() -> None:
    primary = FakeProvider("primary", chunks=[], raise_after=0, error=ProviderUnavailable("down"))
    gateway = _gateway(primary, None)

    with pytest.raises(ProviderUnavailable):
        await gateway.complete([ChatMessage(role="user", content="hi")])


def test_provider_health_ok_property() -> None:
    assert ProviderHealth(provider="p", model="m", status="ok").ok
    assert not ProviderHealth(provider="p", model="m", status="unavailable").ok

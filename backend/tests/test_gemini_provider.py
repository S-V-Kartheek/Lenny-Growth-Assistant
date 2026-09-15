"""GeminiProvider: request shaping, SSE parsing, and error translation.

Unlike the FakeProvider-based gateway tests in test_llm.py, this exercises the
actual HTTP shape via httpx.MockTransport -- no live network, no API key
needed -- to prove role mapping (assistant -> model), the systemInstruction
split, and that a streamGenerateContent SSE response is decoded correctly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.errors import ProviderUnavailable
from app.llm.base import ChatMessage
from app.llm.gemini import GeminiProvider


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, gemini_api_key="test-key", **overrides)


def _sse_response(events: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


async def test_stream_yields_text_and_maps_roles() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        body = _sse_response(
            [
                {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]},
                {"candidates": [{"content": {"parts": [{"text": " world"}]}, "finishReason": "STOP"}]},
                {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}},
            ]
        )
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiProvider(_settings(), client=client)

    events = [
        e
        async for e in provider.stream(
            [
                ChatMessage(role="system", content="Be terse."),
                ChatMessage(role="user", content="Hi"),
                ChatMessage(role="assistant", content="Previously said this."),
            ]
        )
    ]
    await client.aclose()

    text = "".join(e.text for e in events)
    assert text == "Hello world"

    final = events[-1]
    assert final.done is True
    assert final.finish_reason == "STOP"
    assert final.usage.input_tokens == 5
    assert final.usage.output_tokens == 2

    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert "streamGenerateContent" in captured["url"]
    body = captured["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "Be terse."}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Hi"}]},
        {"role": "model", "parts": [{"text": "Previously said this."}]},
    ]


async def test_stream_without_api_key_raises_provider_unavailable() -> None:
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key=None))
    with pytest.raises(ProviderUnavailable):
        async for _ in provider.stream([ChatMessage(role="user", content="hi")]):
            pass


async def test_health_without_api_key_is_not_configured() -> None:
    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key=None))
    health = await provider.health()
    assert health.status == "not_configured"
    assert not health.ok


async def test_health_translates_401_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiProvider(_settings(), client=client)
    health = await provider.health()
    await client.aclose()

    assert health.status == "unavailable"
    assert "rejected the credentials" in health.reason

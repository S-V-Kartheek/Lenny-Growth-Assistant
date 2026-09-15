"""The per-process rate limiter added in checkpoint 5.

`Settings.rate_limit_per_minute` and `ErrorCode.RATE_LIMITED` existed since
checkpoint 1, but nothing enforced them -- found live while hardening the
running stack for checkpoint 5 (hammering the API showed every request
succeeding regardless of volume). These tests exercise the enforcement added
in `app.main.create_app`'s `rate_limit` middleware.

Uses `client_without_database`-style setup (an unreachable database) because
the limiter runs before any route touches the database, and `/openapi.json`
is a route that never needs one -- so these tests isolate the middleware
itself, not anything downstream.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db.engine import dispose_engine, init_engine
from app.main import create_app

UNREACHABLE_DB = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"


async def _client(rate_limit_per_minute: int) -> AsyncClient:
    settings = Settings(
        _env_file=None,
        database_url=UNREACHABLE_DB,
        embedding_provider="none",
        rate_limit_per_minute=rate_limit_per_minute,
        cors_origins=["http://localhost:5173"],
    )
    init_engine(settings)
    app = create_app(settings)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
async def tight_client() -> AsyncClient:
    client = await _client(rate_limit_per_minute=2)
    yield client
    await client.aclose()
    await dispose_engine()


@pytest.fixture
async def unlimited_client() -> AsyncClient:
    client = await _client(rate_limit_per_minute=0)
    yield client
    await client.aclose()
    await dispose_engine()


async def test_requests_within_the_limit_succeed(tight_client: AsyncClient) -> None:
    for _ in range(2):
        response = await tight_client.get("/openapi.json")
        assert response.status_code == 200


async def test_the_request_over_the_limit_is_rejected_with_the_shared_envelope(
    tight_client: AsyncClient,
) -> None:
    for _ in range(2):
        await tight_client.get("/openapi.json")
    response = await tight_client.get("/openapi.json")
    assert response.status_code == 429
    body = response.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["request_id"]
    assert response.headers["Retry-After"] == "60"


async def test_a_429_still_carries_cors_headers(tight_client: AsyncClient) -> None:
    # CORS is registered last in create_app specifically so it wraps the
    # rate limiter's early-return response too -- a browser needs
    # Access-Control-Allow-Origin on the 429 to even read its body.
    headers = {"Origin": "http://localhost:5173"}
    for _ in range(2):
        await tight_client.get("/openapi.json", headers=headers)
    response = await tight_client.get("/openapi.json", headers=headers)
    assert response.status_code == 429
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_different_callers_get_independent_budgets(tight_client: AsyncClient) -> None:
    for _ in range(2):
        await tight_client.get("/openapi.json", headers={"X-User-Id": "alice"})
    # alice is exhausted, but bob has not spent anything yet.
    response = await tight_client.get("/openapi.json", headers={"X-User-Id": "bob"})
    assert response.status_code == 200


async def test_health_endpoints_are_never_rate_limited(tight_client: AsyncClient) -> None:
    for _ in range(5):
        response = await tight_client.get("/health")
        assert response.status_code == 200


async def test_a_zero_limit_disables_the_middleware_entirely(
    unlimited_client: AsyncClient,
) -> None:
    for _ in range(10):
        response = await unlimited_client.get("/openapi.json")
        assert response.status_code == 200

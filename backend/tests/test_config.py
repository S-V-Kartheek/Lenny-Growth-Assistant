"""Configuration layer contract.

The model toggle is a stated requirement, so the settings object is treated as
production surface: it is tested, not assumed.
"""

from __future__ import annotations

import pytest

from app.config import Provider, Settings


def test_defaults_target_the_fully_local_demo() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider is Provider.OLLAMA
    assert settings.embedding_provider == "ollama"
    # A pinned corpus commit is what makes citations reproducible.
    assert len(settings.corpus_commit) == 40


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://a.com,http://b.com", ["http://a.com", "http://b.com"]),
        ("http://a.com , http://b.com ", ["http://a.com", "http://b.com"]),
        ("http://a.com", ["http://a.com"]),
        ("", []),
    ],
)
def test_list_settings_accept_comma_separated_env_values(raw: str, expected: list[str]) -> None:
    # Regression: pydantic-settings JSON-decodes list fields from .env before
    # validators run, so a plain "a,b" raised SettingsError at startup. The
    # fields are annotated NoDecode so the CSV validator sees the raw string.
    assert Settings(_env_file=None, cors_origins=raw).cors_origins == expected


def test_list_settings_still_accept_json() -> None:
    value = Settings(_env_file=None, cors_origins='["http://a.com"]').cors_origins
    assert value == ["http://a.com"]


def test_cors_never_defaults_to_wildcard() -> None:
    # The API carries session identity; "*" with credentials is a real hole.
    assert "*" not in Settings(_env_file=None).cors_origins


@pytest.mark.parametrize("field", ["anthropic_api_key", "openai_api_key"])
def test_blank_api_keys_are_normalised_to_none(field: str) -> None:
    # .env.example ships these empty; "" must mean "not configured", not a key.
    assert getattr(Settings(_env_file=None, **{field: "  "}), field) is None


def test_fallback_equal_to_primary_is_disabled() -> None:
    settings = Settings(
        _env_file=None, llm_provider="ollama", llm_fallback_provider="ollama"
    )
    assert settings.llm_fallback_provider is None


def test_blank_fallback_provider_is_none() -> None:
    assert Settings(_env_file=None, llm_fallback_provider="").llm_fallback_provider is None


def test_provider_lookups_stay_in_sync() -> None:
    settings = Settings(_env_file=None)
    for provider in Provider:
        assert settings.model_for(provider)
        assert settings.context_tokens_for(provider) > 0


def test_sync_database_url_drops_the_async_driver() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h:5432/d")
    assert settings.sync_database_url == "postgresql://u:p@h:5432/d"


@pytest.mark.parametrize(
    "raw",
    [
        "postgres://u:p@h:5432/d",
        "postgresql://u:p@h:5432/d",
    ],
)
def test_database_url_accepts_plain_postgres_schemes(raw: str) -> None:
    """Managed Postgres providers (Render, Railway, Supabase, ...) hand out
    postgres:// / postgresql:// connection strings; the app needs asyncpg."""
    settings = Settings(_env_file=None, database_url=raw)
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/d"


def test_database_url_leaves_asyncpg_scheme_untouched() -> None:
    settings = Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@h:5432/d")
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/d"

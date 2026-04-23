"""Centralised settings loaded from env via Pydantic.

Reads from the standard env (Replit injects DATABASE_URL etc. automatically).
The DATABASE_URL coming from Replit / standard Postgres clients uses the
``postgres://`` or ``postgresql://`` prefix; SQLAlchemy + asyncpg requires
``postgresql+asyncpg://``. We normalise here so callers never have to think
about it.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalise_db_url(raw: str) -> str:
    """Normalise a Postgres URL for asyncpg.

    Two transforms:
    1. Force the ``postgresql+asyncpg://`` driver prefix.
    2. Strip query parameters that libpq accepts but asyncpg does not
       (``sslmode``, ``channel_binding``). Replit's DATABASE_URL ships with
       ``?sslmode=require`` which would crash asyncpg; we drop it (asyncpg
       negotiates SSL on its own with hosted Postgres providers).
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    url = raw
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    parts = urlsplit(url)
    drop = {"sslmode", "channel_binding"}
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default_factory=lambda: _normalise_db_url(
            os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://uniportal:Bij%40y12345@localhost:5432/university_portal",
            )
        )
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-flash-latest")
    daily_gemini_budget_usd: float = Field(default=200.0)
    session_secret: str = Field(default="dev-only-change-me")
    cors_origins: list[str] = Field(
        default=[
            "http://159.65.152.72",
            "http://localhost:5173",
            "http://localhost:3000",
        ]
    )
    log_level: str = "INFO"
    debug: bool = False
    port: int = 8000

    # Scraping
    max_browser_concurrency: int = 5
    max_http_concurrency: int = 20
    per_uni_timeout_seconds: int = 1500

    # Auto-publish thresholds (Bug #6 — looser than Node defaults)
    min_completeness_for_auto_publish: int = 75
    rejection_block_days: int = 7  # Bug #7: was 30 in Node

    @field_validator("database_url")
    @classmethod
    def _force_async_driver(cls, v: str) -> str:
        return _normalise_db_url(v)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):  # type: ignore[no-untyped-def]
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

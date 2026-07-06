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
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


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
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default_factory=lambda: _normalise_db_url(
            os.environ.get(
                "DATABASE_URL",
                "postgresql+asyncpg://uniportal:Bij%40y12345@127.0.0.1:5432/university_portal",
            )
        )
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-2.5-flash-lite")
    daily_gemini_budget_usd: float = Field(default=100.0)
    openai_api_key: str = Field(default=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", ""))
    openai_base_url: str = Field(default=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL", ""))
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
    max_browser_concurrency: int = 10
    max_http_concurrency: int = 40
    per_uni_timeout_seconds: int = 1500

    # ── Task #229: large-catalogue scrape lifetime + contention bounding ──────
    # Celery soft/hard task time limits (seconds).  The old hardcoded 45-min
    # (2700s) soft ceiling killed otherwise-healthy 500+ course scrapes mid-run.
    # These are now a *safety* ceiling, deliberately generous; combine with the
    # resume checkpoint (below) so a catalogue that exceeds even this finishes
    # across re-runs without losing progress.  Env-overridable per deployment.
    scrape_task_soft_time_limit_s: int = 7200   # 2h
    scrape_task_hard_time_limit_s: int = 7500   # 2h05m (must be > soft)

    # Discovery-phase deadline (Ulster job_ec86dc5866cb, 2026-07-03): a
    # BFS/sitemap probe stuck in the httpx->curl_cffi->Wayback->Scrape.do
    # fallback chain held a worker claim indefinitely with no further log
    # output ("sitemap: probing 5 URL(s)" then silence). discover_course_links
    # (BFS + sitemap fallback + sitemap supplement) is wrapped in
    # asyncio.wait_for() at this deadline; on breach the job is marked failed
    # with a clear reason instead of blocking the queue for the full
    # scrape_task_soft_time_limit_s ceiling above.
    discovery_phase_timeout_s: int = 300   # 5 min

    # Per-page fetch cap inside the BFS discovery loop (Cardiff job_82781680a1e4,
    # 2026-07-06): fetch_html_scrape_do uses a 90s httpx timeout, and the
    # discovery.scrape_do_skip_fallbacks fast-path tries static-then-render
    # inside ONE fetch_html() call — up to 180s. discovery.py's BFS loop then
    # calls fetch_html() up to 2-3 times per candidate page (immediate retry +
    # bare-URL retry), so a single unresponsive page can burn 360-540s worst
    # case — more than the entire discovery_phase_timeout_s budget above — and
    # the BFS loop never advances to the next page. Wrapping each discovery-
    # level fetch_html() call in asyncio.wait_for() at this cap ensures one bad
    # page degrades to a skipped page (existing "fetch failed" handling)
    # instead of consuming the whole deadline and stalling the crawl outright.
    discovery_page_fetch_timeout_s: int = 45

    # Resume checkpoint: when True, a re-run of an interrupted large scrape skips
    # course URLs already staged for the university (instead of restarting from
    # course 0), and _clear_stale_dedup preserves an interrupted run's partial
    # progress rather than wiping it.  resume_window bounds how recent a prior
    # interrupted run must be for its rows to be treated as a resumable checkpoint.
    scrape_resume_enabled: bool = True
    scrape_resume_window_minutes: int = 180

    # Global cap on how many universities may scrape concurrently across ALL
    # Celery workers (Redis-coordinated).  0 = disabled (no global cap; existing
    # behaviour).  Bounds Scrape.do / Gemini contention at the job level.
    max_concurrent_scrapes: int = 0

    # Cross-process token-bucket rate limits (calls/sec, Redis-coordinated).
    # In-process semaphores (e.g. `max_scrape_do_concurrency`) cannot bound
    # contention across the 8 prefork Celery workers that share ONE Scrape.do
    # account and ONE Gemini quota — each worker process gets its OWN
    # semaphore instance, so real fleet-wide concurrency is up to
    # 8 * max_scrape_do_concurrency, not just max_scrape_do_concurrency.
    # QMUL job_4fb674e585b2 (2026-07-06): with this still at 0.0 (disabled),
    # 279/409 (~68%) courses were lost to fetch_failed under concurrent
    # cross-university Scrape.do load — far worse than the 11-28% documented
    # for the in-process-only mitigations above. Defaulting to a small
    # positive fleet-wide ceiling (still overridable via env var) actually
    # engages this pre-built Redis token bucket so bursts get smoothed across
    # every worker, not just within one. 3.0/sec is deliberately conservative
    # (fails open after a 30s wait budget, so it can only add latency, never
    # block scraping outright) — raise if Scrape.do's plan concurrency allows.
    scrape_do_rate_limit_per_sec: float = 3.0
    gemini_rate_limit_per_sec: float = 0.0

    # In-process hard cap on concurrent Scrape.do HTTP requests (per Celery
    # worker process).  QMUL job_8221ce960e02 (2026-07-03): with
    # _MAX_PARALLEL_FETCH=12 course-fetch tasks running concurrently and NO
    # concurrency limit on fetch_html_scrape_do() (unlike the plain-httpx path,
    # which has `_sem = asyncio.Semaphore(max_http_concurrency)`), up to 12+
    # simultaneous render=true requests hit the single shared Scrape.do account
    # at once. Scrape.do's plan-level concurrent-connection cap then rejects
    # the overflow (502/429), and because all 12 tasks retry on roughly the
    # same backoff schedule, the retry ALSO lands in a saturated window —
    # producing a burst of genuine fetch_failed results (116/409 = 28% in that
    # run) even though every individual URL fetches fine in isolation
    # (confirmed by hand). This semaphore bounds real concurrent Scrape.do
    # requests per worker process independently of _MAX_PARALLEL_FETCH, so a
    # course-fetch task queues for a slot instead of firing straight into a
    # saturated pool. Complements (does not replace) scrape_do_rate_limit_per_sec
    # — that token bucket smooths cross-worker call *rate*, this bounds
    # simultaneous in-flight *connections* from this process.
    max_scrape_do_concurrency: int = 5

    # Task #233: per-course Gemini primary-extraction timeout (seconds).
    # The full-extraction Gemini call is wrapped in asyncio.wait_for around the
    # SDK call (AFTER the rate-limiter token is acquired, so this measures only
    # genuine API/SDK slowness — not limiter backpressure).  A healthy
    # gemini-2.5-flash-lite call returns in well under 10s; the old hard-coded
    # 30s default meant every full-extraction course on a large catalogue paid
    # up to 30s for a guaranteed-empty result whenever Gemini was slow.  Repeated
    # timeouts now also trip the circuit breaker (see GeminiQuotaTracker.
    # record_timeout) so subsequent courses skip Gemini instantly.  Override via
    # the GEMINI_PRIMARY_TIMEOUT_S env var on prod.
    gemini_primary_timeout_s: float = 20.0

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

# ── Scrape / requeue constants accessible to both task and router layers ──

#: A queued job with no ``updated_at`` change for this many minutes is
#: considered stale and eligible for automatic re-dispatch by the beat task.
STALE_QUEUED_MINUTES: int = 5

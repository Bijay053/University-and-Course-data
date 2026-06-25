"""Cross-process token-bucket rate limiter (Task #229).

The scraper runs under 8 Celery prefork workers that share ONE Scrape.do account
and ONE Gemini quota.  Each worker has its own in-process ``asyncio.Semaphore``,
so the in-process limits multiply by the worker count and cannot bound the real
external contention — the result is 429 storms that stall large scrapes.

This module provides a tiny Redis-coordinated token bucket so the *fleet* of
workers shares a single rate budget per resource (e.g. ``scrape_do`` or
``gemini``).  It is:

* **Opt-in** — a non-positive ``rate_per_sec`` disables it entirely (the caller
  proceeds immediately, preserving existing behaviour).
* **Fail-open** — any Redis error logs a warning and lets the call through.  A
  Redis outage must never block scraping.
* **Best-effort** — this is a smoothing throttle, not a hard quota.  It uses a
  fixed-window counter (atomic INCR + EXPIRE) which is good enough to flatten
  bursts; it is intentionally simple and dependency-free.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from app.config import settings

log = logging.getLogger(__name__)

# How long to wait between retries when the current window is saturated.
_POLL_INTERVAL_S = 0.1
# Hard cap on how long a single acquire() may block before failing open, so a
# misconfigured (very low) rate can never wedge a worker forever.
_MAX_WAIT_S = 30.0


def _window_key(resource: str, window_start: int) -> str:
    return f"scrape:ratelimit:{resource}:{window_start}"


async def acquire(resource: str, rate_per_sec: float) -> bool:
    """Acquire one token for ``resource``, blocking until the rate allows it.

    Returns ``True`` when a token was granted (either because limiting is
    disabled, Redis is unavailable, or capacity was available within the wait
    budget).  Never raises.

    ``rate_per_sec`` is the steady-state ceiling for the whole worker fleet.  A
    value of e.g. ``5.0`` means at most ~5 calls/sec across every process.
    """
    if rate_per_sec is None or rate_per_sec <= 0:
        return True  # disabled — proceed immediately

    # Per-second fixed window; capacity is the integer rate (>=1).
    capacity = max(1, int(math.ceil(rate_per_sec)))

    try:
        import redis.asyncio as _aioredis

        client = _aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=3
        )
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("rate_limiter: Redis connect failed (fail-open): %s", exc)
        return True

    deadline = time.monotonic() + _MAX_WAIT_S
    try:
        while True:
            window_start = int(time.time())
            key = _window_key(resource, window_start)
            try:
                count = await client.incr(key)
                if count == 1:
                    # First hit in this window — set a short TTL so old windows
                    # self-clean without a sweeper.
                    await client.expire(key, 2)
            except Exception as exc:  # noqa: BLE001 — fail open mid-loop
                log.warning(
                    "rate_limiter[%s]: Redis INCR failed (fail-open): %s",
                    resource, exc,
                )
                return True

            if count <= capacity:
                return True

            # Window saturated — wait for the next window (or until the budget).
            if time.monotonic() >= deadline:
                log.warning(
                    "rate_limiter[%s]: wait budget exhausted (rate=%.2f/s) — "
                    "proceeding to avoid stalling the worker",
                    resource, rate_per_sec,
                )
                return True
            await asyncio.sleep(_POLL_INTERVAL_S)
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


async def acquire_scrape_do() -> bool:
    """Throttle a Scrape.do call to ``settings.scrape_do_rate_limit_per_sec``."""
    return await acquire("scrape_do", settings.scrape_do_rate_limit_per_sec)


async def acquire_gemini() -> bool:
    """Throttle a Gemini call to ``settings.gemini_rate_limit_per_sec``."""
    return await acquire("gemini", settings.gemini_rate_limit_per_sec)

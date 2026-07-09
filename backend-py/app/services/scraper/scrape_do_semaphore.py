"""Account-wide Scrape.do concurrency semaphore (fetch-layer brief, Part B).

The in-process ``asyncio.Semaphore`` in ``http_fetcher`` caps concurrent
Scrape.do requests *per Celery worker process* (default 5), but the fleet runs
8 prefork workers sharing ONE Scrape.do account — so the true account-level
concurrency can reach 40, far above the plan's concurrent-connection limit.
The overflow gets rejected (429/502) and surfaces as fetch_failed bursts.

This module coordinates a fleet-wide slot count in Redis so the *account's*
in-flight request number is bounded, regardless of worker count.

Design:

* **Opt-in** — ``settings.scrape_do_account_concurrency <= 0`` disables it
  entirely (callers proceed straight to the local semaphore, preserving
  existing behaviour).
* **Fail-open** — any Redis error logs a warning and lets the call through.
  A Redis outage must never block scraping; the in-process semaphore still
  bounds per-worker concurrency in that case.
* **Self-healing** — slots are members of a Redis sorted set scored by
  acquire-time.  A holder that dies without releasing (worker OOM/restart) is
  reaped by the next acquirer once its score is older than ``_HOLD_TTL_S``
  (180 s — comfortably above the 90 s httpx timeout on a Scrape.do render
  call), so leaked slots cannot permanently shrink capacity.
* **Gauge** — every successful acquire logs ``[SCRAPEDO GAUGE] in-flight N/cap``
  so operators can see real account-level concurrency in the worker logs.

Usage (mirrors the local semaphore's position INSIDE the retry loop — the
slot is held only for the duration of one HTTP attempt, released between
backoff sleeps so a waiting worker elsewhere can use it).  The LOCAL
per-process semaphore must be acquired FIRST so a coroutine never holds a
scarce fleet-wide slot while queueing behind its own process's semaphore:

    async with _scrape_do_sem:
        async with account_slot():
            ... one Scrape.do HTTP attempt ...
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
import weakref

from app.config import settings

log = logging.getLogger(__name__)

# One shared Redis client per event loop (redis.asyncio connections are bound
# to the loop that created them; Celery prefork tasks may each run their own
# asyncio.run() loop, so a plain module-global client would break the moment
# a second loop reuses a pooled connection from the first).  WeakKeyDictionary
# lets dead loops drop their client without bookkeeping.
_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _get_client():
    """Return the shared Redis client for the current event loop (create once)."""
    import redis.asyncio as _aioredis

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = _aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=3
        )
        _clients[loop] = client
    return client

# Sorted set of in-flight holder tokens, scored by acquire epoch-seconds.
_KEY = "scrapedo:account_inflight"
# Stale-holder reap threshold.  Must exceed the longest possible single HTTP
# attempt (90 s httpx timeout on render=true) with headroom.
_HOLD_TTL_S = 180.0
# Poll cadence while waiting for a free slot.
_POLL_INTERVAL_S = 0.25
# Hard cap on how long acquire() may block before failing open, so a
# misconfigured (too-low) cap can never wedge the fleet.
_MAX_WAIT_S = 120.0

# Atomic reap-count-add:  KEYS[1]=zset  ARGV = now, ttl, cap, token
# Returns the new in-flight count (>0) on success, or -(current count) when
# the account is saturated.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - tonumber(ARGV[2]))
local n = redis.call('ZCARD', KEYS[1])
if n < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]), ARGV[4])
  redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) * 2))
  return n + 1
end
return -n
"""


async def acquire_slot() -> str | None:
    """Acquire one account-wide Scrape.do slot.

    Returns the holder token (pass it to :func:`release_slot`) or ``None``
    when no Redis slot is held — either because the feature is disabled or
    because Redis failed / the wait budget ran out (fail-open).  Never raises.
    """
    cap = settings.scrape_do_account_concurrency
    if cap is None or cap <= 0:
        return None  # disabled — local semaphore only

    try:
        client = _get_client()
    except Exception as exc:  # noqa: BLE001 — fail open
        log.warning("scrape_do_semaphore: Redis connect failed (fail-open): %s", exc)
        return None

    token = uuid.uuid4().hex
    deadline = time.monotonic() + _MAX_WAIT_S
    while True:
        try:
            result = await client.eval(
                _ACQUIRE_LUA, 1, _KEY,
                time.time(), _HOLD_TTL_S, cap, token,
            )
        except Exception as exc:  # noqa: BLE001 — fail open mid-loop
            log.warning(
                "scrape_do_semaphore: Redis EVAL failed (fail-open): %s", exc
            )
            return None

        n = int(result)
        if n > 0:
            log.info("[SCRAPEDO GAUGE] in-flight %d/%d", n, cap)
            return token

        if time.monotonic() >= deadline:
            log.warning(
                "scrape_do_semaphore: wait budget exhausted (in-flight=%d, "
                "cap=%d) — proceeding without account slot to avoid "
                "stalling the worker",
                -n, cap,
            )
            return None
        await asyncio.sleep(_POLL_INTERVAL_S)


async def release_slot(token: str | None) -> None:
    """Release a slot acquired by :func:`acquire_slot`.  Never raises."""
    if not token:
        return
    try:
        await _get_client().zrem(_KEY, token)
    except Exception as exc:  # noqa: BLE001 — a leaked slot self-heals via TTL reap
        log.warning(
            "scrape_do_semaphore: release failed (slot will self-heal via "
            "%.0fs TTL reap): %s", _HOLD_TTL_S, exc,
        )


@contextlib.asynccontextmanager
async def account_slot():
    """Async context manager wrapping acquire/release of one account slot."""
    token = await acquire_slot()
    try:
        yield
    finally:
        await release_slot(token)

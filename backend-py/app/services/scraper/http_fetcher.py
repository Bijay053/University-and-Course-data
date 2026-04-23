"""Plain HTTP fetching with concurrency limiting + small retry loop.

Used by extractors when JS rendering isn't required (most fee/intake pages).
Falls back to ``BrowserPool`` for SPAs.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx

from app.config import settings

log = logging.getLogger(__name__)
_sem = asyncio.Semaphore(settings.max_http_concurrency)


@asynccontextmanager
async def _client():
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Linux; UniportalBot/1.0; +https://example.com) "
                "AppleWebKit/537.36 Chrome/124"
            )
        },
    ) as c:
        yield c


async def fetch_html(url: str, *, retries: int = 2) -> str | None:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        async with _sem:
            try:
                async with _client() as c:
                    r = await c.get(url)
                    if r.status_code == 200:
                        return r.text
                    log.warning("fetch %s -> %s", url, r.status_code)
            except Exception as exc:
                last_exc = exc
                log.warning("fetch %s attempt %s failed: %s", url, attempt, exc)
        await asyncio.sleep(1.5 * (attempt + 1))
    if last_exc:
        log.error("fetch %s exhausted retries: %s", url, last_exc)
    return None

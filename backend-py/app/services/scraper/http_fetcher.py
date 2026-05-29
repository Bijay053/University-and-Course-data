"""Plain HTTP fetching with concurrency limiting + small retry loop.

Used by extractors when JS rendering isn't required (most fee/intake pages).
Falls back to ``BrowserPool`` for SPAs.

Cloudflare bypass (free, no API key):
--------------------------------------
When httpx receives HTTP 403 with Cloudflare signatures (``cf-mitigated``
header or ``server: cloudflare``), we automatically retry the request using
``curl_cffi`` with Chrome TLS impersonation.  Cloudflare blocks scrapers at
the TLS handshake level — Python's standard SSL library emits a different JA3
fingerprint than a real Chrome browser.  ``curl_cffi`` patches libcurl to send
the exact TLS ClientHello Chrome 124 uses, which passes the fingerprint check
without spawning any browser process.  Cost: zero.  Overhead: ~50-200 ms per
page (vs 3-5 s for Xvfb/patchright).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx

from app.config import settings
from app.services.scraper.extractors.curtin_session import cookies_for_url

log = logging.getLogger(__name__)
_sem = asyncio.Semaphore(settings.max_http_concurrency)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_cloudflare_block(resp: httpx.Response) -> bool:
    """Return True when the response looks like a Cloudflare WAF block."""
    if resp.status_code not in (403, 429, 503):
        return False
    server = resp.headers.get("server", "").lower()
    if "cloudflare" in server:
        return True
    if resp.headers.get("cf-mitigated"):
        return True
    if resp.headers.get("cf-ray"):
        return True
    return False


async def fetch_html_cffi(url: str) -> str | None:
    """Fetch using curl_cffi Chrome TLS impersonation (Cloudflare bypass).

    Impersonates the exact TLS ClientHello + HTTP/2 SETTINGS frames of a real
    Chrome 124 browser.  Cloudflare's JA3/JA4 fingerprint check passes because
    the handshake is byte-identical to a genuine browser — no browser process
    or proxy required.

    Returns the response text on HTTP 200, None otherwise.
    """
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore
    except ImportError:
        log.debug("curl_cffi not installed — skipping TLS-impersonation fallback")
        return None

    try:
        async with AsyncSession(impersonate="chrome124") as s:
            r = await s.get(
                url,
                timeout=30,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-AU,en;q=0.9",
                    "User-Agent": _BROWSER_UA,
                },
            )
            if r.status_code == 200:
                log.info("cffi fetch %s -> 200 (Cloudflare bypassed)", url)
                return r.text
            log.warning("cffi fetch %s -> %s", url, r.status_code)
            return None
    except Exception as exc:
        log.warning("cffi fetch %s failed: %s", url, exc)
        return None


@asynccontextmanager
async def _client():
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            # Many university sites refuse anything that looks like a bot. We
            # use a real browser UA and accept-headers so plain HTML pages load.
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as c:
        yield c


async def fetch_html(url: str, *, retries: int = 2) -> str | None:
    last_exc: Exception | None = None
    got_cloudflare_block = False
    for attempt in range(retries + 1):
        async with _sem:
            try:
                async with _client() as c:
                    # Per-host session priming (currently only Curtin —
                    # see extractors/curtin_session.py for rationale).
                    # Returns {} for every other host so this is a true
                    # no-op for ~100 universities in the fleet.
                    r = await c.get(url, cookies=cookies_for_url(url))
                    if r.status_code == 200:
                        return r.text
                    if _is_cloudflare_block(r):
                        got_cloudflare_block = True
                        log.info(
                            "fetch %s -> %s (Cloudflare WAF) — will retry with TLS impersonation",
                            url, r.status_code,
                        )
                        break  # no point retrying with plain httpx; go straight to cffi
                    log.warning("fetch %s -> %s", url, r.status_code)
            except Exception as exc:
                last_exc = exc
                log.warning("fetch %s attempt %s failed: %s", url, attempt, exc)
        await asyncio.sleep(1.5 * (attempt + 1))

    if got_cloudflare_block:
        return await fetch_html_cffi(url)

    if last_exc:
        log.error("fetch %s exhausted retries: %s", url, last_exc)
    return None

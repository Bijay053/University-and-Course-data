"""Plain HTTP fetching with concurrency limiting + small retry loop.

Used by extractors when JS rendering isn't required (most fee/intake pages).
Falls back to ``BrowserPool`` for SPAs.

Cloudflare bypass — four-tier fallback (cheapest first):
---------------------------------------------------------
1. ``httpx`` with browser UA/Accept headers.  Works for most sites.
2. ``curl_cffi`` Chrome TLS impersonation.  Cloudflare blocks scrapers at
   the TLS handshake level — Python's standard SSL library emits a different
   JA3 fingerprint than a real Chrome browser.  ``curl_cffi`` patches libcurl
   to send the exact TLS ClientHello Chrome 124 uses, which passes the
   fingerprint check without spawning any browser process.  Cost: zero.
   Overhead: ~50-200 ms per page.  Handles TLS-fingerprint-only CF protection.
3. ``fetch_html_scrape_do`` — scrape.do residential proxy.  When both httpx
   and curl_cffi are blocked (IP/ASN-level Cloudflare Enterprise), scrape.do
   routes the request through a residential IP that CF does not block.  Costs
   API credits (SCRAPE_DO_TOKEN required).  Enabled ONLY for universities that
   set ``discovery.scrape_do_fallback: true`` in their YAML.  Never called
   fleet-wide.  Overhead: ~1-3 s per page.
4. ``fetch_html_wayback`` — Wayback Machine archived HTML (free, zero API
   cost).  Some sites block all datacenter IPs at the IP/ASN level.  For
   these, we fall back to the Internet Archive's CDX API to find the most
   recent snapshot of the URL, then fetch the raw archived HTML via the ``id_``
   Wayback modifier.  The archived data may be weeks/months old, but for
   stable course-catalogue content this is usually acceptable.
   Cost: zero.  Overhead: two HTTP calls to archive.org (~1-3 s).
"""
from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from contextlib import asynccontextmanager

import httpx

from app.config import settings
from app.services.scraper.extractors.curtin_session import cookies_for_url

log = logging.getLogger(__name__)
_sem = asyncio.Semaphore(settings.max_http_concurrency)

# Per-job scrape.do opt-in flag.  Set to True by set_scrape_do_fallback()
# when the current university has ``discovery.scrape_do_fallback: true`` in
# its YAML.  Cleared after every scrape job by clear_scrape_do_fallback() so
# it never bleeds into the next job on the same worker process.
_scrape_do_enabled: bool = False


def set_scrape_do_fallback(enabled: bool) -> None:
    """Enable or disable the scrape.do fallback tier for the current job.

    Call once after loading per-uni config.  Always pair with a corresponding
    ``clear_scrape_do_fallback()`` call in the job's finally block.
    """
    global _scrape_do_enabled
    _scrape_do_enabled = enabled


def clear_scrape_do_fallback() -> None:
    """Reset the scrape.do flag after a scrape job finishes."""
    global _scrape_do_enabled
    _scrape_do_enabled = False


# Per-job Wayback timestamp cache: normalised-url → CDX timestamp string.
# Populated by wayback_discover() during the discovery phase so that
# fetch_html_wayback() can use the exact timestamp from the CDX index
# instead of re-querying the Availability API (which is inconsistent —
# it returns the "closest to now" snapshot, which may be a 404/301,
# even when the CDX has a valid 200 snapshot at an older timestamp).
_wayback_ts_cache: dict[str, str] = {}


def set_wayback_timestamps(url_timestamps: dict[str, str]) -> None:
    """Register Wayback CDX timestamps found during discovery.

    Called by ``wayback_discover()`` once per scrape job.  The mapping
    is *url → timestamp* where *url* is already in normalised
    ``https://host/path`` form (as returned by ``_normalise_wayback_url``)
    and *timestamp* is the 14-digit CDX timestamp string (e.g.
    ``"20251207034443"``).
    """
    _wayback_ts_cache.update(url_timestamps)


def clear_wayback_timestamps() -> None:
    """Discard the per-job timestamp cache after the scrape finishes."""
    _wayback_ts_cache.clear()

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


async def fetch_html_scrape_do(url: str) -> str | None:
    """Fetch via scrape.do residential proxy (paid, opt-in per-uni fallback).

    Called ONLY when:
      1. The current university has ``discovery.scrape_do_fallback: true``.
      2. Both httpx and curl_cffi were blocked (Cloudflare WAF / IP block).

    The scrape.do API accepts a plain GET:
      https://api.scrape.do?token=TOKEN&url=ENCODED_URL&render=false

    ``render=false`` (default) requests static HTML — cheaper than JS
    rendering and sufficient for all server-rendered university pages.  Do
    NOT set ``render=true`` here; that is 5× more expensive and should only
    be used if the caller explicitly needs JS execution (use the browser pool
    instead for those cases).

    Cost accounting: every call consumes at least one scrape.do credit.
    Operators can monitor spend in the scrape.do dashboard.

    Returns the response text on HTTP 200, None on any failure.
    """
    token = os.environ.get("SCRAPE_DO_TOKEN", "")
    if not token:
        log.warning("scrape.do fallback requested but SCRAPE_DO_TOKEN is not set — skipping")
        return None

    endpoint = "https://api.scrape.do"
    params = {
        "token": token,
        "url": url,
        "render": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(endpoint, params=params)
            if r.status_code == 200:
                log.info(
                    "scrape.do fetch %s -> 200 (%d chars, 1 credit consumed)",
                    url, len(r.text),
                )
                return r.text
            log.warning("scrape.do fetch %s -> %s", url, r.status_code)
            return None
    except Exception as exc:
        log.warning("scrape.do fetch %s failed: %s", url, exc)
        return None


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


async def fetch_html_wayback(url: str) -> str | None:
    """Fetch archived HTML from Wayback Machine (last-resort for IP-blocked sites).

    Two-step process:
    1.  Wayback Availability API — a single fast call to
        ``https://archive.org/wayback/available?url=<url>`` returns the
        closest archived snapshot URL and timestamp.  Much faster than the
        CDX search API (~0.5 s vs 5-20 s under CDX load).
    2.  Raw HTML fetch — retrieve the archived page via the ``id_`` Wayback
        modifier (inserted before the original URL in the snapshot URL), which
        strips the IA toolbar and returns clean HTML identical to what the
        original server returned.

    Suitable for sites like Notre Dame Australia where Cloudflare Enterprise
    Bot Management blocks all datacenter IPs at the IP/ASN level — httpx and
    curl_cffi both receive HTTP 403 regardless of TLS fingerprint.  archive.org
    is not behind the university's Cloudflare zone so we can always reach it.

    The archived HTML may be weeks-to-months old.  For stable course-catalogue
    content (degree names, fee tables, English requirements) this is usually
    acceptable and far better than returning nothing.  The extraction pipeline
    records ``extraction_method`` so operators can see which courses were
    sourced from Wayback vs the live site.

    Returns response text on success, ``None`` on any failure (so the caller
    can fall through to the browser pool).
    """
    # Fast path: use the CDX timestamp cached by wayback_discover() during
    # the discovery phase.  This avoids the Availability API entirely, which
    # is inconsistent — it returns the "closest to NOW" snapshot and often
    # resolves to a 404 or 301 for URLs whose last 200 snapshot is years old,
    # even though the CDX index has a perfectly valid 200 snapshot at that
    # earlier timestamp.  The cache is populated with the exact timestamps
    # that the CDX wildcard query returned, so they are guaranteed to be
    # real 200 snapshots.
    cached_ts = _wayback_ts_cache.get(url)
    if cached_ts:
        raw_url = f"https://web.archive.org/web/{cached_ts}id_/{url}"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                r = await c.get(raw_url, headers={"User-Agent": _BROWSER_UA})
                if r.status_code == 200:
                    log.info(
                        "wayback fetch %s -> 200 (CDX-cached snapshot %s, %d chars)",
                        url, cached_ts, len(r.text),
                    )
                    return r.text
                log.warning(
                    "wayback fetch %s -> %s (CDX-cached snapshot %s) — "
                    "falling through to Availability API",
                    url, r.status_code, cached_ts,
                )
        except Exception as exc:
            log.warning(
                "wayback fetch %s (CDX-cached snapshot %s) failed: %s — "
                "falling through to Availability API",
                url, cached_ts, exc,
            )

    # Slow path: Availability API lookup for URLs not in the CDX cache
    # (e.g. PDF central pages, fee pages, or non-Wayback-discovered URLs).
    _AVAIL_ENDPOINT = "https://archive.org/wayback/available"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            avail_r = await c.get(
                _AVAIL_ENDPOINT,
                params={
                    "url": url,
                    # Request a snapshot close to late 2025 — well before the
                    # usual CF Enterprise Bot Management activation window and
                    # likely to match snapshots in the CDX index.
                    "timestamp": "20251201000000",
                },
            )
            if avail_r.status_code != 200:
                log.info(
                    "wayback fetch: Availability API returned %s for %s",
                    avail_r.status_code, url,
                )
                return None
            data = avail_r.json()
            closest = data.get("archived_snapshots", {}).get("closest", {})
            if not closest or not closest.get("available"):
                log.info("wayback fetch: no archived snapshot found for %s", url)
                return None
            snapshot_url: str = closest["url"]
            timestamp: str = closest.get("timestamp", "?")
    except Exception as exc:
        log.warning("wayback fetch: Availability API failed for %s: %s", url, exc)
        return None

    raw_url = snapshot_url.replace(f"/web/{timestamp}/", f"/web/{timestamp}id_/", 1)
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(raw_url, headers={"User-Agent": _BROWSER_UA})
            if r.status_code == 200:
                log.info(
                    "wayback fetch %s -> 200 (Availability snapshot %s, %d chars)",
                    url, timestamp, len(r.text),
                )
                return r.text
            log.warning(
                "wayback fetch %s -> %s (Availability snapshot %s)",
                url, r.status_code, timestamp,
            )
            return None
    except Exception as exc:
        log.warning("wayback fetch %s failed: %s", url, exc)
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
        cffi_result = await fetch_html_cffi(url)
        if cffi_result is not None:
            return cffi_result
        # curl_cffi also blocked (IP/ASN-level block — TLS fingerprint alone
        # is not the issue).
        if _scrape_do_enabled:
            log.info(
                "fetch %s: curl_cffi blocked — trying scrape.do residential proxy",
                url,
            )
            scrape_do_result = await fetch_html_scrape_do(url)
            if scrape_do_result is not None:
                return scrape_do_result
            log.info(
                "fetch %s: scrape.do also failed — falling back to Wayback Machine",
                url,
            )
        else:
            log.info(
                "fetch %s: curl_cffi blocked — trying Wayback Machine archived HTML "
                "(scrape.do not enabled for this university)",
                url,
            )
        return await fetch_html_wayback(url)

    if last_exc:
        log.error("fetch %s exhausted retries: %s", url, last_exc)
    return None

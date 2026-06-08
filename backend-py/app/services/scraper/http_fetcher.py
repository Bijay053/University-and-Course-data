"""Plain HTTP fetching with concurrency limiting + small retry loop.

Used by extractors when JS rendering isn't required (most fee/intake pages).
Falls back to ``BrowserPool`` for SPAs.

Cloudflare bypass — three-tier fallback (cheapest first):
---------------------------------------------------------
1. ``httpx`` with browser UA/Accept headers.  Works for most sites.
2. ``curl_cffi`` Chrome TLS impersonation.  Cloudflare blocks scrapers at
   the TLS handshake level — Python's standard SSL library emits a different
   JA3 fingerprint than a real Chrome browser.  ``curl_cffi`` patches libcurl
   to send the exact TLS ClientHello Chrome 124 uses, which passes the
   fingerprint check without spawning any browser process.  Cost: zero.
   Overhead: ~50-200 ms per page.  Handles TLS-fingerprint-only CF protection.
3. ``fetch_html_wayback`` — Wayback Machine archived HTML (free, zero API
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
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import httpx

from app.config import settings
from app.services.scraper.extractors.curtin_session import cookies_for_url

log = logging.getLogger(__name__)
_sem = asyncio.Semaphore(settings.max_http_concurrency)

# ---------------------------------------------------------------------------
# Scrape.do render gate — extraction-only ContextVar
# ---------------------------------------------------------------------------
# `_scrape_do_render_active` is False by default so that discovery, sitemap,
# central-page, and PDF-link fetches NEVER trigger Scrape.do render (which
# costs ~$0.006/call).  It is set True only inside `scrape_do_render_scope()`,
# which `single_course.extract_course()` enters for each per-course HTTP
# fetch when the university's YAML has `extraction.scrape_do_render: true`.
#
# Scrape.do *static* (CF-blocked tier 4) is NOT gated here — it still fires
# for any CF-blocked fetch so that CF-protected discovery pages can be reached.
# Only the httpx-200 → render upgrade and the CF tier-5 render fallback are
# gated behind this var.
_scrape_do_render_active: ContextVar[bool] = ContextVar(
    "scrape_do_render_active", default=False
)

# Per-process running counter — logged at INFO level on each scrape job so
# operators can see how many paid render calls were consumed.
_scrape_do_render_call_count: int = 0

# Per-job mutable counter dict: {"render": N, "static": N}.
# Set to a fresh dict at the start of each run_scrape() call via
# scrape_do_counter_scope().  All coroutines that share the same asyncio
# context reference the SAME dict object, so increments are visible across
# gather() boundaries.  None (default) means "no job scope active".
_scrape_do_job_counters: ContextVar[dict | None] = ContextVar(
    "_scrape_do_job_counters", default=None
)


@contextmanager
def scrape_do_counter_scope() -> "Generator[dict, None, None]":
    """Context manager: start a fresh Scrape.do call counter for one job.

    Usage (orchestrator.py)::

        from app.services.scraper.http_fetcher import scrape_do_counter_scope

        with scrape_do_counter_scope() as sd_counters:
            ...  # run the whole scrape job
        render_calls = sd_counters["render"]
        static_calls = sd_counters["static"]
    """
    counters: dict = {"render": 0, "static": 0}
    token = _scrape_do_job_counters.set(counters)
    try:
        yield counters
    finally:
        _scrape_do_job_counters.reset(token)


@contextmanager
def scrape_do_render_scope():
    """Context manager: activate Scrape.do render for the enclosed fetch.

    Usage (single_course.py)::

        from app.services.scraper.http_fetcher import scrape_do_render_scope

        with scrape_do_render_scope():
            html = await fetch_html(url)

    Outside this scope every ``fetch_html()`` call behaves as if
    ``scrape_do_render`` were False — no paid render calls are made during
    discovery, sitemap crawling, central-page fetching, or any other phase
    that does not need JS-rendered fee data.
    """
    token = _scrape_do_render_active.set(True)
    try:
        yield
    finally:
        _scrape_do_render_active.reset(token)

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


def _unescape_json_html(html: str) -> str:
    """Un-escape JSON Unicode sequences (\\uXXXX → char) embedded in HTML.

    Contensis CMS (Canterbury CC) and other Next.js / React-hydration sites
    embed the server-rendered content as a JSON string inside a <script> tag
    (e.g. ``__NEXT_DATA__`` or ``window.__data``).  The JSON encoder escapes
    ``<`` → ``\\u003C``, ``/`` → ``\\u002F``, ``"`` → ``\\"``, etc., making
    the fee table body invisible to simple ``£`` / ``&pound;`` regex extractors.

    Decoding the ``\\uXXXX`` sequences converts them back to their Unicode
    characters so downstream extractors can parse the HTML normally.

    Safe to apply globally: ASCII ``\\uXXXX`` sequences (\\u0020–\\u007E) map
    back to themselves; the only risk is sequences in JavaScript string
    literals that are *not* HTML content, but BeautifulSoup handles those
    gracefully because it never executes JS.

    Applied only when at least one ``\\u`` sequence is present to avoid any
    overhead on plain static HTML pages.
    """
    import re as _re
    if "\\u" not in html:
        return html
    try:
        return _re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            html,
        )
    except Exception:
        return html


def _is_spa_shell(html: str) -> bool:
    """Return True when HTML looks like an unrendered React/SPA shell.

    A SPA shell is a 200 response that contains only JavaScript bundles
    and CSS — no visible text content.  Universities like Canterbury CC
    return this when Cloudflare lets the request through but the page
    data is injected by JS at runtime.

    Heuristic: strip tags, collapse whitespace, check visible text length.
    If the ratio of visible text to total HTML size is < 3 % AND the
    total visible text is less than 800 characters the page has no useful
    extractable content.
    """
    import re as _re
    text = _re.sub(r"<[^>]+>", "", html)
    text = _re.sub(r"\s+", " ", text).strip()
    if len(text) >= 800:
        return False
    if len(html) > 5_000 and len(text) / len(html) < 0.03:
        return True
    return len(text) < 200


async def fetch_html_scrape_do(
    url: str,
    *,
    render: bool = False,
    wait_for_ms: int = 3000,
) -> str | None:
    """Fetch via Scrape.do residential proxy — paid tier-4/5 Cloudflare bypass.

    Scrape.do routes requests through a pool of residential IPs and can
    optionally execute the page in a headless Chrome instance (render=True).
    Use for sites where Cloudflare Enterprise blocks all datacenter IPs
    (httpx, curl_cffi, Wayback Machine CDN all return CF challenges) OR
    where fee / course data is injected by JavaScript at runtime.

    When render=True, ``waitFor=wait_for_ms`` is sent so Scrape.do waits for
    JavaScript hydration to complete before returning the HTML.  Canterbury's
    Contensis CMS injects the fee table via React hydration; the default 3 s
    wait is sufficient.

    The returned HTML is post-processed by ``_unescape_json_html`` to decode
    ``\\uXXXX`` sequences that Next.js / Contensis embed as JSON-encoded HTML
    inside their hydration payloads (e.g. ``\\u003Ctd\\u003E&pound;9,790``
    → ``<td>&pound;9,790``).  Without this step, fee/IELTS regexes see no
    ``£`` or ``&pound;`` patterns in the text.

    Requires SCRAPE_DO_TOKEN environment variable (set as a Replit secret).
    Returns None if the token is absent, the request fails, or the
    response is suspiciously short (likely an error page from scrape.do).
    """
    token = os.environ.get("SCRAPE_DO_TOKEN")
    if not token:
        log.debug("fetch_html_scrape_do: SCRAPE_DO_TOKEN not set — skipping")
        return None
    try:
        params: dict[str, str] = {"token": token, "url": url}
        if render:
            params["render"] = "true"
            params["waitFor"] = str(wait_for_ms)
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as c:
            r = await c.get("https://api.scrape.do", params=params)
            if r.status_code == 200 and len(r.text) > 500:
                log.info(
                    "scrape.do fetch %s render=%s -> 200 (%d chars)",
                    url,
                    render,
                    len(r.text),
                )
                # Per-job call counter (shared mutable dict via ContextVar).
                _sd_ctrs = _scrape_do_job_counters.get()
                if _sd_ctrs is not None:
                    if render:
                        _sd_ctrs["render"] += 1
                    else:
                        _sd_ctrs["static"] += 1
                return _unescape_json_html(r.text)
            log.warning(
                "scrape.do fetch %s render=%s -> %s (%d chars)",
                url,
                render,
                r.status_code,
                len(r.text),
            )
            return None
    except Exception as exc:
        log.warning("scrape.do fetch %s render=%s failed: %s", url, render, exc)
        return None


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
    # Scrape.do render is only active inside scrape_do_render_scope() —
    # i.e. during per-course extraction in single_course.extract_course().
    # It is NEVER True during discovery / sitemap / central-page phases.
    _scrape_do_render = _scrape_do_render_active.get()
    _has_scrape_do = bool(os.environ.get("SCRAPE_DO_TOKEN"))

    last_exc: Exception | None = None
    got_cloudflare_block = False
    got_hard_403 = False
    html_200: str | None = None  # track 200 result so we can check for SPA shell

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
                        html_200 = r.text
                        break  # exit loop; post-loop logic decides what to return
                    if _is_cloudflare_block(r):
                        got_cloudflare_block = True
                        log.info(
                            "fetch %s -> %s (Cloudflare WAF) — will retry with TLS impersonation",
                            url, r.status_code,
                        )
                        break  # no point retrying with plain httpx; go straight to cffi
                    if r.status_code == 403:
                        # Hard 403 from the origin server (not Cloudflare).
                        # The server explicitly refused our request — retrying
                        # will not help and wastes ~15-20s per blocked course.
                        # cffi / Wayback are also skipped: if the server rejects
                        # on HTTP level, the archived copy may not exist or may
                        # also be gated. Return None immediately.
                        got_hard_403 = True
                        log.warning("fetch %s -> 403 (hard block) — skipping retries", url)
                        break
                    log.warning("fetch %s -> %s", url, r.status_code)
            except Exception as exc:
                last_exc = exc
                log.warning("fetch %s attempt %s failed: %s", url, attempt, exc)
        await asyncio.sleep(1.5 * (attempt + 1))

    if got_hard_403:
        # Server explicitly rejected the request — no point trying cffi or
        # Wayback Machine. Return None so the pipeline records a fetch_failed.
        return None

    # ── Got HTTP 200 ──────────────────────────────────────────────────────────
    if html_200 is not None:
        # When scrape_do_render is explicitly set for this university, ALWAYS
        # upgrade to Scrape.do headless Chrome rendering even when httpx returned
        # a 200 with substantial content.  Canterbury CC is the canonical case:
        # httpx gets a 200 with 2.6MB of pre-rendered Contensis HTML, but the
        # fee tables are injected at runtime by JavaScript (not in the static
        # HTML).  Scrape.do render=True executes JS and returns the full page
        # including per-course fee rows (UK £9,790 / Overseas £17,000).
        # We always render — no SPA-shell heuristic needed — because the flag
        # is explicitly configured per-university in the YAML.
        if _scrape_do_render:
            log.info(
                "fetch %s -> 200 (scrape_do_render=True) — upgrading to Scrape.do headless render",
                url,
            )
            rendered = await fetch_html_scrape_do(url, render=True)
            if rendered is not None:
                return rendered
            log.info(
                "fetch %s: Scrape.do render failed — falling back to plain httpx 200 response",
                url,
            )
        return html_200

    # ── Cloudflare WAF block — tiered fallback ────────────────────────────────
    if got_cloudflare_block:
        # Tier 2: curl_cffi Chrome TLS impersonation
        cffi_result = await fetch_html_cffi(url)
        if cffi_result is not None:
            return cffi_result
        # Tier 3: Wayback Machine archived HTML (free, zero API cost)
        log.info(
            "fetch %s: curl_cffi blocked — trying Wayback Machine archived HTML",
            url,
        )
        wayback_result = await fetch_html_wayback(url)
        if wayback_result is not None:
            return wayback_result
        # Tier 4: Scrape.do static (residential proxy, no JS rendering)
        if _has_scrape_do:
            log.info(
                "fetch %s: Wayback Machine empty — trying Scrape.do static",
                url,
            )
            scrape_do_static = await fetch_html_scrape_do(url, render=False)
            if scrape_do_static is not None and not _is_spa_shell(scrape_do_static):
                return scrape_do_static
            # Tier 5: Scrape.do render (paid headless Chrome — most expensive)
            if _scrape_do_render:
                log.info(
                    "fetch %s: Scrape.do static empty/SPA — trying Scrape.do render",
                    url,
                )
                scrape_do_rendered = await fetch_html_scrape_do(url, render=True)
                if scrape_do_rendered is not None:
                    return scrape_do_rendered
        log.info(
            "fetch %s: all tiers exhausted (httpx, curl_cffi, Wayback, Scrape.do)",
            url,
        )
        return None

    if last_exc:
        log.error("fetch %s exhausted retries: %s", url, last_exc)
    return None

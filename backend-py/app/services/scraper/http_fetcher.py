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
import json
import logging
import os
import re
import time
import urllib.parse
import weakref
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import httpx

from app.config import settings
from app.services.scraper.challenge_shell import is_challenge_shell
from app.services.scraper.course_deadline import clamp_timeout, has_budget, remaining_seconds
from app.services.scraper.extractors.curtin_session import cookies_for_url
from app.services.scraper.url_identity import canonical_course_url_key

log = logging.getLogger(__name__)


_SCRAPE_DO_TOKEN_QUERY_RE = re.compile(
    r"(?i)([?&]token=)[^&#\s'\"<>]+"
)
_REDACTED_TOKEN = "[REDACTED]"


def _redact_scrape_do_log_text(value: object, *, token: str | None = None) -> str:
    """Remove Scrape.do credentials from text destined for worker logs.

    HTTPX includes the complete request URL in its automatic request log, and
    provider exceptions can include that URL too.  Keep every other diagnostic
    (including the encoded target URL) so operators can still diagnose the
    failing fetch without gaining access to a reusable provider credential.
    """
    text = str(value)
    if token:
        text = text.replace(token, _REDACTED_TOKEN)
    return _SCRAPE_DO_TOKEN_QUERY_RE.sub(rf"\1{_REDACTED_TOKEN}", text)


class _RedactScrapeDoHttpxLogFilter(logging.Filter):
    """Sanitize HTTPX's automatic request line before it reaches a handler."""

    _scrape_do_token_redactor = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = _redact_scrape_do_log_text(message)
            if redacted != message:
                # Rendering first, then clearing args, supports HTTPX's
                # ``logger.info(..., request.url, ...)`` structured message.
                record.msg = redacted
                record.args = ()
        except Exception:  # pragma: no cover - logging must never break fetches
            pass
        return True


def _install_scrape_do_httpx_log_filter() -> None:
    """Install the provider-credential filter once on HTTPX's own logger."""
    httpx_log = logging.getLogger("httpx")
    if not any(
        getattr(existing, "_scrape_do_token_redactor", False)
        for existing in httpx_log.filters
    ):
        httpx_log.addFilter(_RedactScrapeDoHttpxLogFilter())


_install_scrape_do_httpx_log_filter()


class ScrapedoAccountError(RuntimeError):
    """Raised when Scrape.do returns 401 or 403 — auth token invalid or credits exhausted.

    Propagates out of fetch_html_scrape_do so the orchestrator can abort the job
    immediately rather than accumulating hundreds of silent None returns.
    """


# ---------------------------------------------------------------------------
# A3 — last-fetch-error registry
# ---------------------------------------------------------------------------
# fetch_html / fetch_html_scrape_do return ``str | None`` at ~40 call sites, so
# failures can't carry status codes in the return value without touching every
# caller.  Instead, the *final-failure* point of each fetcher records the last
# known error (HTTP status, tier that failed, response snippet) here, keyed by
# URL.  Callers that log user-facing failure lines (e.g. discovery's
# "[DISCOVER] ERROR: fetch failed") consult ``get_last_fetch_error(url)`` to
# include a real diagnosis instead of "check site connectivity".
# Bounded FIFO dict — per-process, same event loop as the callers that read it.
_MAX_FETCH_ERRORS = 512
_last_fetch_errors: dict[str, dict] = {}


def _record_fetch_error(
    url: str,
    *,
    status: int | None = None,
    tier: str = "",
    detail: str = "",
) -> None:
    """Record the final failure for *url* (bounded, oldest-first eviction)."""
    while len(_last_fetch_errors) >= _MAX_FETCH_ERRORS:
        try:
            _last_fetch_errors.pop(next(iter(_last_fetch_errors)))
        except (StopIteration, KeyError):  # pragma: no cover — race-free in asyncio
            break
    _last_fetch_errors[url] = {
        "status": status,
        "tier": tier,
        "detail": (detail or "")[:200],
        "ts": time.time(),
    }


def get_last_fetch_error(url: str) -> dict | None:
    """Return the last recorded fetch failure for *url* (or None).

    Checks the exact URL first, then the bare URL without query string —
    discovery retries strip query params, so the recorded key may differ.
    """
    err = _last_fetch_errors.get(url)
    if err is None and "?" in url:
        err = _last_fetch_errors.get(url.split("?", 1)[0])
    return err


def format_fetch_error(url: str) -> str:
    """Human-readable one-liner of the last failure for *url* ('' if unknown)."""
    err = get_last_fetch_error(url)
    if not err:
        return ""
    parts = []
    if err.get("status") is not None:
        parts.append(f"HTTP {err['status']}")
    if err.get("tier"):
        parts.append(f"tier={err['tier']}")
    if err.get("detail"):
        parts.append(f"body={err['detail'][:120]!r}")
    return " ".join(parts)


# Concurrency caps are per-EVENT-LOOP, not per-process: Celery prefork runs
# each task in its own asyncio.run() loop, and an asyncio.Semaphore binds to
# the first loop that awaits it.  A module-level instance therefore raised
# "<Semaphore ...> is bound to a different event loop" for EVERY fetch in the
# second scrape job handled by the same worker process (observed as a
# whole-job discovery failure on JCU, 2026-07-09).  The WeakKeyDictionary
# drops a loop's semaphores automatically when the loop is garbage-collected.
_LOOP_SEMS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def _loop_sem(name: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    per_loop = _LOOP_SEMS.get(loop)
    if per_loop is None:
        per_loop = {}
        _LOOP_SEMS[loop] = per_loop
    sem = per_loop.get(name)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        per_loop[name] = sem
    return sem


def _get_sem() -> asyncio.Semaphore:
    """General outbound-HTTP concurrency cap for the current event loop."""
    return _loop_sem("http", settings.max_http_concurrency)


def _get_wayback_sem() -> asyncio.Semaphore:
    """Keep Internet Archive snapshot replay below its burst-rate limit."""
    return _loop_sem("wayback_snapshot", 1)


def _get_scrape_do_sem(
    local_concurrency_limit: int | None = None,
) -> asyncio.Semaphore:
    """In-process cap on concurrent Scrape.do requests (current loop).

    See config.py `max_scrape_do_concurrency` for the QMUL burst-failure
    rationale.  Without this, up to _MAX_PARALLEL_FETCH (12) course-fetch
    tasks can each fire a render=true request at the shared Scrape.do account
    simultaneously, and the retry on failure lands in the same saturated
    window, producing genuine fetch_failed results for URLs that fetch fine
    in isolation.
    """
    limit = (
        settings.max_scrape_do_concurrency
        if local_concurrency_limit is None
        else max(1, int(local_concurrency_limit))
    )
    # Include the limit in the key so a per-university override cannot reuse a
    # semaphore that was already created with the global default in this loop.
    return _loop_sem(f"scrape_do:{limit}", limit)

# ---------------------------------------------------------------------------
# Per-process Cloudflare fast-path cache
# ---------------------------------------------------------------------------
# When BOTH httpx AND curl_cffi return a Cloudflare block for a host, every
# subsequent request to that host skips those two tiers and goes straight to
# Scrape.do static.  This saves 20-40 s per course on heavily CF-protected
# sites like Westminster where the httpx+cffi chain always fails.
# The set persists for the life of the Celery worker process — acceptable
# because CF protection on a domain rarely disappears between scrape jobs.
_cf_always_scrape_do: set[str] = set()

# ---------------------------------------------------------------------------
# Shared persistent AsyncClient — connection-pool reuse, per event loop
# ---------------------------------------------------------------------------
# Creating a fresh AsyncClient() per request pays a new TCP+TLS handshake for
# every URL even when hitting the same host repeatedly (500 courses → 500
# handshakes).  A module-level shared client with keep-alive reuses open
# connections across coroutines, cutting per-request overhead to ~5-20ms.
# asyncio is single-threaded/cooperative so there is no data-race risk on the
# client object itself; httpx.AsyncClient is explicitly documented as
# concurrency-safe.
#
# Per-event-loop clients (not per-process):
# Celery prefork runs each task in its own asyncio.run() loop.  A single
# module-level AsyncClient instance is bound to the first loop that created
# it; when the second task starts on the same worker process, that loop is
# already closed.  Calling `await _shared_http_client.get(url)` then raises
# "Event loop is closed" because the client's internal connection pool tries
# to schedule on the closed loop.  The check `.is_closed` only catches
# explicit `await client.aclose()` calls — it does NOT detect a closed loop.
# Solution: same WeakKeyDictionary pattern as _LOOP_SEMS so each event loop
# gets its own client and stale clients are garbage-collected with their loop.
_LOOP_CLIENTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def _get_shared_client() -> httpx.AsyncClient:
    """Return (or lazily create) the shared AsyncClient for the current event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        client = _LOOP_CLIENTS.get(loop)
        if client is not None and not client.is_closed:
            return client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=60,
            max_keepalive_connections=40,
            keepalive_expiry=30,
        ),
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    if loop is not None:
        _LOOP_CLIENTS[loop] = client
    return client

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


# ---------------------------------------------------------------------------
# Scrape.do *static* gate — geo-block bypass without JS rendering
# ---------------------------------------------------------------------------
# When active, fetch_html() skips httpx/cffi entirely and goes straight to
# fetch_html_scrape_do(url, render=False).  This routes the request through
# Scrape.do's residential proxy network (~$0.0005/call) which returns a
# non-US IP, bypassing server-side geo-detection (Lancaster returns a US
# welcome page when fetched from a US IP even though the response is 200).
# Unlike scrape_do_render this does NOT execute JavaScript — use only for
# fully SSR pages where the geo-block is the sole obstacle.
_scrape_do_static_active: ContextVar[bool] = ContextVar(
    "scrape_do_static_active", default=False
)


@contextmanager
def scrape_do_static_scope():
    """Context manager: activate Scrape.do static proxy for the enclosed fetch.

    Routes per-course HTTP fetches through Scrape.do's residential proxy
    (render=False, ~$0.0005/call) to bypass server-side geo-detection.
    Use when course pages return geo-targeted content for US IPs even though
    the HTTP status is 200 (Lancaster is the canonical case).

    Usage (single_course.py)::

        from app.services.scraper.http_fetcher import scrape_do_static_scope

        with scrape_do_static_scope():
            html = await fetch_html(url)
    """
    token = _scrape_do_static_active.set(True)
    try:
        yield
    finally:
        _scrape_do_static_active.reset(token)

# Per-job Wayback timestamp cache:
# canonical identity key → (CDX timestamp, exact captured original URL).
# Populated by wayback_discover() during the discovery phase so that
# fetch_html_wayback() can use the exact timestamp from the CDX index
# instead of re-querying the Availability API (which is inconsistent —
# it returns the "closest to now" snapshot, which may be a 404/301,
# even when the CDX has a valid 200 snapshot at an older timestamp).
_wayback_ts_cache: dict[str, tuple[str, str]] = {}
_wayback_authoritative_prefixes: set[str] = set()
_last_fetch_failure: ContextVar[dict[str, object] | None] = ContextVar(
    "last_fetch_failure",
    default=None,
)


def _record_fetch_failure(
    *,
    kind: str,
    reason: str,
    retryable: bool,
    transport: str,
    status_code: int | None = None,
    terminal: bool = False,
) -> None:
    info: dict[str, object] = {
        "kind": kind,
        "reason": reason,
        "retryable": retryable,
        "transport": transport,
        "terminal": terminal,
    }
    if status_code is not None:
        info["status_code"] = status_code
    _last_fetch_failure.set(info)


def _mark_last_fetch_failure_terminal() -> None:
    info = _last_fetch_failure.get()
    if info:
        terminal_info = dict(info)
        terminal_info["terminal"] = True
        _last_fetch_failure.set(terminal_info)


def get_last_fetch_failure() -> dict[str, object] | None:
    """Return task-local metadata for the most recent failed fetch."""
    info = _last_fetch_failure.get()
    return dict(info) if info else None



def set_wayback_timestamps(
    url_timestamps: dict[str, str | tuple[str, str]],
    *,
    authoritative_prefixes: list[str] | None = None,
) -> None:
    """Register Wayback CDX timestamps found during discovery.

    New callers pass ``identity-key → (timestamp, original_url)`` so replay
    uses the exact scheme/host/port captured by CDX. For compatibility, callers
    may still pass ``url → timestamp``; that URL is then used as the original.
    """
    for url, value in url_timestamps.items():
        if isinstance(value, tuple):
            timestamp, original_url = value
        else:
            timestamp, original_url = value, url
        key = canonical_course_url_key(url)
        if key:
            _wayback_ts_cache[key] = (timestamp, original_url)
    for prefix in authoritative_prefixes or []:
        prefix_key = _wayback_archive_scope_key(prefix)
        if prefix_key:
            _wayback_authoritative_prefixes.add(prefix_key)


def clear_wayback_timestamps() -> None:
    """Discard the per-job timestamp cache after the scrape finishes."""
    _wayback_ts_cache.clear()
    _wayback_authoritative_prefixes.clear()


def _wayback_archive_scope_key(url: str) -> str:
    """Return a host-preserving key for one CDX wildcard scope.

    Unlike course deduplication, archive completeness must not collapse
    ``www`` and apex hosts: CDX indexes them as distinct captured originals.
    CDX queries without an explicit scheme cover both HTTP and HTTPS, so the
    key is host/port/path rather than scheme/host/path.
    """
    raw = str(url or "").strip().rstrip("*").rstrip("/")
    if not raw:
        return ""
    target = raw if "://" in raw else f"//{raw}"
    try:
        parsed = urllib.parse.urlsplit(target)
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return ""
        port = f":{parsed.port}" if parsed.port else ""
        path = re.sub(
            r"/+",
            "/",
            urllib.parse.unquote(parsed.path or ""),
        ).rstrip("/")
        return f"{host}{port}{path}"
    except (TypeError, ValueError):
        return ""


def _wayback_scope_is_authoritative(url: str) -> bool:
    url_key = _wayback_archive_scope_key(url)
    return any(
        url_key == prefix or url_key.startswith(f"{prefix}/")
        for prefix in _wayback_authoritative_prefixes
    )

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
    super_mode: bool = False,
    wait_for_ms: int = 3000,
    geo_code: str | None = None,
    rate_limit: bool = True,
    max_retries: int | None = None,
    play_with_browser: list[dict[str, object]] | None = None,
    unescape_json_html: bool = True,
    local_concurrency_limit: int | None = None,
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
    if not has_budget():
        log.info("[COURSE DEADLINE] skipping Scrape.do fetch for %s", url)
        return None
    # Task #229: cross-process throttle so the 8-worker fleet doesn't exhaust the
    # shared Scrape.do account in bursts.  No-op unless scrape_do_rate_limit_per_sec
    # is configured > 0; fail-open on any Redis issue.
    #
    # rate_limit=False is used by the discovery-phase fast-path (sequential,
    # max ~25 listing-page fetches, hard-deadlined by discovery_phase_timeout_s)
    # so it never queues behind a *different* university's high-volume parallel
    # course-extraction burst.  Cardiff job_68778b8f7bb2 (2026-07-06): the fleet
    # rate limiter (enabled fleet-wide the same day to fix QMUL's fetch_failed
    # burst) shares one small per-second budget across every Scrape.do caller;
    # QMUL's concurrent course-fetch retries saturated that budget, so each of
    # Cardiff's one-at-a-time discovery calls waited up to _MAX_WAIT_S (30s) for
    # a token, and ~10 such waits blew through the 300s discovery deadline.
    # Discovery itself is low-volume and doesn't need protection from bursts —
    # it's the victim here, not the cause — so it's exempted from the limiter.
    if rate_limit:
        try:
            from app.services.scraper.rate_limiter import acquire_scrape_do
            _limiter_timeout = clamp_timeout(None)
            if _limiter_timeout is None:
                await acquire_scrape_do()
            elif _limiter_timeout > 0:
                await asyncio.wait_for(acquire_scrape_do(), timeout=_limiter_timeout)
            else:
                return None
        except asyncio.TimeoutError:
            log.info("[COURSE DEADLINE] Scrape.do limiter wait expired for %s", url)
            return None
        except Exception as _rl_exc:  # noqa: BLE001 — never block a fetch on the limiter
            log.debug("scrape_do rate-limit acquire skipped: %s", _rl_exc)
    params: dict[str, str] = {"token": token, "url": url}
    if render:
        params["render"] = "true"
        params["waitFor"] = str(wait_for_ms)
    if super_mode:
        params["super"] = "true"
    if play_with_browser:
        params["playWithBrowser"] = json.dumps(
            play_with_browser,
            separators=(",", ":"),
        )
    if geo_code:
        params["geoCode"] = geo_code.upper()
    # T03: Exponential-backoff retry for transient Scrape.do failures.
    # JCU root cause: account concurrency/rate-limit rejection returned non-200
    # with zero retry — 96/103 courses silently returned None.
    # Three total attempts with 2 s / 8 s / 30 s inter-attempt pauses.
    # The semaphore is RELEASED between attempts so sibling coroutines can
    # proceed while this one waits; it is re-acquired before the next attempt.
    _SD_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _SD_BACKOFFS = (2.0, 8.0, 30.0)  # seconds between attempts
    if max_retries is not None:
        # Caller wants a shorter (or zero) retry ladder — e.g. a doomed
        # static-tier probe on a host that never recovers on this leg, so
        # the caller would rather fail fast and spend its time budget on
        # the tier that actually works (see Ulster sitemap discovery).
        _SD_BACKOFFS = _SD_BACKOFFS[:max_retries]
    _last_sd_r: httpx.Response | None = None

    for _sd_attempt in range(len(_SD_BACKOFFS) + 1):  # attempts 0..3
        if _sd_attempt > 0:
            _wait = _SD_BACKOFFS[_sd_attempt - 1]
            # Honor Retry-After header when the server provides one.
            if _last_sd_r is not None:
                _ra = _last_sd_r.headers.get("retry-after", "")
                try:
                    _wait = max(_wait, float(_ra))
                except (ValueError, TypeError):
                    pass
            log.info(
                "[FETCH RETRY] scrape.do %s render=%s → attempt %d/%d after %.0fs backoff",
                url, render, _sd_attempt + 1, len(_SD_BACKOFFS) + 1, _wait,
            )
            _remaining = remaining_seconds()
            if _remaining is not None and _remaining <= _wait:
                log.info(
                    "[COURSE DEADLINE] skipping %.0fs Scrape.do retry backoff for %s "
                    "(remaining=%.3fs)",
                    _wait, url, _remaining,
                )
                return None
            await asyncio.sleep(_wait)
        try:
            # Bound real concurrent connections to the shared Scrape.do account —
            # see `_scrape_do_sem` definition for the QMUL burst-failure rationale.
            # Part B (fetch-layer brief): the ACCOUNT-WIDE Redis slot bounds the
            # true fleet-wide in-flight count too (8 prefork workers × 5 local
            # = 40 without it).  Nesting order matters: the LOCAL semaphore is
            # acquired FIRST so a coroutine never holds a scarce fleet-wide
            # Redis slot while merely queueing behind its own process's local
            # semaphore.  Both are acquired INSIDE the retry loop and released
            # between attempts so a backing-off coroutine never starves the
            # rest of the fleet.  account_slot() is a no-op unless
            # scrape_do_account_concurrency > 0 and fails open on any Redis error.
            from app.services.scraper.scrape_do_semaphore import account_slot
            _request_timeout = clamp_timeout(90.0)
            if _request_timeout is not None and _request_timeout <= 0:
                return None

            async def _request() -> httpx.Response:
                async with _get_scrape_do_sem(local_concurrency_limit):
                    async with account_slot():
                        async with httpx.AsyncClient(
                            timeout=_request_timeout or 90.0,
                            follow_redirects=True,
                        ) as c:
                            return await c.get("https://api.scrape.do", params=params)

            if _request_timeout is None:
                _last_sd_r = await _request()
            else:
                _last_sd_r = await asyncio.wait_for(
                    _request(),
                    timeout=_request_timeout,
                )
            _status = _last_sd_r.status_code
            if _status == 200 and is_challenge_shell(_last_sd_r.text):
                _record_fetch_error(
                    url,
                    status=_status,
                    tier="scrape_do",
                    detail="anti-bot challenge shell",
                )
                _record_fetch_failure(
                    kind="challenge_page",
                    reason=(
                        "Scrape.do returned an anti-bot challenge shell instead "
                        "of the requested page."
                    ),
                    retryable=True,
                    transport="scrape_do_render" if render else "scrape_do_static",
                    status_code=_status,
                )
                if _sd_attempt < len(_SD_BACKOFFS):
                    log.warning(
                        "[FETCH FAIL] scrape.do %s render=%s → HTTP 200 "
                        "challenge shell on attempt %d/%d — retrying",
                        url,
                        render,
                        _sd_attempt + 1,
                        len(_SD_BACKOFFS) + 1,
                    )
                    continue
                log.warning(
                    "[FETCH FAIL] scrape.do %s render=%s → HTTP 200 "
                    "challenge shell after %d attempt(s) — exhausted",
                    url,
                    render,
                    _sd_attempt + 1,
                )
                return None
            if _status == 200 and len(_last_sd_r.text) > 500:
                log.info(
                    "scrape.do fetch %s render=%s -> 200 (%d chars)",
                    url, render, len(_last_sd_r.text),
                )
                # Per-job call counter (shared mutable dict via ContextVar).
                _sd_ctrs = _scrape_do_job_counters.get()
                if _sd_ctrs is not None:
                    if render:
                        _sd_ctrs["render"] += 1
                    else:
                        _sd_ctrs["static"] += 1
                html_result = (
                    _unescape_json_html(_last_sd_r.text)
                    if unescape_json_html
                    else _last_sd_r.text
                )
                # Stage the final fetched HTML so _extract_only() can save it
                # to S3 *after* extract_course() completes.  This ensures only
                # the winning fetch (not retries or intermediate fallbacks) is
                # stored, and the original extraction result is attached.
                from app.services.scraper.snapshot_context import stage_snapshot
                stage_snapshot(
                    url,
                    html_result,
                    fetch_method="scrape_do_render" if render else "scrape_do_static",
                )
                return html_result
            # 401/403 from Scrape.do itself = bad token or credits exhausted.
            # Raise immediately — no retry will fix a bad account state, and
            # the orchestrator needs to abort the job to avoid burning quota.
            if _status in (401, 403):
                raise ScrapedoAccountError(
                    f"Scrape.do auth/credits error HTTP {_status} for {url!r} — "
                    "check SCRAPE_DO_TOKEN and account balance"
                )
            # 404/410 = page genuinely not found on the origin — no retry needed.
            if _status in (404, 410):
                log.info(
                    "[FETCH FAIL] scrape.do %s render=%s → %s page-not-found (no retry)",
                    url, render, _status,
                )
                _record_fetch_error(
                    url, status=_status, tier="scrape_do",
                    detail="page not found",
                )
                _record_fetch_failure(
                    kind="origin_not_found",
                    reason=(
                        f"The live rendered origin returned HTTP {_status}; "
                        "retrying will not restore a removed page."
                    ),
                    retryable=False,
                    transport="scrape_do_render" if render else "scrape_do_static",
                    status_code=_status,
                )
                return None
            # ROTATION_FAILED (ErrorCode 90, "cannot connect target url") on a
            # STATIC call is a proxy-level connect failure: the target host is
            # refusing Scrape.do's datacenter/proxy exit IPs outright.  Ulster
            # (job_ec86dc5866cb) and JCU (job_a127d35039d1) both show this is
            # persistent — retrying static harder never helps, only the
            # render=True residential-browser pool gets through.  Fail fast so
            # the caller's static→render escalation fires while there is still
            # budget left (each doomed static attempt costs ~30-60s of latency).
            # render=True ROTATION_FAILED stays on the normal retry ladder —
            # the browser pool rotates real residential IPs, so retrying there
            # genuinely can succeed.
            if (
                not render
                and _status in _SD_RETRY_STATUSES
                and "ROTATION_FAILED" in _last_sd_r.text[:600]
            ):
                log.warning(
                    "[FETCH FAIL] scrape.do %s render=False → status=%s "
                    "ROTATION_FAILED (proxy cannot connect to target) — failing "
                    "fast so the render=True tier can fire (no static retries)",
                    url, _status,
                )
                _record_fetch_error(
                    url, status=_status, tier="scrape_do",
                    detail="ROTATION_FAILED static — fail fast to render tier",
                )
                return None
            # Transient overload — retry if budget remains.
            if _status in _SD_RETRY_STATUSES and _sd_attempt < len(_SD_BACKOFFS):
                log.warning(
                    "[FETCH FAIL] scrape.do %s render=%s → status=%s attempt %d/%d "
                    "body=%r — scheduling retry with backoff",
                    url, render, _status, _sd_attempt + 1, len(_SD_BACKOFFS) + 1,
                    _redact_scrape_do_log_text(_last_sd_r.text[:300], token=token),
                )
                continue
            # Final attempt or non-retryable non-200.
            log.warning(
                "[FETCH FAIL] scrape.do %s render=%s → status=%s attempt %d/%d body=%r",
                url, render, _status, _sd_attempt + 1, len(_SD_BACKOFFS) + 1,
                _redact_scrape_do_log_text(_last_sd_r.text[:300], token=token),
            )
            _record_fetch_error(
                url, status=_status, tier="scrape_do",
                detail=_redact_scrape_do_log_text(_last_sd_r.text[:200], token=token),
            )
            _record_fetch_failure(
                kind="scrape_do_unavailable",
                reason=f"Scrape.do returned HTTP {_status}.",
                retryable=_status in _SD_RETRY_STATUSES,
                transport="scrape_do_render" if render else "scrape_do_static",
                status_code=_status,
            )
            return None
        except ScrapedoAccountError:
            raise  # always propagate — orchestrator must abort the job
        except asyncio.TimeoutError:
            log.warning(
                "[COURSE DEADLINE] Scrape.do request exceeded remaining %.3fs for %s",
                _request_timeout or 0.0,
                url,
            )
            _record_fetch_failure(
                kind="scrape_do_timeout",
                reason="Scrape.do did not finish within the shared course deadline.",
                retryable=True,
                transport="scrape_do_render" if render else "scrape_do_static",
            )
            return None
        except Exception as _sd_exc:
            _redacted_exception = _redact_scrape_do_log_text(_sd_exc, token=token)
            if _sd_attempt < len(_SD_BACKOFFS):
                log.warning(
                    "[FETCH FAIL] scrape.do %s render=%s → exception attempt %d/%d: %s"
                    " — scheduling retry",
                    url, render, _sd_attempt + 1, len(_SD_BACKOFFS) + 1, _redacted_exception,
                )
                continue
            log.warning(
                "[FETCH FAIL] scrape.do %s render=%s → exception final attempt %d/%d: %s",
                url, render, _sd_attempt + 1, len(_SD_BACKOFFS) + 1, _redacted_exception,
            )
            _record_fetch_error(
                url, status=None, tier="scrape_do",
                detail=f"exception: {_redacted_exception}",
            )
            _record_fetch_failure(
                kind="scrape_do_unavailable",
                reason=f"Scrape.do request failed: {_redacted_exception}",
                retryable=True,
                transport="scrape_do_render" if render else "scrape_do_static",
            )
            return None
    # All retries exhausted — should not reach here in practice.
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
                timeout=15,
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
    url_key = canonical_course_url_key(url)
    cached_entry = _wayback_ts_cache.get(url_key)
    cached_ts = cached_entry[0] if cached_entry else ""
    cached_original_url = cached_entry[1] if cached_entry else ""
    if cached_ts:
        raw_url = (
            f"https://web.archive.org/web/{cached_ts}id_/"
            f"{cached_original_url}"
        )
        _snapshot_transient = False
        async with _get_wayback_sem():
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
                for _snapshot_attempt in range(1, 4):
                    try:
                        r = await c.get(
                            raw_url,
                            headers={"User-Agent": _BROWSER_UA},
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        _snapshot_transient = True
                        if _snapshot_attempt < 3:
                            _wait = (5, 15)[_snapshot_attempt - 1]
                            log.info(
                                "wayback fetch %s connection failed on cached "
                                "snapshot attempt %d/3 (%s) — "
                                "retrying in %ss",
                                url,
                                _snapshot_attempt,
                                exc,
                                _wait,
                            )
                            await asyncio.sleep(_wait)
                            continue
                        log.warning(
                            "wayback fetch %s (CDX-cached snapshot %s) failed "
                            "after %d attempts: %s",
                            url,
                            cached_ts,
                            _snapshot_attempt,
                            exc,
                        )
                        _record_fetch_failure(
                            kind="wayback_transient",
                            reason=f"Cached Wayback snapshot fetch failed: {exc}",
                            retryable=True,
                            transport="wayback_snapshot",
                        )
                        break

                    if r.status_code == 200:
                        _last_fetch_failure.set(None)
                        log.info(
                            "wayback fetch %s -> 200 (CDX-cached snapshot %s, "
                            "%d chars, attempt %d)",
                            url,
                            cached_ts,
                            len(r.text),
                            _snapshot_attempt,
                        )
                        return r.text
                    _snapshot_transient = r.status_code in {
                        429, 500, 502, 503, 504
                    }
                    if _snapshot_transient and _snapshot_attempt < 3:
                        _wait = (5, 15)[_snapshot_attempt - 1]
                        log.info(
                            "wayback fetch %s -> %s (cached snapshot) — "
                            "retrying in %ss",
                            url,
                            r.status_code,
                            _wait,
                        )
                        await asyncio.sleep(_wait)
                        continue
                    log.warning(
                        "wayback fetch %s -> %s (CDX-cached snapshot %s)",
                        url,
                        r.status_code,
                        cached_ts,
                    )
                    _record_fetch_failure(
                        kind=(
                            "wayback_transient"
                            if _snapshot_transient
                            else "wayback_snapshot_unavailable"
                        ),
                        reason=(
                            f"Cached Wayback snapshot returned HTTP "
                            f"{r.status_code}."
                        ),
                        retryable=_snapshot_transient,
                        transport="wayback_snapshot",
                        status_code=r.status_code,
                    )
                    break

        # A transient replay failure does not invalidate the exact CDX result.
        # Querying CDX again for every course only amplifies archive.org load and
        # caused 203/215 SEGi courses to fail in one burst. Let the bounded sweep
        # retry the same cached timestamp later instead.
        if _snapshot_transient:
            return None

    if not cached_entry and _wayback_scope_is_authoritative(url):
        log.info(
            "wayback fetch: no snapshot for %s in complete discovery CDX scope",
            url,
        )
        _record_fetch_failure(
            kind="wayback_no_snapshot",
            reason=(
                "No 200-status Wayback snapshot exists in the complete "
                "discovery CDX result for this URL."
            ),
            retryable=False,
            transport="wayback_cdx_cache",
        )
        return None

    # Slow path: live CDX search for URLs not in the CDX cache (e.g. PDF
    # central pages, fee pages, or non-Wayback-discovered URLs).
    #
    # We deliberately do NOT use the Wayback Availability API
    # (``archive.org/wayback/available``) here.  It returns the snapshot
    # "closest" to a requested timestamp *regardless of HTTP status code*.
    # For QMUL course pages we confirmed cases where the closest-in-time
    # snapshot was itself a 403 (archived while the site's WAF was blocking
    # the IA crawler too), while a perfectly good 200 snapshot existed
    # months earlier or later — the Availability API silently returned the
    # bad one and the caller gave up. A direct CDX query filtered to
    # ``statuscode:200`` avoids this: we always pick from snapshots that
    # are known-good responses, then take the most recent of those.
    _CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            cdx_r = await c.get(
                _CDX_ENDPOINT,
                params={
                    "url": url,
                    "output": "json",
                    "filter": "statuscode:200",
                },
            )
            if cdx_r.status_code == 503:
                # Wayback CDX is transiently overloaded — retry once after a
                # short sleep rather than giving up and falling to the live path.
                log.info(
                    "wayback fetch: CDX search returned 503 for %s — retrying in 3 s",
                    url,
                )
                await asyncio.sleep(3)
                cdx_r = await c.get(
                    _CDX_ENDPOINT,
                    params={
                        "url": url,
                        "output": "json",
                        "filter": "statuscode:200",
                    },
                )
            if cdx_r.status_code != 200:
                log.info(
                    "wayback fetch: CDX search returned %s for %s",
                    cdx_r.status_code, url,
                )
                _record_fetch_failure(
                    kind="wayback_cdx_unavailable",
                    reason=f"Wayback CDX returned HTTP {cdx_r.status_code}.",
                    retryable=True,
                    transport="wayback_cdx",
                    status_code=cdx_r.status_code,
                )
                return None
            rows = cdx_r.json()
            # First row is the header (["urlkey","timestamp",...]); real
            # snapshot rows follow. No 200-status rows means genuinely no
            # usable archive exists for this URL.
            if not rows or len(rows) < 2:
                log.info("wayback fetch: no 200-status snapshot found for %s", url)
                _record_fetch_failure(
                    kind=(
                        "wayback_transient"
                        if cached_ts
                        else "wayback_no_snapshot"
                    ),
                    reason=(
                        "A cached snapshot exists but the archive could not "
                        "resolve it."
                        if cached_ts
                        else "No 200-status Wayback snapshot exists for this URL."
                    ),
                    retryable=bool(cached_ts),
                    transport="wayback_cdx",
                )
                return None
            snapshot_rows = rows[1:]
            snapshot_rows.sort(key=lambda r: r[1])
            timestamp: str = snapshot_rows[-1][1]
            original: str = snapshot_rows[-1][2]
            snapshot_url = f"https://web.archive.org/web/{timestamp}/{original}"
    except Exception as exc:
        log.warning("wayback fetch: CDX search failed for %s: %s", url, exc)
        _record_fetch_failure(
            kind="wayback_cdx_unavailable",
            reason=f"Wayback CDX request failed: {exc}",
            retryable=True,
            transport="wayback_cdx",
        )
        return None

    raw_url = snapshot_url.replace(f"/web/{timestamp}/", f"/web/{timestamp}id_/", 1)
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            r = await c.get(raw_url, headers={"User-Agent": _BROWSER_UA})
            if r.status_code == 200:
                _last_fetch_failure.set(None)
                log.info(
                    "wayback fetch %s -> 200 (Availability snapshot %s, %d chars)",
                    url, timestamp, len(r.text),
                )
                return r.text
            log.warning(
                "wayback fetch %s -> %s (Availability snapshot %s)",
                url, r.status_code, timestamp,
            )
            _record_fetch_failure(
                kind="wayback_snapshot_unavailable",
                reason=f"Wayback snapshot returned HTTP {r.status_code}.",
                retryable=r.status_code not in (404, 410),
                transport="wayback_snapshot",
                status_code=r.status_code,
            )
            return None
    except Exception as exc:
        log.warning("wayback fetch %s failed: %s", url, exc)
        _record_fetch_failure(
            kind="wayback_transient",
            reason=f"Wayback snapshot request failed: {exc}",
            retryable=True,
            transport="wayback_snapshot",
        )
        return None


@asynccontextmanager
async def _client():
    # Yield the shared persistent client (connection-pool reuse).
    # No enter/exit — the shared client stays open for the life of the worker.
    yield _get_shared_client()


async def fetch_html(url: str, *, retries: int = 2, wait_for_ms: int = 3000) -> str | None:
    _last_fetch_failure.set(None)
    # Scrape.do render is only active inside scrape_do_render_scope() —
    # i.e. during per-course extraction in single_course.extract_course().
    # It is NEVER True during discovery / sitemap / central-page phases.
    _scrape_do_render = _scrape_do_render_active.get()
    _scrape_do_static = _scrape_do_static_active.get()
    _has_scrape_do = bool(os.environ.get("SCRAPE_DO_TOKEN"))

    # Exact-host TLS exception for official servers with an incomplete
    # certificate chain. This route is deliberately terminal: once configured,
    # a failure must not fall through to paid proxies or stale archives.
    _insecure_tls_hosts: set[str] = set()
    try:
        from app.services.scraper.config.context import get_uni_config as _guc_tls
        _insecure_tls_hosts = {
            str(host).strip().lower().rstrip(".")
            for host in _guc_tls().discovery.insecure_tls_direct_hostnames
            if str(host).strip()
        }
    except Exception:  # noqa: BLE001
        pass
    try:
        _url_host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        _url_host = ""
    if _url_host and _url_host in _insecure_tls_hosts:
        _attempts = max(1, int(retries) + 1)
        for _attempt in range(1, _attempts + 1):
            try:
                async with _get_sem():
                    async with httpx.AsyncClient(
                        timeout=30,
                        follow_redirects=False,
                        verify=False,
                        headers={
                            "User-Agent": _BROWSER_UA,
                            "Accept": (
                                "text/html,application/xhtml+xml,"
                                "application/xml;q=0.9,*/*;q=0.8"
                            ),
                            "Accept-Language": "en-AU,en;q=0.9",
                        },
                    ) as _tls_client:
                        _tls_url = url
                        _tls_seen_urls = {url}
                        for _redirect_count in range(6):
                            _tls_response = await _tls_client.get(_tls_url)
                            if _tls_response.status_code not in {
                                301, 302, 303, 307, 308
                            }:
                                break
                            _location = _tls_response.headers.get("location", "")
                            try:
                                _redirect_url = urllib.parse.urljoin(
                                    _tls_url, _location
                                )
                                _redirect_parts = urllib.parse.urlsplit(_redirect_url)
                                _redirect_host = (
                                    _redirect_parts.hostname or ""
                                ).lower().rstrip(".")
                            except (TypeError, ValueError):
                                _redirect_url = ""
                                _redirect_parts = urllib.parse.SplitResult(
                                    "", "", "", "", ""
                                )
                                _redirect_host = ""
                            if (
                                _redirect_parts.scheme.lower() != "https"
                                or _redirect_host not in _insecure_tls_hosts
                            ):
                                _record_fetch_failure(
                                    kind="unsafe_redirect",
                                    reason=(
                                        "Exact-host TLS exception rejected redirect "
                                        f"from {_tls_url!r} to {_redirect_url!r}."
                                    ),
                                    retryable=False,
                                    transport="direct_insecure_tls",
                                    status_code=_tls_response.status_code,
                                    terminal=True,
                                )
                                log.error(
                                    "fetch %s: exact-host TLS exception rejected "
                                    "non-HTTPS or non-allowlisted redirect to %s",
                                    url,
                                    _redirect_url or "<invalid>",
                                )
                                return None
                            if (
                                _redirect_url in _tls_seen_urls
                                or _redirect_count == 5
                            ):
                                _record_fetch_failure(
                                    kind="redirect_loop",
                                    reason=(
                                        "Exact-host TLS exception exceeded its "
                                        "same-host redirect limit."
                                    ),
                                    retryable=False,
                                    transport="direct_insecure_tls",
                                    status_code=_tls_response.status_code,
                                    terminal=True,
                                )
                                return None
                            _tls_seen_urls.add(_redirect_url)
                            _tls_url = _redirect_url
                if (
                    _tls_response.status_code == 200
                    and len(_tls_response.text) > 500
                    and not is_challenge_shell(_tls_response.text)
                ):
                    _last_fetch_failure.set(None)
                    from app.services.scraper.snapshot_context import (
                        stage_snapshot as _stage,
                    )
                    _stage(url, _tls_response.text, "direct_insecure_tls")
                    log.info(
                        "fetch %s -> 200 via exact-host TLS exception "
                        "(%d chars, attempt %d/%d)",
                        url,
                        len(_tls_response.text),
                        _attempt,
                        _attempts,
                    )
                    return _tls_response.text
                _status = _tls_response.status_code
                _record_fetch_failure(
                    kind="origin_http_error",
                    reason=(
                        f"Exact-host TLS exception returned HTTP {_status} "
                        "or unusable HTML."
                    ),
                    retryable=_status in {429, 500, 502, 503, 504},
                    transport="direct_insecure_tls",
                    status_code=_status,
                )
            except (httpx.TimeoutException, httpx.TransportError) as _tls_exc:
                _record_fetch_failure(
                    kind="network_error",
                    reason=f"Exact-host TLS exception failed: {_tls_exc}",
                    retryable=True,
                    transport="direct_insecure_tls",
                )
            if _attempt < _attempts:
                await asyncio.sleep(float(_attempt))
        log.warning(
            "fetch %s: exact-host TLS exception exhausted after %d attempt(s); "
            "not falling through to proxy/archive transports",
            url,
            _attempts,
        )
        return None

    # Read per-university geo code (ISO 3166-1 alpha-2) for Scrape.do pinning.
    _scrape_do_geo: str | None = None
    if _scrape_do_static or _scrape_do_render:
        try:
            from app.services.scraper.config.context import get_uni_config as _guc
            _scrape_do_geo = getattr(_guc().extraction, "scrape_do_geo", "") or None
        except Exception:  # noqa: BLE001
            pass

    # Geo-block bypass: skip httpx/cffi entirely and proxy through Scrape.do
    # static (render=False, ~$0.0005/call).  Activated by scrape_do_static=true
    # in extraction YAML for SSR universities that serve geo-targeted content
    # when the request comes from a US IP (Lancaster is the canonical case).
    if _scrape_do_static and _has_scrape_do:
        log.info(
            "fetch %s: scrape_do_static=True geo=%s — routing via Scrape.do proxy (render=False)",
            url, _scrape_do_geo or "auto",
        )
        _static = await fetch_html_scrape_do(url, render=False, geo_code=_scrape_do_geo)
        if _static is not None:
            from app.services.scraper.snapshot_context import stage_snapshot as _stage
            _stage(url, _static, "scrape_do_static")
            return _static
        # Proxy failed — fall through to normal httpx so we get *something*
        log.warning(
            "fetch %s: Scrape.do static proxy failed — falling back to direct httpx",
            url,
        )

    # Fast-path: skip httpx + curl_cffi entirely when the university has
    # scrape_do_skip_fallbacks=True.  For Angular/React SPA sites behind
    # Cloudflare WAF (e.g. UWL) every direct-HTTP attempt returns a challenge
    # page — skipping them saves ~1-2s per course (several minutes per full run).
    if _scrape_do_render and _has_scrape_do:
        _allow_wayback_last_resort = True
        try:
            from app.services.scraper.config.context import get_uni_config
            _active_config = get_uni_config()
            _skip_fallbacks = _active_config.extraction.scrape_do_skip_fallbacks
            _allow_wayback_last_resort = (
                _active_config.discovery.use_wayback is not False
            )
        except Exception:  # noqa: BLE001
            _skip_fallbacks = False
        if _skip_fallbacks:
            log.info(
                "fetch %s: scrape_do_skip_fallbacks=True — going straight to Scrape.do render",
                url,
            )
            _rendered = await fetch_html_scrape_do(url, render=True, wait_for_ms=wait_for_ms)
            if _rendered is not None:
                return _rendered
            # Render returned 502 / None (e.g. Scrape.do rate-limited under
            # concurrent load).  Fall back to Scrape.do static before giving up —
            # for many Cloudflare-protected SPAs (e.g. UWL) the residential proxy
            # path returns fully hydrated HTML even without headless Chrome.
            log.info(
                "fetch %s: scrape_do_skip_fallbacks fast-path: render=True failed"
                " — falling back to Scrape.do static",
                url,
            )
            _static = await fetch_html_scrape_do(url, render=False)
            if _static is not None and not _is_spa_shell(_static):
                return _static
            # Both render AND static failed on the first pass.  For universities
            # that ALSO have skip_browser_rescue=true (e.g. QMUL — datacenter IP
            # is blocked for both httpx and our own Playwright pool, so browser
            # rescue would fail anyway), this fast-path is the *only* fetch
            # attempt — there is no further fallback tier below it.  Under
            # concurrent load (12 parallel HTTP workers) Scrape.do's proxy pool
            # occasionally returns a transient 502/"ROTATION_FAILED" for a
            # request that would succeed moments later (same failure signature
            # documented for the Ulster sitemap fetch).
            #
            # QMUL job_5f5ab180197a (2026-07-03): a single short-backoff retry
            # recovered most of the 47/409 (~11%) courses lost to this gap.
            # QMUL job_4fb674e585b2 (2026-07-06): under heavier concurrent load
            # (multiple universities sharing the same SCRAPE_DO_TOKEN account
            # at once) the transient-failure rate got much worse — 279/409
            # (~68%) courses were lost — and a single retry was no longer
            # enough because the account-wide contention window regularly
            # outlasts one 3s backoff.  Use a short exponential-backoff retry
            # ladder (render, static, render) instead of one fixed retry so
            # a course only fails for good after 5 total Scrape.do attempts
            # spread across ~26s of backoff, giving transient saturation time
            # to clear.
            _retry_backoffs = (3.0, 8.0, 15.0)
            _retry_modes = (True, False, True)
            _rendered_retry: str | None = None
            for _attempt, (_backoff, _use_render) in enumerate(
                zip(_retry_backoffs, _retry_modes), start=1
            ):
                log.info(
                    "fetch %s: scrape_do_skip_fallbacks fast-path exhausted"
                    " (render+static) — retry %d/%d (%s) after %.0fs backoff",
                    url, _attempt, len(_retry_backoffs),
                    "render" if _use_render else "static", _backoff,
                )
                await asyncio.sleep(_backoff)
                _candidate = await fetch_html_scrape_do(
                    url, render=_use_render,
                    wait_for_ms=wait_for_ms if _use_render else 3000,
                )
                if _candidate is not None and (
                    _use_render or not _is_spa_shell(_candidate)
                ):
                    _rendered_retry = _candidate
                    break
            if _rendered_retry is not None:
                return _rendered_retry
            # Last resort: Wayback Machine.  Universities with
            # scrape_do_skip_fallbacks=True + skip_browser_rescue=True (e.g.
            # QMUL) have NO other fallback tier — httpx/cffi are skipped by
            # design (blocked by the live WAF the same as Scrape.do's proxy
            # pool occasionally is) and browser rescue is disabled because
            # Playwright is blocked from the same datacenter IP range.
            # Archive.org is not subject to the live WAF at all, so it can
            # recover the handful of courses (~5% of the fleet, QMUL
            # job_aba92c0d3316 2026-07-03: 7/125) where Scrape.do's render
            # AND static AND the render-retry all failed transiently. This
            # only fires after 3 Scrape.do attempts already failed, so the
            # extra archive.org round-trip cost is negligible.
            if _allow_wayback_last_resort:
                log.info(
                    "fetch %s: scrape_do_skip_fallbacks fast-path exhausted"
                    " after render retry — trying Wayback Machine as last resort",
                    url,
                )
                _wayback_last_resort = await fetch_html_wayback(url)
                if _wayback_last_resort is not None:
                    from app.services.scraper.snapshot_context import stage_snapshot as _stage
                    _stage(url, _wayback_last_resort, "wayback")
                    return _wayback_last_resort
            log.info(
                "fetch %s: scrape_do_skip_fallbacks fast-path exhausted"
                " (wayback_allowed=%s) — giving up (fetch_failed)",
                url,
                _allow_wayback_last_resort,
            )
            return None

    # Force-Wayback-first: use CDX-cached snapshots as the cheap primary
    # transport, then apply the configured miss policy for current-only URLs.
    # This avoids paying the doomed ~57 s static-proxy cost before a rendered
    # live fallback on mixed archive/current catalogues.
    if not _scrape_do_render and not _scrape_do_static:
        _force_wb = False
        try:
            from app.services.scraper.config.context import get_uni_config as _guc_fwb
            _force_wb = bool(
                getattr(_guc_fwb().extraction, "force_wayback_first", False)
            )
        except Exception:  # noqa: BLE001
            pass
        if _force_wb:
            log.info(
                "fetch %s: extraction.force_wayback_first=True"
                " — trying Wayback before any live scrape.do attempt",
                url,
            )
            _fwb_html = await fetch_html_wayback(url)
            if _fwb_html is not None:
                from app.services.scraper.snapshot_context import stage_snapshot as _stg_fwb
                _stg_fwb(url, _fwb_html, "wayback")
                return _fwb_html
            log.info(
                "fetch %s: force_wayback_first — Wayback returned no usable "
                "page; applying configured miss policy",
                url,
            )
            _cfg_fwb2 = None
            try:
                from app.services.scraper.config.context import (
                    get_uni_config as _guc_fwb2,
                )
                _cfg_fwb2 = _guc_fwb2()
            except Exception:  # noqa: BLE001
                pass
            _wayback_miss_fallback = str(
                getattr(
                    getattr(_cfg_fwb2, "extraction", None),
                    "wayback_miss_fallback",
                    "none",
                )
                or "none"
            ).lower()
            if _wayback_miss_fallback == "scrape_do_render":
                log.info(
                    "fetch %s: Wayback miss — trying configured Scrape.do "
                    "render fallback",
                    url,
                )
                if not _has_scrape_do:
                    _record_fetch_failure(
                        kind="provider_account_failure",
                        reason=(
                            "Wayback had no snapshot and the configured "
                            "Scrape.do render fallback is unavailable."
                        ),
                        retryable=False,
                        transport="scrape_do_render",
                    )
                    _mark_last_fetch_failure_terminal()
                    return None
                _live_fallback = await fetch_html_scrape_do(
                    url,
                    render=True,
                    wait_for_ms=wait_for_ms,
                    max_retries=1,
                    local_concurrency_limit=getattr(
                        getattr(_cfg_fwb2, "extraction", None),
                        "scrape_do_local_concurrency",
                        None,
                    ),
                )
                if _live_fallback is not None:
                    from app.services.scraper.snapshot_context import (
                        stage_snapshot as _stg_live_fwb,
                    )
                    _stg_live_fwb(url, _live_fallback, "scrape_do_render")
                    _last_fetch_failure.set(None)
                    return _live_fallback
                _live_failure = get_last_fetch_failure()
                if not _live_failure or bool(_live_failure.get("retryable", True)):
                    _record_fetch_failure(
                        kind="scrape_do_render_unavailable",
                        reason=(
                            "Wayback had no usable page and the configured live "
                            "render fallback returned no usable content."
                        ),
                        retryable=True,
                        transport="scrape_do_render",
                    )
                _mark_last_fetch_failure_terminal()
                return None
            # "none" is deliberately terminal. Archive-only universities use
            # this policy because every live transport is known to fail; falling
            # through here would reintroduce the same 60-90s-per-page timeout
            # that force_wayback_first exists to avoid.
            log.warning(
                "fetch %s: force_wayback_first miss + wayback_miss_fallback=none"
                " — returning None without any live transport",
                url,
            )
            _mark_last_fetch_failure_terminal()
            return None

    # Discovery-phase fast-path: when discovery.scrape_do_skip_fallbacks=True,
    # skip httpx + curl_cffi for listing/sitemap pages and go straight to
    # Scrape.do static (residential proxy, render=False, ~$0.0005/call).
    # Active only when we are NOT inside scrape_do_render_scope() or
    # scrape_do_static_scope() — those scopes already handle their own routing.
    # Cardiff is the canonical case: CF Enterprise blocks all datacenter IPs for
    # both listing pages AND course pages, so the httpx→cffi chain always wastes
    # 2-4 attempts per listing URL before the host-cache kicks in.
    if not _scrape_do_render and not _scrape_do_static and _has_scrape_do:
        _disc_skip = False
        _disc_render_first = False
        _disc_super = False
        _disc_wait_for_ms = 3000
        try:
            from app.services.scraper.config.context import get_uni_config as _guc_disc
            _disc_cfg = _guc_disc().discovery
            _disc_skip = _disc_cfg.scrape_do_skip_fallbacks
            _disc_render_first = _disc_cfg.scrape_do_render
            _disc_super = _disc_cfg.scrape_do_super
            _disc_wait_for_ms = _disc_cfg.scrape_do_wait_for_ms
        except Exception:  # noqa: BLE001
            pass
        if _disc_skip:
            # discovery.scrape_do_render=True: static ALWAYS fails with
            # ROTATION_FAILED on this host (e.g. JCU) — skip the doomed
            # ~30-60s static attempt and go straight to headless render so
            # the seed prefetch fits inside its timeout.
            _disc_static = None
            if not _disc_render_first:
                log.info(
                    "fetch %s: discovery.scrape_do_skip_fallbacks=True"
                    " — going straight to Scrape.do static (skipping httpx/cffi)",
                    url,
                )
                # max_retries=0: this static leg is known-doomed on hosts that
                # reach this branch (Cloudflare Enterprise blocks it outright),
                # so don't burn the internal 2/8/30s backoff ladder chasing a
                # 502 that will never turn into a 200 — fail fast and spend
                # the discovery time budget on the render=True tier instead.
                _disc_static = await fetch_html_scrape_do(
                    url,
                    render=False,
                    super_mode=_disc_super,
                    rate_limit=False,
                    max_retries=0,
                )
            else:
                log.info(
                    "fetch %s: discovery.scrape_do_render=True"
                    " — going straight to Scrape.do headless render"
                    " (skipping httpx/cffi AND the doomed static attempt)",
                    url,
                )
            if _disc_static is not None and not _is_spa_shell(_disc_static):
                from app.services.scraper.snapshot_context import stage_snapshot as _stage
                _stage(url, _disc_static, "scrape_do_static")
                return _disc_static
            # Static failed (None/502) or returned an SPA shell. Before
            # falling through to plain httpx (which Cloudflare blocks
            # outright for these hosts), retry once with render=True.
            # Handoff: Ulster job_ec86dc5866cb, 2026-07-03 — the Ulster
            # sitemap URL only succeeds through Scrape.do's headless-browser
            # proxy pool (render=True); static (render=False, including
            # super=True) always 502s with `ROTATION_FAILED: cannot connect
            # target url`, which is a proxy-level failure and not a
            # Cloudflare challenge page, so retrying static harder never
            # helps. Without this retry, discovery silently degrades to
            # httpx -> also blocked -> 0 candidates, with no code path ever
            # trying the one fetch mode that actually works for this URL.
            log.info(
                "fetch %s: discovery scrape_do_skip_fallbacks static fast-path"
                " failed or SPA shell — retrying with render=True before httpx",
                url,
            )
            # max_retries=1: cap the render leg at 2 total attempts (not the
            # default 4). Observed render-tier failures on this host take
            # ~57-60s EACH to come back with a 502 (not a quick rejection),
            # so the default 2s/8s/30s/4-attempt ladder needs ~250s+ to
            # exhaust — far more than the 150s outer sitemap-probe budget
            # (_PROBE_TIMEOUT_S in sitemap.py) can ever provide, so every
            # probe timed out mid-ladder without the outer wait_for() ever
            # seeing a real success/failure result from a later attempt.
            # Two attempts (~60s + 2s backoff + ~60s = ~122s worst case)
            # reliably fit inside 150s and matches what that budget was
            # originally sized for.
            _disc_rendered = await fetch_html_scrape_do(
                url,
                render=True,
                super_mode=_disc_super,
                rate_limit=False,
                max_retries=1,
                wait_for_ms=_disc_wait_for_ms,
            )
            if _disc_rendered is not None and not _is_spa_shell(_disc_rendered):
                from app.services.scraper.snapshot_context import stage_snapshot as _stage
                _stage(url, _disc_rendered, "scrape_do_render")
                return _disc_rendered
            # Both Scrape.do modes failed. When scrape_do_skip_fallbacks=True
            # the caller explicitly told us to skip the httpx/cffi chain because
            # Cloudflare blocks all datacenter IPs on this host. Falling through
            # to httpx now would hang for 60-90 s before returning "" (httpx
            # timeout 30s + cffi timeout 30s) — consuming the 300s discovery
            # deadline. Return "" immediately so the BFS/sitemap loop moves on.
            # If this was a legitimate URL the subsequent browser/render tiers
            # will still discover it.
            log.warning(
                "fetch %s: discovery scrape_do_skip_fallbacks fast-path failed"
                " (static AND render) — returning empty immediately"
                " (skipping httpx/cffi per discovery.scrape_do_skip_fallbacks)",
                url,
            )
            return ""

    # Fast-path: if BOTH httpx and curl_cffi previously failed for this host
    # (recorded in _cf_always_scrape_do), skip straight to Scrape.do static
    # instead of wasting 15-30 s on tiers that always fail.  Saves ~35 s/course
    # for heavily CF-protected sites like Westminster.
    from urllib.parse import urlparse as _urlparse
    _host = _urlparse(url).netloc
    if _host in _cf_always_scrape_do and _has_scrape_do:
        log.info(
            "fetch %s: host %s known CF-always-blocked — going straight to Scrape.do static",
            url, _host,
        )
        _fast = await fetch_html_scrape_do(url, render=False)
        if _fast is not None and not _is_spa_shell(_fast):
            return _fast
        if _scrape_do_render:
            _fast_r = await fetch_html_scrape_do(url, render=True, wait_for_ms=wait_for_ms)
            return _fast_r
        return None

    last_exc: Exception | None = None
    last_status: int | None = None
    got_cloudflare_block = False
    cf_block_status: int | None = None
    got_hard_403 = False
    html_200: str | None = None  # track 200 result so we can check for SPA shell

    for attempt in range(retries + 1):
        async with _get_sem():
            try:
                async with _client() as c:
                    # Per-host session priming (currently only Curtin —
                    # see extractors/curtin_session.py for rationale).
                    # Returns {} for every other host so this is a true
                    # no-op for ~100 universities in the fleet.
                    r = await c.get(url, cookies=cookies_for_url(url))
                    last_status = r.status_code
                    if r.status_code == 200:
                        html_200 = r.text
                        break  # exit loop; post-loop logic decides what to return
                    if _is_cloudflare_block(r):
                        got_cloudflare_block = True
                        cf_block_status = r.status_code
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
            except httpx.TooManyRedirects as exc:
                # Permanent redirect loop at the origin server (e.g. A→B→A).
                # Retrying will produce the same loop — break immediately.
                # cffi/Wayback/Scrape.do are inside the `got_cloudflare_block`
                # branch and are NOT reached here, so the function returns None
                # at the tail without wasting further round-trips.
                last_exc = exc
                log.warning(
                    "fetch %s: TooManyRedirects — redirect loop at origin,"
                    " skipping retry ladder",
                    url,
                )
                break
            except Exception as exc:
                last_exc = exc
                log.warning("fetch %s attempt %s failed: %s", url, attempt, exc)
        await asyncio.sleep(0.5 * (attempt + 1))

    if got_hard_403:
        # Server explicitly rejected the request — no point trying cffi or
        # Wayback Machine. Return None so the pipeline records a fetch_failed.
        _record_fetch_error(
            url, status=403, tier="httpx",
            detail="hard 403 from origin (not Cloudflare) — retries skipped",
        )
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
            rendered = await fetch_html_scrape_do(url, render=True, wait_for_ms=wait_for_ms)
            if rendered is not None:
                return rendered
            log.info(
                "fetch %s: Scrape.do render failed — falling back to plain httpx 200 response",
                url,
            )
        from app.services.scraper.snapshot_context import stage_snapshot as _stage
        _stage(url, html_200, "httpx")
        return html_200

    # ── Cloudflare WAF block — tiered fallback ────────────────────────────────
    if got_cloudflare_block:
        # Rate-limit fast path (Kingston job_*, 2026-07-06): a 429 with
        # cf-ray/cf-mitigated headers is Cloudflare's WAF *rate limiter*
        # tripping, not a bot-detection challenge page. For hosts where
        # plain httpx/cffi normally succeeds fine (Kingston: "cffi bypasses
        # it successfully"), escalating straight into the full CF-block
        # ladder (cffi retry -> Wayback -> Scrape.do static -> Scrape.do
        # render) on every rate-limited page burns several extra seconds of
        # external round-trips per page for a problem those tiers cannot
        # actually fix — only a longer wait can. At Kingston's bfs_page_
        # budget=35, once page ~11 starts tripping 429s, every subsequent
        # page paying that full ladder's latency instead of a short backoff
        # is what exhausted the whole discovery_phase_timeout_s (300s)
        # budget with the crawl stuck deep in the queue. Give 429s a couple
        # of cheap same-tier backoff retries first; only fall through to the
        # heavier escalation ladder below if the rate limit hasn't cleared.
        if cf_block_status == 429:
            for _rl_attempt, _rl_backoff in enumerate((3.0, 8.0), start=1):
                log.info(
                    "fetch %s: 429 rate-limited — backoff retry %d/2 after %.0fs"
                    " before escalating to cffi/Wayback/Scrape.do",
                    url, _rl_attempt, _rl_backoff,
                )
                await asyncio.sleep(_rl_backoff)
                try:
                    async with _get_sem():
                        async with _client() as _rl_c:
                            _rl_r = await _rl_c.get(url, cookies=cookies_for_url(url))
                            if _rl_r.status_code == 200:
                                from app.services.scraper.snapshot_context import (
                                    stage_snapshot as _stage,
                                )
                                _stage(url, _rl_r.text, "httpx")
                                return _rl_r.text
                            if not _is_cloudflare_block(_rl_r):
                                break
                except Exception as _rl_exc:  # noqa: BLE001
                    log.warning(
                        "fetch %s: 429 backoff retry attempt %d failed: %s",
                        url, _rl_attempt, _rl_exc,
                    )

        # Tier 2: curl_cffi Chrome TLS impersonation
        cffi_result = await fetch_html_cffi(url)
        if cffi_result is not None:
            from app.services.scraper.snapshot_context import stage_snapshot as _stage
            _stage(url, cffi_result, "cffi")
            return cffi_result
        # Both httpx AND cffi failed → record host as always-CF-blocked so
        # future requests in this process skip both tiers immediately.
        from urllib.parse import urlparse as _urlparse2
        _blocked_host = _urlparse2(url).netloc
        if _blocked_host not in _cf_always_scrape_do:
            _cf_always_scrape_do.add(_blocked_host)
            log.info(
                "fetch %s: httpx+cffi both CF-blocked — caching host '%s' for Scrape.do fast-path",
                url, _blocked_host,
            )
        # Tier 2.5: Scrape.do render fast-path for explicit SPA universities.
        # When scrape_do_render=True the university is tagged as an Angular/
        # React SPA whose data is injected at runtime by JavaScript.  Wayback
        # Machine archives the *unrendered* SPA shell (same {{ }} template
        # literals, no fee/intake data) — going there first wastes a round-trip
        # and still returns blank fees.  Skip Wayback and go straight to
        # Scrape.do headless Chrome so JS executes and real values appear.
        if _scrape_do_render and _has_scrape_do:
            log.info(
                "fetch %s: curl_cffi blocked + scrape_do_render=True"
                " — skipping Wayback (SPA shell), using Scrape.do render",
                url,
            )
            scrape_do_rendered = await fetch_html_scrape_do(url, render=True)
            if scrape_do_rendered is not None:
                return scrape_do_rendered
            log.info(
                "fetch %s: Scrape.do render failed — falling back to Wayback Machine",
                url,
            )
        # Tier 3: Wayback Machine archived HTML (free, zero API cost) — unless
        # the per-university config explicitly disables it (use_wayback:
        # false). Kingston docs this explicitly: "archive.org has no useful
        # snapshots and adds latency for nothing". This per-request tier
        # previously ignored that flag entirely (it only gated the
        # orchestrator's separate discovery-wide Wayback CDX sweep), so every
        # rate-limited/blocked page still paid an archive.org round-trip that
        # the operator had already opted out of.
        _skip_wayback_tier = False
        try:
            from app.services.scraper.config.context import get_uni_config as _guc_wb
            _skip_wayback_tier = _guc_wb().discovery.use_wayback is False
        except Exception:  # noqa: BLE001
            pass
        if _skip_wayback_tier:
            log.info(
                "fetch %s: curl_cffi blocked — use_wayback=false, skipping"
                " Wayback tier",
                url,
            )
        else:
            log.info(
                "fetch %s: curl_cffi blocked — trying Wayback Machine archived HTML",
                url,
            )
            wayback_result = await fetch_html_wayback(url)
            if wayback_result is not None:
                from app.services.scraper.snapshot_context import stage_snapshot as _stage
                _stage(url, wayback_result, "wayback")
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
        _record_fetch_error(
            url, status=cf_block_status, tier="cf_ladder",
            detail="Cloudflare block — httpx/cffi/Wayback/Scrape.do all failed",
        )
        return None

    if last_exc:
        log.error("fetch %s exhausted retries: %s", url, last_exc)
        _record_fetch_error(
            url, status=None, tier="httpx",
            detail=f"exception: {last_exc}",
        )
    else:
        _record_fetch_error(
            url, status=last_status, tier="httpx",
            detail="non-200 responses on all attempts",
        )
    return None

"""Discover course URLs via the Internet Archive Wayback Machine CDX API.

When a university site actively blocks our crawler (Cloudflare, rate
limits, JS challenges that even Playwright cannot pass), we can still
discover *which URLs exist* on their domain by querying the Wayback
Machine's CDX API — a public, free, key-less index of ~700 billion
crawled pages that cannot block us because we are querying archive.org,
not the live site.

How it works
------------
1.  Parse the hostname from the university's ``scrape_url``.
2.  Query ``http://web.archive.org/cdx/search/cdx`` for all 200-status
    ``text/html`` URLs under that host, collapsed by ``urlkey`` so each
    canonical URL appears at most once.
3.  Apply the same ``_looks_like_course`` heuristics used by the BFS
    crawler to filter the ~thousands of returned URLs down to likely
    course-detail pages.
4.  Return the deduped ``[{"url": str, "name": str}]`` list.  ``name``
    is always ``""`` because CDX does not store page titles or anchor
    text — the per-course extractor fills it in later.

The returned URLs point to the *live* site (CDX stores original URLs),
so downstream extraction still needs to handle Cloudflare on a
per-course basis via the browser pool.  This module solves the
*discovery* problem only.

Limits
------
* CDX cap: ``_CDX_MAX_RESULTS`` (10 000) records per request to avoid
  multi-MB payloads on large university sites.
* ``max_courses`` caps the output list returned to the orchestrator.
* On any network failure the function returns ``[]`` so the caller can
  fall back to the hard-fail error path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_CDX_URL = "http://web.archive.org/cdx/search/cdx"
_CDX_TIMEOUT = 45
_CDX_MAX_RESULTS = 10_000

# CDX is hosted on web.archive.org, which periodically returns 503 / 429
# under load (the IA infrastructure is famously over-subscribed).  A single
# hit is enough to wipe out a whole scrape's discovery merge — observed
# 2026-05-15 on QUT, where the orchestrator correctly fired
# ``use_wayback=True`` but the single CDX request returned 503 and the
# merge added 0 URLs, leaving QUT stuck at 56 candidates.
#
# The retry budget is intentionally small (3 attempts, max ~7s of backoff)
# because the CDX call sits inline in the discovery phase; longer waits
# would be visible to the operator as "scrape stuck on Wayback".  Short,
# bounded retries handle the common transient case (one bad load-balancer
# slot) without blowing the discovery time budget.
_CDX_MAX_ATTEMPTS = 3
_CDX_RETRY_BACKOFF_SECONDS = (1.0, 3.0)  # waits between attempts 1→2 and 2→3
_CDX_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Per-host CDX URL prefix overrides.
#
# Problem: CDX returns results sorted by urlkey (SURT order).  When a
# university has thousands of non-course URLs that sort *before* the
# course paths (e.g. UTAS has masses of /__data/assets/pdf_file/...
# URLs — underscore ASCII 95 sorts before 'a' ASCII 97, so they appear
# first in SURT order and exhaust the 10 000-row CDX limit before any
# /courses/ path is reached), the course pages are never returned.
#
# Fix: map the hostname to a more targeted CDX prefix so the query
# returns only URLs under the course subtree, then let the existing
# _looks_like_course filter clean up any remaining non-course pages.
# The "*" wildcard at the END of the url param is supported by the CDX
# API; wildcards in the middle are not, hence the two-level prefix for
# UTAS (/courses/*) which covers all faculty sub-trees at once.
_HOST_CDX_URL_PREFIX: dict[str, str] = {
    # UTAS: real course pages are at /courses/<faculty>/courses/<slug>.
    # The top-level wildcard hits ~10 000 /__data/assets/pdf_file/...
    # URLs first (SURT: '/_' < '/a'), leaving 0 slots for course pages.
    # Targeting /courses/* returns only the courses subtree; category
    # landings (/courses/postgraduate, /courses/arts-soc/courses, …)
    # are correctly rejected by _is_category_landing() downstream.
    "www.utas.edu.au": "www.utas.edu.au/courses/*",
    "utas.edu.au": "www.utas.edu.au/courses/*",
    # QUT: course pages live at /courses/<slug>.  The site is fully
    # Cloudflare-protected (HTTP 403 for plain BFS, sitemap.xml does
    # not exist) and the /study + /courses listing pages are pure JS
    # SPA shells with zero anchor-based course links in the rendered
    # DOM, so Playwright nav-following only finds ~56 courses out of
    # the full ~150-200 catalogue.  Wayback CDX recovers the missing
    # ~75 % of /courses/<slug> URLs that the live site cannot enumerate.
    "www.qut.edu.au": "www.qut.edu.au/courses/*",
    "qut.edu.au": "www.qut.edu.au/courses/*",
    # Notre Dame Australia: course pages live at /programs/<school>/<level>/<slug>
    # (confirmed real URL: /programs/arts-and-sciences/postgraduate/master-of-architecture).
    # The site is fully Cloudflare Enterprise Bot Management protected (HTTP 403 for
    # plain httpx, curl_cffi, and patchright+Xvfb — IP-level block from datacenter).
    # Wayback CDX with the top-level wildcard returns 10 000 mixed URLs sorted in
    # SURT order — paths like /about/, /careers/, /contact/ sort before /programs/
    # and exhaust the 10 000-row limit, leaving 0 slots for course pages.
    # Targeting /programs/* returns only the course subtree; faculty-level landings
    # (/programs/arts-and-sciences/) are rejected by _is_category_landing() downstream.
    "www.notredame.edu.au": "www.notredame.edu.au/programs/*",
    "notredame.edu.au": "www.notredame.edu.au/programs/*",
}


# Degree-level slug prefixes used by _interleave_by_degree_level to bucket
# course-like URLs before round-robin truncation.  Order is irrelevant —
# every bucket is sampled in lockstep — but the prefixes are deliberately
# the most common slug roots used by Australian university course
# catalogues so the bucketing covers ~95 % of real /courses/<slug> URLs.
# Anything that does not match any prefix lands in the "other" bucket
# (e.g. "phd-...", "research-degrees-...", "honours-...", and any custom
# vocabulary like UTAS "associate-degree-...").
_DEGREE_LEVEL_PREFIXES: tuple[str, ...] = (
    "bachelor-",
    "master-",
    "graduate-diploma-",
    "graduate-certificate-",
    "doctor-",
    "diploma-",
    "certificate-",
    "associate-",
    "undergraduate-",
    "postgraduate-",
)


def _degree_bucket(url: str) -> str:
    """Return the degree-level slug-prefix bucket for *url*.

    The bucket key is taken from the last path segment after ``/courses/``
    (or the last non-empty segment) so query strings and trailing slashes
    do not influence bucketing.  URLs whose final segment does not start
    with any known prefix go into the ``"other"`` bucket.
    """
    from urllib.parse import urlsplit

    try:
        path = urlsplit(url).path
    except Exception:
        return "other"
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "other"
    slug = segs[-1].lower()
    for prefix in _DEGREE_LEVEL_PREFIXES:
        if slug.startswith(prefix):
            return prefix
    return "other"


def _interleave_by_degree_level(
    results: list[dict], max_courses: int
) -> list[dict]:
    """Round-robin interleave *results* by degree-level slug bucket then cap.

    Preserves CDX ordering *within* each bucket (so the most-recent or
    canonical URL for each course is taken first) while ensuring no single
    bucket can monopolise the ``max_courses`` cap.  When ``len(results) <=
    max_courses``, the cap is a no-op and the input order is preserved
    exactly (no buckets are constructed, no order changes).

    This is the discovery-side fix for Bug 3 (2026-05-15 QUT scrape):
    bachelor-* slugs (140+ on QUT) sort before master-* slugs (b < m), so
    a straight take-first-N truncation at the 200/300 cap dropped every
    Master course from the staged catalogue.
    """
    if max_courses <= 0 or len(results) <= max_courses:
        return list(results[:max_courses]) if max_courses > 0 else []
    buckets: dict[str, list[dict]] = {}
    bucket_order: list[str] = []  # preserve first-seen order for determinism
    for r in results:
        key = _degree_bucket(r.get("url", ""))
        if key not in buckets:
            buckets[key] = []
            bucket_order.append(key)
        buckets[key].append(r)
    out: list[dict] = []
    idx = 0
    while len(out) < max_courses:
        progressed = False
        for key in bucket_order:
            bucket = buckets[key]
            if idx < len(bucket):
                out.append(bucket[idx])
                progressed = True
                if len(out) >= max_courses:
                    break
        if not progressed:
            break
        idx += 1
    return out


def _normalise_wayback_url(url: str) -> str:
    """Strip noise that bloats the dedup set on Wayback CDX results.

    The CDX index returns the URL as it was originally crawled, which means:
      - http:// and https:// variants of the same path appear as separate
        rows;
      - legacy ``:80`` ports survive (e.g. ``http://www.qut.edu.au:80/courses/``);
      - audience-toggle query strings (``?domestic``, ``?international``)
        explode each canonical course URL into 2-3 rows.

    Without normalisation, QUT's CDX returns the same Bachelor of Architectural
    Design course as four separate rows (http+:80 / https / https?domestic /
    https?international), eating four slots out of the 10 000-row CDX cap and
    pushing the dedup set well past the post-filter ``max_courses`` cap.

    This helper rewrites every URL to the canonical ``https://<host><path>``
    form before insertion into the dedup set so each course is counted once.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except Exception:
        return url
    netloc = parts.netloc
    if ":" in netloc:
        host, _, port = netloc.partition(":")
        if port in ("80", "443", ""):
            netloc = host
    return urlunsplit(("https", netloc, parts.path, "", ""))


async def wayback_discover(
    scrape_url: str,
    *,
    max_courses: int = 300,
    emit=None,
) -> list[dict]:
    """Query the Wayback Machine CDX API for course URLs on the given domain.

    Returns a list of ``{"url": str, "name": str}`` dicts (``name`` is
    always ``""``).  Returns ``[]`` on any failure.
    """

    async def _emit(msg: str, **kw) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover", kind="wayback_discover", **kw)
            except Exception:
                pass

    parsed = urlparse(scrape_url)
    host = parsed.hostname
    if not host:
        log.warning("wayback_discover: cannot parse hostname from %s", scrape_url)
        return []

    cdx_prefix = _HOST_CDX_URL_PREFIX.get(host, f"{host}/*")
    await _emit(f"[DISCOVER] Wayback: querying CDX index for {cdx_prefix} (this may take ~10s)")
    log.info("wayback_discover: querying CDX for %s (prefix=%s)", host, cdx_prefix)

    # Collapse by urlkey so each canonical URL appears at most once.
    # We skip the mimetype filter because some universities serve their
    # HTML pages with non-standard content types — the _looks_like_course
    # heuristic filters to HTML-shaped URLs in Python instead.
    params = {
        "url": cdx_prefix,
        "output": "json",
        "fl": "original,timestamp",   # timestamp threaded to fetch_html_wayback cache
        "collapse": "urlkey",
        "filter": "statuscode:200",
        "limit": str(_CDX_MAX_RESULTS),
    }

    raw_text: str | None = None
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_CDX_TIMEOUT, follow_redirects=True) as client:
        for _attempt in range(1, _CDX_MAX_ATTEMPTS + 1):
            try:
                r = await client.get(_CDX_URL, params=params)
                # Retry transient server-side errors / rate limits but
                # raise immediately on permanent failures (4xx other than
                # 429) so we don't burn the budget on a hopeless URL.
                if r.status_code in _CDX_RETRY_STATUSES:
                    raise httpx.HTTPStatusError(
                        f"CDX returned HTTP {r.status_code} (transient)",
                        request=r.request, response=r,
                    )
                r.raise_for_status()
                raw_text = r.text
                break
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if _attempt >= _CDX_MAX_ATTEMPTS:
                    break
                _wait = _CDX_RETRY_BACKOFF_SECONDS[_attempt - 1]
                log.info(
                    "wayback_discover: CDX attempt %d/%d failed (%s) — retrying in %.1fs",
                    _attempt, _CDX_MAX_ATTEMPTS, exc, _wait,
                )
                await _emit(
                    f"[DISCOVER] Wayback: CDX attempt {_attempt}/{_CDX_MAX_ATTEMPTS} "
                    f"failed ({exc}) — retrying in {_wait:.0f}s"
                )
                await asyncio.sleep(_wait)
            except Exception as exc:  # noqa: BLE001 — non-retryable
                last_exc = exc
                break

    if raw_text is None:
        log.warning("wayback_discover: CDX request failed after %d attempts — %s",
                    _CDX_MAX_ATTEMPTS, last_exc)
        await _emit(
            f"[DISCOVER] Wayback: CDX request failed after {_CDX_MAX_ATTEMPTS} "
            f"attempts — {last_exc}"
        )
        return []

    try:
        rows = json.loads(raw_text)
    except Exception as exc:
        log.warning("wayback_discover: CDX JSON parse failed — %s", exc)
        await _emit("[DISCOVER] Wayback: CDX response was not valid JSON")
        return []

    # CDX returns [["original"], [url1], [url2], ...]  (first row = header)
    if not rows or len(rows) < 2:
        await _emit("[DISCOVER] Wayback: CDX returned no URLs for this domain")
        log.info("wayback_discover: CDX returned no URLs for %s", host)
        return []

    total_urls = len(rows) - 1
    await _emit(
        f"[DISCOVER] Wayback: CDX returned {total_urls} URLs — "
        "filtering for course pages..."
    )
    log.info("wayback_discover: CDX returned %d URLs for %s", total_urls, host)

    try:
        from app.services.scraper.discovery import _looks_like_course
    except Exception as exc:
        log.warning("wayback_discover: cannot import _looks_like_course — %s", exc)
        return []

    # Collect ALL course-like URLs first (no max_courses cap), then bucket
    # by degree-level slug prefix and round-robin interleave so the
    # ``max_courses`` truncation does not silently drop entire degree levels.
    #
    # Why: the CDX API returns URLs sorted in SURT (Sort-friendly URI
    # Reordering Transform) order, which for QUT means alphabetical by path.
    # The full /courses/<slug> catalogue has ~140 ``bachelor-of-...``
    # entries before the first ``master-of-...`` entry (b < m).  With a
    # straight take-first-N truncation at ``max_courses=300`` the bachelor
    # URLs alone could exhaust the cap, leaving zero master / graduate /
    # diploma courses staged.  Observed 2026-05-15 on QUT: 200 of 200
    # discovered URLs were bachelor-* slugs; every Master course was missing
    # from the staged catalogue.
    seen: set[str] = set()
    all_results: list[dict] = []
    # url → CDX timestamp for all 200-OK rows (used by fetch_html_wayback to
    # skip the Availability API — see http_fetcher.set_wayback_timestamps).
    ts_map: dict[str, str] = {}

    for row in rows[1:]:
        if not row:
            continue
        raw_url = row[0] if len(row) > 0 else None
        timestamp = row[1] if len(row) > 1 else ""
        if not raw_url:
            continue
        norm_url = _normalise_wayback_url(raw_url)
        # Keep the most-recent timestamp when the same URL appears more than
        # once (should be rare after collapse=urlkey, but defensive).
        if norm_url not in ts_map or (timestamp and timestamp > ts_map[norm_url]):
            ts_map[norm_url] = timestamp or ""
        if norm_url in seen:
            continue
        seen.add(norm_url)
        if _looks_like_course(norm_url, ""):
            all_results.append({"url": norm_url, "name": ""})

    # Thread the CDX timestamps into the http_fetcher cache so extraction
    # can fetch directly by timestamp without a round-trip to the
    # Availability API (which has a known inconsistency for old snapshots).
    try:
        from app.services.scraper.http_fetcher import set_wayback_timestamps
        set_wayback_timestamps(ts_map)
        log.info(
            "wayback_discover: populated Wayback timestamp cache with %d URLs for %s",
            len(ts_map), host,
        )
    except Exception as _cache_exc:
        log.warning("wayback_discover: could not populate timestamp cache: %s", _cache_exc)

    results = _interleave_by_degree_level(all_results, max_courses)

    log.info(
        "wayback_discover: %d course URLs found for %s (from %d total CDX URLs, "
        "%d course-like before degree-level interleave / %d cap)",
        len(results), host, total_urls, len(all_results), max_courses,
    )
    await _emit(
        f"[DISCOVER] Wayback: found {len(results)} course-like URLs "
        f"(from {total_urls} total in CDX index)"
    )
    return results

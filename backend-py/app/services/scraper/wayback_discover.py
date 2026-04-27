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

import json
import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_CDX_URL = "http://web.archive.org/cdx/search/cdx"
_CDX_TIMEOUT = 45
_CDX_MAX_RESULTS = 10_000


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

    await _emit(f"[DISCOVER] Wayback: querying CDX index for {host}/* (this may take ~10s)")
    log.info("wayback_discover: querying CDX for %s", host)

    # Collapse by urlkey so each canonical URL appears at most once.
    # We skip the mimetype filter because some universities serve their
    # HTML pages with non-standard content types — the _looks_like_course
    # heuristic filters to HTML-shaped URLs in Python instead.
    params = {
        "url": f"{host}/*",
        "output": "json",
        "fl": "original",
        "collapse": "urlkey",
        "filter": "statuscode:200",
        "limit": str(_CDX_MAX_RESULTS),
    }

    try:
        async with httpx.AsyncClient(timeout=_CDX_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(_CDX_URL, params=params)
            r.raise_for_status()
            raw_text = r.text
    except Exception as exc:
        log.warning("wayback_discover: CDX request failed — %s", exc)
        await _emit(f"[DISCOVER] Wayback: CDX request failed — {exc}")
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

    seen: set[str] = set()
    results: list[dict] = []

    for row in rows[1:]:
        if not row:
            continue
        url = row[0]
        if not url or url in seen:
            continue
        seen.add(url)
        if _looks_like_course(url, ""):
            results.append({"url": url, "name": ""})
            if len(results) >= max_courses:
                break

    log.info(
        "wayback_discover: %d course URLs found for %s (from %d total CDX URLs)",
        len(results), host, total_urls,
    )
    await _emit(
        f"[DISCOVER] Wayback: found {len(results)} course-like URLs "
        f"(from {total_urls} total in CDX index)"
    )
    return results

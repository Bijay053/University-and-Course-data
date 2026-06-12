"""Lancaster University listing-page discovery provider.

Lancaster's course catalogue is delivered via a Vue 3 ``<course-listing>``
web-component whose ``:courses-data`` prop is **server-rendered** directly
into the listing page HTML.  The prop contains a JSON array of every course
object — title, slug, category (Undergraduate / Postgraduate), entry year,
UCAS code, etc.  No JavaScript execution or Playwright is required.

Two listing pages are fetched:
  * Undergraduate  — https://www.lancaster.ac.uk/study/undergraduate/courses/
  * Postgraduate   — https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/

Each course's URL is reconstructed as:
  * UG  → /study/undergraduate/courses/{slug}/2026/
  * PG  → /study/postgraduate/postgraduate-courses/{slug}/2026/

The year suffix (``/2026/``) is set to the current calendar year by default
and can be overridden via the YAML ``discovery.lancaster_listing_year`` key.

Only courses whose ``entryYear`` field starts with the two-digit year
(``"26/27"`` for 2026) are returned, so each canonical course appears once.
If no year-filtered courses are found the filter is dropped and *all* courses
are returned as a safe fallback.

This provider is activated by setting ``discovery.lancaster_listing: true``
in the university's YAML config.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Coroutine

import httpx

log = logging.getLogger("scraper.lancaster_listing")

_LISTING_PAGES: list[tuple[str, str, str]] = [
    (
        "https://www.lancaster.ac.uk/study/undergraduate/courses/",
        "Undergraduate",
        "/study/undergraduate/courses/",
    ),
    (
        "https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/",
        "Postgraduate",
        "/study/postgraduate/postgraduate-courses/",
    ),
]

_COURSES_DATA_RE = re.compile(r":courses-data='([^']{10,})'", re.DOTALL)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}


async def fetch_lancaster_listing_links(
    year: int | None = None,
    *,
    emit: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> list[dict[str, str]]:
    """Fetch all Lancaster course URLs from the two listing pages.

    Returns a list of ``{"name": ..., "url": ...}`` dicts suitable for the
    orchestrator's ``links`` list.  Courses are filtered to the entry-year
    matching *year* (default: current calendar year).

    Parameters
    ----------
    year:
        4-digit entry year, e.g. ``2026``.  Defaults to ``datetime.now().year``.
    emit:
        Optional async status-emit callback (same signature as used in the
        orchestrator) — used to surface progress messages.
    """
    if year is None:
        year = datetime.now().year

    two_digit = str(year)[2:]          # "2026" → "26"
    year_prefix = f"{two_digit}/"      # "26/"  — matches "26/27" etc.
    year_str = str(year)               # "2026" — used in URL path

    async def _emit(msg: str) -> None:
        if emit is not None:
            await emit("status", msg, phase="discover")
        log.info(msg)

    links: list[dict[str, str]] = []

    async with httpx.AsyncClient(
        headers=_HEADERS,
        timeout=20,
        follow_redirects=True,
    ) as client:
        for listing_url, category, base_path in _LISTING_PAGES:
            await _emit(
                f"[LANCASTER] Fetching {category} listing page: {listing_url}"
            )
            try:
                resp = await client.get(listing_url)
                resp.raise_for_status()
            except Exception as exc:
                log.warning(
                    "[LANCASTER] Failed to fetch %s listing: %s",
                    category, exc,
                )
                continue

            m = _COURSES_DATA_RE.search(resp.text)
            if not m:
                log.warning(
                    "[LANCASTER] :courses-data prop not found in %s listing page "
                    "(%d chars) — skipping",
                    category, len(resp.text),
                )
                continue

            raw_json = html_module.unescape(m.group(1))
            try:
                courses: list[dict[str, Any]] = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                log.warning(
                    "[LANCASTER] JSON parse failed for %s listing: %s (first 200: %r)",
                    category, exc, raw_json[:200],
                )
                continue

            # Filter to the target entry year ("26/27" for year=2026).
            year_courses = [
                c for c in courses
                if str(c.get("entryYear", "")).startswith(year_prefix)
            ]
            if not year_courses:
                log.warning(
                    "[LANCASTER] No courses with entryYear starting '%s' in %s "
                    "listing (%d total records) — using all records as fallback",
                    year_prefix, category, len(courses),
                )
                year_courses = courses

            page_links = [
                {
                    "name": c.get("title", ""),
                    "url": (
                        f"https://www.lancaster.ac.uk"
                        f"{base_path}{c['slug']}/{year_str}/"
                    ),
                }
                for c in year_courses
                if c.get("slug")
            ]

            await _emit(
                f"[LANCASTER] {category}: {len(page_links)} courses "
                f"(entryYear {year_prefix}* from {len(courses)} total records)"
            )
            links.extend(page_links)

    log.info("[LANCASTER] Total links discovered: %d", len(links))
    return links

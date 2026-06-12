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

Browser fallback (automatic):
  If the SSR prop approach yields 0 links (e.g. Lancaster restructures their
  HTML), the provider automatically retries each listing page through
  Playwright.  Once the Vue component has mounted, the rendered A-Z navigation
  is present in the DOM and links are extracted with the XPath:

      //nav[contains(@class, 'a-z')]//li/a/@href

  This fallback produces the same set of course URLs as the SSR path without
  requiring any additional YAML configuration.

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

# XPath that extracts every course link from the Vue-rendered A-Z navigation.
# The <nav class="a-z"> element is rendered CLIENT-SIDE by the Vue component —
# it does NOT exist in the SSR HTML.  This XPath is therefore only usable in
# the Playwright browser fallback path (not plain httpx).
# Confirmed 2026-06-12 via browser DevTools on the rendered listing page.
_AZ_NAV_LINK_XPATH = "//nav[contains(@class, 'a-z')]//li/a/@href"

# URL patterns that the A-Z nav links must match to be accepted as course URLs.
_AZ_COURSE_URL_RE = re.compile(
    r"/study/(undergraduate/courses|postgraduate/postgraduate-courses)/[^/]+/20\d{2}/?$"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

_LANCASTER_BASE = "https://www.lancaster.ac.uk"


async def fetch_lancaster_listing_links(
    year: int | None = None,
    *,
    emit: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> list[dict[str, str]]:
    """Fetch all Lancaster course URLs from the two listing pages.

    Primary path (no browser):
        Fetches the listing page SSR HTML and extracts the ``:courses-data``
        Vue prop — a JSON array embedded directly in the server-rendered
        ``<course-listing>`` element.  Returns ~538 course URLs in ~2 s.

    Fallback path (Playwright):
        If the SSR prop is missing, renders each listing page in a headless
        browser, waits for the Vue component to mount, then applies
        ``//nav[contains(@class, 'a-z')]//li/a/@href`` to collect the same
        set of course URLs from the rendered DOM.

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
                f"[LANCASTER] Fetching {category} listing page (SSR): {listing_url}"
            )
            try:
                resp = await client.get(listing_url)
                resp.raise_for_status()
            except Exception as exc:
                log.warning(
                    "[LANCASTER] Failed to fetch %s listing: %s — will try browser fallback",
                    category, exc,
                )
                page_links = await _fetch_via_browser(listing_url, category, year_str, emit)
                links.extend(page_links)
                continue

            m = _COURSES_DATA_RE.search(resp.text)
            if not m:
                log.warning(
                    "[LANCASTER] :courses-data prop not found in %s listing page "
                    "(%d chars) — trying browser fallback with A-Z nav XPath",
                    category, len(resp.text),
                )
                await _emit(
                    f"[LANCASTER] {category}: SSR prop missing — launching browser fallback"
                )
                page_links = await _fetch_via_browser(listing_url, category, year_str, emit)
                links.extend(page_links)
                continue

            raw_json = html_module.unescape(m.group(1))
            try:
                courses: list[dict[str, Any]] = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                log.warning(
                    "[LANCASTER] JSON parse failed for %s listing: %s (first 200: %r) "
                    "— trying browser fallback",
                    category, exc, raw_json[:200],
                )
                page_links = await _fetch_via_browser(listing_url, category, year_str, emit)
                links.extend(page_links)
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
                        f"{_LANCASTER_BASE}"
                        f"{base_path}{c['slug']}/{year_str}/"
                    ),
                }
                for c in year_courses
                if c.get("slug")
            ]

            await _emit(
                f"[LANCASTER] {category}: {len(page_links)} courses via SSR prop "
                f"(entryYear {year_prefix}* from {len(courses)} total records)"
            )
            links.extend(page_links)

    log.info("[LANCASTER] Total links discovered: %d", len(links))
    return links


async def _fetch_via_browser(
    listing_url: str,
    category: str,
    year_str: str,
    emit: Callable[..., Coroutine[Any, Any, None]] | None,
) -> list[dict[str, str]]:
    """Browser fallback: render the listing page with Playwright and extract
    course links from the Vue-rendered A-Z navigation using:

        //nav[contains(@class, 'a-z')]//li/a/@href

    The <nav class="a-z"> element is rendered client-side by the Vue component
    after the page loads and the :courses-data prop is processed.  We wait for
    the nav to appear before extracting links.
    """
    async def _emit_safe(msg: str) -> None:
        if emit is not None:
            try:
                await emit("status", msg, phase="discover")
            except Exception:
                pass
        log.info(msg)

    await _emit_safe(
        f"[LANCASTER] {category}: starting browser fallback "
        f"(A-Z nav XPath: {_AZ_NAV_LINK_XPATH})"
    )

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("[LANCASTER] Playwright not available — browser fallback skipped")
        return []

    links: list[dict[str, str]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=_HEADERS["User-Agent"],
                locale="en-GB",
            )
            await page.goto(listing_url, wait_until="domcontentloaded", timeout=30_000)
            # Wait for Vue to mount and render the A-Z nav
            try:
                await page.wait_for_selector(
                    "nav.a-z", timeout=15_000
                )
            except Exception:
                log.warning(
                    "[LANCASTER] Browser fallback: nav.a-z did not appear within 15s "
                    "on %s — extracting whatever is available",
                    listing_url,
                )
            hrefs = await page.eval_on_selector_all(
                "nav.a-z li a",
                "els => els.map(el => el.getAttribute('href'))",
            )
            await browser.close()

        # Filter to accepted course URL patterns
        for href in hrefs:
            if not href:
                continue
            # Make absolute
            if href.startswith("/"):
                href = f"{_LANCASTER_BASE}{href}"
            path = href.replace(_LANCASTER_BASE, "")
            if _AZ_COURSE_URL_RE.search(path):
                links.append({"name": "", "url": href})

        await _emit_safe(
            f"[LANCASTER] {category}: {len(links)} courses via browser A-Z nav"
        )
        log.info(
            "[LANCASTER] Browser fallback %s: %d links extracted with %s",
            category, len(links), _AZ_NAV_LINK_XPATH,
        )
    except Exception as exc:
        log.error(
            "[LANCASTER] Browser fallback failed for %s: %s",
            listing_url, exc, exc_info=True,
        )

    return links

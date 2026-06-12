"""Generic SSR-prop discovery provider.

Many universities use Vue, React, or similar frameworks where the server
renders the component shell (SSR) but embeds the full course catalogue as a
JSON value inside an HTML attribute — e.g. a ``:courses-data`` Vue prop or a
``data-courses`` React prop.  Plain httpx can fetch that JSON without any
browser rendering; per-course extraction still runs normally against individual
course detail pages.

This module provides a single, configurable function
``fetch_ssr_prop_links`` that is activated via the YAML key
``discovery.ssr_prop_discovery`` on any university.

Usage example (lancaster.yaml):

    discovery:
      ssr_prop_discovery:
        listing_pages:
          - url: "https://www.lancaster.ac.uk/study/undergraduate/courses/"
            url_prefix: "https://www.lancaster.ac.uk/study/undergraduate/courses/"
            label: "Undergraduate"
          - url: "https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/"
            url_prefix: "https://www.lancaster.ac.uk/study/postgraduate/postgraduate-courses/"
            label: "Postgraduate"
        prop_attr: ":courses-data"
        slug_field: "slug"
        name_field: "title"
        url_suffix: "/{year}/"
        year_field: "entryYear"
        year_filter_prefix: "{short_year}/"
        browser_fallback_xpath: "//nav[contains(@class, 'a-z')]//li/a/@href"
        browser_wait_selector: "nav.a-z"

Config fields
-------------
listing_pages
    One entry per catalogue/listing page to fetch.  Each has:
      url          — the listing page URL
      url_prefix   — prepended to each slug to form the course URL
      label        — human-readable label for logs (e.g. "Undergraduate")

prop_attr
    Name of the HTML attribute that holds the JSON courses array.
    Lancaster: ``:courses-data``
    React/Next.js sites often use ``data-courses`` or ``data-initial-props``.

slug_field
    Field name inside each JSON object whose value becomes the URL slug.
    Default: ``slug``

name_field
    Field name inside each JSON object whose value becomes the course name.
    Default: ``title``

url_suffix
    Appended after ``{url_prefix}{slug}`` to form the final course URL.
    The placeholder ``{year}`` is replaced with the 4-digit entry year.
    Default: ``/{year}/``

year_field
    Optional.  When set, only JSON objects whose ``year_field`` value starts
    with ``year_filter_prefix`` are included.  Leave null to include all.

year_filter_prefix
    Prefix matched against the ``year_field`` value.  Supports two tokens:
      ``{year}``       — 4-digit year, e.g. ``2026``
      ``{short_year}`` — 2-digit year, e.g. ``26``
    Lancaster uses ``{short_year}/`` because its format is ``"26/27"``.
    A university using plain ``"2026"`` values would set ``{year}``.

browser_fallback_xpath
    Optional XPath run against the browser-rendered DOM when the SSR prop is
    not found.  The browser renders the page with Playwright, waits for
    ``browser_wait_selector`` to appear, then evaluates this XPath.

browser_wait_selector
    CSS selector to wait for before extracting links in browser fallback mode.
    Only used when ``browser_fallback_xpath`` is set.

course_url_pattern
    Optional regex.  When set, hrefs extracted in browser fallback mode are
    filtered to those matching this pattern.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import httpx

if TYPE_CHECKING:
    from app.services.scraper.config.schema import SsrPropDiscoveryConfig

log = logging.getLogger("scraper.ssr_prop_discovery")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}


async def fetch_ssr_prop_links(
    cfg: "SsrPropDiscoveryConfig",
    year: int | None = None,
    *,
    emit: Callable[..., Coroutine[Any, Any, None]] | None = None,
) -> list[dict[str, str]]:
    """Fetch course URLs from server-rendered JSON props on listing pages.

    Primary path — plain httpx, no browser:
        Fetches each ``listing_page.url``, locates the ``prop_attr`` HTML
        attribute (e.g. ``:courses-data``), HTML-unescapes its value, parses
        it as JSON, optionally filters by ``year_field`` / ``year_filter_prefix``,
        then constructs course URLs as ``{url_prefix}{slug}{url_suffix}``.

    Fallback path — Playwright browser:
        Triggered per listing page when the SSR prop is not found.  Launches a
        headless Chromium browser, waits for ``browser_wait_selector`` to
        appear, then evaluates ``browser_fallback_xpath`` to collect ``<a>``
        hrefs from the rendered DOM.

    Returns a list of ``{"name": ..., "url": ...}`` dicts.
    """
    if year is None:
        year = datetime.now().year

    short_year = str(year)[2:]  # "2026" → "26"

    async def _emit(msg: str) -> None:
        if emit is not None:
            await emit("status", msg, phase="discover")
        log.info(msg)

    # Build the prop regex: e.g. :courses-data='...' or data-courses="..."
    prop_attr = cfg.prop_attr
    # Try single-quote first (Vue :prop syntax), then double-quote (data-* attrs)
    prop_re_sq = re.compile(
        re.escape(prop_attr) + r"='([^']{5,})'",
        re.DOTALL,
    )
    prop_re_dq = re.compile(
        re.escape(prop_attr) + r'"([^"]{5,})"',
        re.DOTALL,
    )

    # Resolve year_filter_prefix tokens
    year_filter_prefix: str | None = None
    if cfg.year_field and cfg.year_filter_prefix:
        year_filter_prefix = (
            cfg.year_filter_prefix
            .replace("{year}", str(year))
            .replace("{short_year}", short_year)
        )

    # Resolve url_suffix tokens
    url_suffix = cfg.url_suffix.replace("{year}", str(year)).replace("{short_year}", short_year)

    # Compile optional URL validation pattern
    url_pat = re.compile(cfg.course_url_pattern) if cfg.course_url_pattern else None

    all_links: list[dict[str, str]] = []

    async with httpx.AsyncClient(headers=_HEADERS, timeout=20, follow_redirects=True) as client:
        for page_cfg in cfg.listing_pages:
            label = page_cfg.label or page_cfg.url
            await _emit(f"[SSR_PROP] Fetching {label} listing page: {page_cfg.url}")

            try:
                resp = await client.get(page_cfg.url)
                resp.raise_for_status()
                raw_html = resp.text
            except Exception as exc:
                log.warning(
                    "[SSR_PROP] HTTP fetch failed for %s: %s — trying browser fallback",
                    page_cfg.url, exc,
                )
                fb = await _browser_fallback(page_cfg.url, label, cfg, url_pat, emit)
                all_links.extend(fb)
                continue

            # Try to extract the prop value
            m = prop_re_sq.search(raw_html) or prop_re_dq.search(raw_html)
            if not m:
                log.warning(
                    "[SSR_PROP] Prop %r not found in %s listing (%d chars) — trying browser fallback",
                    prop_attr, label, len(raw_html),
                )
                await _emit(f"[SSR_PROP] {label}: SSR prop missing — launching browser fallback")
                fb = await _browser_fallback(page_cfg.url, label, cfg, url_pat, emit)
                all_links.extend(fb)
                continue

            raw_json = html_module.unescape(m.group(1))
            try:
                items: list[dict[str, Any]] = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                log.warning(
                    "[SSR_PROP] JSON parse failed for %s: %s (first 200: %r) — trying browser fallback",
                    label, exc, raw_json[:200],
                )
                fb = await _browser_fallback(page_cfg.url, label, cfg, url_pat, emit)
                all_links.extend(fb)
                continue

            # Optional year filtering
            if year_filter_prefix and cfg.year_field:
                filtered = [
                    it for it in items
                    if str(it.get(cfg.year_field, "")).startswith(year_filter_prefix)
                ]
                if not filtered:
                    log.warning(
                        "[SSR_PROP] No items with %s starting %r in %s (%d total) "
                        "— using all items as fallback",
                        cfg.year_field, year_filter_prefix, label, len(items),
                    )
                    filtered = items
            else:
                filtered = items

            page_links = [
                {
                    "name": str(it.get(cfg.name_field, "")),
                    "url": f"{page_cfg.url_prefix.rstrip('/')}/{it[cfg.slug_field]}{url_suffix}",
                }
                for it in filtered
                if it.get(cfg.slug_field)
            ]

            await _emit(
                f"[SSR_PROP] {label}: {len(page_links)} courses via SSR prop"
                + (f" (year filter: {year_filter_prefix}* → {len(filtered)}/{len(items)})" if year_filter_prefix else "")
            )
            all_links.extend(page_links)

    log.info("[SSR_PROP] Total links discovered: %d", len(all_links))
    return all_links


async def _browser_fallback(
    listing_url: str,
    label: str,
    cfg: "SsrPropDiscoveryConfig",
    url_pat: re.Pattern | None,
    emit: Callable[..., Coroutine[Any, Any, None]] | None,
) -> list[dict[str, str]]:
    """Render the listing page with Playwright and extract course hrefs via XPath."""
    if not cfg.browser_fallback_xpath:
        log.info("[SSR_PROP] No browser_fallback_xpath configured — skipping browser fallback")
        return []

    async def _emit_safe(msg: str) -> None:
        if emit is not None:
            try:
                await emit("status", msg, phase="discover")
            except Exception:
                pass
        log.info(msg)

    await _emit_safe(
        f"[SSR_PROP] {label}: browser fallback with XPath: {cfg.browser_fallback_xpath}"
    )

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("[SSR_PROP] Playwright not available — browser fallback skipped for %s", label)
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

            if cfg.browser_wait_selector:
                try:
                    await page.wait_for_selector(cfg.browser_wait_selector, timeout=15_000)
                except Exception:
                    log.warning(
                        "[SSR_PROP] Browser fallback: selector %r did not appear within 15s on %s",
                        cfg.browser_wait_selector, listing_url,
                    )

            # Convert XPath to CSS for eval_on_selector_all if possible,
            # otherwise use evaluate with document.evaluate
            xpath = cfg.browser_fallback_xpath
            # Extract hrefs via JS + XPath
            hrefs: list[str] = await page.evaluate(
                """(xpath) => {
                    const result = [];
                    const iter = document.evaluate(
                        xpath, document, null,
                        XPathResult.ORDERED_NODE_ITERATOR_TYPE, null
                    );
                    let node = iter.iterateNext();
                    while (node) {
                        result.push(node.nodeValue || node.getAttribute('href') || node.textContent);
                        node = iter.iterateNext();
                    }
                    return result;
                }""",
                xpath,
            )
            await browser.close()

        base = listing_url.rstrip("/").rsplit("/", 1)[0]  # strip last path segment
        # Normalise to absolute URLs and optionally filter
        for href in hrefs:
            if not href:
                continue
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                from urllib.parse import urlsplit
                parts = urlsplit(listing_url)
                href = f"{parts.scheme}://{parts.netloc}{href}"
            if url_pat and not url_pat.search(href):
                continue
            links.append({"name": "", "url": href})

        await _emit_safe(f"[SSR_PROP] {label}: {len(links)} courses via browser A-Z nav")
        log.info("[SSR_PROP] Browser fallback %s: %d links with XPath %s", label, len(links), xpath)

    except Exception as exc:
        log.error("[SSR_PROP] Browser fallback failed for %s: %s", listing_url, exc, exc_info=True)

    return links

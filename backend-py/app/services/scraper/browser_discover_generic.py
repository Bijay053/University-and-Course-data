"""Generic browser-based course discovery for sites that block plain HTTP.

When the BFS HTTP crawler returns 0 results (e.g. Cloudflare-protected
sites like UEL), this module uses the Playwright browser pool to render
the page in a real Chromium browser, which passes JS challenges and bot
detection that plain ``httpx`` cannot.

Strategy
--------
1. Navigate to the scrape URL with realistic browser headers (Google
   Referer, Accept-Language, etc.).
2. Wait for the page to settle (domcontentloaded + 3 s sleep).
3. Extract all ``<a href>`` links from the DOM.
4. Apply the same ``_looks_like_course`` heuristics as the BFS crawler
   to separate course detail pages from junk/nav links.
5. Follow up to 10 nav-category links one level deeper to pick up
   courses that only appear on listing sub-pages (e.g. /courses/ug,
   /courses/pg).
6. Return deduped ``[{"url": str, "name": str}]`` or ``[]`` on failure
   (callers fall back to Wayback Machine CDX).
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SETTLE_S = 3.0
_NAV_SETTLE_S = 2.5
_MAX_NAV_DEPTH = 10

_EXTRACT_LINKS_JS = r"""
(origin) => {
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    let href = (a.getAttribute('href') || '').trim();
    if (!href || href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('#'))
      return;
    let url;
    try { url = new URL(href, origin).href; } catch (_) { return; }
    const clean = url.split(/[?#]/)[0];
    if (seen.has(clean)) return;
    seen.add(clean);
    const text = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    results.push({ url: clean, name: text });
  });
  return results;
}
"""

_NAV_URL_HINTS = (
    "/study", "/course", "/program", "/academ",
    "/facult", "/school", "/department", "/undergrad", "/postgrad",
)


def _is_nav_url(url: str) -> bool:
    lurl = url.lower()
    return any(h in lurl for h in _NAV_URL_HINTS)


async def browser_discover_generic(
    scrape_url: str,
    *,
    max_courses: int = 200,
    emit=None,
) -> list[dict]:
    """Render ``scrape_url`` in a real Playwright browser and harvest course links.

    Returns a list of ``{"url": str, "name": str}`` dicts, or ``[]`` on
    any failure so the caller can chain to the next fallback strategy.
    """

    async def _emit(msg: str, **kw) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover", kind="browser_discover", **kw)
            except Exception:
                pass

    try:
        from app.services.scraper.browser_pool import pool as _pool
        from playwright.async_api import TimeoutError as _PwTimeout
    except Exception as exc:
        log.warning("browser_discover_generic: browser pool unavailable — %s", exc)
        return []

    parsed = urlparse(scrape_url)
    origin_str = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.netloc

    await _emit(f"[DISCOVER] Browser: navigating to {scrape_url}")
    log.info("browser_discover_generic: starting for %s", scrape_url)

    seen: set[str] = set()
    results: list[dict] = []
    nav_queue: list[str] = []

    try:
        from app.services.scraper.discovery import (
            _looks_like_course,
            _is_known_non_course_url,
        )
    except Exception as exc:
        log.warning("browser_discover_generic: cannot import discovery helpers — %s", exc)
        return []

    def _process_links(raw: list[dict]) -> None:
        for item in raw:
            url = (item.get("url") or "").strip()
            name = (item.get("name") or "").strip()
            if not url:
                continue
            p = urlparse(url)
            if p.netloc and p.netloc != host:
                continue
            if url in seen:
                continue
            seen.add(url)
            if _looks_like_course(url, name):
                results.append({"url": url, "name": name})
            elif _is_nav_url(url) and not _is_known_non_course_url(url):
                nav_queue.append(url)

    try:
        async with _pool.page() as page:
            await page.set_extra_http_headers({
                "Referer": "https://www.google.com/",
                "Accept-Language": "en-US,en;q=0.9",
            })

            try:
                await page.goto(
                    scrape_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except _PwTimeout:
                log.warning(
                    "browser_discover_generic: domcontentloaded timed out — "
                    "continuing with partial DOM"
                )
                await _emit("[DISCOVER] Browser: page load timed out — using partial DOM")
            except Exception as exc:
                log.warning("browser_discover_generic: navigation failed — %s", exc)
                await _emit(f"[DISCOVER] Browser: navigation failed — {exc}")
                return []

            await asyncio.sleep(_SETTLE_S)

            try:
                snippet = (await asyncio.wait_for(page.content(), timeout=5.0))[:2000].lower()
                if any(k in snippet for k in (
                    "neterror", "err_connection", "chrome-error://", "err_name_not_resolved"
                )):
                    await _emit("[DISCOVER] Browser: Chromium error page detected — aborting")
                    return []
            except Exception:
                pass

            try:
                raw = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
            except Exception as exc:
                log.warning("browser_discover_generic: link extraction failed — %s", exc)
                await _emit(f"[DISCOVER] Browser: link extraction failed — {exc}")
                return []

            _process_links(raw or [])
            await _emit(
                f"[DISCOVER] Browser: start page → {len(results)} course links, "
                f"{len(nav_queue)} nav candidates to follow"
            )
            log.info(
                "browser_discover_generic: start page %s → %d courses, %d nav links",
                scrape_url, len(results), len(nav_queue),
            )

            for nav_url in nav_queue[:_MAX_NAV_DEPTH]:
                if len(results) >= max_courses:
                    break
                try:
                    await page.goto(
                        nav_url, wait_until="domcontentloaded", timeout=30_000
                    )
                    await asyncio.sleep(_NAV_SETTLE_S)
                    raw2 = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
                    before = len(results)
                    _process_links(raw2 or [])
                    gained = len(results) - before
                    if gained:
                        await _emit(
                            f"[DISCOVER] Browser: nav {nav_url} → +{gained} courses "
                            f"(total {len(results)})"
                        )
                        log.info(
                            "browser_discover_generic: nav %s → +%d courses",
                            nav_url, gained,
                        )
                except Exception as exc:
                    log.debug(
                        "browser_discover_generic: nav page %s failed — %s",
                        nav_url, exc,
                    )

    except Exception as exc:
        log.warning("browser_discover_generic: unexpected error — %s", exc)
        await _emit(f"[DISCOVER] Browser: unexpected error — {exc}")
        return []

    if len(results) < 3:
        log.warning(
            "browser_discover_generic: only %d course(s) found for %s — "
            "site may be blocking browser too",
            len(results), scrape_url,
        )
        await _emit(
            f"[DISCOVER] Browser: only {len(results)} course(s) found — "
            "trying next fallback"
        )
        return []

    log.info(
        "browser_discover_generic: discovered %d courses for %s",
        len(results), scrape_url,
    )
    await _emit(f"[DISCOVER] Browser: discovered {len(results)} course links")
    return results[:max_courses]

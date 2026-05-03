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
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SETTLE_S = 3.0
_NAV_SETTLE_S = 2.0
_MAX_NAV_PAGES = 30   # total nav pages to visit across all BFS levels

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

_HOST_EXTRA_SEEDS: dict[str, list[str]] = {
    "www.ecu.edu.au": [
        "https://www.ecu.edu.au/degrees/courses/all",
        "https://www.ecu.edu.au/degrees/postgraduate",
    ],
    "ecu.edu.au": [
        "https://www.ecu.edu.au/degrees/courses/all",
        "https://www.ecu.edu.au/degrees/postgraduate",
    ],
    "www.une.edu.au": [
        "https://www.une.edu.au/study/courses",
        "https://www.une.edu.au/study/postgraduate-study",
        "https://www.une.edu.au/study/find-a-course",
    ],
    "une.edu.au": [
        "https://www.une.edu.au/study/courses",
        "https://www.une.edu.au/study/postgraduate-study",
        "https://www.une.edu.au/study/find-a-course",
    ],
    # UTAS: browser BFS starts from /courses (undergrad landing) and exhausts
    # its page budget on undergraduate listing pages, never reaching the
    # postgraduate A-Z listing.  Seed both levels directly so master courses
    # are included in every scrape run.
    "www.utas.edu.au": [
        "https://www.utas.edu.au/courses/postgraduate",
        "https://www.utas.edu.au/study/postgraduate",
        "https://www.utas.edu.au/courses/honours",
    ],
    "utas.edu.au": [
        "https://www.utas.edu.au/courses/postgraduate",
        "https://www.utas.edu.au/study/postgraduate",
        "https://www.utas.edu.au/courses/honours",
    ],
}

_LISTING_URL_RE = re.compile(
    r"/(?:degrees|study|courses?|programs?)"
    r"(?:/courses?)?"
    r"/(?:all|search|list|find(?:-a-course)?|postgrad(?:uate)?(?:-study)?|undergrad(?:uate)?)",
    re.I,
)

_SCROLL_AND_LOAD_JS = r"""
async () => {
  let prev = 0;
  for (let i = 0; i < 6; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, 1800));
    const cur = document.body.scrollHeight;
    if (cur === prev) break;
    prev = cur;
  }
}
"""


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

    for seed_url in _HOST_EXTRA_SEEDS.get(host, []):
        if seed_url not in seen:
            nav_queue.append(seed_url)
            seen.add(seed_url)

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

            # BFS over nav links — newly discovered nav pages are appended
            # to nav_queue inside _process_links, so the while-loop picks
            # them up automatically (2+ level deep site hierarchies like
            # ECU: homepage → study-area → individual course).
            nav_visited: set[str] = set()
            nav_i = 0
            while nav_i < len(nav_queue) and nav_i < _MAX_NAV_PAGES:
                nav_url = nav_queue[nav_i]
                nav_i += 1
                if nav_url in nav_visited:
                    continue
                nav_visited.add(nav_url)
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

                    # Scroll-to-load: for paginated/infinite-scroll course
                    # listing pages, scroll to the bottom repeatedly so that
                    # JavaScript-rendered results fully hydrate before the
                    # second link harvest.
                    if _LISTING_URL_RE.search(nav_url):
                        try:
                            await page.evaluate(_SCROLL_AND_LOAD_JS)
                            raw3 = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
                            before2 = len(results)
                            _process_links(raw3 or [])
                            scroll_gained = len(results) - before2
                            gained += scroll_gained
                            if scroll_gained:
                                await _emit(
                                    f"[DISCOVER] Browser: scroll {nav_url} "
                                    f"→ +{scroll_gained} more courses "
                                    f"(total {len(results)})"
                                )
                        except Exception as se:
                            log.debug(
                                "browser_discover_generic: scroll failed for %s — %s",
                                nav_url, se,
                            )

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

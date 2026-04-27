"""Browser-based discovery for CSU's international courses listing page.

The CSU international courses listing at
https://study.csu.edu.au/international/courses
is a React SPA that renders course cards via client-side JavaScript.  A
plain HTTP fetch returns a shell with zero outbound links, so the normal
BFS discovery falls back to the domestic sitemap and misses all
international-specific URLs.

This module fetches the listing page via Playwright (the same browser pool
used by per-course extractions), waits for the SPA to hydrate, then
harvests every ``/international/courses/<slug>`` link.

Public entry-point
------------------
:func:`browser_discover_csu_international`
    ``(emit) → list[dict]``  — list of ``{"url": str, "name": str}`` dicts,
    one per international course link found on the listing page.  Returns
    ``[]`` on any failure so callers can fall back gracefully.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin

log = logging.getLogger(__name__)

_LISTING_URL = "https://study.csu.edu.au/international/courses"
_CSU_ORIGIN = "https://study.csu.edu.au"

# JS run inside the rendered page to extract all course card links.
# Finds every <a> whose href starts with /international/courses/ and
# has at least one more path segment (the course slug).
_EXTRACT_LINKS_JS = r"""
() => {
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.getAttribute('href') || '';
    // Must match /international/courses/<slug> — reject the listing root itself
    if (!/^\/international\/courses\/[^/]+/.test(href)) return;
    const url = href.startsWith('http') ? href : 'https://study.csu.edu.au' + href;
    if (seen.has(url)) return;
    seen.add(url);
    const text = (a.innerText || a.textContent || '').trim();
    results.push({ url, name: text });
  });
  return results;
}
"""


async def browser_discover_csu_international(
    emit=None,
    *,
    max_courses: int = 300,
) -> list[dict]:
    """Fetch the CSU international courses listing via Playwright and return
    a list of ``{"url": str, "name": str}`` dicts.

    Returns an empty list when:
    * The browser pool is unavailable (test environment / import error).
    * The page fetch times out or returns fewer than 3 links (likely a
      partial render — caller falls back to normal discovery).
    """

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover", kind="csu_browser_discover")
            except Exception:
                pass

    try:
        from app.services.scraper.browser_pool import pool as browser_pool
    except Exception as exc:
        log.warning("csu_browser_discover: browser pool unavailable — %s", exc)
        return []

    await _emit(f"[DISCOVER] CSU: fetching international listing via browser → {_LISTING_URL}")

    try:
        html = await browser_pool.fetch_html(
            _LISTING_URL,
            wait_until="networkidle",
            timeout=50_000,
            settle_ms=4_000,
        )
    except Exception as exc:
        log.warning("csu_browser_discover: browser fetch failed — %s", exc)
        await _emit(f"[DISCOVER] CSU: browser fetch of listing failed — {exc}")
        return []

    if not html:
        log.warning("csu_browser_discover: browser returned empty HTML for %s", _LISTING_URL)
        await _emit("[DISCOVER] CSU: browser returned empty HTML for listing — falling back")
        return []

    # Parse links from the rendered HTML with BeautifulSoup (no extra dep —
    # already required by other extractors).
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        links: list[dict] = []
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            # Must be /international/courses/<slug> — reject bare listing root
            import re
            if not re.match(r"^/international/courses/[^/]+", href):
                # Also accept absolute URLs on the same host
                if "study.csu.edu.au/international/courses/" not in href:
                    continue
            url = href if href.startswith("http") else urljoin(_CSU_ORIGIN, href)
            if url in seen:
                continue
            seen.add(url)
            name = (a.get_text(separator=" ") or "").strip()
            links.append({"url": url, "name": name})
            if len(links) >= max_courses:
                break
    except Exception as exc:
        log.warning("csu_browser_discover: HTML parsing failed — %s", exc)
        await _emit(f"[DISCOVER] CSU: link extraction from rendered HTML failed — {exc}")
        return []

    if len(links) < 3:
        log.warning(
            "csu_browser_discover: only %d link(s) found in rendered listing — "
            "page may not have hydrated; falling back to normal discovery",
            len(links),
        )
        await _emit(
            f"[DISCOVER] CSU: only {len(links)} international course link(s) found "
            f"— falling back to normal discovery"
        )
        return []

    await _emit(
        f"[DISCOVER] CSU: browser found {len(links)} international course link(s)"
    )
    log.info("csu_browser_discover: found %d international course links", len(links))
    return links

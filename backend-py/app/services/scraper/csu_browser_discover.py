"""Browser-based discovery for CSU's international courses listing page.

The CSU international courses listing at
https://study.csu.edu.au/international/courses
is a React SPA that renders course cards via client-side JavaScript.  A
plain HTTP fetch returns a 112 KB server-side-rendered shell with zero
course links — all cards are injected by the React bundle after hydration.

This module uses the same Playwright browser pool used by per-course
extraction to:
  1. Navigate to the listing page and wait for the SPA to hydrate.
  2. Scroll to the bottom in a loop, triggering any infinite-scroll or
     lazy-load behaviour.
  3. Click any "Load more" / "Show more" buttons that appear.
  4. Repeat until no new course links appear in two consecutive passes.
  5. Extract every ``/international/courses/<slug>`` link with its visible
     text (the course name shown on the card).

Public entry-point
------------------
:func:`browser_discover_csu_international`
    ``(emit, max_courses) → list[dict]``
    Returns a list of ``{"url": str, "name": str}`` dicts, one per
    international course link.  Returns ``[]`` on any failure so callers
    can fall back gracefully to the normal BFS / sitemap discovery.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_LISTING_URL = "https://study.csu.edu.au/international/courses"

# Maximum scroll attempts before giving up on pagination
_MAX_SCROLL_ITERS = 30

# Seconds to wait after each scroll / button click for content to render
_SCROLL_SETTLE_S = 2.5

# JavaScript evaluated inside the rendered page to collect every
# /international/courses/<slug> anchor.  Returns a JSON-serialisable array
# of {url, name} objects.
_EXTRACT_LINKS_JS = r"""
() => {
  const ORIGIN = 'https://study.csu.edu.au';
  // Exact same-origin path pattern: /international/courses/<slug>
  // where <slug> is at least one non-separator character and there is
  // no further path segment (avoids sub-pages like /international/courses/health/fees).
  const PATH_RE = /^\/international\/courses\/[^/?#]+\/?$/;
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    // Resolve to absolute URL (handle relative, absolute-same-origin, and
    // fully-qualified same-origin URLs)
    let url;
    if (raw.startsWith('/')) {
      url = ORIGIN + raw;
    } else if (raw.startsWith(ORIGIN)) {
      url = raw;
    } else {
      return;  // Off-site or protocol-relative — skip
    }
    // Strip query-string and hash before testing the path
    const [base] = url.split(/[?#]/);
    const path = base.replace(ORIGIN, '');
    if (!PATH_RE.test(path)) return;
    if (seen.has(base)) return;
    seen.add(base);
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
    results.push({ url: base, name: text });
  });
  return results;
}
"""

# Patterns for "Load more" buttons — tried in order, first match wins.
# Using JS-based click to avoid strict-mode locator failures on sites
# that have multiple matching elements.
_LOAD_MORE_JS = r"""
() => {
  const patterns = [
    /load\s*more/i, /show\s*more/i, /view\s*more/i,
    /see\s*more/i, /more\s*courses/i, /next\s*page/i,
  ];
  const clickable = Array.from(
    document.querySelectorAll('button, a[role="button"], [class*="load"], [class*="more"]')
  );
  for (const el of clickable) {
    const text = (el.innerText || el.textContent || '').trim();
    if (patterns.some(p => p.test(text))) {
      const style = window.getComputedStyle(el);
      if (style.display !== 'none' && style.visibility !== 'hidden') {
        el.click();
        return text;  // Return the button text for logging
      }
    }
  }
  return null;  // Nothing clicked
}
"""


async def browser_discover_csu_international(
    emit=None,
    *,
    max_courses: int = 300,
) -> list[dict]:
    """Fetch the CSU international courses listing via Playwright with
    full scroll/paginate support.

    Returns a list of ``{"url": str, "name": str}`` dicts — one per
    discovered international course URL — or ``[]`` on failure.

    Failure modes that return [] (caller falls back to normal BFS):
    * Browser pool unavailable (test environment).
    * Navigation timeout / Chromium error page.
    * Fewer than 3 course links found after all scroll passes (probable
      render failure or bot-block).
    """

    async def _emit(msg: str, **kw) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover",
                           kind="csu_browser_discover", **kw)
            except Exception:
                pass

    # ── 1. Acquire browser pool ──────────────────────────────────────────
    try:
        from app.services.scraper.browser_pool import pool as _pool
        from playwright.async_api import TimeoutError as _PwTimeout
    except Exception as exc:
        log.warning("csu_browser_discover: browser pool unavailable — %s", exc)
        return []

    await _emit(f"[DISCOVER] CSU: fetching international listing via browser → {_LISTING_URL}")

    links: list[dict] = []

    try:
        async with _pool.page() as page:
            # Mimic a real browser navigating from Google
            await page.set_extra_http_headers({"Referer": "https://www.google.com/"})

            # ── 2. Navigate ──────────────────────────────────────────────
            try:
                await page.goto(
                    _LISTING_URL,
                    wait_until="networkidle",
                    timeout=60_000,
                )
            except _PwTimeout:
                log.warning(
                    "csu_browser_discover: goto networkidle timed out — "
                    "continuing with partial DOM"
                )
            except Exception as exc:
                log.warning("csu_browser_discover: goto failed — %s", exc)
                await _emit(f"[DISCOVER] CSU: navigation failed ({exc}) — falling back")
                return []

            # ── 2b. Error-page sniff (parity with browser_pool.fetch_html) ─
            # Chromium can land on an error interstitial (DNS failure, cert
            # error, site down) that looks like a page with no links.  Detect
            # and bail early rather than burning the full scroll budget.
            try:
                partial = await asyncio.wait_for(page.content(), timeout=5.0)
                lowered = (partial or "")[:4096].lower()
                if (
                    "neterror" in lowered
                    or "chrome-error://" in lowered
                    or "err_name_not_resolved" in lowered
                    or "err_connection_" in lowered
                    or "err_cert_" in lowered
                ):
                    log.warning(
                        "csu_browser_discover: Chromium error page detected — falling back"
                    )
                    await _emit(
                        "[DISCOVER] CSU: Chromium error page detected — falling back"
                    )
                    return []
            except Exception:
                pass  # If we can't sniff the content, proceed anyway

            # ── 3. Initial settle ────────────────────────────────────────
            await asyncio.sleep(4.0)

            # ── 4. Scroll + Load-More loop ───────────────────────────────
            # Strategy: scroll to the bottom, pause for content to render,
            # check for Load-More buttons, then compare link counts.
            # Stop when two consecutive passes find no new links.
            prev_count = -1
            stall_streak = 0

            for iteration in range(_MAX_SCROLL_ITERS):
                # Scroll to absolute bottom of page
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(_SCROLL_SETTLE_S)

                # Try to click any "Load more" style button
                try:
                    btn_text = await page.evaluate(_LOAD_MORE_JS)
                    if btn_text:
                        await _emit(
                            f"[DISCOVER] CSU: clicked '{btn_text}' button "
                            f"(iter {iteration + 1})"
                        )
                        # Wait for the new batch to load
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=8_000
                            )
                        except _PwTimeout:
                            pass
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                # Count links now visible
                try:
                    current_js = await page.evaluate(_EXTRACT_LINKS_JS)
                    current_count = len(current_js)
                except Exception:
                    current_count = prev_count

                if current_count == prev_count:
                    stall_streak += 1
                    if stall_streak >= 2:
                        # Two passes with no growth → all content loaded
                        log.debug(
                            "csu_browser_discover: link count stable at %d "
                            "after %d scroll iters — done",
                            current_count, iteration + 1,
                        )
                        break
                else:
                    stall_streak = 0
                    await _emit(
                        f"[DISCOVER] CSU: scroll iter {iteration + 1} → "
                        f"{current_count} course links found"
                    )

                prev_count = current_count

                if current_count >= max_courses:
                    log.debug(
                        "csu_browser_discover: hit max_courses cap (%d)", max_courses
                    )
                    break

            # ── 5. Final extraction ──────────────────────────────────────
            try:
                raw = await page.evaluate(_EXTRACT_LINKS_JS)
            except Exception as exc:
                log.warning("csu_browser_discover: final JS extraction failed — %s", exc)
                await _emit(
                    f"[DISCOVER] CSU: JS link extraction failed — {exc}"
                )
                return []

            # De-duplicate and cap at max_courses
            seen: set[str] = set()
            for item in raw:
                url = (item.get("url") or "").strip()
                name = (item.get("name") or "").strip()
                if url and url not in seen:
                    seen.add(url)
                    links.append({"url": url, "name": name})
                if len(links) >= max_courses:
                    break

    except Exception as exc:
        log.warning("csu_browser_discover: unexpected error — %s", exc)
        await _emit(f"[DISCOVER] CSU: browser discovery error — {exc}")
        return []

    # ── 6. Validate & return ─────────────────────────────────────────────
    if len(links) < 3:
        log.warning(
            "csu_browser_discover: only %d link(s) found — page may not have "
            "hydrated correctly; falling back to normal discovery",
            len(links),
        )
        await _emit(
            f"[DISCOVER] CSU: only {len(links)} link(s) found after scroll — "
            "falling back"
        )
        return []

    log.info(
        "csu_browser_discover: found %d international course links "
        "(scroll iters exhausted or stable)",
        len(links),
    )
    await _emit(
        f"[DISCOVER] CSU: browser discovered {len(links)} international course link(s)"
    )
    return links

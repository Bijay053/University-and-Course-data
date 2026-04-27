"""Browser-based discovery for CSU's international courses listing page.

The CSU international courses listing at
https://study.csu.edu.au/international/courses
is a React SPA that renders course cards via client-side JavaScript.  A
plain HTTP fetch returns a 112 KB server-side-rendered shell with zero
course links — all cards are injected by the React bundle after hydration.

This module uses the same Playwright browser pool used by per-course
extraction to:
  1. Navigate to the listing page and wait for the SPA to hydrate.
  2. Read ``window.course_finder.resultsArr`` — the full in-memory array
     that the page's search widget populates on hydration.  This array
     contains **all** international courses regardless of how many cards
     are currently rendered in the DOM (the DOM paginates at 12 per page,
     but the full dataset is loaded up-front).
  3. Fall back to a scroll / "Show more" DOM scrape if ``resultsArr`` is
     unavailable or suspiciously small (< 3 entries).

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
import re

log = logging.getLogger(__name__)

_LISTING_URL = "https://study.csu.edu.au/international/courses"
_CSU_ORIGIN = "https://study.csu.edu.au"

# Matches /courses/<slug> or /international/courses/<slug> with no query/fragment.
# Slug must be at least one char and may have a trailing slash.
_COURSE_PATH_RE = re.compile(
    r"^/(?:international/)?courses/[^/?#]+/?$"
)


def _normalise_csu_course_url(raw: str) -> str | None:
    """Normalise a raw CSU course URL to the canonical international path.

    Mirrors the inline URL-normalisation logic in :data:`_EXTRACT_FROM_RESULTS_JS`
    and :data:`_EXTRACT_LINKS_JS` so the same behaviour can be unit-tested in
    Python without a browser.

    Rules (identical to the JS snippets):
    * Only ``https://study.csu.edu.au`` origin is accepted.
    * Path must match ``/courses/<slug>`` or ``/international/courses/<slug>``.
    * Query strings and fragments are stripped before matching.
    * ``/courses/<slug>`` is rewritten to ``/international/courses/<slug>``.
    * Already-correct ``/international/courses/<slug>`` paths are returned
      unchanged (no double-prefixing).

    Returns the full normalised URL on success, or ``None`` when the input is
    not a recognised CSU course URL.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # Resolve relative same-origin paths.
    if raw.startswith("/"):
        url = _CSU_ORIGIN + raw
    elif raw.startswith(_CSU_ORIGIN):
        url = raw
    else:
        return None

    # Strip query and fragment.
    base = url.split("?")[0].split("#")[0]
    path = base[len(_CSU_ORIGIN):]

    if not _COURSE_PATH_RE.match(path):
        return None

    # Normalise /courses/<slug> → /international/courses/<slug>.
    if path.startswith("/courses/"):
        path = "/international" + path

    return _CSU_ORIGIN + path

# Maximum scroll attempts before giving up on pagination (fallback path only)
_MAX_SCROLL_ITERS = 30

# Seconds to wait after each scroll / button click for content to render
_SCROLL_SETTLE_S = 2.5

# Minimum number of entries in window.course_finder.resultsArr before we trust
# it as a complete listing and skip the scroll fallback.  CSU currently returns
# 179 entries; 20 is chosen well above the first-page DOM count (12) so a
# partial hydration or pagination regression triggers the scroll fallback rather
# than returning a silently small result set.
_RESULTS_ARR_MIN_FLOOR = 20

# Warn threshold: emit a log/status warning when resultsArr is above the min
# floor but still below this value (signals unexpected shrinkage).
_RESULTS_ARR_WARN_FLOOR = 30

# ── Primary extraction: read window.course_finder.resultsArr ─────────────────
# The CSU course-finder widget loads its complete filtered dataset into
# window.course_finder.resultsArr on hydration.  Each entry has at minimum
# { url: string, label: string }.  This gives us all courses in one shot
# without any scrolling or button-clicking.
_EXTRACT_FROM_RESULTS_JS = r"""
() => {
  const cf = window.course_finder;
  if (!cf || !Array.isArray(cf.resultsArr) || cf.resultsArr.length === 0) {
    return null;  // Signal: resultsArr not available, use DOM fallback
  }
  const ORIGIN = 'https://study.csu.edu.au';
  // Accept any /courses/<slug> or /international/courses/<slug> path.
  const PATH_RE = /^\/(?:international\/)?courses\/[^/?#]+\/?$/;
  const results = [];
  const seen = new Set();
  for (const item of cf.resultsArr) {
    const raw = (item.url || '').trim();
    if (!raw) continue;
    // Resolve relative or absolute same-origin URLs
    let url;
    if (raw.startsWith('/')) {
      url = ORIGIN + raw;
    } else if (raw.startsWith(ORIGIN)) {
      url = raw;
    } else {
      continue;
    }
    const [base] = url.split(/[?#]/);
    let path = base.replace(ORIGIN, '');
    if (!PATH_RE.test(path)) continue;
    // Always normalise to /international/courses/<slug> so the per-course
    // static extractor reads the international-student page which carries
    // INT-tagged offering data (location, mode, IELTS).  The /courses/<slug>
    // pages have the same slug but show domestic-student offering data.
    if (path.startsWith('/courses/')) {
      path = '/international' + path;
    }
    const intlUrl = ORIGIN + path;
    if (seen.has(intlUrl)) continue;
    seen.add(intlUrl);
    const name = (item.label || item.name || '').replace(/\s+/g, ' ').trim();
    results.push({ url: intlUrl, name });
  }
  return results;
}
"""

# ── Fallback extraction: scan DOM anchors ────────────────────────────────────
# Used when window.course_finder.resultsArr is not available.  Matches both
# /international/courses/<slug> and /courses/<slug> paths so it works
# regardless of which URL scheme CSU uses on the rendered cards.
_EXTRACT_LINKS_JS = r"""
() => {
  const ORIGIN = 'https://study.csu.edu.au';
  // Match /international/courses/<slug> OR /courses/<slug>
  const PATH_RE = /^\/(?:international\/)?courses\/[^/?#]+\/?$/;
  const results = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    let url;
    if (raw.startsWith('/')) {
      url = ORIGIN + raw;
    } else if (raw.startsWith(ORIGIN)) {
      url = raw;
    } else {
      return;
    }
    const [base] = url.split(/[?#]/);
    let path = base.replace(ORIGIN, '');
    if (!PATH_RE.test(path)) return;
    // Normalise to /international/courses/<slug> (same reason as primary path).
    if (path.startsWith('/courses/')) {
      path = '/international' + path;
    }
    const intlUrl = ORIGIN + path;
    if (seen.has(intlUrl)) return;
    seen.add(intlUrl);
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
    results.push({ url: intlUrl, name: text });
  });
  return results;
}
"""

# Patterns for "Load more" / "Show more" buttons — used in the fallback path.
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
        return text;
      }
    }
  }
  return null;
}
"""


async def browser_discover_csu_international(
    emit=None,
    *,
    max_courses: int = 300,
) -> list[dict]:
    """Fetch the CSU international courses listing via Playwright.

    Primary strategy: read ``window.course_finder.resultsArr`` which the
    page's search widget populates with the full filtered dataset on
    hydration (typically 100–200 courses).  This is faster and more
    reliable than DOM scrolling because all data is already in memory.

    Fallback strategy: scroll + "Show more" DOM scrape (used when
    ``resultsArr`` is unavailable or returns < 3 entries).

    Returns a list of ``{"url": str, "name": str}`` dicts — one per
    discovered international course URL — or ``[]`` on failure.

    Failure modes that return [] (caller falls back to normal BFS):
    * Browser pool unavailable (test environment).
    * Navigation timeout / Chromium error page.
    * Fewer than 3 course links found after all strategies (probable
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

            # ── 2b. Error-page sniff ─────────────────────────────────────
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
                pass

            # ── 3. Initial settle ────────────────────────────────────────
            await asyncio.sleep(4.0)

            # ── 4. Primary: extract from window.course_finder.resultsArr ─
            # The widget loads the full filtered dataset into resultsArr on
            # hydration — no scrolling needed.  This is the fast path.
            #
            # Guard: require at least _RESULTS_ARR_MIN_FLOOR entries to use
            # this path.  If resultsArr ever regresses to a partial subset
            # (e.g. 12 items — the initial DOM page) we skip the fast path
            # and run the scroll fallback so we still attempt a fuller scan
            # rather than silently returning a partial list.
            try:
                results_raw = await page.evaluate(_EXTRACT_FROM_RESULTS_JS)
            except Exception as exc:
                log.warning(
                    "csu_browser_discover: resultsArr JS extraction failed — %s", exc
                )
                results_raw = None

            raw_count = len(results_raw) if results_raw else 0

            if results_raw and raw_count >= _RESULTS_ARR_MIN_FLOOR:
                if raw_count < _RESULTS_ARR_WARN_FLOOR:
                    log.warning(
                        "csu_browser_discover: resultsArr returned only %d course(s) "
                        "(below expected ~%d) — possible partial hydration",
                        raw_count,
                        _RESULTS_ARR_WARN_FLOOR,
                    )
                    await _emit(
                        f"[DISCOVER] CSU: WARNING — resultsArr has only {raw_count} "
                        f"course(s); expected ~{_RESULTS_ARR_WARN_FLOOR}+"
                    )
                await _emit(
                    f"[DISCOVER] CSU: extracted {raw_count} courses from "
                    "window.course_finder.resultsArr (no scrolling needed)"
                )
                log.info(
                    "csu_browser_discover: resultsArr path — %d course(s) found",
                    raw_count,
                )
                seen: set[str] = set()
                for item in results_raw:
                    url = (item.get("url") or "").strip()
                    name = (item.get("name") or "").strip()
                    if url and url not in seen:
                        seen.add(url)
                        links.append({"url": url, "name": name})
                    if len(links) >= max_courses:
                        break
                # Skip scroll fallback — we have a sufficiently complete list
                return await _validate_and_return(links, emit_fn=_emit)

            # ── 5. Fallback: scroll + "Show more" DOM scrape ─────────────
            if results_raw is not None and raw_count < _RESULTS_ARR_MIN_FLOOR:
                await _emit(
                    f"[DISCOVER] CSU: resultsArr returned only {raw_count} entries "
                    f"(< floor {_RESULTS_ARR_MIN_FLOOR}) — "
                    "falling back to scroll/DOM approach"
                )
                log.warning(
                    "csu_browser_discover: resultsArr has only %d entries "
                    "(floor=%d) — scroll fallback",
                    raw_count,
                    _RESULTS_ARR_MIN_FLOOR,
                )
            else:
                await _emit(
                    "[DISCOVER] CSU: resultsArr unavailable — "
                    "falling back to scroll/DOM approach"
                )
                log.debug("csu_browser_discover: resultsArr unavailable — scroll fallback")

            prev_count = -1
            stall_streak = 0

            for iteration in range(_MAX_SCROLL_ITERS):
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                await asyncio.sleep(_SCROLL_SETTLE_S)

                try:
                    btn_text = await page.evaluate(_LOAD_MORE_JS)
                    if btn_text:
                        await _emit(
                            f"[DISCOVER] CSU: clicked '{btn_text}' button "
                            f"(iter {iteration + 1})"
                        )
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=8_000
                            )
                        except _PwTimeout:
                            pass
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                try:
                    current_js = await page.evaluate(_EXTRACT_LINKS_JS)
                    current_count = len(current_js)
                except Exception:
                    current_count = prev_count

                if current_count == prev_count:
                    stall_streak += 1
                    if stall_streak >= 2:
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

            try:
                raw = await page.evaluate(_EXTRACT_LINKS_JS)
            except Exception as exc:
                log.warning("csu_browser_discover: final JS extraction failed — %s", exc)
                await _emit(
                    f"[DISCOVER] CSU: JS link extraction failed — {exc}"
                )
                return []

            seen_set: set[str] = set()
            for item in raw:
                url = (item.get("url") or "").strip()
                name = (item.get("name") or "").strip()
                if url and url not in seen_set:
                    seen_set.add(url)
                    links.append({"url": url, "name": name})
                if len(links) >= max_courses:
                    break

    except Exception as exc:
        log.warning("csu_browser_discover: unexpected error — %s", exc)
        await _emit(f"[DISCOVER] CSU: browser discovery error — {exc}")
        return []

    return await _validate_and_return(links, emit_fn=_emit)


async def _validate_and_return(links: list[dict], *, emit_fn) -> list[dict]:
    """Log and return discovered links, or empty list if too few found."""
    if len(links) < 3:
        log.warning(
            "csu_browser_discover: only %d link(s) found — page may not have "
            "hydrated correctly; falling back to normal discovery",
            len(links),
        )
        await emit_fn(
            f"[DISCOVER] CSU: only {len(links)} link(s) found after all strategies — "
            "falling back"
        )
        return []

    log.info(
        "csu_browser_discover: found %d international course links",
        len(links),
    )
    await emit_fn(
        f"[DISCOVER] CSU: browser discovered {len(links)} international course link(s)"
    )
    return links

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
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_SETTLE_S = 3.0
_NAV_SETTLE_S = 2.0
# Raised 30 → 50 (2026-05-17) so per-discipline seed sets (e.g. QUT's 15
# /study/<area> pages + ~20 homepage-nav candidates from the start page)
# can all be visited within a single discovery pass.  Previously the 30
# cap let homepage-nav candidates evict ~5-7 of the per-discipline seeds
# (design-and-architecture, health, justice-and-law, science-and-
# mathematics, social-work-and-human-services, communication), yielding
# only 76 candidates from a ~200-course international catalogue.
_MAX_NAV_PAGES = 50   # total nav pages to visit across all BFS levels

# ── Discovery time budget ─────────────────────────────────────────────────────
# Hard wall-clock limit for the nav BFS loop.  Overridable per-uni via
# discovery.browser_time_budget_s in the university YAML.
_DEFAULT_TIME_BUDGET_S: int = 90

# ── Early-stop course threshold ───────────────────────────────────────────────
# Stop following nav pages as soon as this many course links are found.
# Overridable per-uni via discovery.browser_early_stop_courses in YAML.
_DEFAULT_EARLY_STOP_COURSES: int = 100

# ── Global nav blocklist ──────────────────────────────────────────────────────
# Path-segment substrings for low-value navigation pages that are never
# individual course pages and should not be crawled.  Applied to every
# nav-candidate URL regardless of university.  Per-uni YAMLs can extend
# this list via discovery.block_nav_patterns.
_GLOBAL_NAV_BLOCKLIST: frozenset[str] = frozenset({
    "/apprenticeship",
    "/fees",       "/fee-",       "/funding",
    "/scholarship",
    "/pre-entry",  "/pre-sessional",
    "/contact",    "/about",
    "/news",       "/event",
    "/accommodation",
    "/student-life", "/studentlife",
    "/open-day",   "/openday",    "/open-evening",
    "/alumni",     "/staff",      "/governance",
    "/accessibility", "/privacy", "/cookie",
    "/sitemap",    "/search",
    "/job",        "/career",     "/vacancy",
    "/library",    "/sport",
    "/international-pathways",   # pathway/foundation-level nav
})
# After page.goto on a nav page, wait this long for at least one
# course-shaped link selector to appear before extracting links.
# JS-rendered SPA discipline pages (QUT, Newcastle, UTAS, ECU) hydrate
# the course grid asynchronously after domcontentloaded; without this
# wait the 2-second _NAV_SETTLE_S sleep often races the hydration and
# the link harvest returns 0.  Caught-timeout fallback keeps the
# existing "sleep + extract" behaviour for sites that don't need it.
# Narrow course-anchored selectors only — generic header/footer nav links
# (e.g. `<a href="/study/">Study</a>`) are present on every page before
# JS hydration, so they would let the wait return early even though the
# actual course grid hasn't populated yet.  These patterns target the
# per-course URL shape (`/courses/<slug>`, `/course/<slug>`,
# `/degrees/<slug>`, `/programs/<slug>`) which only appears once the
# JS-rendered listing has hydrated.
_NAV_LINK_SELECTOR = "a[href*='/courses/'], a[href*='/course/'], a[href*='/degrees/'], a[href*='/programs/']"
_NAV_LINK_WAIT_MS = 8_000

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
    # Macquarie University: www.mq.edu.au returns a Cloudflare 403 challenge
    # to plain HTTP, so BFS yields zero candidates.  Browser-based discovery
    # bypasses Cloudflare but starts from the homepage with only 14 nav
    # links, none of which reach the catalogue.  Seed the two listing
    # roots so the browser pass enumerates the full undergraduate and
    # postgraduate A-Z indexes (~300 courses total).
    "www.mq.edu.au": [
        "https://www.mq.edu.au/study/find-a-course",
        "https://www.mq.edu.au/study/find-a-course/undergraduate",
        "https://www.mq.edu.au/study/find-a-course/postgraduate",
    ],
    "mq.edu.au": [
        "https://www.mq.edu.au/study/find-a-course",
        "https://www.mq.edu.au/study/find-a-course/undergraduate",
        "https://www.mq.edu.au/study/find-a-course/postgraduate",
    ],
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
    # postgraduate A-Z listing OR the per-faculty A-Z listings where the
    # ~219-course CRICOS catalogue actually lives.  Seed:
    #   - postgraduate / honours hub pages (existing)
    #   - 7 faculty A-Z index pages (/courses/<faculty>/courses) which each
    #     enumerate ~30-50 individual course URLs.
    # Empirically the previous seed set yielded only 123 candidates from a
    # 219-course catalogue (Master of Teaching e7g and ~80 others missed
    # entirely); the faculty seeds bring discovery up to the full catalogue.
    # IMPORTANT: /study/postgraduate was removed (2026-05-11) — that page
    # contains discipline navigation links that include /study/undergraduate/*
    # faculty listing pages, which are NOT individual CRICOS course pages.
    # Using only /courses/* URLs keeps discovery scoped to the structured
    # per-faculty course listings that contain individual course page links.
    # NOTE (2026-05-12): /courses/undergraduate was MISSING from the seeds
    # despite being the canonical UTAS undergraduate A-Z catalogue. Symptom:
    # a UTAS scrape staged ~50 postgraduate courses (Master / Graduate
    # Certificate / Graduate Diploma) and ZERO Bachelor's degrees, even
    # though UTAS lists ~140 international courses overall. /courses/postgraduate
    # was the only level-listing seed; honours hub yields a handful; the
    # faculty A-Z pages (/courses/<faculty>/courses) are JS-heavy and the
    # Playwright pass typically gets 0 links from them. Adding the matching
    # /courses/undergraduate seed mirrors the postgraduate path and brings
    # the Bachelor's catalogue back into discovery.
    "www.utas.edu.au": [
        "https://www.utas.edu.au/courses/undergraduate",
        "https://www.utas.edu.au/courses/postgraduate",
        "https://www.utas.edu.au/courses/honours",
        "https://www.utas.edu.au/courses/arts-soc/courses",
        "https://www.utas.edu.au/courses/health/courses",
        "https://www.utas.edu.au/courses/sci-eng/courses",
        "https://www.utas.edu.au/courses/tsbe/courses",
        "https://www.utas.edu.au/courses/acad/courses",
        "https://www.utas.edu.au/courses/dvc-research/courses",
        "https://www.utas.edu.au/courses/uc/courses",
    ],
    "utas.edu.au": [
        "https://www.utas.edu.au/courses/undergraduate",
        "https://www.utas.edu.au/courses/postgraduate",
        "https://www.utas.edu.au/courses/honours",
        "https://www.utas.edu.au/courses/arts-soc/courses",
        "https://www.utas.edu.au/courses/health/courses",
        "https://www.utas.edu.au/courses/sci-eng/courses",
        "https://www.utas.edu.au/courses/tsbe/courses",
        "https://www.utas.edu.au/courses/acad/courses",
        "https://www.utas.edu.au/courses/dvc-research/courses",
        "https://www.utas.edu.au/courses/uc/courses",
    ],
    # University of Newcastle (Australia) — uni_id 17.
    # Cloudflare-protected, plain HTTP returns 403 for every /study/* path
    # so BFS finds 0 candidates. The catalogue lives at /degrees/<slug>.
    # Seed the A-Z degree finder + the level-listing pages so the
    # Playwright pass can harvest the ~280-course international catalogue.
    # Paired with discovery.always_browser_discover=true and
    # must_contain=["/degrees/"] in scraper_config/unis/newcastle.yaml.
    "www.newcastle.edu.au": [
        "https://www.newcastle.edu.au/degrees",
        "https://www.newcastle.edu.au/degrees/undergraduate",
        "https://www.newcastle.edu.au/degrees/postgraduate",
        "https://www.newcastle.edu.au/degrees/research",
    ],
    "newcastle.edu.au": [
        "https://www.newcastle.edu.au/degrees",
        "https://www.newcastle.edu.au/degrees/undergraduate",
        "https://www.newcastle.edu.au/degrees/postgraduate",
        "https://www.newcastle.edu.au/degrees/research",
    ],
    # University of East London (UEL) — www.uel.ac.uk.
    # Site is Cloudflare-protected so BFS returns 0.  Browser discovery falls
    # into generic nav following, which wastes 9+ minutes on low-value pages
    # (apprenticeships, fees, pre-entry, student-life).  Seeding the three
    # known catalogue roots lets the BFS jump straight to course listing pages.
    "www.uel.ac.uk": [
        "https://www.uel.ac.uk/study/undergraduate",
        "https://www.uel.ac.uk/study/postgraduate",
        "https://www.uel.ac.uk/study/courses",
        "https://www.uel.ac.uk/study/all-courses",
        "https://www.uel.ac.uk/study/course-search",
    ],
    "uel.ac.uk": [
        "https://www.uel.ac.uk/study/undergraduate",
        "https://www.uel.ac.uk/study/postgraduate",
        "https://www.uel.ac.uk/study/courses",
        "https://www.uel.ac.uk/study/all-courses",
        "https://www.uel.ac.uk/study/course-search",
    ],
    # University of Huddersfield — uni_id 1166.
    # Both www.hud.ac.uk and courses.hud.ac.uk are React SPAs.  HTTP BFS on
    # www.hud.ac.uk finds only nav/research pages (no taught course links).
    # The full catalogue of 500+ taught courses lives on courses.hud.ac.uk.
    # Seeding the browser at courses.hud.ac.uk/ lets Playwright render the
    # SPA and harvest the JS-injected <a> links to individual course pages.
    # The cross-domain filter in _process_links is relaxed for seed hosts so
    # courses.hud.ac.uk/* links are accepted (see _allowed_hosts below).
    "www.hud.ac.uk": [
        "https://courses.hud.ac.uk/2025-26/",
        "https://courses.hud.ac.uk/2025-26/undergraduate/",
        "https://courses.hud.ac.uk/2025-26/postgraduate/",
    ],
    "hud.ac.uk": [
        "https://courses.hud.ac.uk/2025-26/",
        "https://courses.hud.ac.uk/2025-26/undergraduate/",
        "https://courses.hud.ac.uk/2025-26/postgraduate/",
    ],
    # Queensland University of Technology (QUT) — uni_id 1011.
    # Cloudflare-walled (HTTP 403 for plain BFS) + JS-rendered SPA shells +
    # no sitemap.xml.  Without seeds, the browser-discover homepage-nav
    # walk only reaches /study/qut-college, /study/options/double-degrees,
    # /study/combined-diploma-degrees, and a handful of short-course hubs
    # — yielding 56 candidates from a ~200-course international catalogue.
    # The full Bachelor / Master catalogue lives one click deeper under
    # the per-discipline /study/<area> pages (e.g. /study/business,
    # /study/engineering, /study/health, /study/law), which homepage nav
    # does NOT link to directly.  Symptom of the missing seeds: the
    # 2026-05-17 user-reported QUT scrape staged only ~33 rows (mostly
    # Diplomas + Graduate Certificates + a handful of double-degree
    # bachelors) with virtually every single-degree Bachelor (Business,
    # Engineering, IT, Nursing, Law) and most Masters absent.
    # Pairs with always_browser_discover=true and must_contain=["/courses/"]
    # in scraper_config/unis/qut.yaml (Wayback CDX is enabled but
    # archive.org has been throwing transient 503s for QUT — seeds give
    # us a deterministic discovery path that doesn't depend on Wayback).
    "www.qut.edu.au": [
        "https://www.qut.edu.au/study",
        "https://www.qut.edu.au/study/business",
        "https://www.qut.edu.au/study/creative-industries",
        "https://www.qut.edu.au/study/design-and-architecture",
        "https://www.qut.edu.au/study/education",
        "https://www.qut.edu.au/study/engineering",
        "https://www.qut.edu.au/study/health",
        "https://www.qut.edu.au/study/information-technology",
        "https://www.qut.edu.au/study/justice-and-law",
        "https://www.qut.edu.au/study/science-and-mathematics",
        "https://www.qut.edu.au/study/social-work-and-human-services",
        "https://www.qut.edu.au/study/communication",
        "https://www.qut.edu.au/study/options/double-degrees",
        "https://www.qut.edu.au/study/combined-diploma-degrees",
        "https://www.qut.edu.au/study/qut-college/international",
    ],
    "qut.edu.au": [
        "https://www.qut.edu.au/study",
        "https://www.qut.edu.au/study/business",
        "https://www.qut.edu.au/study/creative-industries",
        "https://www.qut.edu.au/study/design-and-architecture",
        "https://www.qut.edu.au/study/education",
        "https://www.qut.edu.au/study/engineering",
        "https://www.qut.edu.au/study/health",
        "https://www.qut.edu.au/study/information-technology",
        "https://www.qut.edu.au/study/justice-and-law",
        "https://www.qut.edu.au/study/science-and-mathematics",
        "https://www.qut.edu.au/study/social-work-and-human-services",
        "https://www.qut.edu.au/study/communication",
        "https://www.qut.edu.au/study/options/double-degrees",
        "https://www.qut.edu.au/study/combined-diploma-degrees",
        "https://www.qut.edu.au/study/qut-college/international",
    ],
}

_LISTING_URL_RE = re.compile(
    r"/(?:degrees|study|courses?|programs?)"
    r"(?:/courses?)?"
    r"/(?:all|search|list|find(?:-a-course)?|postgrad(?:uate)?(?:-study)?|undergrad(?:uate)?"
    # 2026-05-17 — match per-discipline listing slugs (QUT /study/business,
    # /study/health, /study/justice-and-law, /study/science-and-mathematics,
    # /study/social-work-and-human-services, /study/communication; UTAS
    # /courses/<faculty>; Newcastle /degrees/<area>).  Any single-segment
    # slug after the catalogue prefix is treated as a listing candidate
    # so scroll-to-load fires and lazy-loaded course grids hydrate before
    # the link harvest.  Individual course URLs are filtered out earlier
    # via _looks_like_course in _process_links, so this broadening
    # cannot accidentally re-queue course pages as nav targets.
    r"|[a-z][a-z0-9\-]{2,})",
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


def _is_blocked_nav(url: str, extra_patterns: list[str] | None = None) -> bool:
    """Return True if *url* matches the global nav blocklist or per-uni extras.

    Checked against the URL path (lowercased).  Returns False quickly for
    the common case where nothing matches.
    """
    lpath = urlparse(url).path.lower()
    if any(token in lpath for token in _GLOBAL_NAV_BLOCKLIST):
        return True
    if extra_patterns:
        if any(p.lower() in lpath for p in extra_patterns):
            return True
    return False


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

    # ── Per-uni config ────────────────────────────────────────────────────────
    # Read time budget, early-stop threshold, and extra nav block patterns
    # from the active UniConfig contextvar (set by the orchestrator).
    # Falls back to module-level defaults when no config is active.
    _time_budget_s: int = _DEFAULT_TIME_BUDGET_S
    _early_stop_courses: int = _DEFAULT_EARLY_STOP_COURSES
    _extra_block_patterns: list[str] = []
    try:
        from app.services.scraper.config.context import require_uni_config
        _ucfg = require_uni_config()
        _time_budget_s = getattr(_ucfg.discovery, "browser_time_budget_s", _DEFAULT_TIME_BUDGET_S)
        _early_stop_courses = getattr(_ucfg.discovery, "browser_early_stop_courses", _DEFAULT_EARLY_STOP_COURSES)
        _extra_block_patterns = list(getattr(_ucfg.discovery, "block_nav_patterns", []) or [])
    except Exception:
        pass  # contextvar not set (e.g. test / standalone call) — use defaults

    _t_start = time.monotonic()

    await _emit(
        f"[DISCOVER] Browser: starting for {scrape_url} "
        f"(time_budget={_time_budget_s}s, early_stop={_early_stop_courses} courses)"
    )
    log.info(
        "browser_discover_generic: starting for %s (time_budget=%ds, early_stop=%d)",
        scrape_url, _time_budget_s, _early_stop_courses,
    )

    seen: set[str] = set()
    results: list[dict] = []
    nav_queue: list[str] = []
    _blocked_count: int = 0
    _stop_reason: str = "start_page_only"  # overwritten once the nav BFS begins

    _seed_urls = _HOST_EXTRA_SEEDS.get(host, [])
    if _seed_urls:
        log.info(
            "browser_discover_generic: %d extra seed URLs for %s — "
            "queuing as first nav targets",
            len(_seed_urls), host,
        )
    for seed_url in _seed_urls:
        if seed_url not in seen:
            nav_queue.append(seed_url)
            seen.add(seed_url)

    # Build allowed-hosts set: the primary host + every host referenced in
    # extra seeds.  This lets seeds on a different subdomain/domain (e.g.
    # courses.hud.ac.uk for www.hud.ac.uk) contribute course links without
    # being silently dropped by the cross-domain filter below.
    _allowed_hosts: set[str] = {host}
    for _seed in _seed_urls:
        try:
            _sh = urlparse(_seed).netloc
            if _sh:
                _allowed_hosts.add(_sh)
        except Exception:
            pass

    try:
        from app.services.scraper.discovery import (
            _looks_like_course,
            _is_known_non_course_url,
        )
    except Exception as exc:
        log.warning("browser_discover_generic: cannot import discovery helpers — %s", exc)
        return []

    def _process_links(raw: list[dict]) -> None:
        nonlocal _blocked_count
        for item in raw:
            url = (item.get("url") or "").strip()
            name = (item.get("name") or "").strip()
            if not url:
                continue
            p = urlparse(url)
            if p.netloc and p.netloc not in _allowed_hosts:
                continue
            if url in seen:
                continue
            seen.add(url)
            if _looks_like_course(url, name):
                results.append({"url": url, "name": name})
            elif _is_nav_url(url) and not _is_known_non_course_url(url):
                if _is_blocked_nav(url, _extra_block_patterns):
                    _blocked_count += 1
                    log.debug(
                        "browser_discover_generic: blocked low-value nav %s", url
                    )
                else:
                    nav_queue.append(url)

    # Stealth opt-in (Macquarie etc.): swap the regular browser pool for
    # patchright + Xvfb when the active uni config sets
    # discovery.use_stealth_browser=true.  The yielded `page` has the same
    # Playwright Page surface so the rest of this function is unchanged.
    from app.services.scraper.stealth_browser import (
        stealth_context,
        stealth_required,
    )

    @asynccontextmanager
    async def _open_page():
        # Stealth (patchright + Xvfb) when the uni opts in; fall back to the
        # regular headless pool if the stealth context can't start (Xvfb
        # missing, patchright import error, etc.) so discovery still
        # attempts a fetch instead of silently aborting.
        #
        # IMPORTANT: only fall back on INIT failure (before first yield).
        # Exceptions raised after yield (caller's page.goto, page.evaluate,
        # etc.) must propagate to the caller — wrapping yield in
        # try/except would silently retry whole-page errors against the
        # regular pool and mask real bugs.  Per code-review 2026-05-25.
        if stealth_required():
            log.info("browser_discover_generic: using stealth (patchright+xvfb) for %s", host)
            stealth_cm = stealth_context()
            page = None
            try:
                ctx = await stealth_cm.__aenter__()
                page = await ctx.new_page()
            except Exception as exc:
                log.warning(
                    "browser_discover_generic: stealth init failed for %s (%s) — "
                    "falling back to regular pool",
                    host, exc,
                )
                try:
                    await stealth_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            else:
                try:
                    yield page
                finally:
                    try:
                        if page is not None:
                            await page.close()
                    except Exception:
                        pass
                    try:
                        await stealth_cm.__aexit__(None, None, None)
                    except Exception:
                        pass
                return
        async with _pool.page() as page:
            yield page

    try:
        async with _open_page() as page:
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
            _stop_reason: str = "nav_queue_exhausted"
            while nav_i < len(nav_queue) and nav_i < _MAX_NAV_PAGES:
                # ── Time budget check ─────────────────────────────────────
                _elapsed = time.monotonic() - _t_start
                if _elapsed >= _time_budget_s:
                    _stop_reason = f"time_budget_exceeded ({_elapsed:.0f}s >= {_time_budget_s}s)"
                    log.info(
                        "browser_discover_generic: time budget %ds exceeded after %.0fs "
                        "(%d courses, %d nav pages visited, %d blocked) — stopping",
                        _time_budget_s, _elapsed, len(results), nav_i, _blocked_count,
                    )
                    await _emit(
                        f"[DISCOVER] Browser: time budget {_time_budget_s}s reached "
                        f"({_elapsed:.0f}s elapsed) — stopping with {len(results)} courses"
                    )
                    break

                # ── Early-stop threshold ──────────────────────────────────
                if len(results) >= _early_stop_courses:
                    _stop_reason = f"early_stop_threshold ({len(results)} >= {_early_stop_courses})"
                    log.info(
                        "browser_discover_generic: early-stop threshold %d reached "
                        "(%d courses found, %d nav pages visited) — stopping",
                        _early_stop_courses, len(results), nav_i,
                    )
                    await _emit(
                        f"[DISCOVER] Browser: {len(results)} course links found — "
                        f"early-stop threshold reached, stopping discovery"
                    )
                    break

                if len(results) >= max_courses:
                    _stop_reason = f"max_courses_cap ({max_courses})"
                    break

                nav_url = nav_queue[nav_i]
                nav_i += 1
                if nav_url in nav_visited:
                    continue
                nav_visited.add(nav_url)
                try:
                    await page.goto(
                        nav_url, wait_until="domcontentloaded", timeout=30_000
                    )
                    # Wait for at least one course-shaped link selector to
                    # appear before harvesting.  JS-rendered SPA discipline
                    # pages (QUT /study/<area>, Newcastle /degrees/<area>,
                    # UTAS /courses/<faculty>) hydrate the course grid
                    # asynchronously after domcontentloaded; without this
                    # wait the link extractor races the hydration and
                    # returns 0 candidates.  Caught-timeout fallback keeps
                    # the existing sleep-then-extract behaviour for sites
                    # that don't need the wait.
                    try:
                        await page.wait_for_selector(
                            _NAV_LINK_SELECTOR,
                            state="attached",
                            timeout=_NAV_LINK_WAIT_MS,
                        )
                    except _PwTimeout:
                        pass
                    except Exception:
                        pass
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

    _total_elapsed = time.monotonic() - _t_start

    if len(results) < 3:
        log.warning(
            "browser_discover_generic: only %d course(s) found for %s "
            "(%.0fs elapsed, stop=%s, blocked=%d) — site may be blocking browser too",
            len(results), scrape_url, _total_elapsed, _stop_reason, _blocked_count,
        )
        await _emit(
            f"[DISCOVER] Browser: only {len(results)} course(s) found — "
            "trying next fallback"
        )
        return []

    log.info(
        "browser_discover_generic: discovered %d courses for %s "
        "(%.0fs elapsed, stop=%s, blocked=%d)",
        len(results), scrape_url, _total_elapsed, _stop_reason, _blocked_count,
    )
    await _emit(
        f"[DISCOVER] Browser: discovered {len(results)} course links "
        f"({_total_elapsed:.0f}s, stop={_stop_reason}, blocked_nav={_blocked_count})"
    )
    return results[:max_courses]

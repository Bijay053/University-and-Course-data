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
import heapq
import json as _json
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
    "/sitemap",    "/site-search", "/search-results", "/global-search",
    "/job",        "/career",     "/vacancy",
    "/library",    "/sport",
    "/international-pathways",   # pathway/foundation-level nav
})

# ── Nav URL priority scoring ──────────────────────────────────────────────────
# URLs are scored before being added to the BFS heap.  High-score pages are
# visited first; pages scoring ≤ 0 are skipped entirely.  Per-uni YAML can
# extend the blocklist via discovery.block_nav_patterns; scoring thresholds
# cannot be overridden (they apply fleet-wide).
_HIGH_VALUE_WORDS: tuple[str, ...] = (
    "course", "courses", "program", "programs",
    "undergraduate", "postgraduate", "degree", "degrees",
    "study", "find-courses", "course-search",
)
_LOW_VALUE_WORDS: tuple[str, ...] = (
    "about", "contact", "news", "event",
    "fees", "funding", "scholarship",
    "accommodation", "student-life", "studentlife",
    "apprenticeship", "privacy", "terms", "cookie",
)
# URL path patterns that strongly indicate a course-listing page.
_HIGH_VALUE_PATH_RE: re.Pattern[str] = re.compile(
    r"/(courses?|programs?|undergraduate|postgraduate|study|degrees?|"
    r"find-courses?|course-search|international/courses)[/$?]?",
    re.IGNORECASE,
)
# Minimum score for a nav URL to be queued at all (negative or zero → drop).
_NAV_SCORE_THRESHOLD: int = 1
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
    # Entry intentionally empty: the YAML (uel.yaml seed_urls) is the sole
    # source of seed URLs for UEL.  Previous entries had 18 paginated ?page=N
    # seeds (all 0 links, JS pagination) and later hub pages (/study/undergraduate,
    # /study/postgraduate) that surface only 1–4 featured courses rather than
    # the full catalogue.  The correct listing pages are at
    # /study/undergraduate/courses and /study/postgraduate/courses — those are
    # configured exclusively in the YAML so the admin can change them without a
    # code deploy.  Keeping this entry empty also ensures the hardcoded fallback
    # never silently overrides the YAML-configured seeds.
    "www.uel.ac.uk": [],
    "uel.ac.uk": [],
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


def _score_nav_url(url: str, anchor_text: str = "") -> int:
    """Return a priority score for a candidate nav URL.

    Higher = visit sooner.  Scores ≤ 0 are dropped before queuing.

    Scoring rules (additive):
      +20 per high-value word in path or anchor text
      -30 per low-value word in path or anchor text
      +40 when the path matches _HIGH_VALUE_PATH_RE

    Typical results:
      /study/undergraduate         → 80  (+20 study, +20 undergraduate, +40 path)
      /study/postgraduate          → 80
      /courses/                    → 60  (+20 course, +20 courses, +40 path − dup)
      /about/contact               → -60 (-30 about, -30 contact)
      /news/events                 → -60 (-30 news, -30 event[s])
    """
    text = (urlparse(url).path + " " + anchor_text).lower()
    score = 0
    matched_high: list[str] = []
    matched_low: list[str] = []

    for word in _HIGH_VALUE_WORDS:
        if word in text:
            score += 20
            matched_high.append(word)

    for word in _LOW_VALUE_WORDS:
        if word in text:
            score -= 30
            matched_low.append(word)

    if _HIGH_VALUE_PATH_RE.search(url):
        score += 40

    return score


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
    # Priority max-heap: entries are (-score, tiebreak_counter, url, anchor_text).
    # heapq is a min-heap, so we negate the score to get highest-score-first.
    _nav_heap: list[tuple[int, int, str, str]] = []
    _nav_heap_ctr: int = 0   # stable tiebreaker (insertion order within same score)
    _blocked_count: int = 0
    _stop_reason: str = "start_page_only"  # overwritten once the nav BFS begins

    # ── XHR / JSON-API capture state ──────────────────────────────────────────
    # For sites that load course listings via fetch()/XHR rather than static
    # anchor tags (e.g. UEL Drupal course-search, Solr-backed SPAs), we
    # intercept network responses so we can harvest course URLs from JSON even
    # when the DOM link extractor returns nothing.
    _xhr_courses: list[dict] = []          # courses extracted from JSON responses
    _xhr_endpoints_seen: set[str] = set()  # endpoint URLs already processed
    # Pagination metadata: list of {url, total, page_size} for endpoints where
    # the response advertised more items than one page.  Fetched after BFS ends.
    _xhr_pagination: list[dict] = []

    def _heap_push(url: str, anchor: str, score: int) -> None:
        nonlocal _nav_heap_ctr
        heapq.heappush(_nav_heap, (-score, _nav_heap_ctr, url, anchor))
        _nav_heap_ctr += 1

    _seed_urls = _HOST_EXTRA_SEEDS.get(host, [])
    if _seed_urls:
        log.info(
            "browser_discover_generic: %d extra seed URLs for %s — "
            "scoring and queuing as first nav targets",
            len(_seed_urls), host,
        )
    for seed_url in _seed_urls:
        if seed_url not in seen:
            _seed_score = _score_nav_url(seed_url)
            # Seeds always get queued regardless of score (they're hand-curated
            # for this university); add a +200 bonus to guarantee they visit first.
            _heap_push(seed_url, "", _seed_score + 200)
            seen.add(seed_url)
            log.debug(
                "browser_discover_generic: seed %s queued (score=%d+200)",
                seed_url, _seed_score,
            )

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

    # ── Per-uni config seed_urls (YAML / admin_config) ────────────────────────
    # These come from the admin portal / YAML `discovery.seed_urls` and get a
    # +300 bonus (higher than _HOST_EXTRA_SEEDS +200) so they always run first.
    _cfg_seed_urls: list[str] = getattr(getattr(_ucfg, "discovery", None), "seed_urls", []) or []
    if _cfg_seed_urls:
        log.info(
            "[SEED_URLS] %d configured seed URL(s) for %s — queuing with +300 priority",
            len(_cfg_seed_urls), host,
        )
    for _raw_seed in _cfg_seed_urls:
        # Expand relative paths against the origin
        if _raw_seed.startswith("/"):
            _full_seed = origin_str + _raw_seed
        elif not _raw_seed.startswith("http"):
            _full_seed = scrape_url.rstrip("/") + "/" + _raw_seed.lstrip("/")
        else:
            _full_seed = _raw_seed
        if _full_seed not in seen:
            _cfg_score = _score_nav_url(_full_seed)
            _heap_push(_full_seed, "", _cfg_score + 300)
            seen.add(_full_seed)
            # Allow the seed's host (handles listing pages on a different subdomain)
            try:
                _sh = urlparse(_full_seed).netloc
                if _sh:
                    _allowed_hosts.add(_sh)
            except Exception:
                pass
            log.info(
                "[SEED_URLS] queued %s (score=%d+300)",
                _full_seed, _cfg_score,
            )
        else:
            log.debug("[SEED_URLS] seed %s already queued — skipping duplicate", _full_seed)

    try:
        from app.services.scraper.discovery import (
            _looks_like_course,
            _is_known_non_course_url,
        )
    except Exception as exc:
        log.warning("browser_discover_generic: cannot import discovery helpers — %s", exc)
        return []

    # ── XHR course extractor ──────────────────────────────────────────────────
    def _extract_courses_from_xhr_json(data: object) -> list[dict]:
        """Extract {url, name} pairs from an XHR/fetch JSON response.

        Handles the most common API shapes seen on Drupal, Solr, and
        generic REST course-search endpoints:
          - Top-level array of objects with url/link/path fields
          - {"results": [...], "data": [...], "courses": [...], ...}
          - Drupal JSON:API: {"data": [{"attributes": {"path": {"alias": "..."}}}]}
        """
        candidates: list[dict] = []

        def _from_item(item: object) -> None:
            if not isinstance(item, dict):
                return
            url: str | None = None
            name: str | None = None
            # 1. Direct url/link/href/path/uri fields
            for key in ("url", "link", "href", "path", "uri", "course_url",
                        "courseUrl", "course_link"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    url = val
                    break
                if isinstance(val, dict):  # Drupal path object
                    alias = val.get("alias") or val.get("href") or val.get("url")
                    if isinstance(alias, str) and alias:
                        url = alias
                        break
            # 2. Drupal JSON:API shape: {"attributes": {"path": {"alias": ...}}}
            if not url:
                attrs = item.get("attributes")
                if isinstance(attrs, dict):
                    path_obj = attrs.get("path") or {}
                    url = (
                        attrs.get("url") or attrs.get("link")
                        or (path_obj.get("alias") if isinstance(path_obj, dict) else None)
                    )
                    name = attrs.get("title") or attrs.get("name") or attrs.get("label")
            # 3. Name / title fields
            if not name:
                for key in ("title", "name", "label", "course_name",
                            "courseTitle", "heading", "courseName"):
                    val = item.get(key)
                    if isinstance(val, str) and val:
                        name = val
                        break
            if not url:
                return
            # Resolve relative URLs
            if url.startswith("/"):
                url = origin_str.rstrip("/") + url
            elif not url.startswith("http"):
                return
            # Same-origin / allowed-host guard
            p = urlparse(url)
            if p.netloc and p.netloc not in _allowed_hosts:
                return
            # Must pass course heuristic
            if _looks_like_course(url, name or ""):
                candidates.append({"url": url, "name": name or ""})

        if isinstance(data, list):
            for item in data:
                _from_item(item)
        elif isinstance(data, dict):
            for key in ("results", "data", "courses", "items", "programmes",
                        "hits", "docs", "records", "content", "nodes",
                        "courseList", "course_list"):
                val = data.get(key)
                if isinstance(val, list):
                    for item in val:
                        _from_item(item)
        return candidates

    # ── XHR response handler ──────────────────────────────────────────────────
    async def _on_json_response(response) -> None:  # type: ignore[no-untyped-def]
        """Playwright response listener: capture JSON course-data responses."""
        try:
            ct = (response.headers.get("content-type") or "").lower()
            if "json" not in ct:
                return
            if response.status != 200:
                return
            ep_url = response.url
            if ep_url in _xhr_endpoints_seen:
                return
            rp = urlparse(ep_url)
            if rp.netloc and rp.netloc not in _allowed_hosts:
                return
            text = await response.text()
            if len(text) < 80:
                return
            try:
                data = _json.loads(text)
            except Exception:
                return
            extracted = _extract_courses_from_xhr_json(data)
            _xhr_endpoints_seen.add(ep_url)
            if extracted:
                new_urls = {c["url"] for c in _xhr_courses}
                added = [c for c in extracted if c["url"] not in new_urls]
                _xhr_courses.extend(added)
                log.info(
                    "browser_discover_generic: XHR JSON hit %s → +%d course(s) "
                    "(xhr_total=%d)",
                    ep_url, len(added), len(_xhr_courses),
                )
                await _emit(
                    f"[DISCOVER] XHR API hit: {ep_url} → +{len(added)} course(s)"
                )
                # ── Pagination detection ───────────────────────────────────
                # If the API returned fewer items than the advertised total,
                # remember this endpoint so we can fetch remaining pages after
                # the BFS loop (using page.evaluate to stay in the browser
                # context and bypass Cloudflare).
                if isinstance(data, dict):
                    _reported_total: int | None = None
                    for _tk in ("total", "numFound", "count", "totalResults",
                                "total_results", "totalCount", "totalItems",
                                "total_count", "Total", "total_records"):
                        _tv = data.get(_tk)
                        if isinstance(_tv, int) and _tv > 0:
                            _reported_total = _tv
                            break
                    if _reported_total and _reported_total > len(extracted):
                        log.info(
                            "browser_discover_generic: XHR pagination detected "
                            "at %s — total=%d, page=%d; will fetch remaining pages",
                            ep_url, _reported_total, len(extracted),
                        )
                        await _emit(
                            f"[DISCOVER] Pagination detected: {ep_url} "
                            f"total={_reported_total}, got={len(extracted)} — "
                            f"fetching remaining pages"
                        )
                        _xhr_pagination.append({
                            "url": ep_url,
                            "total": _reported_total,
                            "page_size": len(extracted),
                        })
            else:
                log.debug(
                    "browser_discover_generic: XHR JSON %s — no course links extracted",
                    ep_url,
                )
        except Exception as _xe:
            log.debug("browser_discover_generic: XHR handler error — %s", _xe)

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

            # ── Block low-value URLs BEFORE course detection ──────────────
            # Must come first so pages like /study/apprenticeships or
            # /study/fees-funding are never accepted as course links even
            # when _looks_like_course() would otherwise return True.
            if _is_blocked_nav(url, _extra_block_patterns):
                _blocked_count += 1
                seen.add(url)   # mark seen so it is never re-evaluated
                log.debug(
                    "browser_discover_generic: blocked (blocklist) %s", url
                )
                continue

            seen.add(url)

            if _looks_like_course(url, name):
                results.append({"url": url, "name": name})
            elif _is_nav_url(url) and not _is_known_non_course_url(url):
                # Score the URL before deciding whether to queue it.
                nav_score = _score_nav_url(url, name)
                if nav_score <= _NAV_SCORE_THRESHOLD:
                    _blocked_count += 1
                    log.debug(
                        "browser_discover_generic: blocked (low score=%d) nav %s",
                        nav_score, url,
                    )
                else:
                    _heap_push(url, name, nav_score)
                    log.debug(
                        "browser_discover_generic: queued nav score=%d %s",
                        nav_score, url,
                    )

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

            # ── Register XHR response listener ────────────────────────────
            # Must be attached before any page.goto so responses during the
            # very first navigation (the start page) are captured too.
            page.on("response", lambda r: asyncio.ensure_future(_on_json_response(r)))

            # ── Pre-seed direct navigation ──────────────────────────────────
            # When course listing URLs are known (discovery.seed_urls from YAML
            # or admin Recipe Editor), navigate DIRECTLY to each listing page
            # BEFORE touching the homepage.  This guarantees the real catalogue
            # pages are visited immediately, not discovered via fragile homepage
            # nav-link crawling.
            #
            # Combines _cfg_seed_urls (YAML / admin_config discovery.seed_urls,
            # higher priority — admin-configured, run FIRST) with _seed_urls
            # (hardcoded _HOST_EXTRA_SEEDS fallback, run after YAML seeds).
            # YAML seeds lead so that an admin can update uel.yaml without a
            # code deploy and have those URLs take effect immediately.
            _pre_seeded: set[str] = set()
            _all_configured_seeds = list(dict.fromkeys(
                [u for u in _cfg_seed_urls if u] +   # YAML / admin seeds FIRST
                [u for u in _seed_urls if u]           # hardcoded fallback after
            ))
            if _cfg_seed_urls:
                log.info(
                    "[SEED] Configured seed URLs (from YAML/admin): %s",
                    _cfg_seed_urls,
                )
            if _seed_urls:
                log.info(
                    "[SEED] Hardcoded extra seeds (HOST_EXTRA_SEEDS): %s",
                    [u for u in _seed_urls if u],
                )
            if _all_configured_seeds:
                log.info(
                    "[SEED] Pre-seed direct navigation: %d listing page(s) for %s "
                    "— order: %s",
                    len(_all_configured_seeds), host,
                    ", ".join(_all_configured_seeds),
                )
                await _emit(
                    f"[DISCOVER] Seed: navigating {len(_all_configured_seeds)} "
                    f"course listing page(s) directly"
                )
                await _emit(
                    "[SEED] Configured seed URLs: "
                    + ", ".join(_all_configured_seeds)
                )
                for _sv_url in _all_configured_seeds:
                    if time.monotonic() - _t_start >= _time_budget_s:
                        break
                    _before_seed = len(results)
                    try:
                        await page.goto(
                            _sv_url,
                            wait_until="domcontentloaded",
                            timeout=30_000,
                        )
                        try:
                            await page.wait_for_selector(
                                _NAV_LINK_SELECTOR,
                                state="attached",
                                timeout=_NAV_LINK_WAIT_MS,
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(_SETTLE_S)
                        _sv_raw = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
                        _process_links(_sv_raw or [])
                        # Scroll-to-load for JS-rendered listing pages
                        if _LISTING_URL_RE.search(_sv_url):
                            try:
                                await page.evaluate(_SCROLL_AND_LOAD_JS)
                                _sv_raw2 = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
                                _process_links(_sv_raw2 or [])
                            except Exception:
                                pass
                        _pre_seeded.add(_sv_url)
                    except Exception as _sv_exc:
                        log.warning("[SEED] Failed to navigate %s: %s", _sv_url, _sv_exc)
                    _sv_gained = len(results) - _before_seed
                    log.info(
                        "[SEED] Actually visited: %s → +%d course links (total=%d)",
                        _sv_url, _sv_gained, len(results),
                    )
                    await _emit(
                        f"[DISCOVER] Seed visited: {_sv_url} → +{_sv_gained} courses "
                        f"(total={len(results)})"
                    )

            # If seed pages already found enough courses, skip homepage + BFS
            _seeds_filled = (
                bool(_all_configured_seeds) and len(results) >= _early_stop_courses
            )
            if _seeds_filled:
                log.info(
                    "[SEED] %d courses from listing pages ≥ early_stop=%d — "
                    "skipping homepage BFS entirely",
                    len(results), _early_stop_courses,
                )
                await _emit(
                    f"[DISCOVER] Seed: {len(results)} courses found — "
                    f"skipping homepage BFS (threshold={_early_stop_courses})"
                )

            if not _seeds_filled:
                # ── Navigate the homepage (start URL) ────────────────────────
                # Only when seed URLs did not supply enough course links.
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
                    if not results:
                        return []

                await asyncio.sleep(_SETTLE_S)

                try:
                    snippet = (await asyncio.wait_for(page.content(), timeout=5.0))[:2000].lower()
                    if any(k in snippet for k in (
                        "neterror", "err_connection", "chrome-error://", "err_name_not_resolved"
                    )):
                        await _emit("[DISCOVER] Browser: Chromium error page detected — aborting")
                        if not results:
                            return []
                except Exception:
                    pass

                try:
                    raw = await page.evaluate(_EXTRACT_LINKS_JS, origin_str)
                except Exception as exc:
                    log.warning("browser_discover_generic: link extraction failed — %s", exc)
                    await _emit(f"[DISCOVER] Browser: link extraction failed — {exc}")
                    if not results:
                        return []
                    raw = []

                _process_links(raw or [])
                await _emit(
                    f"[DISCOVER] Browser: start page → {len(results)} course links, "
                    f"{len(_nav_heap)} nav candidates queued (priority-scored)"
                )
                log.info(
                    "browser_discover_generic: start page %s → %d courses, "
                    "%d priority-scored nav links queued",
                    scrape_url, len(results), len(_nav_heap),
                )

            # ── Priority BFS over nav links ────────────────────────────────
            # The heap is a max-heap by score (highest-scoring URL visited
            # first).  _process_links scores and pushes newly discovered nav
            # URLs so multi-level hierarchies (ECU: homepage → study-area →
            # course) are handled automatically.
            nav_visited: set[str] = set(_pre_seeded)  # skip URLs already visited in pre-seed phase
            nav_pages_visited: int = 0
            _stop_reason: str = "nav_heap_exhausted"
            while _nav_heap and nav_pages_visited < _MAX_NAV_PAGES:
                # ── Time budget check ─────────────────────────────────────
                _elapsed = time.monotonic() - _t_start
                if _elapsed >= _time_budget_s:
                    _stop_reason = f"time_budget_exceeded ({_elapsed:.0f}s >= {_time_budget_s}s)"
                    log.info(
                        "browser_discover_generic: time budget %ds exceeded after %.0fs "
                        "(%d courses, %d nav pages visited, %d blocked) — stopping",
                        _time_budget_s, _elapsed, len(results),
                        nav_pages_visited, _blocked_count,
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
                        _early_stop_courses, len(results), nav_pages_visited,
                    )
                    await _emit(
                        f"[DISCOVER] Browser: {len(results)} course links found — "
                        f"early-stop threshold reached, stopping discovery"
                    )
                    break

                if len(results) >= max_courses:
                    _stop_reason = f"max_courses_cap ({max_courses})"
                    break

                # Pop the highest-scoring nav candidate.
                neg_score, _ctr, nav_url, nav_anchor = heapq.heappop(_nav_heap)
                nav_score = -neg_score
                nav_pages_visited += 1

                if nav_url in nav_visited:
                    continue
                nav_visited.add(nav_url)

                log.info(
                    "browser_discover_generic: visiting nav page #%d "
                    "score=%d courses_so_far=%d heap_remaining=%d url=%s",
                    nav_pages_visited, nav_score, len(results),
                    len(_nav_heap), nav_url,
                )
                await _emit(
                    f"[DISCOVER] Browser: nav #{nav_pages_visited} "
                    f"score={nav_score} → {nav_url} "
                    f"(courses={len(results)}, heap={len(_nav_heap)})"
                )
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
                            f"[DISCOVER] Browser: nav #{nav_pages_visited} "
                            f"score={nav_score} → +{gained} course links "
                            f"(total={len(results)}) {nav_url}"
                        )
                        log.info(
                            "browser_discover_generic: nav #%d score=%d %s "
                            "→ +%d courses (total=%d, heap=%d)",
                            nav_pages_visited, nav_score, nav_url,
                            gained, len(results), len(_nav_heap),
                        )
                        # Log the actual URLs found at this nav page (DEBUG)
                        _new_urls = [r["url"] for r in results[-gained:]]
                        for _u in _new_urls:
                            log.debug(
                                "browser_discover_generic: nav #%d found course %s",
                                nav_pages_visited, _u,
                            )
                    else:
                        log.debug(
                            "browser_discover_generic: nav #%d score=%d %s "
                            "→ 0 new courses (total=%d, heap=%d)",
                            nav_pages_visited, nav_score, nav_url,
                            len(results), len(_nav_heap),
                        )
                except Exception as exc:
                    log.debug(
                        "browser_discover_generic: nav page %s (score=%d) failed — %s",
                        nav_url, nav_score, exc,
                    )

            # ── XHR pagination: fetch remaining pages via browser fetch() ─────
            # For endpoints that reported more items than one page, use
            # page.evaluate(fetch(...)) to load subsequent pages from inside
            # the browser context so Cloudflare / cookie state is preserved.
            if _xhr_pagination and page:
                from urllib.parse import urlparse as _uparse, parse_qs as _pqs, urlencode as _uenc, urlunparse as _uunparse
                _PAGINATION_SAFETY_CAP = 400  # never fetch more than this many extra courses
                for _pg_info in _xhr_pagination:
                    _base_url  = _pg_info["url"]
                    _total     = _pg_info["total"]
                    _page_size = _pg_info["page_size"]
                    _fetched   = _page_size
                    _parsed    = _uparse(_base_url)
                    _params    = _pqs(_parsed.query, keep_blank_values=True)
                    # Detect offset / page parameter name used in the URL
                    _offset_key = next((k for k in _params if k in ("offset", "start", "from", "skip")), None)
                    _page_key   = next((k for k in _params if k in ("page", "p", "pageNum", "page_number", "pg")), None)
                    # If no pagination param in URL, assume offset-based and inject one
                    if not _offset_key and not _page_key:
                        _offset_key = "offset"
                    _consecutive_empty = 0
                    while _fetched < min(_total, _PAGINATION_SAFETY_CAP) and _consecutive_empty < 2:
                        if _offset_key:
                            _params[_offset_key] = [str(_fetched)]
                            # Keep limit/size/rows consistent with what we already have
                            for _sz_key in ("limit", "size", "rows", "per_page", "pageSize"):
                                if _sz_key not in _params:
                                    _params[_sz_key] = [str(_page_size)]
                                break
                        elif _page_key:
                            _params[_page_key] = [str(_fetched // _page_size + 1)]
                        _next_qs  = "&".join(f"{k}={_v}" for k, vs in _params.items() for _v in vs)
                        _next_url = _uunparse(_parsed._replace(query=_next_qs))
                        try:
                            _page_text = await page.evaluate(
                                """async (url) => {
                                    try {
                                        const r = await fetch(url, {credentials: 'include'});
                                        if (!r.ok) return null;
                                        const ct = r.headers.get('content-type') || '';
                                        if (!ct.includes('json')) return null;
                                        return await r.text();
                                    } catch(e) { return null; }
                                }""",
                                _next_url,
                            )
                            if not _page_text:
                                _consecutive_empty += 1
                                _fetched += _page_size
                                continue
                            _page_data = _json.loads(_page_text)
                            _page_items = _extract_courses_from_xhr_json(_page_data)
                            if not _page_items:
                                _consecutive_empty += 1
                                _fetched += _page_size
                                continue
                            _consecutive_empty = 0
                            _existing_urls = {c["url"] for c in _xhr_courses}
                            _new_items = [c for c in _page_items if c["url"] not in _existing_urls]
                            _xhr_courses.extend(_new_items)
                            log.info(
                                "browser_discover_generic: XHR paginate offset=%d → +%d course(s) (xhr_total=%d)",
                                _fetched, len(_new_items), len(_xhr_courses),
                            )
                            _fetched += _page_size
                        except Exception as _pe:
                            log.debug("browser_discover_generic: XHR paginate error at offset=%d: %s", _fetched, _pe)
                            break
                if _xhr_pagination:
                    await _emit(
                        f"[DISCOVER] XHR pagination complete: {len(_xhr_courses)} total course(s) from API"
                    )

            # ── Merge XHR-captured courses into DOM results ────────────────
            # Give the event loop a moment to drain any in-flight response
            # callbacks before we read _xhr_courses.
            await asyncio.sleep(0.2)
            if _xhr_courses:
                _dom_urls = {r["url"] for r in results}
                _xhr_new = [c for c in _xhr_courses if c["url"] not in _dom_urls]
                if _xhr_new:
                    log.info(
                        "browser_discover_generic: merging %d XHR course(s) from "
                        "%d JSON endpoint(s) — DOM found %d",
                        len(_xhr_new), len(_xhr_endpoints_seen), len(results),
                    )
                    await _emit(
                        f"[DISCOVER] XHR merge: +{len(_xhr_new)} course(s) from "
                        f"{len(_xhr_endpoints_seen)} JSON API endpoint(s)"
                    )
                    results.extend(_xhr_new)
                else:
                    log.info(
                        "browser_discover_generic: XHR captured %d endpoint(s) "
                        "but all %d course(s) already in DOM results",
                        len(_xhr_endpoints_seen), len(_xhr_courses),
                    )
            elif _xhr_endpoints_seen:
                log.info(
                    "browser_discover_generic: %d JSON endpoint(s) captured "
                    "but none yielded recognisable course links",
                    len(_xhr_endpoints_seen),
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

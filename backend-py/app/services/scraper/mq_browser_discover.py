"""Browser-based discovery for Macquarie University's find-a-course catalogue.

Macquarie's catalogue at https://www.mq.edu.au/study/find-a-course is a
Svelte-based SPA served behind Cloudflare. Two compounding problems break
the default discovery path:

1. **Cloudflare 403 on plain HTTP** — ``curl https://www.mq.edu.au/`` returns
   HTTP 403 with ``cf-mitigated: challenge``. The BFS HTTP crawler therefore
   yields zero candidates. (mq.yaml sets ``always_browser_discover: true`` to
   bypass.)

2. **URL shape mismatch** — every other Australian university we scrape
   exposes course detail pages at ``/courses/<slug>``, ``/course/<slug>``,
   ``/degrees/<slug>`` or ``/programs/<slug>``. Macquarie does NOT: real
   course URLs live at::

       /study/find-a-course/undergraduate/<slug>
       /study/find-a-course/postgraduate/<slug>
       /study/find-a-course/undergraduate/<faculty>/<slug>  (combined / co-op)

   ``browser_discover_generic._NAV_LINK_SELECTOR`` and ``_looks_like_course``
   require ``/courses/`` or sibling tokens to be present in the path — so the
   generic browser pass harvests only the 6 nav links from the homepage and
   stages them as junk courses (the user-reported "Undergraduate", "Browse
   all degrees" etc. that the guards now block).

This module is a Macquarie-specific browser sweep modelled on
``csu_browser_discover.py``:

* Visits the three catalogue seed pages
  (find-a-course, /undergraduate, /postgraduate).
* For each page: waits for the Svelte course grid to hydrate, scrolls to the
  bottom in small steps to trigger any lazy-load, and harvests every anchor
  whose path matches the MQ course-URL regex.
* Dedupes by URL, drops listing roots and major / specialisation sub-pages.
* Returns ``[{"url": str, "name": str}, ...]`` or ``[]`` on failure (caller
  falls back to ``browser_discover_generic`` then Wayback CDX).

Discovery floor / defence-in-depth
----------------------------------
A successful sweep should return at least ~150 course URLs (Macquarie's
catalogue is ~300 UG+PG). When the count falls below
``_DISCOVERY_FLOOR``, this module emits a loud ``[DISCOVER] MQ: WARNING``
status so the operator notices the regression in the live job log — but it
still returns whatever it found so partial discovery is better than zero.

Live verification
-----------------
The module cannot be exercised from the Replit dev sandbox because the
Cloudflare layer challenges headless Chromium with our outbound IP range.
Set ``MQ_LIVE_TEST=1`` and run the smoke test from a network that MQ
accepts (the user's local machine, the prod droplet, etc.)::

    cd /root/University-and-Course-data && \\
        cd backend-py && PYTHONPATH=. MQ_LIVE_TEST=1 \\
        python -m pytest tests/test_mq_browser_discover.py -k live -v -s
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Faculty subpages — the 4 MQ faculties each publish a per-faculty course
# index.  Tried FIRST because they render course anchors in plain HTML
# without needing filter UI interaction (the catalogue landing pages are
# pure-SPA search shells that show filters + faculty cards only, no
# course links until the user clicks something).
_FACULTY_SEED_URLS: tuple[str, ...] = (
    "https://www.mq.edu.au/study/find-a-course/arts",
    "https://www.mq.edu.au/study/find-a-course/business",
    "https://www.mq.edu.au/study/find-a-course/medicine-and-health-sciences",
    "https://www.mq.edu.au/study/find-a-course/science-and-engineering",
)

# Catalogue landing seeds — visited AFTER the faculty pages.  These are
# the SPA search shells; they require filter UI interaction to populate
# course anchors (see ``_interactive_filter_harvest``).  Mirrors the
# entries in ``_HOST_EXTRA_SEEDS`` so the two discovery paths agree on
# which roots to enumerate.
_CATALOGUE_SEED_URLS: tuple[str, ...] = (
    "https://www.mq.edu.au/study/find-a-course",
    "https://www.mq.edu.au/study/find-a-course/undergraduate",
    "https://www.mq.edu.au/study/find-a-course/postgraduate",
)

_SEED_URLS: tuple[str, ...] = _FACULTY_SEED_URLS + _CATALOGUE_SEED_URLS

_MQ_ORIGIN = "https://www.mq.edu.au"

# Course-detail URL regex.  Allows one OPTIONAL faculty segment between the
# level token and the course slug to cover MQ's combined-degree and co-op
# listings (e.g. ``/undergraduate/combined-bachelor-master-degrees/
# bachelor-of-laws-master-of-laws`` and ``/undergraduate/employability-
# initiatives/cooperative-education-program-in-actuarial-studies``).
#
# Listing roots — ``/undergraduate``, ``/postgraduate``, ``/research``,
# ``/undergraduate/combined-bachelor-master-degrees`` (path ends here) —
# deliberately do NOT match because they have no trailing slug segment.
#
# ``courses`` is the generic sub-path used by the coursehandbook resolver
# when it constructs admissions URLs (``/study/find-a-course/courses/<slug>``).
# ``research`` was omitted from the original regex, silently dropping all
# research degrees (Doctor of Philosophy, Professional Doctorates, etc.)
# from the browser-sweep harvest tier.
_COURSE_PATH_RE = re.compile(
    r"^/study/find-a-course/"
    r"(?:undergraduate|postgraduate|research|courses)"
    r"(?:/[^/]+){1,2}/?$"
)

# Search-result link regex — broader than _COURSE_PATH_RE because the
# /search page links directly to the admissions URL for each course, which
# may use any of the four level tokens above.  Does NOT require a trailing
# slug segment count (the search page guarantees it's a real course page,
# not a listing root).
_SEARCH_COURSE_LINK_RE = re.compile(
    r"^/study/find-a-course/"
    r"(?:undergraduate|postgraduate|research|courses)"
    r"/[^/?#]+",
    re.IGNORECASE,
)

# Last-segment slugs that look like a course URL but are category /
# wizard / builder pages.  Belt-and-suspenders alongside ``mq.yaml``'s
# ``block_url_patterns`` and ``guards.is_blocked_page``.
_LISTING_LAST_SEGMENTS: frozenset[str] = frozenset({
    "combined-bachelor-master-degrees",
    "double-degree-builder",
    "browse-all-degrees",
    "view-degrees",
    "view-all-degrees",
})

# Path substrings that always indicate a sub-degree page (a major or
# specialisation), not a real course.
_BLOCKED_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/find-a-course/courses/major/",
    "/find-a-course/courses/specialisation/",
    "/find-a-course/courses/specialization/",
)

# Selector that waits for the catalogue/faculty page to hydrate.  Faculty
# subpages render plain ``/study/find-a-course/<slug>`` anchors, while the
# catalogue landing pages only show course-shape anchors AFTER filter
# interaction (handled by ``_interactive_filter_harvest``).  Match the
# broadest catalogue-relative shape so faculty pages don't time out
# waiting for the narrower UG/PG-anchored variant that never appears
# there.
_HYDRATE_WAIT_SELECTOR = "a[href*='/study/find-a-course/']"
_HYDRATE_WAIT_MS = 12_000

# Filter buttons / chips we try on catalogue landing pages to coax the
# SPA into rendering course results.  Tried in order; the FIRST one that
# resolves to a visible, clickable element fires.  Defensive — every
# click is wrapped in try/except so a missing selector never aborts the
# sweep.  Sourced from common SPA patterns; the live MQ filter UI uses
# accessible labels so role+name selectors are the most resilient.
_FILTER_CLICK_SELECTORS: tuple[str, ...] = (
    "button:has-text('Undergraduate')",
    "button:has-text('Postgraduate')",
    "label:has-text('Undergraduate')",
    "label:has-text('Postgraduate')",
    "a:has-text('All courses')",
    "a:has-text('View all')",
    "button:has-text('Search')",
    "button:has-text('Apply filters')",
)

# Scroll loop bounds
_MAX_SCROLL_ITERS = 25
_SCROLL_SETTLE_S = 1.5
_INITIAL_SETTLE_S = 4.0

# Discovery floor: when total deduped URLs across all seeds falls below
# this we emit a [DISCOVER] MQ: WARNING.  MQ publishes 367 international
# courses as of August 2026; 300 is a reasonable two-thirds cushion.
_DISCOVERY_FLOOR = 300

# Hard cap mirrors the CSU module — guard against runaway harvests if MQ
# ever exposes a duplicated link grid.  Capped well above _DISCOVERY_FLOOR
# so the warning fires before the cap.
_HARD_MAX_LINKS = 1_500

# Extract every ``<a href>`` from the DOM and resolve it against the page
# origin.  Returns ``[{href, text}, ...]`` so the Python caller can apply
# the canonical URL filter.
_EXTRACT_ANCHORS_JS = r"""
() => {
  const ORIGIN = 'https://www.mq.edu.au';
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    if (!raw || raw.startsWith('mailto:') || raw.startsWith('tel:')
        || raw.startsWith('#') || raw.startsWith('javascript:')) {
      return;
    }
    let url;
    try { url = new URL(raw, ORIGIN).href; } catch (_) { return; }
    const text = (a.innerText || a.textContent || '')
      .replace(/\s+/g, ' ').trim();
    out.push({ href: url, text });
  });
  return out;
}
"""


# ── Coursehandbook sitemap discovery ──────────────────────────────────────
# The real, complete MQ course catalogue lives at
# ``coursehandbook.mq.edu.au`` (a Squiz-fronted handbook host, NOT the
# Svelte SPA at ``www.mq.edu.au/study/find-a-course``).  Its sitemap
# index at ``/sitemap.xml`` lists 14 child sitemaps containing ~28K URLs
# across years 2020-2027 in three shapes:
#
#     /YYYY/courses/CXXXXXX       — actual course detail pages (the target)
#     /YYYY/units/<UNITCODE>      — individual subjects (NOT courses)
#     /YYYY/aos/NXXXXXX           — areas-of-study / majors (NOT courses)
#     /YYYY/doubledegree/DXXXXXX  — combined degrees (NOT individual courses)
#
# We harvest ONLY ``/YYYY/courses/CXXXXXX`` for the current year + next
# year (the user-facing UI defaults to the current academic year and
# offers the next year as a tab; older years are still served but
# represent expired offerings we do not want to stage).
#
# Probed 2026-05-25 from Replit sandbox via stealth: the index returns
# 200 + 14 child sitemap URLs, child sitemap-1 contains
# ``/2026/courses/C000001`` -> "Bachelor of Biodiversity and Conservation",
# which proves the host is reachable and the URLs render real course HTML.
_COURSEHANDBOOK_SITEMAP_INDEX = (
    "https://coursehandbook.mq.edu.au/sitemap.xml"
)
_COURSEHANDBOOK_COURSE_RE = re.compile(
    r"^https://coursehandbook\.mq\.edu\.au/(\d{4})/courses/C\d+/?$"
)
_COURSEHANDBOOK_SITEMAP_TIMEOUT_S = 30.0

# After harvesting coursehandbook URLs (which point at the academic catalogue —
# descriptions, learning outcomes, credit points, but NO fees / IELTS /
# session / campus data), we resolve each to its equivalent admissions page
# at www.mq.edu.au/study/find-a-course/courses/<slug>. The admissions pages
# DO have fee, IELTS, session, campus, study-mode data (verified live
# 2026-05-25 on bachelor-of-arts, bachelor-of-biodiversity-and-conservation,
# bachelor-of-environment, bachelor-of-chiropractic-science, master-of-
# business-administration — 9/10 sample courses returned 200 with
# "Estimated annual fee AUD $XX,XXX", "Session 1 (23 February 2026)",
# "North Ryde", "International student" toggle). Coursehandbook is the
# academic-staff handbook, not the prospective-student admissions site.
_STUDY_URL_BASE = "https://www.mq.edu.au/study/find-a-course/courses/"
# Parallel batch size for the resolver pass — each stealth goto takes
# ~2-3s, so 6 parallel keeps a 350-course resolve under ~3 minutes.
_RESOLVE_PARALLEL = 6
# Per-page timeout for the title-only resolve goto (no body wait required;
# <title> is in the SPA shell static HTML).
_RESOLVE_GOTO_TIMEOUT_MS = 15_000
# Strip the "| Macquarie University" or " - Macquarie University" suffix
# that some coursehandbook titles carry. The bare course name is what
# slugifies to the admissions URL.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:\||\-|–|—)\s*Macquarie\s+University\s*$", re.I,
)
# Slug character whitelist: lowercase letters, digits, hyphen. Anything
# else (parens, slashes, ampersands, apostrophes, commas) is replaced by
# a hyphen, and runs of hyphens are collapsed. Matches the canonical
# www.mq.edu.au URL shape (e.g. "Bachelor of Game Design and Development"
# → "bachelor-of-game-design-and-development").
_SLUG_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
# Restrict to recent academic years.  The handbook keeps prior-year
# offerings live (2020+) which would explode the harvest to ~2K stale
# URLs; only the previous + current + next year are real catalogue.
#
# Including the previous year (2025) is intentional: the coursehandbook
# sitemap has 205 entries for 2025 vs 177 for 2026 — many are the same
# programs with different year prefixes, but ~50-80 are unique courses
# only in 2025 (discontinued in 2026 or not yet re-published).  Since
# all handbook URLs resolve to year-agnostic admissions URLs at
# www.mq.edu.au/study/find-a-course/courses/<slug>, duplicates are
# deduplicated by the resolver so stale-year entries never double-count.
# Courses genuinely discontinued will either 404 (skipped) or show
# "not currently available" (rejected by guards).  Net effect: ~+50-80
# additional unique admissions URLs discovered per run.
import datetime as _dt
_THIS_YEAR = _dt.date.today().year
_COURSEHANDBOOK_YEARS: frozenset[str] = frozenset({
    str(_THIS_YEAR - 1), str(_THIS_YEAR), str(_THIS_YEAR + 1),
})


async def _discover_from_coursehandbook_sitemap(
    emit_fn,
    *,
    max_courses: int,
) -> list[dict]:
    """Harvest MQ course URLs from coursehandbook.mq.edu.au sitemaps.

    Uses the stealth context (patchright + xvfb) to bypass the
    Cloudflare challenge that fronts the handbook host for the sitemap
    XML files.  The per-course title resolver that follows uses plain
    httpx (coursehandbook.mq.edu.au course-detail pages are accessible
    without a browser; the sitemap index/child URLs are not reliably so).

    Returns ``[{"url": admissions_url, "name": course_name}, ...]``
    deduped, capped at *max_courses*, or ``[]`` on any failure (caller
    falls back to the widget sweep).
    """
    try:
        from app.services.scraper.stealth_browser import stealth_context
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: stealth_browser unavailable for "
            "coursehandbook sitemap — %s", exc,
        )
        return []

    await emit_fn(
        f"[DISCOVER] MQ: trying coursehandbook sitemap "
        f"(years={sorted(_COURSEHANDBOOK_YEARS)})"
    )

    course_urls: set[str] = set()
    try:
        async with stealth_context() as ctx:
            page = await ctx.new_page()

            # Step 1: fetch the sitemap index → list of child sitemap URLs
            try:
                await page.goto(
                    _COURSEHANDBOOK_SITEMAP_INDEX,
                    wait_until="domcontentloaded",
                    timeout=int(_COURSEHANDBOOK_SITEMAP_TIMEOUT_S * 1000),
                )
                index_body = await page.content()
            except Exception as exc:  # noqa: BLE001
                await emit_fn(
                    f"[DISCOVER] MQ: coursehandbook index unreachable "
                    f"({exc!r}); falling back to widget sweep"
                )
                return []

            child_sitemaps = re.findall(
                r"<loc>([^<]+\.xml)</loc>", index_body
            )
            if not child_sitemaps:
                await emit_fn(
                    "[DISCOVER] MQ: coursehandbook index returned no "
                    "child sitemaps; falling back to widget sweep"
                )
                return []

            await emit_fn(
                f"[DISCOVER] MQ: coursehandbook index → "
                f"{len(child_sitemaps)} child sitemap(s)"
            )

            # Step 2: walk each child sitemap, filter to target-year
            # /courses/CXXXX URLs.  Sitemap-1 + sitemap-3 each hold ~10K
            # URLs (units + aos + doubledegree dominate), so the filter
            # is strict and we exit early on max_courses.
            for child in child_sitemaps:
                if len(course_urls) >= max_courses:
                    break
                try:
                    await page.goto(
                        child,
                        wait_until="domcontentloaded",
                        timeout=int(_COURSEHANDBOOK_SITEMAP_TIMEOUT_S * 1000),
                    )
                    body = await page.content()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: child sitemap %s failed: %s",
                        child, exc,
                    )
                    continue

                for loc in re.findall(r"<loc>([^<]+)</loc>", body):
                    m = _COURSEHANDBOOK_COURSE_RE.match(loc)
                    if m and m.group(1) in _COURSEHANDBOOK_YEARS:
                        course_urls.add(loc.rstrip("/"))
                        if len(course_urls) >= max_courses:
                            break
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: coursehandbook sitemap pass failed: %s",
            exc,
        )
        return []

    handbook_urls = sorted(course_urls)
    await emit_fn(
        f"[DISCOVER] MQ: coursehandbook sitemap harvested "
        f"{len(handbook_urls)} course URL(s); resolving to admissions URLs…"
    )

    # ── Resolve coursehandbook URLs → www.mq.edu.au admissions URLs ─────
    # Coursehandbook is the ACADEMIC catalogue (descriptions, learning
    # outcomes, credit points) and contains NO fee / IELTS / session /
    # campus data. The admissions pages at
    # www.mq.edu.au/study/find-a-course/courses/<slug> are where all the
    # student-facing data lives. We render each coursehandbook URL just
    # long enough to read its <title> tag (present in the SPA shell
    # static HTML — no body wait needed), slugify the name, and emit
    # the equivalent admissions URL. Verified live 2026-05-25.
    study_courses = await _resolve_to_study_urls(handbook_urls, emit_fn)
    await emit_fn(
        f"[DISCOVER] MQ: resolved {len(study_courses)}/{len(handbook_urls)} "
        f"coursehandbook URLs to admissions URLs"
    )
    return study_courses[:max_courses]


def _slugify_course_name(name: str) -> str:
    """Convert a course name to its www.mq.edu.au URL slug.

    Examples (verified against live admissions URLs):
      "Bachelor of Arts" → "bachelor-of-arts"
      "Bachelor of Game Design and Development"
        → "bachelor-of-game-design-and-development"
      "Master of Business Administration"
        → "master-of-business-administration"

    Strips the "| Macquarie University" page-title suffix first when
    present (some pages carry it, others don't), lowercases, replaces
    any non-alphanumeric run with a single hyphen, and trims leading/
    trailing hyphens.
    """
    if not name:
        return ""
    cleaned = _TITLE_SUFFIX_RE.sub("", name.strip())
    slug = _SLUG_NON_WORD_RE.sub("-", cleaned.lower()).strip("-")
    return slug


async def _resolve_to_study_urls(
    handbook_urls: list[str],
    emit_fn,
) -> list[dict]:
    """For each coursehandbook URL, extract the course name from <title>
    and construct the equivalent www.mq.edu.au admissions URL.

    coursehandbook.mq.edu.au is NOT Cloudflare-protected — plain httpx
    returns 200 OK with the correct per-course <title> in the static HTML
    (verified 2026-07-24: C000001 → "Bachelor of Biodiversity and
    Conservation", 208 KB SSR response, no CF challenge).  Using plain
    httpx instead of patchright is therefore both faster (~200 ms/request
    vs 10-15 s browser goto) and more reliable — the old patchright-based
    resolver timed out on ~256/383 courses because the 15 s per-page limit
    was exhausted by browser startup + 208 KB page load, silently dropping
    two-thirds of the catalogue.

    Runs 20 concurrent httpx requests (no rate-limit risk; coursehandbook
    is a lightweight CDN-backed SSR app).  Returns
    ``[{"url": admissions_url, "name": title}, ...]`` deduped on
    admissions URL.

    URLs whose <title> can't be parsed (empty / "Handbook" site-nav title
    / fetch error) are skipped with a warning rather than emitted with a
    bad slug.
    """
    if not handbook_urls:
        return []

    import httpx as _httpx

    _HTTPX_PARALLEL = 20
    _HTTPX_TIMEOUT = 20.0  # per-request; 208 KB SSR page ~200 ms normally

    out_by_url: dict[str, dict] = {}
    skipped = 0
    sem = asyncio.Semaphore(_HTTPX_PARALLEL)

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    async def _resolve_one(client: "_httpx.AsyncClient", handbook_url: str) -> tuple[str, str] | None:
        async with sem:
            try:
                r = await client.get(handbook_url, timeout=_HTTPX_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                log.warning("mq resolver: httpx GET %s failed: %s", handbook_url, exc)
                return None
            if r.status_code >= 400:
                log.warning("mq resolver: %s → HTTP %s", handbook_url, r.status_code)
                return None
            body = r.text
        m = re.search(r"<title>([^<]+)</title>", body, re.I)
        if not m:
            return None
        raw_title = m.group(1).strip()
        # "Handbook" is the fallback site-nav title when the SSR didn't
        # inject a per-course override; skip rather than emit
        # /courses/handbook as a garbage admissions URL.
        if not raw_title or raw_title.lower() in ("handbook", "macquarie university handbook"):
            return None
        slug = _slugify_course_name(raw_title)
        if not slug or len(slug) < 3:
            return None
        admissions_url = f"{_STUDY_URL_BASE}{slug}"
        return (admissions_url, raw_title)

    try:
        async with _httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
        ) as client:
            tasks = [
                asyncio.create_task(_resolve_one(client, url))
                for url in handbook_urls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                skipped += 1
            elif result is None:
                skipped += 1
            else:
                admissions_url, name = result
                if admissions_url not in out_by_url:
                    out_by_url[admissions_url] = {"url": admissions_url, "name": name}
    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: httpx resolver pass raised: %s", exc)

    if skipped:
        await emit_fn(
            f"[DISCOVER] MQ: resolver skipped {skipped} URL(s) with "
            f"unparseable <title> or fetch error"
        )
    return sorted(out_by_url.values(), key=lambda d: d["url"])


async def _discover_from_search_page(
    emit_fn,
    *,
    max_courses: int,
) -> list[dict]:
    """Harvest MQ course URLs from the /search page via stealth browser.

    MQ's search page (https://www.mq.edu.au/search?query=&category=courses)
    is backed by Squiz Matrix / Funnelback and shows every course in the
    catalogue.  As of August 2026 this includes 367 international courses
    — including research degrees and combined degrees that are absent from
    the coursehandbook sitemap.

    The page is Cloudflare Enterprise-protected (same as the rest of
    www.mq.edu.au); the stealth browser (patchright + Xvfb) passes it
    cleanly.

    Pagination strategy
    -------------------
    Squiz Matrix supports ``start_rank`` (1-based offset) and ``num_ranks``
    (results per page) as URL parameters.  We load pages of 100 at a time
    until either the page returns zero *new* course anchors (pagination
    exhausted or params ignored + wrap-around detected) or ``max_courses``
    is reached.  Hard cap: 10 pages × 100 = 1 000 >> 367, so runaway is
    impossible.

    Fallback: if URL-param pagination stalls after page 1 (same 10 results
    on every page), we attempt click-based "next page" navigation as a
    secondary approach before giving up.

    Returns ``[{"url": str, "name": str}, ...]`` deduped, or ``[]`` on
    any failure (caller's handbook + browser-sweep tiers still run).
    """
    try:
        from app.services.scraper.stealth_browser import stealth_context
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_search_discover: stealth_browser unavailable — %s", exc,
        )
        return []

    await emit_fn(
        "[DISCOVER] MQ: search-page tier — paginating "
        "/search?query=&category=courses"
    )

    out: dict[str, dict] = {}
    _PER_PAGE = 100    # num_ranks per request; Squiz Matrix cap is typically 100
    _MAX_PAGES = 10    # hard cap: 10 × 100 = 1 000 > 367

    # JS: extract all anchors pointing at /study/find-a-course/ paths.
    _SEARCH_EXTRACT_JS = r"""
() => {
  const ORIGIN = 'https://www.mq.edu.au';
  const PREFIX = '/study/find-a-course/';
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    if (!raw) return;
    let url;
    try { url = new URL(raw, ORIGIN).href; } catch (_) { return; }
    if (!url.startsWith(ORIGIN + PREFIX)) return;
    const path = url.slice(ORIGIN.length);
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
    out.push({ href: url, path, text });
  });
  return out;
}
"""

    # Selectors for a "Next page" or "Load more" button — tried in order if
    # the URL-param pagination stalls.
    _NEXT_PAGE_SELECTORS = (
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a[rel='next']",
        "a:has-text('Load more')",
        "button:has-text('Load more')",
        "a:has-text('Show more')",
        "button:has-text('Show more')",
    )

    def _accept_search_url(href: str) -> str | None:
        """Return the clean course URL if *href* matches a real course page."""
        try:
            from urllib.parse import urlparse as _up
            p = _up(href).path
        except Exception:  # noqa: BLE001
            return None
        if not _SEARCH_COURSE_LINK_RE.match(p):
            return None
        # Drop query-string / fragment; normalise trailing slash.
        return f"https://www.mq.edu.au{p.rstrip('/')}"

    try:
        async with stealth_context() as ctx:
            page = await ctx.new_page()

            # ── URL-param pagination loop ──────────────────────────────────
            url_pagination_stalled = False
            for page_num in range(_MAX_PAGES):
                start_rank = page_num * _PER_PAGE + 1
                search_url = (
                    f"https://www.mq.edu.au/search?query=&category=courses"
                    f"&start_rank={start_rank}&num_ranks={_PER_PAGE}"
                )
                try:
                    await page.goto(
                        search_url,
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_search_discover: goto page %d failed: %s",
                        page_num + 1, exc,
                    )
                    break

                # Wait for course-link anchors to materialise in the DOM.
                try:
                    await page.wait_for_selector(
                        "a[href*='/study/find-a-course/']",
                        timeout=14_000,
                    )
                except Exception:  # noqa: BLE001
                    pass  # proceed with whatever is in the DOM

                await asyncio.sleep(3.0)  # allow lazy-loading to settle

                anchors = await page.evaluate(_SEARCH_EXTRACT_JS) or []
                added_this_page = 0
                for a in anchors:
                    clean = _accept_search_url(a.get("href", ""))
                    if not clean or clean in out:
                        continue
                    out[clean] = {
                        "url": clean,
                        "name": (a.get("text") or "").strip(),
                    }
                    added_this_page += 1
                    if len(out) >= max_courses:
                        break

                await emit_fn(
                    f"[DISCOVER] MQ: search page {page_num + 1} "
                    f"(start_rank={start_rank}) → "
                    f"+{added_this_page} new (total {len(out)})"
                )

                if added_this_page == 0:
                    # Either pagination exhausted naturally, or start_rank is
                    # ignored and the page always shows the same first batch.
                    # Detect the latter: if we harvested ≥ 5 courses on page 1
                    # but 0 on page 2, params are probably ignored — fall back
                    # to click-based next-page navigation.
                    if page_num == 1 and len(out) >= 5:
                        url_pagination_stalled = True
                    break

                if len(out) >= max_courses:
                    break

            # ── Click-based "next page" fallback ──────────────────────────
            # Only runs when URL-param pagination stalled after page 1, which
            # means the search page ignores start_rank and we already have the
            # first batch in ``out``.  Navigate back to page 1 and click "Next"
            # repeatedly.
            if url_pagination_stalled:
                await emit_fn(
                    "[DISCOVER] MQ: start_rank ignored — switching to "
                    "click-based next-page navigation"
                )
                # Start fresh on page 1 to get the pagination widget rendered.
                try:
                    await page.goto(
                        "https://www.mq.edu.au/search?query=&category=courses"
                        f"&num_ranks={_PER_PAGE}",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    await asyncio.sleep(3.0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("mq_search_discover: fallback goto failed: %s", exc)

                for _click_page in range(_MAX_PAGES):
                    anchors = await page.evaluate(_SEARCH_EXTRACT_JS) or []
                    added = 0
                    for a in anchors:
                        clean = _accept_search_url(a.get("href", ""))
                        if not clean or clean in out:
                            continue
                        out[clean] = {
                            "url": clean,
                            "name": (a.get("text") or "").strip(),
                        }
                        added += 1
                        if len(out) >= max_courses:
                            break

                    await emit_fn(
                        f"[DISCOVER] MQ: search click-page {_click_page + 1} "
                        f"→ +{added} new (total {len(out)})"
                    )

                    if len(out) >= max_courses:
                        break

                    # Try to click a "Next" button to load the next page.
                    clicked = False
                    for sel in _NEXT_PAGE_SELECTORS:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() == 0 or not await loc.is_visible():
                                continue
                            await loc.click(timeout=4_000)
                            await asyncio.sleep(3.0)
                            clicked = True
                            break
                        except Exception:  # noqa: BLE001
                            continue

                    if not clicked:
                        await emit_fn(
                            "[DISCOVER] MQ: no next-page button found — "
                            "click pagination complete"
                        )
                        break

            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: search-page harvest raised: %s", exc)

    results = sorted(out.values(), key=lambda d: d["url"])
    await emit_fn(
        f"[DISCOVER] MQ: search-page harvest complete — "
        f"{len(results)} unique course URL(s)"
    )
    return results[:max_courses]


def _is_mq_course_url(url: str) -> bool:
    """Return True when *url* is a Macquarie course detail page.

    Pure-Python mirror of the path filter applied to the JS-harvested
    anchors; exposed so the unit tests can assert behaviour without
    spinning up a browser.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if host != "www.mq.edu.au" and host != "mq.edu.au":
        return False

    path = parsed.path or ""
    # Strip query string + fragment for matching.
    if not _COURSE_PATH_RE.match(path):
        return False

    # Block sub-degree pages (majors / specialisations).
    lowered = path.lower()
    if any(sub in lowered for sub in _BLOCKED_PATH_SUBSTRINGS):
        return False

    # Block listing-root last segments (combined-bachelor-master-degrees etc.).
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last in _LISTING_LAST_SEGMENTS:
        return False

    return True


def filter_mq_course_anchors(
    anchors: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Apply ``_is_mq_course_url`` + dedup to a list of ``{href, text}``.

    Exposed for the unit tests so the URL filter can be exercised against
    real captured anchor fixtures without a live browser.
    """
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for entry in anchors:
        raw = (entry.get("href") or "").strip()
        if not raw:
            continue
        # Resolve same-origin relative paths (the in-browser JS does this via
        # `new URL(href, origin)`; mirror it here so the helper can be unit
        # tested against raw anchor dicts).
        if raw.startswith("/") and not raw.startswith("//"):
            raw = _MQ_ORIGIN + raw
        url = raw.split("#")[0].split("?")[0]
        if not _is_mq_course_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "url": url,
            "name": (entry.get("text") or "").strip(),
        })
    return out


async def _interactive_filter_harvest(
    *,
    page,
    seed: str,
    merged: dict[str, dict],
    emit,
) -> int:
    """Try to coax SPA catalogue pages into rendering course anchors.

    Defensive: every selector is wrapped in try/except so a missing
    button never aborts the sweep.  Returns the number of NEW course
    URLs added to ``merged`` (zero if nothing rendered).

    Strategy:
      1. For each selector in ``_FILTER_CLICK_SELECTORS``, click the
         first visible match (best-effort).
      2. After each click, settle + re-extract anchors.
      3. Stop early as soon as any click yields >= 5 new course URLs
         (heuristic — a populated result list will overshoot this on
         the first click; a still-empty SPA will yield 0 every time).
    """
    added_total = 0
    for selector in _FILTER_CLICK_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue

        try:
            await locator.click(timeout=3_000)
        except Exception:  # noqa: BLE001
            continue

        try:
            await emit(
                f"[DISCOVER] MQ: interactive click on {seed} → {selector!r}"
            )
        except Exception:  # noqa: BLE001
            pass

        await asyncio.sleep(_SCROLL_SETTLE_S)
        # Trigger a scroll to provoke lazy load after the filter populates.
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_SCROLL_SETTLE_S)

        try:
            anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
        except Exception:  # noqa: BLE001
            anchors = []

        kept = filter_mq_course_anchors(anchors or [])
        added_this_click = 0
        for item in kept:
            if item["url"] not in merged:
                merged[item["url"]] = item
                added_this_click += 1
            if len(merged) >= _HARD_MAX_LINKS:
                break

        added_total += added_this_click
        if added_this_click >= 5:
            # Filter populated the result grid — no need to try more
            # selectors on this seed.
            break

    return added_total


async def browser_discover_mq(
    emit=None,
    *,
    max_courses: int = 500,
) -> list[dict]:
    """Discover Macquarie course URLs via Playwright across all catalogue seeds.

    Returns a list of ``{"url": str, "name": str}`` dicts (one per
    discovered MQ course URL).  Returns ``[]`` only when the browser
    pool is unavailable OR every seed fails to harvest a single link
    (so the caller can fall back to BFS / generic browser / Wayback).

    Partial harvests below ``_DISCOVERY_FLOOR`` (150) are **returned as
    is** rather than discarded — on Cloudflare-walled MQ the downstream
    BFS (403) and generic browser (URL-shape miss) tiers would only
    drop the partial result and stage zero courses.  Operators are
    notified of below-floor harvests via the
    ``discovery_failure_alerts`` row that ``orchestrator.py`` persists
    immediately after this function returns.

    The function is intentionally tolerant: if seed N fails or returns
    nothing, seeds N+1..K are still attempted.  All successful harvests
    are merged and deduped.
    """

    async def _emit(msg: str, **kw) -> None:
        if emit is None:
            return
        try:
            await emit(
                "status", msg, phase="discover",
                kind="mq_browser_discover", **kw,
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        from app.services.scraper.browser_pool import pool as _pool
        from playwright.async_api import TimeoutError as _PwTimeout
    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: browser pool unavailable — %s", exc)
        return []

    # ── Tier 1: coursehandbook sitemap (real catalogue host) ────────
    # Try the structured handbook sitemap FIRST.  The www.mq.edu.au SPA
    # widget sweep below is fragile (Svelte mount, filter UI selectors,
    # CF challenges) and only yields anchors on a small subset of pages.
    # The handbook sitemap is a static XML index that returns the full
    # current-year course list deterministically when reachable.
    try:
        ch_links = await _discover_from_coursehandbook_sitemap(
            _emit, max_courses=max_courses,
        )
    except Exception as _ch_exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: coursehandbook sitemap raised: %s",
            _ch_exc,
        )
        ch_links = []
    # Seed the merged dict with handbook results immediately so BFS
    # additions below de-duplicate on admissions URL.
    merged: dict[str, dict] = {d["url"]: d for d in ch_links}

    # ── Tier 1.5: /search page harvest ──────────────────────────────
    # MQ's search page (https://www.mq.edu.au/search?query=&category=courses)
    # indexes the complete catalogue — 367 international courses as of
    # August 2026 — including research degrees and combined degrees that
    # are absent from the coursehandbook sitemap.
    #
    # The coursehandbook resolver is the RIGHT source for UG/PG courses
    # (it resolves handbook → admissions URLs reliably); the search page
    # is the ONLY reliable source for research degrees (Doctor of
    # Philosophy, Professional Doctorates, etc.) whose URLs are at
    # ``/study/find-a-course/research/<slug>`` — a path the handbook
    # resolver never constructs (it always uses ``/courses/``).
    #
    # Run this tier even when the handbook succeeds so both sources
    # contribute to the merged set.  Dedup on admissions URL is free.
    try:
        sp_links = await _discover_from_search_page(
            _emit, max_courses=max_courses,
        )
    except Exception as _sp_exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: search-page tier raised: %s", _sp_exc,
        )
        sp_links = []

    for d in sp_links:
        merged.setdefault(d["url"], d)

    await _emit(
        f"[DISCOVER] MQ: after handbook + search-page tiers: "
        f"{len(merged)} unique course URL(s)"
    )

    # If the two structured tiers already produced a full catalogue, skip
    # the expensive browser sweep.  The threshold is 450 (well above the
    # 367 target) to allow for a small future catalogue growth before
    # the sweep is wrongly skipped.
    if len(merged) >= 450:
        await _emit(
            f"[DISCOVER] MQ: {len(merged)} courses from handbook + search — "
            f"skipping browser sweep (catalogue is complete)"
        )
        return sorted(merged.values(), key=lambda d: d["url"])[:max_courses]

    await _emit(
        f"[DISCOVER] MQ: starting browser sweep across {len(_SEED_URLS)} "
        f"catalogue seed(s) to supplement {len(merged)} handbook+search courses"
    )

    try:
        async with _pool.page() as page:
            await page.set_extra_http_headers(
                {"Referer": "https://www.google.com/"}
            )

            for seed in _SEED_URLS:
                await _emit(f"[DISCOVER] MQ: seed → {seed}")
                # ── Navigate ────────────────────────────────────────────
                try:
                    await page.goto(seed, wait_until="networkidle",
                                    timeout=60_000)
                except _PwTimeout:
                    log.warning(
                        "mq_browser_discover: goto networkidle timed out on %s "
                        "— continuing with partial DOM", seed,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: goto failed on %s — %s",
                        seed, exc,
                    )
                    await _emit(
                        f"[DISCOVER] MQ: seed {seed} navigation failed ({exc})"
                    )
                    continue

                # Error-page sniff (Chromium interstitials).
                try:
                    partial = await asyncio.wait_for(
                        page.content(), timeout=5.0,
                    )
                    lowered = (partial or "")[:4096].lower()
                    if (
                        "neterror" in lowered
                        or "chrome-error://" in lowered
                        or "err_name_not_resolved" in lowered
                        or "err_connection_" in lowered
                        or "err_cert_" in lowered
                    ):
                        log.warning(
                            "mq_browser_discover: Chromium error page on %s",
                            seed,
                        )
                        await _emit(
                            f"[DISCOVER] MQ: Chromium error page on {seed}"
                        )
                        continue
                except Exception:  # noqa: BLE001
                    pass

                # ── Wait for hydration (course-anchor selector) ─────────
                try:
                    await page.wait_for_selector(
                        _HYDRATE_WAIT_SELECTOR,
                        timeout=_HYDRATE_WAIT_MS,
                    )
                except _PwTimeout:
                    log.info(
                        "mq_browser_discover: hydration selector not seen on "
                        "%s within %dms — extracting whatever is in the DOM",
                        seed, _HYDRATE_WAIT_MS,
                    )
                except Exception:  # noqa: BLE001
                    pass

                await asyncio.sleep(_INITIAL_SETTLE_S)

                # ── Scroll loop to trigger lazy load ────────────────────
                prev_count = -1
                stall_streak = 0
                for it in range(_MAX_SCROLL_ITERS):
                    try:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                    except Exception:  # noqa: BLE001
                        break
                    await asyncio.sleep(_SCROLL_SETTLE_S)
                    try:
                        anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
                    except Exception:  # noqa: BLE001
                        anchors = []
                    current = len(filter_mq_course_anchors(anchors or []))
                    if current == prev_count:
                        stall_streak += 1
                        if stall_streak >= 2:
                            break
                    else:
                        stall_streak = 0
                        if it == 0 or current - prev_count >= 10:
                            await _emit(
                                f"[DISCOVER] MQ: {seed} scroll iter "
                                f"{it + 1} → {current} course link(s)"
                            )
                    prev_count = current
                    if current >= _HARD_MAX_LINKS:
                        break

                # ── Final extract for this seed ─────────────────────────
                try:
                    anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: final extract failed on %s — %s",
                        seed, exc,
                    )
                    anchors = []

                kept = filter_mq_course_anchors(anchors or [])
                added = 0
                for item in kept:
                    if item["url"] not in merged:
                        merged[item["url"]] = item
                        added += 1
                    if len(merged) >= _HARD_MAX_LINKS:
                        break
                await _emit(
                    f"[DISCOVER] MQ: seed {seed} contributed +{added} "
                    f"new course(s) (total now {len(merged)})"
                )

                # ── 0-anchor diagnostics ────────────────────────────────
                # If a seed returned 0 course-shape anchors, dump page
                # title + raw-anchor count so the operator can tell whether
                # the page is a Cloudflare shell, a pre-hydration SPA, or
                # a genuinely empty catalogue page (so the next debug
                # iteration knows what to fix instead of guessing).
                if added == 0:
                    try:
                        title = await page.title()
                    except Exception:  # noqa: BLE001
                        title = "?"
                    raw_count = len(anchors or [])
                    await _emit(
                        f"[DISCOVER] MQ: seed {seed} yielded 0 course "
                        f"anchors — page title={title!r}, total <a> "
                        f"tags on DOM={raw_count}"
                    )

                # ── Interactive filter fallback for catalogue seeds ─────
                # Catalogue landing pages (/study/find-a-course[/level])
                # are SPA search shells.  If we got 0 anchors after the
                # passive scroll loop, try clicking the known filter
                # buttons to coax the SPA into rendering results.  Skip
                # for faculty pages which are plain HTML and either work
                # or genuinely have no courses.
                if added == 0 and seed in _CATALOGUE_SEED_URLS:
                    interactive_added = await _interactive_filter_harvest(
                        page=page, seed=seed, merged=merged, emit=_emit,
                    )
                    if interactive_added:
                        await _emit(
                            f"[DISCOVER] MQ: interactive filter rescue on "
                            f"{seed} → +{interactive_added} course(s) "
                            f"(total now {len(merged)})"
                        )
                if len(merged) >= _HARD_MAX_LINKS:
                    break

    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: unexpected error — %s", exc)
        await _emit(f"[DISCOVER] MQ: browser discovery error — {exc}")
        return list(merged.values())[:max_courses]

    out = list(merged.values())

    # ── Discovery floor warning ────────────────────────────────────────
    if len(out) < _DISCOVERY_FLOOR:
        log.warning(
            "mq_browser_discover: only %d course URL(s) discovered (floor=%d) "
            "— Cloudflare challenge or catalogue regression",
            len(out), _DISCOVERY_FLOOR,
        )
        await _emit(
            f"[DISCOVER] MQ: WARNING — only {len(out)} course URL(s) found "
            f"(expected ≥{_DISCOVERY_FLOOR}); possible Cloudflare challenge "
            "or catalogue regression",
        )

    # Don't return [] for partial harvests (1-2 links): on Macquarie the
    # downstream fallbacks (BFS → 403, generic browser → URL-shape miss)
    # would BOTH discard the partial result and stage zero courses.  Better
    # to return what we have and let the downstream alert layer flag the
    # low count (handled by the `discovery_failure_alerts` table when the
    # final candidate stream is < 3).
    if not out:
        log.warning(
            "mq_browser_discover: harvested 0 links — caller will fall "
            "back to generic browser / Wayback",
        )
        return []

    log.info(
        "mq_browser_discover: discovered %d course URL(s) across %d seed(s)",
        len(out), len(_SEED_URLS),
    )
    await _emit(
        f"[DISCOVER] MQ: total {len(out)} unique course URL(s) discovered"
    )
    return out[:max_courses]

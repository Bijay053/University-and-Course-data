"""Discover course pages from a university homepage.

Slimmed-down port of Node ``crawlForCourseLinks`` /
``isCourseUrl`` / ``isCourseText`` (artifacts/api-server/src/routes/scrape.ts
~6700-6850 + helpers). The full Node version walks the DOM with cheerio,
follows pagination, parses sitemaps, and probes JSON APIs. The Python
port covers the two highest-yield paths: HTML link harvesting + sitemap
fallback. AI-assisted discovery (Gemini classifier) is wired separately
in ``app/services/ai/gemini_client.py`` and not invoked from the
orchestrator yet.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)


_COURSE_URL_HINTS = (
    "/course/",
    "/courses/",
    "/program/",
    "/programs/",
    "/programme/",
    "/programmes/",
    "/study/",
    "/studies/",
    "/degree/",
    "/degrees/",
    "/major/",
    "/majors/",
    "/discipline/",
)
_NAV_URL_HINTS = (
    "/study",
    "/course",
    "/program",
    "/academ",
    "/facult",
    "/school",
    "/department",
    "/undergrad",
    "/postgrad",
)
_COURSE_TEXT = re.compile(
    r"\b(bachelor|master|phd|doctorate|diploma|certificate|associate|"
    r"undergrad(?:uate)?|postgrad(?:uate)?|MBA|MSc|MA|BA|BSc|BEng|MEng)\b",
    re.I,
)
# Host-specific *brand* acronyms that name a degree program rather than a
# discipline — VIT uses BITS (Bachelor of IT & Systems), MITS (Master of
# IT & Systems), and BBus (Bachelor of Business). These are degree-
# qualifier-equivalent for the universities that use them, so a link
# whose anchor text contains one is a real course detail page even when
# its URL slug looks superficially category-shaped (e.g.
# /courses/bits-cybersecurity is a real course, not a discipline index).
# Word-boundary anchored to avoid matching incidental substrings ("8-bit
# ADC", "bits and pieces"). Mirrors the host-slug list in
# `home_page_redirect._HOST_CATEGORY_SLUGS` — keep the two in sync.
_COURSE_BRAND_TEXT = re.compile(r"\b(BITS|MITS|BBus)\b", re.I)
_JUNK_TEXT = re.compile(
    r"^(home|about|contact|news|events?|search|menu|login|sign\s*in|"
    r"apply\s*now|read\s*more|learn\s*more|view\s*all|see\s*all|"
    # CSU (and other SPA sites) produce bare section-header link text
    # like "Undergraduate" / "Postgraduate" as nav anchors — these are
    # never course names and must be blocked here so the BFS candidate
    # count stays accurate and the sitemap-fallback threshold fires.
    r"undergraduate|postgraduate|"
    r"courses?|programs?|degrees?|study|explore)$",
    re.I,
)

# PR-5 Bug 4: nav/admin/news/marketing URL substrings that are NEVER
# course detail pages. Ported from Node `excludePatterns` (routes/
# scrape.ts:6553-6566) plus the explicit Torrens regression patterns:
# /stories/, /studying-with-us/, /student-support/, /student-showcase/,
# /success-coaches/, /why-study-with-us/. Without this filter, the
# discovery BFS staged 22 "courses" for Torrens of which most were nav
# or news (job_..., university_id=3).
_NON_COURSE_URL_PATTERNS: tuple[str, ...] = (
    "/accommodation", "/student-life", "/campus-life", "/campus-map",
    "/campus-tour", "/apply/", "/application/", "/contact",
    "/about-us", "/about/", "/news/", "/newsroom/", "/events/",
    "/event/", "/stories/", "/story/", "/search", "/category/", "/tag/",
    "/blog/", "/blogs/", "/staff/", "/faculty-profile", "/research/",
    "/library/", "/scholarships", "/support/", "/services/",
    "/student-support", "/student-showcase", "/success-coaches",
    "/why-study-with-us", "/why-choose", "/info-night", "/open-day",
    "/virtual-info", "/keydates", "/key-dates", "/career-finder",
    "/testimonials", "/study/why-", "/studying-with-us/",
    "/all-courses", "/browse-courses", "/explore-courses",
    # CSU regression (T007): these path prefixes generated 6 of the 7
    # garbage staged rows (nav sections, career-browsing pages, and a
    # short-course finder that hides behind a JS filter UI).
    # "/information-for/" — /information-for/undergraduate-students etc.
    # "/why-"            — /why-charles-sturt/our-rankings etc. (catches
    #                      any /why-<brand>/ marketing section, not just
    #                      the already-listed /why-study-with-us exact).
    # "/career-area/"    — CSU by-career course-browsing sidebar pages.
    # "/find-courses/"   — CSU JS-rendered short-course filter UI.
    "/information-for/", "/career-area/", "/find-courses/",
    # "/why-" matches any URL that contains the substring "/why-" —
    # i.e. any path segment that starts with "why-".  Real university
    # course pages never use a slug beginning with "why-" (they use
    # degree-qualifier prefixes: bachelor-*, master-*, graduate-*, etc.),
    # so the false-positive risk is effectively zero.  The leading slash
    # prevents matching "elearning/anywhere" (no "why-" substring) while
    # still catching /why-charles-sturt/…, /why-choose-csu/…, etc.
    "/why-",
)

# Last-segment junk suffix regex (Node routes/scrape.ts:5540) — even
# under a "course-y" parent path, segments ending in these words are
# always info pages, not real courses (e.g. /courses/scholarships,
# /degrees/open-day, /programs/info-night).
_JUNK_LAST_SEG_RE = re.compile(
    r"(scholarships?|jobs?|internships?|employment|career|life|"
    r"accommodation|sport|news|events?|blogs?|faq|help|support|overview|"
    r"guide|information|handbook|tips|process|pathway|pathways?|"
    r"class(?:es)?|fair|expo|hub|community|connect|network|info-night|"
    r"open-day|keydates?|key-dates?|story|stories|testimonials?)$",
    re.I,
)

# Top-level catalogue path segments. A URL of shape
# /<one of these>/<single-segment-without-degree-qualifier> is a
# category landing page (e.g. /courses/design, /programs/business),
# not a real course detail page.
_CATEGORY_BASE_SEGMENTS: frozenset[str] = frozenset({
    "courses", "course", "programs", "programmes", "programme", "program",
    "degrees", "degree", "study",
})


def _is_known_non_course_url(url: str) -> bool:
    """True when the URL matches a hard-coded blocklist of nav/admin/
    news/marketing patterns. Source of truth for keeping site nav out
    of the staged-courses table."""
    lurl = url.lower()
    if any(p in lurl for p in _NON_COURSE_URL_PATTERNS):
        return True
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last and _JUNK_LAST_SEG_RE.search(last):
        return True
    return False


def _is_category_landing(url: str) -> bool:
    """True for `/<catalogue>/<single-segment>` URLs whose final segment
    has no degree qualifier — i.e. category index pages like
    /courses/design, /programs/business, /degrees/health.

    These match the `/courses/` URL hint and would otherwise be treated
    as real courses by :func:`_looks_like_course`. The BFS uses this to
    (a) reject them from the candidate set and (b) enqueue them for
    drill-in so their listed courses are harvested. Mirrors Node's
    ``isShallowCatalogPath`` (routes/scrape.ts:5535-5544).
    """
    try:
        path = urlparse(url).path.lower().rstrip("/")
    except Exception:
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        return False
    if parts[0] not in _CATEGORY_BASE_SEGMENTS:
        return False
    last = parts[1].replace("-", " ").replace("_", " ")
    return not _COURSE_TEXT.search(last)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, text)
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "a":
            href = next((v for (k, v) in attrs if k == "href" and v), None)
            if href:
                self._current_href = href
                self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_href is not None:
            text = re.sub(r"\s+", " ", "".join(self._current_text)).strip()
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_text = []


def _resolve(href: str, base: str, origin: str) -> str | None:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    full = urljoin(base, href).split("#")[0]
    if not full.startswith(origin):
        return None
    return full


def _looks_like_course(url: str, text: str) -> bool:
    # PR-5 Bug 4 + Bug 5: hard reject nav/news/admin URLs and shallow
    # category landings (e.g. /courses/design) BEFORE the URL-hint
    # check would otherwise accept them. Without these filters the
    # Torrens scrape staged 22 candidates of which most were nav,
    # news, or category indexes — and missed the real 152 courses
    # because the category landings were leaves instead of being
    # drilled.
    if _is_known_non_course_url(url):
        return False
    if _is_category_landing(url):
        # Anchor-text override: when the URL slug looks category-shaped
        # but the link text contains a degree qualifier (Bachelor, MBA,
        # …) or a host-specific brand acronym (BITS, MITS, BBus), the
        # link is actually a real course detail page and the URL-shape
        # heuristic is a false positive. Without this override, VIT's
        # /courses/bits-cybersecurity (anchor "BITS - Cybersecurity")
        # was silently rejected by the category-landing filter even
        # though the BITS expansion explicitly fetched the page to
        # harvest it — the regression that surfaced as the
        # `test_expand_merges_new_candidates` failure.
        if text and (
            _COURSE_TEXT.search(text) or _COURSE_BRAND_TEXT.search(text)
        ):
            pass  # fall through to the URL-hint / text-match acceptance
        else:
            return False
    lurl = url.lower()
    if any(h in lurl for h in _COURSE_URL_HINTS):
        return True
    if text and not _JUNK_TEXT.match(text) and (
        _COURSE_TEXT.search(text) or _COURSE_BRAND_TEXT.search(text)
    ):
        return True
    return False


def _is_nav(url: str) -> bool:
    lurl = url.lower()
    return any(h in lurl for h in _NAV_URL_HINTS)


_SITEMAP_FALLBACK_THRESHOLD = 5


async def discover_course_links(
    start_url: str,
    *,
    max_pages: int = 25,
    max_courses: int = 200,
    emit=None,
) -> list[dict]:
    """BFS crawl from ``start_url`` with rule-based page-type classification
    and a sitemap fallback when the crawl yields too few candidates.

    Returns ``[{url, name}]`` for each course-like link, deduped by URL.

    Pipeline:

    1. BFS-crawl from ``start_url``. For each fetched page, run the
       rule-based classifier (:func:`page_type.classify_page`) — when a
       page is identified as a real course-detail page we DO NOT follow
       its navigation links (would waste the per-page budget on
       guaranteed dead ends). Listing/unknown pages contribute their
       course links and may have nav links followed at depth 0.
    2. If the crawl produces fewer than
       :data:`_SITEMAP_FALLBACK_THRESHOLD` candidates, probe the
       institution's ``sitemap.xml`` (and ``robots.txt`` for non-standard
       sitemap locations). New, deduped course URLs are merged in.

    ``emit`` is an optional async callable ``emit(event, message, **kwargs)``
    used to stream per-page progress into the runtime log so the UI panel
    can show what discovery is doing turn-by-turn. When ``None`` the crawler
    is silent (preserves the existing test signature).
    """
    # Lazy imports — avoid a circular import via discovery → sitemap →
    # discovery (sitemap reuses our regex constants).
    from app.services.scraper.page_type import classify_page
    from app.services.scraper.sitemap import discover_from_sitemap
    from app.services.scraper.home_page_redirect import (
        _is_home_page,
        detect_course_listing_page,
        expand_course_list_with_categories,
    )

    parsed = urlparse(start_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # ── Home-page → course-listing redirect (T001) ──────────────────────
    # When the caller hands us the marketing home page (path is "/" or
    # empty), VIT-style universities won't yield any course links from
    # the home-page DOM. Detect the real catalogue URL via HEAD-probe +
    # link-scan and switch start_url before BFS begins. Without this,
    # the Python crawler used to fall back to the sitemap (yielding ~24
    # candidates) instead of using the per-listing pagination Node uses
    # (yielding ~30).
    if _is_home_page(start_url):
        home_html = await fetch_html(start_url) or ""
        redirect = None
        try:
            redirect = await detect_course_listing_page(start_url, home_html, emit=emit)
        except Exception as exc:  # noqa: BLE001
            log.warning("home_page_redirect failed for %s: %s", start_url, exc)
        if redirect and redirect != start_url:
            start_url = redirect
            parsed = urlparse(start_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"

    queue: list[tuple[str, int]] = [(start_url, 0)]
    visited: set[str] = set()
    found: dict[str, str] = {}

    if emit:
        await emit(
            "status",
            f"[DISCOVER] Crawling from {start_url} (max {max_pages} pages, "
            f"max {max_courses} candidates)",
            phase="discover",
            kind="crawl_start",
        )

    while queue and len(visited) < max_pages and len(found) < max_courses:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        html = await fetch_html(url)
        if not html:
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] Page {len(visited)}/{max_pages}: fetch failed — {url}",
                    phase="discover",
                    kind="page_fetch_fail",
                )
            continue

        # Classify the page first. Listing pages get their links harvested
        # AND may have nav links followed; detail pages only contribute
        # themselves (no nav drill-in); unknown pages still get the legacy
        # link-extraction treatment so we don't regress on sites whose
        # template the classifier doesn't recognise.
        try:
            classification = classify_page(html, url)
        except Exception:
            classification = {"page_type": "unknown", "course_links": [], "reason": "classify failed"}
        ptype = classification.get("page_type", "unknown")

        if emit:
            await emit(
                "status",
                f"[DISCOVER] classified {url}: {ptype} ({classification.get('reason', '')})",
                phase="discover",
                kind="page_classified",
                page_type=ptype,
            )

        before = len(found)

        # Take the classifier's curated list when it found any — those
        # have already been deduped, junk-filtered, and resolved against
        # the page's origin.
        for link in classification.get("course_links", []) or []:
            u = link.get("url")
            n = link.get("name") or ""
            if not u or u in found:
                continue
            found[u] = n
            if len(found) >= max_courses:
                break

        # ALWAYS run the legacy link sweep for listing/unknown pages.
        # The classifier curates COURSE links, but real catalogues are
        # spread across multiple listing pages reached via nav links —
        # if we skip this pass on a listing page that happens to surface
        # a few featured courses, the BFS never reaches the rest of the
        # catalogue. We only suppress this pass on `detail` pages: a
        # single course page's nav links would just send the crawler
        # back into the course we're already extracting from, wasting
        # the per-page budget.
        #
        # We deliberately re-run `_looks_like_course` here too so that
        # course links the classifier missed (unusual link templates,
        # text outside the 5–180-char window) still get harvested.
        if ptype != "detail" and len(found) < max_courses:
            ext = _LinkExtractor()
            try:
                ext.feed(html)
            except Exception:
                continue
            for href, text in ext.links:
                full = _resolve(href, url, origin)
                if not full or full in found:
                    continue
                if _looks_like_course(full, text):
                    if not _JUNK_TEXT.match(text or ""):
                        found[full] = text or full.rsplit("/", 1)[-1]
                    if len(found) >= max_courses:
                        break
                # PR-5 Bug 5: enqueue category landings (e.g. /courses/
                # design) for drill-in alongside generic nav. depth<2
                # allows the BFS to walk: catalogue root → category →
                # course-detail-list, which is how Torrens hides 152
                # courses behind 11 single-word category pages.
                elif (
                    depth < 2
                    and full not in visited
                    and (_is_nav(full) or _is_category_landing(full))
                ):
                    queue.append((full, depth + 1))

        added = len(found) - before
        if emit:
            await emit(
                "status",
                f"[DISCOVER] Page {len(visited)}/{max_pages}: +{added} candidates "
                f"(total {len(found)}) — {url}",
                phase="discover",
                kind="page_done",
                added=added,
                total=len(found),
            )

    # Sitemap fallback when the homepage crawl yields too few candidates.
    # Many universities (e.g. those with JS-driven catalogues) link only
    # a handful of "featured" courses from the homepage but publish the
    # full catalogue in sitemap.xml.
    if len(found) < _SITEMAP_FALLBACK_THRESHOLD and origin:
        if emit:
            await emit(
                "status",
                f"[DISCOVER] Crawl yielded only {len(found)} candidate(s) "
                f"(< {_SITEMAP_FALLBACK_THRESHOLD}); trying sitemap fallback",
                phase="discover",
                kind="sitemap_trigger",
                crawl_total=len(found),
            )
        try:
            sitemap_courses = await discover_from_sitemap(origin, emit=emit)
        except Exception as exc:
            log.warning("sitemap fallback failed for %s: %s", origin, exc)
            sitemap_courses = []
        for c in sitemap_courses:
            u = c.get("url")
            n = c.get("name") or ""
            if not u or u in found:
                continue
            found[u] = n
            if len(found) >= max_courses:
                break

    # ── Category-filter expansion (T004) ────────────────────────────────
    # VIT-style course-list pages expose category filters (?course_categories
    # [0]=bbus, ?category=master, …). Each filter shows a different slice
    # of the catalogue, and the union covers more courses than the
    # unfiltered listing alone (24 → 30 on VIT). Only fires when the
    # listing path matches the expand-eligible regex inside
    # ``expand_course_list_with_categories``.
    if found and len(found) < max_courses and origin:
        existing_list = [{"url": u, "name": n} for u, n in found.items()]
        try:
            expanded = await expand_course_list_with_categories(
                start_url, existing_list, emit=emit
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("category expansion failed for %s: %s", start_url, exc)
            expanded = existing_list
        for c in expanded:
            u = c.get("url")
            n = c.get("name") or ""
            if not u or u in found:
                continue
            found[u] = n
            if len(found) >= max_courses:
                break

    return [{"url": u, "name": n} for u, n in list(found.items())[:max_courses]]

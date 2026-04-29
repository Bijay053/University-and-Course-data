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

import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl

from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)


_COURSE_URL_HINTS = (
    "/course/",
    "/courses/",
    "/international/courses/",
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

# Promotional card / CTA link text: long blurb ending in "LEARN MORE" /
# "ENQUIRE" / "APPLY NOW" etc.  These sometimes contain degree keywords
# (e.g. "…offered in the Bachelor of Science… LEARN MORE") so they pass
# the _COURSE_TEXT check, but are NOT individual course detail pages.
# Max real course-name length is ~80 chars; anything ≥ 120 chars is
# almost always a promotional blurb.
_PROMO_TEXT_RE = re.compile(
    r"(learn\s+more|enquire(?:\s+now)?|apply\s+now|find\s+out\s+more|"
    r"click\s+here|get\s+started|read\s+more)\s*$",
    re.I,
)
_MAX_COURSE_NAME_LEN = 120  # chars — real course titles are shorter
_JUNK_TEXT = re.compile(
    r"^(home|about|contact|news|events?|search|menu|login|sign\s*in|"
    r"apply\s*now|read\s*more|learn\s*more|view\s*all|see\s*all|"
    r"enquire|enquire\s*now|enquiry|enquiries|"
    r"register|register\s*now|register\s*here|"
    r"find\s*out\s*more|click\s*here|get\s*started|"
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
    # KBS MBA specialisations overview page — not a real course, just a list
    # of MBA specialisation tracks.  Both British and American spellings kept.
    "/two-specialisations", "/two-specializations",
    # ECU / generic: study-area browsing pages (not individual course pages).
    # Real ECU courses are at /degrees/courses/NAME, not /degrees/study-areas/NAME.
    "/study-areas/", "/study-area/",
    "/subject-areas/", "/subject-area/",
    "/discipline-areas/", "/discipline-area/",
    "/fields-of-study/", "/field-of-study/",
    "/areas-of-study/", "/area-of-study/",
    # NOTE: "/our-courses/" and "/our-programs/" were removed from this list.
    # Some universities (e.g. ASA Institute) publish individual course detail
    # pages under /our-courses/<course-slug> — blocking the whole prefix
    # prevented those courses from being added as candidates.  The listing
    # pages themselves (/our-courses/ and /our-programs/) are correctly
    # rejected by _is_category_landing() and _JUNK_LAST_SEG_RE, so removing
    # them here does not risk staging listing pages as courses.
    # Generic marketing / info page indicators (slash-bounded so partial
    # path segments like /discover-X are not accidentally blocked).
    "/explore/", "/discover/",
    "/how-to-apply/", "/how-to-enrol/",
    "/fees-and-scholarships/", "/fees-and-costs/",
    "/international-students/info",
    "/student-experience/",
    "/life-at-",
    "/campus/",
    # Audience / info pages that are never course detail pages
    "/parents-and-carers", "/for-parents", "/parents/",
    "/interstate/", "/interstate-students/",
    "/high-achiever", "/high-achievers/",
    "/starting-at-the-university", "/starting-university",
    "/international-partners/", "/agent-partners/", "/agent-resources/",
    "/learning-abroad/", "/exchange-programs/",
    "/unigo", "/study-tours/", "/study-tour/",
    # Short-course category/search pages (NOT individual short-course pages)
    "/short-courses/category/", "/short-courses/topic/",
    # UTAS orientation / prep program info hubs
    "/orientation-program", "/orientation/",
    "/university-preparation", "/pathway-college",
    # ACU / generic: research school nav pages discovered via nav crawl
    # that look like course links but are hub/listing pages.
    "/research-and-enterprise/",
    "/graduate-research-school/",
    "/graduate-research/",
    # ACU / generic: admission pathway and English pathway listing pages.
    # Individual pathway courses sometimes appear under /course/ instead,
    # so only the broad programme-listing paths are blocked here.
    "/admission-pathways/",
    "/english-and-pathway-programs/",
    "/pathway-programs/",
    "/pathway-program/",
    # Industry engagement / partnership pages that are never individual
    # course detail pages.
    "/industry-engagement/",
    "/industry-opportunities/",
    # Bond University: experience/marketing hub pages that sit under /study/
    # and are therefore picked up by the /study/ URL hint even though they
    # are never real course detail pages.
    "/experience-bond-for-yourself/",
    # Bond / generic: sport pages are never course detail pages.
    "/sport/",
    # Bond / generic: important-information, key-information pages
    # (regulatory / disclaimer content, not course catalogues).
    "/important-information/",
    "/key-information/",
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
    r"open-day|keydates?|key-dates?|story|stories|testimonials?|"
    r"two-specialisations?|two-specializations?|specialisations?|specializations?|"
    # Application / enrolment admin pages — never real course detail pages.
    r"refund|refund-request|refund-request-form|"
    r"application|application-form|application-form-new-students|"
    r"application-form-returning-students|"
    r"enrolment|enrollment|enrol|enroll|"
    r"orientation|induction|"
    r"enquire|enquiry|enquiries|contact-us|"
    # ACU / generic: research hub pages whose last path segment is a nav
    # keyword rather than a course slug (projects, supervisors, engagement).
    r"projects?|supervisors?|engagement|opportunities|"
    # Bond / generic: programme-finder / course-finder tool pages.
    r"program-finder|programme-finder|course-finder|"
    # Bond: study-area category hub pages (e.g. /study/our-study-areas/X).
    r"our-study-areas|study-areas?)$",
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


# Bug 2: Drupal CMS publishes every node at both a human-readable clean URL
# (/courses/master-of-business) AND a numeric node alias (/node/12345).
# Both URLs appear as hrefs in the course listing, causing the BFS to stage
# the same course twice (different URL → not caught by the URL-keyed dedup).
# Reject /node/<digits> URLs here so only the canonical clean URL is kept.
_DRUPAL_NODE_RE = re.compile(r"/node/\d+(/|$)", re.I)

# Some universities (e.g. SCU) publish every course at BOTH a /2026/ and a
# /2027/ URL. Both land in ``found`` as separate candidates, consuming the
# 200-slot budget and creating duplicate staged rows. Collapse them: keep
# only the URL with the highest year.
_YEAR_SUFFIX_RE = re.compile(r"/20(\d{2})(?:/|$)")

# Some universities (e.g. ACU) publish a live canonical course URL at
# /course/<slug> AND archived handbook URLs at /handbook/handbook-YYYY/course/<slug>
# for every year going back to 2021. The sitemap returns all of them; without
# deduplication the same course is staged 4–6 times with stale data.
# Canonical = strip the /handbook/handbook-YYYY/ prefix; prefer the live URL
# (no year) over any handbook URL, and the highest year among handbook-only sets.
_HANDBOOK_YEAR_RE = re.compile(r"/handbook/handbook-20(\d{2})/", re.I)


def _dedup_year_variants(items: list[dict]) -> list[dict]:
    """Collapse year-specific URL variants, keeping the highest / most canonical.

    Two classes of variant are handled:

    1. **Handbook-prefix URLs** (e.g. ACU `/handbook/handbook-2022/course/<slug>`)
       vs. live `/course/<slug>`.  The canonical form is the URL with the handbook
       prefix stripped.  Preference order: live URL > highest-year handbook URL.

    2. **Path-embedded year suffix** (e.g. SCU `/course/2026/`, `/course/2027/`)
       → keep only the highest year.

    URLs that match neither pattern pass through unchanged.
    """
    # ── Pass 1: handbook-prefix dedup ────────────────────────────────────────
    handbook_groups: dict[str, list[tuple[int, dict]]] = {}
    after_pass1: list[dict] = []

    for item in items:
        url = item.get("url", "")
        m = _HANDBOOK_YEAR_RE.search(url)
        if m:
            # Canonical URL: replace /handbook/handbook-YYYY/ with /
            canonical = url[: m.start()] + "/" + url[m.end():]
            year = int(m.group(1))
            handbook_groups.setdefault(canonical, []).append((year, item))
        else:
            after_pass1.append(item)

    # For each handbook group: prefer the live canonical URL if it already
    # exists in after_pass1; otherwise keep the highest-year handbook variant.
    live_url_set = {item.get("url", "") for item in after_pass1}
    for canonical, variants in handbook_groups.items():
        if canonical in live_url_set:
            # Live URL is already in the result; all handbook duplicates dropped.
            continue
        best_year_item = max(variants, key=lambda t: t[0])[1]
        after_pass1.append(best_year_item)

    # ── Pass 2: path-embedded year suffix dedup (SCU-style) ──────────────────
    groups: dict[str, list[tuple[int, dict]]] = {}
    non_year: list[dict] = []

    for item in after_pass1:
        url = item.get("url", "")
        m = _YEAR_SUFFIX_RE.search(url)
        if m:
            # base = everything before /20XX (e.g. /course-name-1007294)
            base = url[: m.start()]
            groups.setdefault(base, []).append((int(m.group(1)), item))
        else:
            non_year.append(item)

    result = non_year[:]
    for variants in groups.values():
        best = max(variants, key=lambda t: t[0])
        result.append(best[1])
    return result


def _is_known_non_course_url(url: str) -> bool:
    """True when the URL matches a hard-coded blocklist of nav/admin/
    news/marketing patterns. Source of truth for keeping site nav out
    of the staged-courses table."""
    lurl = url.lower()
    # Drupal node aliases (e.g. /node/12345) are always duplicates of the
    # corresponding clean URL — drop them at discovery time (Bug 2).
    if _DRUPAL_NODE_RE.search(url):
        return True
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


def _normalize_url_for_dedup(url: str) -> str:
    """Strip non-essential query parameters for URL deduplication.

    Keeps path-essential params (e.g. pagination ?page=N) but drops
    audience-filter params like ``?studentType=international`` that cause
    the same page to appear twice in the BFS queue — once with the param
    (pre-seeded) and once without (from a link on another page).
    """
    _STRIP_PARAMS = frozenset(
        {"studentType", "studenttype", "student_type", "_ga", "_gl", "_gid"}
    )
    try:
        p = urlparse(url)
        qs = [(k, v) for k, v in parse_qsl(p.query) if k not in _STRIP_PARAMS]
        return urlunparse(p._replace(query=urlencode(qs)))
    except Exception:
        return url


def _is_category_landing(url: str) -> bool:
    """True for `/<catalogue>/<single-segment>` URLs whose final segment
    has no degree qualifier — i.e. category index pages like
    /courses/design, /programs/business, /degrees/health.

    Also handles 3-segment paths like /study/degrees-and-courses/arts
    (UniSQ discipline pages) and 4-segment sub-discipline paths like
    /study/degrees-and-courses/arts-and-communication/communication
    (UniSQ sub-discipline pages) where the first segment is a catalog root
    and the last segment carries no degree qualifier. The BFS uses this
    to (a) reject them from the candidate set and (b) enqueue them for
    drill-in so their listed courses are harvested. Mirrors Node's
    ``isShallowCatalogPath`` (routes/scrape.ts:5535-5544).
    """
    try:
        path = urlparse(url).path.lower().rstrip("/")
    except Exception:
        return False
    parts = [p for p in path.split("/") if p]
    if len(parts) == 2:
        if parts[0] not in _CATEGORY_BASE_SEGMENTS:
            return False
        last = parts[1].replace("-", " ").replace("_", " ")
        return not _COURSE_TEXT.search(last)
    # 3-segment paths: e.g. /study/degrees-and-courses/arts-and-communication
    # (UniSQ discipline landing pages). First segment must be a catalog root;
    # last segment must carry no degree qualifier for it to be a drill-in
    # category rather than a real course detail page.
    if len(parts) == 3:
        if parts[0] not in _CATEGORY_BASE_SEGMENTS:
            return False
        last = parts[2].replace("-", " ").replace("_", " ")
        return not _COURSE_TEXT.search(last)
    # 4-segment paths: e.g. /study/degrees-and-courses/arts-and-communication/communication
    # (UniSQ sub-discipline pages). Same rule: first segment is a catalog root
    # and the last segment carries no degree qualifier → it's a sub-category
    # that should be drilled into, not treated as a course detail page.
    # Without this, the /study/ URL hint causes sub-discipline URLs to pass
    # _looks_like_course and end up in the extraction queue, wasting Gemini
    # calls on pages that always return null for every field.
    if len(parts) == 4:
        if parts[0] not in _CATEGORY_BASE_SEGMENTS:
            return False
        last = parts[3].replace("-", " ").replace("_", " ")
        return not _COURSE_TEXT.search(last)
    return False


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, text)
        self._current_href: str | None = None
        self._current_text: list[str] = []
        # Track depth inside tags whose text content we should ignore
        # (SVG <style> blocks, <script> blocks).
        self._skip_depth: int = 0
        self._skip_tags: frozenset[str] = frozenset({"style", "script", "svg"})

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._skip_tags:
            self._skip_depth += 1
        if tag == "a":
            href = next((v for (k, v) in attrs if k == "href" and v), None)
            if href:
                self._current_href = href
                self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None and self._skip_depth == 0:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1
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
    # Reject promotional-card / CTA link text regardless of URL.
    # Real course names are short; blurbs ending in "LEARN MORE" / "ENQUIRE"
    # are marketing cards and must never be treated as course pages even if
    # their anchor text happens to contain a degree keyword.
    if text and (
        len(text) > _MAX_COURSE_NAME_LEN
        or _PROMO_TEXT_RE.search(text)
    ):
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

# Hosts where the BFS HTTP crawl structurally misses courses because
# category listing pages are JS-rendered SPAs (0 anchor-based course links
# in static HTML).  For these universities we ALWAYS merge sitemap results
# with BFS candidates — even when BFS exceeded the fallback threshold — so
# that courses only discoverable from the sitemap (e.g. Torrens design,
# arts) are not silently omitted.
_ALWAYS_SITEMAP_SUPPLEMENT_HOSTS: frozenset[str] = frozenset({
    "www.torrens.edu.au",
    "torrens.edu.au",
    # CDU: course listing pages are JS-rendered (Angular SPA injected into
    # #js-automated-course-list-<id> anchors).  BFS static HTML only finds
    # category overview pages (/study/accounting, /study/arts, …) and never
    # reaches the actual course detail pages.  Supplementing with the sitemap
    # is the lowest-cost fix: CDU's sitemap contains direct course page URLs.
    "www.cdu.edu.au",
    "cdu.edu.au",
})


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
    # Normalized URL set for deduplication: strips non-essential query params
    # (e.g. ?studentType=international) so the same page isn't fetched twice
    # — once from a pre-seed with the param and once from a plain link without.
    visited_normalized: set[str] = set()
    found: dict[str, str] = {}

    # UOW: course listing paginates across ~62 pages (?page=N).  The BFS
    # cap of 25 nav pages only sees pages 1–3 and the "last" link (page 62),
    # leaving pages 4–61 unvisited.  Pre-seed all pagination pages so the
    # crawler fetches every listing page within its budget.
    _uow_hosts = ("www.uow.edu.au", "uow.edu.au")
    if parsed.netloc in _uow_hosts and "/study/courses" in (parsed.path or ""):
        _uow_base = f"{parsed.scheme}://{parsed.netloc}/study/courses/"
        for _pg in range(2, 71):
            _seed = f"{_uow_base}?page={_pg}"
            if _seed not in visited:
                queue.append((_seed, 0))

    # Flinders: the general listing (/study/courses) puts 334 unique URLs on
    # one page but orders pure Master courses after position 255, beyond the
    # default max_courses=200 cap.  Pre-seed the postgraduate international
    # filter page so the BFS harvests all ~54 pure Master courses regardless
    # of the cap on the general listing.
    _flinders_hosts = ("www.flinders.edu.au", "flinders.edu.au")
    if parsed.netloc in _flinders_hosts:
        _pg_seed = f"{parsed.scheme}://{parsed.netloc}/study/courses?level=postgraduate&student=international"
        if _pg_seed not in visited:
            queue.append((_pg_seed, 0))

    # UniSQ: pre-seed the main listing page AND each known discipline
    # landing page so the BFS drills directly into discipline containers and
    # harvests the real individual course URLs inside them.  Without these
    # seeds the BFS only reaches the top-level listing page, which links to
    # discipline-category pages; those discipline pages are correctly
    # identified as category landings (3-segment path, no degree keyword)
    # and enqueued for drill-in, but the `max_pages` budget can be exhausted
    # before they are all visited unless they are pre-seeded at depth 0.
    # NOTE: /study/degrees-and-courses/undergraduate-study and
    # /study/degrees-and-courses/postgraduate-study are NOT seeded here
    # because guards.py blocks them (they are hub/navigation pages, not
    # discipline containers that list real course URLs).
    _unisq_hosts = ("www.unisq.edu.au", "unisq.edu.au")
    if parsed.netloc in _unisq_hosts:
        _unisq_base = f"{parsed.scheme}://{parsed.netloc}/study/degrees-and-courses"
        _unisq_disciplines = (
            "",  # root listing (?studentType=international added below)
            "arts-and-communication",
            "aviation",
            "business",
            "education-and-teaching",
            "engineering-and-surveying",
            "health",
            "information-technology",
            "law",
            "nursing-and-midwifery",
            "psychology-and-counselling",
            "sciences-and-agriculture",
            "uniprep",
        )
        for _disc in _unisq_disciplines:
            _path = f"{_unisq_base}/{_disc}" if _disc else _unisq_base
            _seed = f"{_path}?studentType=international"
            if _seed not in visited:
                queue.append((_seed, 0))

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
        # Dedup on normalized URL so ?studentType=international and the bare
        # URL are treated as the same page and not fetched twice (Issue 2).
        _url_norm = _normalize_url_for_dedup(url)
        if url in visited or _url_norm in visited_normalized:
            continue
        visited.add(url)
        visited_normalized.add(_url_norm)

        # Phase A safety net (SCRAPING_ACCURACY_PLAN.md §A.3): drop URLs
        # that match the explicit page blocklist (apply / fees / news /
        # faculty / etc.) BEFORE we spend a fetch on them.  This is an
        # additional layer on top of the existing _is_known_non_course_url
        # / _JUNK_LAST_SEG_RE checks; both must say "ok" for the URL to
        # be fetched.  Cheaper than a network round-trip and surfaces a
        # clean reason in the discovery log.
        try:
            from app.services.scraper.guards import is_blocked_page

            _blocked, _block_reason = is_blocked_page(url, None)
        except Exception:  # noqa: BLE001 — never let the safety net abort discovery
            _blocked, _block_reason = (False, "")
        if _blocked:
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] blocked {_block_reason}: {url}",
                    phase="discover",
                    kind="page_blocked",
                    reason=_block_reason,
                )
            continue

        # Discovery-level fetch: one shot (retries=0) so that this loop —
        # not the inner http_fetcher retry loop — controls the retry policy.
        # fetch_html(retries=2) [default] would add 3 attempts × 30s timeout
        # inside every discovery-level retry, multiplying total wait time by 9
        # for URLs that ultimately fail (9 HTTP calls instead of 3).
        html = await fetch_html(url, retries=0)
        if not html:
            # Brief pause then one more attempt on the same URL — handles
            # transient 5xx / rate-limit blips on category listing pages
            # (e.g. /nursing-and-midwifery) without compounding delay.
            await asyncio.sleep(1)
            html = await fetch_html(url, retries=0)
        # Issue 3: if still nothing and the URL had a query string (e.g.
        # ?studentType=international), retry with the bare path once — some
        # servers reject the param but serve the page fine without it.
        if not html and "?" in url:
            _bare_url = url.split("?", 1)[0]
            if _bare_url not in visited:
                await asyncio.sleep(1)
                html = await fetch_html(_bare_url, retries=0)
                if html:
                    log.info(
                        "[DISCOVER] fetch succeeded without query params for %s", url
                    )
        if not html:
            # Log at ERROR level so missing-category failures are visible
            # in the dashboard sweep log, not just silently skipped.
            log.error(
                "[DISCOVER] fetch failed (all retries) — %s — courses in this "
                "category will be missing from this run",
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] ERROR: fetch failed for {url} — courses in this "
                    f"category will be missing. Check site connectivity.",
                    phase="discover",
                    kind="page_fetch_fail",
                    error=True,
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

        # ── Self-candidate for confirmed detail pages ────────────────────
        # When the classifier identifies the CURRENT page as a course
        # detail (it fetched it, parsed it, found course content), add
        # the URL itself to the candidate set immediately. Without this,
        # sites like AIT whose course URLs look like category landings
        # (/courses/2d-animation, /courses/game-design) are visited,
        # correctly classified as detail pages, but then silently dropped
        # because:
        #   (a) the detail branch returns course_links=[] (no outbound
        #       course links to harvest), AND
        #   (b) the legacy link sweep is suppressed for detail pages.
        # The fix: trust the page-content classifier over the URL-shape
        # heuristic — if we fetched it and it looks like a course, it IS
        # a course candidate, regardless of its URL depth.
        if ptype == "detail" and url not in found:
            slug_name = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
            found[url] = slug_name
            log.info("[DISCOVER] added self as candidate %s", url)
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] added self as candidate {url}",
                    phase="discover",
                    kind="self_candidate",
                )

        # Take the classifier's curated list when it found any — those
        # have already been deduped, junk-filtered, and resolved against
        # the page's origin.
        for link in classification.get("course_links", []) or []:
            u = link.get("url")
            n = link.get("name") or ""
            if not u or u in found:
                continue
            # If the page classifier returned a 3-segment discipline/category
            # page as a "course link" (e.g. UniSQ /study/degrees-and-courses/
            # business), enqueue it for drill-in rather than adding to the
            # candidate set. Without this check these pages end up in `found`
            # and are later STAGE-rejected as category_landing_page — the BFS
            # never drills into them to harvest the real course URLs inside.
            if _is_category_landing(u):
                if depth < 2 and u not in visited:
                    queue.append((u, depth + 1))
                continue
            found[u] = n
            if len(found) >= max_courses:
                break

        # ── Detail page: still enqueue child/sibling course URLs ────────
        # Even though the current page is a detail, its nav may link to
        # sibling courses or deeper pages (e.g. AIT /courses/information-
        # technology links to /courses/information-technology/vocational-
        # diploma-of-it). Extract those links and enqueue them for BFS
        # drill-in — but only follow pages that look like courses or
        # category landings (to avoid crawling the whole nav).
        if ptype == "detail" and depth < 2 and len(found) < max_courses:
            _ext = _LinkExtractor()
            try:
                _ext.feed(html)
            except Exception:
                pass
            for _href, _text in _ext.links:
                _full = _resolve(_href, url, origin)
                if not _full or _full in visited or _full in found:
                    continue
                if _looks_like_course(_full, _text):
                    # Real child course — add directly
                    if _full not in found and not _JUNK_TEXT.match(_text or ""):
                        found[_full] = _text or _full.rsplit("/", 1)[-1]
                        log.info("[DISCOVER] added child course %s", _full)
                        if emit:
                            await emit(
                                "status",
                                f"[DISCOVER] added child course {_full}",
                                phase="discover",
                                kind="child_candidate",
                            )
                        if len(found) >= max_courses:
                            break
                elif _is_category_landing(_full) or _is_nav(_full):
                    # Might contain more courses — enqueue for drill-in
                    queue.append((_full, depth + 1))

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

    # ── Alternative listing path probe ───────────────────────────────────────
    # When the seed URL failed (e.g. /courses 404s) and the sitemap is
    # incomplete or absent, probe a small set of well-known alternative listing
    # paths (e.g. /our-courses/courses-grid-view) and harvest any linked
    # courses from each.  Only fires when found is below a generous threshold
    # so healthy universities with many results are unaffected.
    _ALT_PROBE_THRESHOLD = 15
    _ALT_LISTING_PATHS: tuple[str, ...] = (
        "/our-courses/courses-grid-view",
        "/our-courses",
        "/our-programs/all",
        "/our-programs",
        "/courses/all",
        "/all-courses",
        "/study/all",
    )
    if len(found) < _ALT_PROBE_THRESHOLD and origin:
        for _alt_path in _ALT_LISTING_PATHS:
            if len(found) >= max_courses:
                break
            _alt_url = f"{origin}{_alt_path}"
            if _alt_url in visited:
                continue
            _alt_html = await fetch_html(_alt_url, retries=0)
            if not _alt_html:
                continue
            visited.add(_alt_url)
            try:
                from bs4 import BeautifulSoup as _BS4

                _alt_soup = _BS4(_alt_html, "html.parser")
                for _a in _alt_soup.find_all("a", href=True):
                    _href = (_a.get("href") or "").strip()
                    _text = _a.get_text(" ", strip=True)[:200]
                    if not _href or _href.startswith(("#", "mailto:", "tel:", "javascript:")):
                        continue
                    _full = _resolve(_href, _alt_url, origin)
                    if not _full or _full in found:
                        continue
                    if _looks_like_course(_full, _text):
                        found[_full] = _text
                        if len(found) >= max_courses:
                            break
            except Exception as _alt_exc:
                log.warning(
                    "discovery: alt listing probe failed for %s: %s",
                    _alt_url,
                    _alt_exc,
                )

    # ── Unconditional sitemap supplement for JS-heavy catalogues ────────────
    # For universities whose category listing pages are React/Vue SPAs the BFS
    # HTTP pass can only find courses that appear in static HTML (e.g. featured
    # picks or courses linked from nav).  The sitemap exposes the full
    # catalogue; merge it here so we don't silently miss entire disciplines.
    if parsed.netloc in _ALWAYS_SITEMAP_SUPPLEMENT_HOSTS and origin:
        if emit:
            await emit(
                "status",
                f"[DISCOVER] JS-heavy site — supplementing {len(found)} BFS candidate(s) "
                f"with sitemap to catch discipline courses missing from static HTML",
                phase="discover",
                kind="sitemap_supplement",
            )
        try:
            _supp_courses = await discover_from_sitemap(origin, emit=emit)
        except Exception as _supp_exc:
            log.warning("sitemap supplement failed for %s: %s", origin, _supp_exc)
            _supp_courses = []
        _supp_added = 0
        for _sc in _supp_courses:
            _su = _sc.get("url")
            _sn = _sc.get("name") or ""
            if not _su or _su in found:
                continue
            found[_su] = _sn
            _supp_added += 1
            if len(found) >= max_courses:
                break
        if emit and _supp_added:
            await emit(
                "status",
                f"[DISCOVER] sitemap supplement added {_supp_added} new candidate(s) "
                f"(total now {len(found)})",
                phase="discover",
                kind="sitemap_supplement_done",
                added=_supp_added,
                total=len(found),
            )

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

    # ── ECU: keep only /degrees/courses/<slug> URLs ──────────────────────────
    # ECU's sitemap and BFS harvest article pages, experience stories, career-
    # switch blogs, hub pages, and listing pages alongside real course pages.
    # For ECU specifically the ONLY valid individual course pages are at
    # /degrees/courses/<single-slug> (not /all, not /postgraduate, not nested).
    _ecu_hosts = frozenset({"ecu.edu.au", "www.ecu.edu.au"})
    if parsed.netloc in _ecu_hosts:
        _pre_ecu = len(found)
        _ecu_valid: dict[str, str] = {}
        for _eu, _en in found.items():
            try:
                _ep = urlparse(_eu).path.lower().rstrip("/")
                # Must be /degrees/courses/<slug> with exactly one slug segment.
                if not _ep.startswith("/degrees/courses/"):
                    continue
                _slug = _ep[len("/degrees/courses/"):]
                if not _slug or _slug in ("all", "search", "postgraduate", "undergraduate"):
                    continue
                # Reject nested paths (category hubs or further sub-pages).
                if "/" in _slug:
                    continue
                _ecu_valid[_eu] = _en
            except Exception:
                continue
        _removed_ecu = _pre_ecu - len(_ecu_valid)
        found = _ecu_valid
        if emit and _removed_ecu:
            await emit(
                "status",
                f"[DISCOVER] ECU post-filter: kept {len(found)} /degrees/courses/ URLs "
                f"(dropped {_removed_ecu} non-course candidates)",
                phase="discover",
                kind="ecu_course_filter",
                kept=len(found),
                dropped=_removed_ecu,
            )

    # ── Bond University: keep only /program/ URLs ────────────────────────────
    # Bond's sitemap includes pages under /study/, /sport/, /experience-bond-
    # for-yourself/ etc. that are never real program detail pages. The
    # _NON_COURSE_URL_PATTERNS filter above blocks the most egregious ones, but
    # the sitemap fallback can still yield marketing pages whose URLs don't
    # match any pattern (they sit two or three segments deep under /study/).
    # For Bond specifically the ONLY real course detail URLs are at /program/<slug>.
    # Apply a strict host-level post-filter so all remaining non-program URLs
    # are dropped here — keeping the extraction phase focused on the ~100
    # real program pages Bond publishes.
    _bond_hosts = frozenset({"bond.edu.au", "www.bond.edu.au"})
    if parsed.netloc in _bond_hosts:
        _pre_filter_count = len(found)
        found = {
            u: n for u, n in found.items()
            if urlparse(u).path.lower().startswith("/program/")
        }
        _removed = _pre_filter_count - len(found)
        if emit:
            await emit(
                "status",
                f"[DISCOVER] Bond post-filter: kept {len(found)} /program/ URLs "
                f"(dropped {_removed} non-program candidates)",
                phase="discover",
                kind="bond_program_filter",
                kept=len(found),
                dropped=_removed,
            )

    # ── CDU: drop category pages and failing redirect URLs ───────────────────
    # CDU's BFS finds two classes of non-course pages that must be removed:
    #   (a) Study-area overviews at /study/<single-word>
    #       (e.g. /study/accounting, /study/arts, /study/business).
    #       These pages have a JS-rendered course list so BFS sees only
    #       marketing copy and classifies them as "detail".
    #   (b) /study/redirect/<slug> URLs that CDU uses to deep-link from its
    #       marketing site to the handbook.  Both HTTP and Playwright fail
    #       to fetch these (auth/cookie redirect loop).  The sitemap
    #       supplement added above provides the real course URLs instead.
    # Both are safe to drop globally (no other university uses these
    # URL shapes for actual course detail pages).
    _cdu_hosts = frozenset({"cdu.edu.au", "www.cdu.edu.au"})
    if parsed.netloc in _cdu_hosts:
        _pre_cdu = len(found)
        _cdu_valid: dict[str, str] = {}
        for _cu, _cn in found.items():
            try:
                _cp = urlparse(_cu).path.lower().rstrip("/")
                # Drop /study/redirect/* — always fails to fetch
                if "/study/redirect" in _cp:
                    continue
                # Drop flat /study/<one-word> category hubs.
                # Valid course pages are either at /study/{area}/{course-slug}
                # (two+ segments after /study/) or at a non-/study/ path.
                _segments_after_study = None
                if _cp.startswith("/study/"):
                    _tail = _cp[len("/study/"):]
                    _segments_after_study = [s for s in _tail.split("/") if s]
                if _segments_after_study is not None and len(_segments_after_study) < 2:
                    # One segment = category hub (e.g. /study/accounting)
                    continue
            except Exception:
                pass
            _cdu_valid[_cu] = _cn
        _removed_cdu = _pre_cdu - len(_cdu_valid)
        found = _cdu_valid
        if emit and _removed_cdu:
            await emit(
                "status",
                f"[DISCOVER] CDU post-filter: kept {len(found)} real-course URL(s) "
                f"(dropped {_removed_cdu} category pages / redirect URLs)",
                phase="discover",
                kind="cdu_course_filter",
                kept=len(found),
                dropped=_removed_cdu,
            )

    # ── AIT: drop flat /courses/<area> category pages ─────────────────────────
    # AIT's course listing uses a BFS pattern where category hubs sit at
    # /courses/<area> (e.g. /courses/2d-animation, /courses/3d-animation,
    # /courses/game-design, /courses/information-technology).  These pages
    # contain marketing copy that the page classifier mistakenly labels as
    # "detail", so they end up in the candidate set even though they have no
    # per-course data.  Actual course detail pages always sit at least one
    # level deeper: /courses/<area>/<course-slug>
    # (e.g. /courses/information-technology/vocational-diploma-of-it).
    # Dropping 1-segment /courses/<area> paths is safe for AIT because the
    # BFS self-candidate logic (line 775) still adds them initially so their
    # child-link harvest (line 816) fires first and correctly finds the deeper
    # sibling courses before we prune here.
    _ait_hosts = frozenset({"ait.edu.au", "www.ait.edu.au"})
    if parsed.netloc in _ait_hosts:
        _pre_ait = len(found)
        _ait_valid: dict[str, str] = {}
        for _au, _an in found.items():
            try:
                _ap = urlparse(_au).path.lower().rstrip("/")
                # Drop flat /courses/<one-segment> category hubs.
                # Valid course pages are at /courses/<area>/<course-slug>
                # (two+ segments after /courses/) or at a non-/courses/ path.
                if _ap.startswith("/courses/"):
                    _tail = _ap[len("/courses/"):]
                    _segs = [s for s in _tail.split("/") if s]
                    if len(_segs) < 2:
                        # e.g. /courses/3d-animation → drop
                        continue
            except Exception:
                pass
            _ait_valid[_au] = _an
        _removed_ait = _pre_ait - len(_ait_valid)
        found = _ait_valid
        if emit and _removed_ait:
            await emit(
                "status",
                f"[DISCOVER] AIT post-filter: kept {len(found)} real-course URL(s) "
                f"(dropped {_removed_ait} category hub URL(s))",
                phase="discover",
                kind="ait_course_filter",
                kept=len(found),
                dropped=_removed_ait,
            )

    raw = [{"url": u, "name": n} for u, n in list(found.items())[:max_courses]]
    return _dedup_year_variants(raw)

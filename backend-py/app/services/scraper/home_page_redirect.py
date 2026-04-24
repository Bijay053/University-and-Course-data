"""Home-page → course-listing redirect detector.

Mirrors Node ``detectCourseListingPage`` and ``expandCourseListWithCategories``
from ``artifacts/api-server/src/routes/scrape.ts`` (lines 6967-7131).

Many universities point users to a marketing home page (``vit.edu.au/``)
that doesn't contain the actual course catalogue — the real listing
lives at ``/course-list``, ``/courses``, ``/study/degrees-and-courses``
or similar. The Python crawler used to BFS the marketing home page,
classify it as ``unknown``, and only fall back to the sitemap when it
ran out of links — yielding ~24 courses for VIT vs the ~30 the
Node-era scraper produced.

Public entry-points:

* :func:`detect_course_listing_page` — given the home-page URL + HTML,
  return the URL of the real course listing (or ``None`` if no
  detection succeeded).
* :func:`expand_course_list_with_categories` — given a listing URL +
  the candidate links already discovered, HEAD-probe well-known
  category-filter variants (``?course_categories[0]=bbus``, ``?category=
  master`` …) and merge any new course links found on the variant
  pages.
"""
from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.scraper.discovery import (
    _JUNK_TEXT,
    _looks_like_course,
    _resolve,
)
from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)


# Same UA the rest of the scraper uses; lifted from browser_pool to keep
# the HEAD probe indistinguishable from the GET that follows.
_HEAD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEAD_HEADERS = {
    "User-Agent": _HEAD_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}
_HEAD_TIMEOUT_SEC = 5.0

# Step-1 high-priority paths — these are preferred over generic
# ``/courses`` link-scan hits because sites like VIT use ``/course-list``
# for the real catalogue while ``/courses`` redirects to a landing page.
_HIGH_PRIORITY_PATHS: tuple[str, ...] = (
    "/study/degrees-and-courses",
    "/degrees",
    "/course-list",
    "/course-finder",
    "/course-guide",
    "/study/courses",
    "/courses/undergraduate",
    "/courses/postgraduate",
    "/courses",
    "/programs",
    "/programmes",
    "/our-courses",
)

# Step-3 broad fallback paths.
_COMMON_COURSE_PATHS: tuple[str, ...] = (
    "/study/degrees-and-courses",
    "/degrees",
    "/courses",
    "/programs",
    "/programmes",
    "/study/programs",
    "/undergraduate-courses",
    "/postgraduate-courses",
    "/our-courses",
    "/find-a-course",
    "/course-search",
    "/study/undergraduate",
    "/study/postgraduate",
    "/academics/programs",
    "/academics/courses",
    "/future-students/courses",
    "/all-courses",
)

# Strong URL patterns scored in step 2 (link-scan).
# Each pattern is anchored to *end-of-path* (``/?$`` or ``\b`` immediately
# before query/fragment) so we never promote a leaf course URL like
# ``/courses/bachelor-of-business`` — only true listing roots like
# ``/courses`` or ``/find-a-course`` win the link-scan score.
_STRONG_URL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"/study/degrees-and-courses/?(?:[?#]|$)",
        r"/degrees/?(?:[?#]|$)",
        r"/study/courses/?(?:[?#]|$)",
        r"/courses/?(?:[?#]|$)",
        r"/programs/?(?:[?#]|$)",
        r"/programmes/?(?:[?#]|$)",
        r"/find-a-course/?(?:[?#]|$)",
        r"/search(?:[-/]).*course",
        r"/course-search/?(?:[?#]|$)",
        r"/undergraduate-courses/?(?:[?#]|$)",
        r"/postgraduate-courses/?(?:[?#]|$)",
        r"/our-courses/?(?:[?#]|$)",
        r"/all-courses/?(?:[?#]|$)",
        r"/browse-courses/?(?:[?#]|$)",
        r"/course-list/?(?:[?#]|$)",
        r"/course-finder/?(?:[?#]|$)",
        r"/course-guide/?(?:[?#]|$)",
    )
)

_TEXT_PRIMARY = re.compile(r"\b(courses?|programmes?|degrees?)\b", re.I)
_TEXT_SECONDARY = re.compile(r"\b(all|search|find|browse|explore|view)\b", re.I)
_TEXT_TERTIARY = re.compile(r"\b(study|study with us|our courses)\b", re.I)
_ERROR_URL_RE = re.compile(
    r"/(404|not[-_]?found|error|page[-_]?not[-_]?found)(/?$|\?|#)", re.I
)


# Category slug configuration — split into a generic set tried on every
# host and a per-host map only tried for that host. Keeps VIT-specific
# slugs (`bits`, `mits`, `mba`, `bbus`, etc.) from leaking onto other
# universities that happen to use a ``/course-finder``-shaped listing
# path. Mirrors the Node config split (routes/scrape.ts:6968-6972 +
# host-overrides table) but reorganised for clarity.

#: Slugs that name a degree level — universally meaningful, so they're
#: safe to probe on any host that exposes a category-filter listing.
_GENERIC_CATEGORY_SLUGS: tuple[str, ...] = (
    "bachelor", "master", "diploma", "certificate", "graduate",
    "undergraduate", "postgraduate", "phd", "honours",
)

#: Slugs that name a *brand* (BBus, BITs, MITs, MBA, …) or a VIT
#: program family (vocational, elicos). These are only meaningful on
#: the listed host — probing them on CSU/USQ/UTAS would waste 4 HEAD
#: requests per slug for zero recall. Keys MUST be the bare host
#: (no scheme, no port, no leading dot).
_HOST_CATEGORY_SLUGS: dict[str, tuple[str, ...]] = {
    "vit.edu.au": ("bits", "mits", "mba", "bbus", "vocational", "elicos"),
}


def _normalise_host(host: str) -> str:
    """Lower-case, strip ``www.`` prefix, drop any port — so e.g.
    ``WWW.vit.edu.au:443`` matches the bare dict key ``vit.edu.au``.

    A raw ``parsed.netloc`` includes user-info, port, and any case
    quirks; without normalisation a real VIT URL like
    ``https://www.vit.edu.au/course-list`` would miss the host-specific
    slug list and fall back to generic-only — the exact regression
    that motivated this helper.
    """
    h = (host or "").lower().strip()
    # Drop user-info if present (``user:pass@host``).
    if "@" in h:
        h = h.rsplit("@", 1)[1]
    # Drop port.
    if ":" in h:
        h = h.split(":", 1)[0]
    # Drop common ``www.`` cosmetic prefix.
    if h.startswith("www."):
        h = h[4:]
    return h


def _slugs_for_host(host: str) -> tuple[str, ...]:
    """Return the slug list to probe for ``host``.

    For hosts with an entry in :data:`_HOST_CATEGORY_SLUGS`, the host-
    specific brand slugs are returned **first** so the 3-empty-slug
    early-exit cannot starve them — without this, a VIT scrape where
    ``bachelor/master/diploma`` all 404 would short-circuit before
    ``bbus/mits/mba`` ever got probed and the 24 → 30 expansion would
    silently fail. For hosts with no host-specific entry, only generic
    slugs are returned.
    """
    h = _normalise_host(host)
    extra = _HOST_CATEGORY_SLUGS.get(h, ())
    if extra:
        # Host-specific slugs FIRST — they're the high-value targets
        # for hosts that actually use category-filter URLs.
        return extra + _GENERIC_CATEGORY_SLUGS
    return _GENERIC_CATEGORY_SLUGS


#: Backward-compatible flat alias kept for any caller (or test) that
#: imported the old tuple directly. New code should call
#: :func:`_slugs_for_host` instead.
COURSE_CATEGORY_SLUGS: tuple[str, ...] = (
    _GENERIC_CATEGORY_SLUGS
    + tuple(s for slugs in _HOST_CATEGORY_SLUGS.values() for s in slugs)
)


class _AnchorExtractor(HTMLParser):
    """Tiny stdlib parser that collects every ``<a href=...>text</a>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "a":
            href = next((v for (k, v) in attrs if k == "href" and v), None)
            if href:
                self._href = href
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self.links.append((self._href, text))
            self._href = None
            self._text = []


def _is_home_page(url: str) -> bool:
    """Return True when ``url`` looks like the institution's marketing home
    page — empty path or just ``/``. Sub-paths like ``/study`` are NOT
    home pages and shouldn't trigger the redirect detector."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = (parsed.path or "").strip("/")
    return path == "" or path.lower() in {"index.html", "index.php", "home"}


async def _head_probe(client: httpx.AsyncClient, test_url: str) -> str | None:
    """HEAD-probe ``test_url`` and return the final URL on a 2xx response,
    or ``None`` on any error / non-2xx / redirect-to-error-page."""
    try:
        resp = await client.head(
            test_url,
            headers=_HEAD_HEADERS,
            follow_redirects=True,
            timeout=_HEAD_TIMEOUT_SEC,
        )
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    final_url = str(resp.url) or test_url
    if _ERROR_URL_RE.search(final_url):
        return None
    return final_url


async def detect_course_listing_page(
    home_url: str,
    html: str,
    *,
    emit=None,
) -> str | None:
    """Return the real course-listing URL for ``home_url`` or ``None``.

    Pipeline (mirrors Node ``detectCourseListingPage``):

    1. **High-priority HEAD probe.** Fast HEAD against a small set of
       well-known catalogue paths (``/course-list``, ``/courses``, …).
       First 2xx wins. Avoids downloading the full page (which can cost
       up to 9× a HEAD on heavy sites).
    2. **Link-scan.** Walk every ``<a href>`` on the home page and score
       it against ``_STRONG_URL_PATTERNS`` + link text heuristics.
       Highest scorer (≥3) wins.
    3. **Broad HEAD-probe fallback.** Same as step 1 but with a wider
       list of well-known catalogue paths.

    Returns ``None`` only if all three steps fail.
    """
    try:
        parsed = urlparse(home_url)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(http2=False) as client:
        # ── STEP 1: high-priority HEAD probe ────────────────────────────
        for path in _HIGH_PRIORITY_PATHS:
            test_url = f"{origin}{path}"
            final_url = await _head_probe(client, test_url)
            if final_url:
                if emit:
                    await emit(
                        "status",
                        f"[DISCOVER] Home page detected → course listing at "
                        f"{final_url} (high-priority probe)",
                        phase="discover",
                        kind="home_redirect",
                    )
                return final_url

        # ── STEP 2: link-scan with weighted scoring ─────────────────────
        candidates: list[tuple[str, int]] = []
        if html:
            ext = _AnchorExtractor()
            try:
                ext.feed(html)
            except Exception:
                pass
            for href, text in ext.links:
                full = _resolve(href, home_url, origin)
                if not full:
                    continue
                lower_url = full.lower()
                lower_text = (text or "").lower()
                score = 0
                if any(p.search(lower_url) for p in _STRONG_URL_PATTERNS):
                    score += 3
                if _TEXT_PRIMARY.search(lower_text):
                    score += 2
                if _TEXT_SECONDARY.search(lower_text):
                    score += 1
                if _TEXT_TERTIARY.search(lower_text):
                    score += 1
                if score >= 3:
                    candidates.append((full, score))
        if candidates:
            candidates.sort(key=lambda c: c[1], reverse=True)
            best = candidates[0][0]
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] Home page detected → course listing found at {best}",
                    phase="discover",
                    kind="home_redirect",
                )
            return best

        # ── STEP 3: broad HEAD-probe fallback ───────────────────────────
        for path in _COMMON_COURSE_PATHS:
            test_url = f"{origin}{path}"
            final_url = await _head_probe(client, test_url)
            if final_url:
                if emit:
                    await emit(
                        "status",
                        f"[DISCOVER] Home page detected → course listing at "
                        f"{final_url}",
                        phase="discover",
                        kind="home_redirect",
                    )
                return final_url

    return None


# ---------------------------------------------------------------------------
# Category-filter expansion (T004)
# ---------------------------------------------------------------------------

# Only try category expansion on listing paths that look like they use
# category-filter URLs (the VIT-style ``/course-list``, ``/course-finder``,
# ``/course-guide`` family). Generic ``/courses`` or ``/programs`` paths
# are excluded because they almost never use ``?course_categories[0]=``
# query strings, and probing 17 slugs × 4 variants per non-VIT site
# would cost ~68 redundant HEAD requests with zero recall benefit.
_CATEGORY_EXPAND_PATH_RE = re.compile(
    r"/(course-list|course-finder|course-guide)/?$", re.I
)

# Short-circuit category expansion when this many consecutive slugs add
# zero candidates — strong signal the host doesn't use category-filter
# URLs at all. Caps the worst-case cost at ~3 slugs × 4 variants = 12
# HEAD probes when expansion accidentally fires on a non-VIT-shaped site.
_CATEGORY_EXPAND_EARLY_EXIT = 3


async def expand_course_list_with_categories(
    listing_url: str,
    existing: list[dict[str, Any]],
    *,
    emit=None,
) -> list[dict[str, Any]]:
    """Probe well-known category-filter URL variants of ``listing_url``
    and merge any new course links into ``existing``.

    Mirrors Node ``expandCourseListWithCategories`` (routes/scrape.ts:7087-7131).
    For each slug in :data:`COURSE_CATEGORY_SLUGS`, try four variant URL
    shapes (``?course_categories[0]=slug``, ``?category=slug``,
    ``?type=slug``, ``/{slug}``); on the first that 200s, fetch the page
    and harvest its ``<a>`` links the same way ``discovery._looks_like_course``
    does. Per-slug, only one working variant is followed.
    """
    try:
        parsed = urlparse(listing_url)
    except Exception:
        return existing
    if not parsed.scheme or not parsed.netloc:
        return existing
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_path = parsed.path or "/"
    if not _CATEGORY_EXPAND_PATH_RE.search(base_path):
        return existing

    seen: set[str] = {c.get("url", "") for c in existing if c.get("url")}
    extra: list[dict[str, Any]] = []
    consecutive_empty = 0

    # Compose the slug list per-host — generic slugs always, plus any
    # host-specific brand slugs. Prevents e.g. CSU from getting probed
    # for ``bits``/``mits``/``mba``/``bbus`` (VIT-only program names).
    host_slugs = _slugs_for_host(parsed.netloc)

    async with httpx.AsyncClient(http2=False) as client:
        for slug in host_slugs:
            if consecutive_empty >= _CATEGORY_EXPAND_EARLY_EXIT:
                # Host clearly doesn't use category-filter URLs — bail.
                break
            variants = (
                f"{origin}{base_path}?course_categories[0]={slug}",
                f"{origin}{base_path}?category={slug}",
                f"{origin}{base_path}?type={slug}",
                f"{origin}{base_path.rstrip('/')}/{slug}",
            )
            harvested_this_slug = False
            for variant in variants:
                if harvested_this_slug:
                    break
                # HEAD probe first — cheap fail-fast for unknown variants.
                head_ok = False
                try:
                    resp = await client.head(
                        variant,
                        headers=_HEAD_HEADERS,
                        follow_redirects=True,
                        timeout=_HEAD_TIMEOUT_SEC,
                    )
                    head_ok = resp.status_code < 400
                except Exception:
                    continue
                if not head_ok:
                    continue
                # Full GET to extract links.
                page_html = await fetch_html(variant)
                if not page_html:
                    continue
                ext = _AnchorExtractor()
                try:
                    ext.feed(page_html)
                except Exception:
                    continue
                added_before = len(extra)
                for href, text in ext.links:
                    full = _resolve(href, variant, origin)
                    if not full or full in seen:
                        continue
                    if not _looks_like_course(full, text):
                        continue
                    if _JUNK_TEXT.match(text or ""):
                        continue
                    seen.add(full)
                    extra.append(
                        {
                            "url": full,
                            "name": text or full.rsplit("/", 1)[-1],
                        }
                    )
                if len(extra) > added_before:
                    harvested_this_slug = True
                    if emit:
                        await emit(
                            "status",
                            f"[DISCOVER] Category /{slug}: +"
                            f"{len(extra) - added_before} new candidates "
                            f"({variant})",
                            phase="discover",
                            kind="category_expand",
                            slug=slug,
                            added=len(extra) - added_before,
                        )
            if harvested_this_slug:
                consecutive_empty = 0
            else:
                consecutive_empty += 1
            # Yield to event loop occasionally so a long expansion doesn't
            # starve the orchestrator.
            await asyncio.sleep(0)

    if extra and emit:
        await emit(
            "status",
            f"[DISCOVER] Category expansion added {len(extra)} candidate(s) "
            f"(total {len(existing) + len(extra)})",
            phase="discover",
            kind="category_expand_done",
            added=len(extra),
            total=len(existing) + len(extra),
        )

    return existing + extra


__all__ = (
    "detect_course_listing_page",
    "expand_course_list_with_categories",
    "COURSE_CATEGORY_SLUGS",
    "_is_home_page",
)

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
_JUNK_TEXT = re.compile(
    r"^(home|about|contact|news|events?|search|menu|login|sign\s*in|"
    r"apply\s*now|read\s*more|learn\s*more|view\s*all|see\s*all)$",
    re.I,
)


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
    lurl = url.lower()
    if any(h in lurl for h in _COURSE_URL_HINTS):
        return True
    if text and not _JUNK_TEXT.match(text) and _COURSE_TEXT.search(text):
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
                elif depth < 1 and _is_nav(full) and full not in visited:
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

    return [{"url": u, "name": n} for u, n in list(found.items())[:max_courses]]

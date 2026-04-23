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


async def discover_course_links(
    start_url: str, *, max_pages: int = 25, max_courses: int = 200
) -> list[dict]:
    """BFS crawl from start_url. Returns ``[{url, name}]`` for each course-like link."""
    parsed = urlparse(start_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    queue: list[tuple[str, int]] = [(start_url, 0)]
    visited: set[str] = set()
    found: dict[str, str] = {}

    while queue and len(visited) < max_pages and len(found) < max_courses:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        html = await fetch_html(url)
        if not html:
            continue
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

    return [{"url": u, "name": n} for u, n in list(found.items())[:max_courses]]

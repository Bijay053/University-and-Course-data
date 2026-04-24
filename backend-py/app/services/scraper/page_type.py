"""Rule-based page-type classifier (zero AI, zero network).

Mirrors Node ``classifyPageByRules`` (artifacts/api-server/src/routes/scrape.ts
~4724). Given an HTML body and the URL it came from, decides whether the
page is:

* ``listing`` — an index page that links out to many courses
* ``detail`` — a single-course landing page
* ``unknown`` — neither (homepage, faculty page, marketing, etc.)

The discovery BFS uses this to AVOID drilling navigation links on real
detail pages (which would waste the per-page budget) and to PREFER
extracting links from pages that look like real listings (which is where
the catalogue actually lives on most university sites).

Pure-stdlib HTMLParser — no cheerio dependency. The DOM model is loose
(headings/title are first-occurrence, body text is a flat string) but
that's enough for the rule tree below.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TypedDict
from urllib.parse import urlparse

from app.services.scraper.discovery import (
    _COURSE_TEXT,
    _JUNK_TEXT,
    _looks_like_course,
    _resolve,
)

_MAX_HTML_CHARS = 200_000  # cap parsing on very large pages
_MAX_LINKS = 400  # match Node's `.slice(0, 400)`
_MAX_BODY_CHARS = 12_000  # match Node's `.slice(0, 12000)`

# Degree-level keywords that, when seen in an H1, strongly suggest the
# page is a single-course detail page rather than a listing.
_DEGREE_H1_RE = re.compile(
    r"\b(bachelor|master|doctor|phd|graduate certificate|graduate diploma|"
    r"diploma of|certificate [iivx]+|honours|mba|msc|bed|bsc|beng|llb|jd)\b",
    re.I,
)
# Listing-style title words.
_LISTING_TITLE_RE = re.compile(
    r"\b(courses?|programs?|programmes?|degrees?|study|undergraduate|postgraduate)\b",
    re.I,
)


class PageClassification(TypedDict):
    page_type: str  # 'listing' | 'detail' | 'unknown'
    course_links: list[dict]  # [{url, name}]
    reason: str


class _ClassifierParser(HTMLParser):
    """Single-pass HTML walk that captures everything the rule tree needs.

    We extend the link extraction from ``discovery._LinkExtractor`` to
    also capture the first ``<h1>`` and ``<title>`` and a chunk of body
    text. Done in one parse rather than three separate sweeps to keep
    classification cheap on large pages.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, text)
        self.h1_text: str = ""
        self.title_text: str = ""
        self.body_text: list[str] = []
        self._body_chars = 0
        self._in_anchor: bool = False
        self._current_href: str | None = None
        self._current_text: list[str] = []
        self._in_h1: bool = False
        self._h1_done: bool = False
        self._in_title: bool = False
        self._title_done: bool = False
        # Skip text we know is noise so the body sample is more
        # representative of the actual page content.
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1
            return
        if tag == "a":
            href = next((v for (k, v) in attrs if k == "href" and v), None)
            if href and len(self.links) < _MAX_LINKS:
                self._in_anchor = True
                self._current_href = href
                self._current_text = []
        elif tag == "h1" and not self._h1_done:
            self._in_h1 = True
        elif tag == "title" and not self._title_done:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "a" and self._in_anchor:
            text = re.sub(r"\s+", " ", "".join(self._current_text)).strip()
            assert self._current_href is not None
            self.links.append((self._current_href, text))
            self._in_anchor = False
            self._current_href = None
            self._current_text = []
        elif tag == "h1" and self._in_h1:
            self.h1_text = re.sub(r"\s+", " ", self.h1_text).strip()
            self._in_h1 = False
            self._h1_done = True
        elif tag == "title" and self._in_title:
            self.title_text = re.sub(r"\s+", " ", self.title_text).strip()
            self._in_title = False
            self._title_done = True

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_anchor:
            self._current_text.append(data)
        if self._in_h1 and not self._h1_done:
            self.h1_text += data
        if self._in_title and not self._title_done:
            self.title_text += data
        # Body sample is everything we see, capped.
        if self._body_chars < _MAX_BODY_CHARS:
            chunk = data[: _MAX_BODY_CHARS - self._body_chars]
            self.body_text.append(chunk)
            self._body_chars += len(chunk)


def _has_degree_qualifier_in_last_segment(path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    last = parts[-1].replace("-", " ").replace("_", " ")
    return bool(_COURSE_TEXT.search(last))


def classify_page(html: str, url: str) -> PageClassification:
    """Classify ``html`` (already-fetched bytes/string for ``url``).

    Always returns a ``PageClassification`` — never raises. Course links
    in the result are deduped by URL, filtered through the same heuristic
    the BFS crawler uses, and capped at ``_MAX_LINKS``.
    """
    sampled = html[:_MAX_HTML_CHARS] if html else ""
    parser = _ClassifierParser()
    try:
        parser.feed(sampled)
    except Exception:
        return PageClassification(
            page_type="unknown", course_links=[], reason="html parse failed"
        )

    try:
        parsed_url = urlparse(url)
        origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        path_lower = parsed_url.path.lower()
    except Exception:
        origin = ""
        path_lower = ""

    # Resolve + dedupe course links visible on the page.
    seen: set[str] = set()
    course_links: list[dict] = []
    for href, text in parser.links:
        if not text or len(text) < 5 or len(text) > 180:
            continue
        full = _resolve(href, url, origin) if origin else None
        if not full or full in seen:
            continue
        if _looks_like_course(full, text) and not _JUNK_TEXT.match(text):
            seen.add(full)
            course_links.append({"url": full, "name": text})

    h1 = parser.h1_text
    title = parser.title_text
    body = "".join(parser.body_text).lower()
    title_or_h1 = h1 or title
    has_degree_h1 = bool(_DEGREE_H1_RE.search(h1))
    url_looks_like_detail = (
        bool(path_lower)
        and len([p for p in path_lower.split("/") if p]) >= 2
        and _has_degree_qualifier_in_last_segment(path_lower)
    )
    has_course_content = bool(_COURSE_TEXT.search(body)) and bool(
        re.search(r"\b(fee|tuition|ielts|duration|intake|entry requirement)", body, re.I)
    )

    # DETAIL: degree H1 + URL pattern + few outbound course links.
    # The "few links" check stops a listing page that happens to mention a
    # degree in its H1 (e.g. "All Bachelor courses") from being miscalled.
    if has_degree_h1 and url_looks_like_detail and len(course_links) < 6:
        return PageClassification(
            page_type="detail",
            course_links=[],
            reason=f'H1="{h1[:60]}", URL matches course-detail pattern',
        )
    # DETAIL: strong course content + very few outbound course links —
    # catches the case where a user pasted a single course URL.
    if has_course_content and len(course_links) < 3:
        return PageClassification(
            page_type="detail",
            course_links=[],
            reason=f"course content present, only {len(course_links)} outbound links",
        )
    # LISTING: many course links found.
    if len(course_links) >= 5:
        return PageClassification(
            page_type="listing",
            course_links=course_links,
            reason=f"{len(course_links)} course links found",
        )
    # LISTING: a few course links + listing-shaped title.
    if course_links and _LISTING_TITLE_RE.search(title_or_h1):
        return PageClassification(
            page_type="listing",
            course_links=course_links,
            reason=f"{len(course_links)} course links + listing title",
        )
    # LISTING: any course links at all — better to surface them than
    # discard.
    if course_links:
        return PageClassification(
            page_type="listing",
            course_links=course_links,
            reason=f"{len(course_links)} course links found",
        )
    return PageClassification(
        page_type="unknown",
        course_links=[],
        reason="no course links or degree content detected",
    )

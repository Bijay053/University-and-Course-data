"""Fetch admission-relevant sub-pages linked from a course detail page.

Some universities split fee, requirements, intake, and scholarship information
across separate tabs or sub-pages rather than putting it all on the main course
URL.  This module detects those links, fetches them, and returns merged text
so Gemini can extract data it would otherwise never see.

Sources followed (per-course sub-pages only):
  - Fees / tuition / fee schedule
  - Entry requirements / academic requirements
  - English language requirements
  - International student requirements / information
  - Intake / start dates
  - Scholarships (when clearly linked from the course page)

Never followed:
  - How to apply / application process
  - Course structure / modules / curriculum
  - Career outcomes / open days / student life
  - University-wide pages (no course identifier in path)
  - External domains

YAML config (both fields on ExtractionConfig):
    extraction:
      follow_admission_links: true   # off by default
      max_admission_linked_pages: 4  # default 4
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Link detection patterns
# ---------------------------------------------------------------------------

# Anchor text that suggests the linked page contains admission data.
_ADMISSION_LINK_TEXT_RE = re.compile(
    r"(?:tuition\s+)?fees?|fee\s+(?:schedule|summary|information|details?)"
    r"|entry\s+req|admission\s+req|academic\s+req|entry\s+criteria"
    r"|english\s+(?:language\s+)?req|language\s+req|ielts|toefl|pte"
    r"|international\s+(?:students?|req|applicants?|fees?|information)"
    r"|intake|start\s+dates?|when\s+(?:can\s+i\s+)?(?:apply|start)"
    r"|scholarships?\b",
    re.IGNORECASE,
)

# URL path segments that clearly indicate an admission sub-page.
_ADMISSION_PATH_RE = re.compile(
    r"/(?:fees?|tuition|fee[-_]schedule|fee[-_](?:information|details?)"
    r"|entry[-_]req|admission[-_]req|requirements?|academic[-_]req"
    r"|english[-_](?:language[-_])?req|language[-_]req"
    r"|international(?:[-_](?:students?|req|fees?))?"
    r"|intake|start[-_]dates?|scholarship"
    r")(?:/|$|\?|#)",
    re.IGNORECASE,
)

# URL path segments that are never useful for admission data — skip immediately.
_NON_ADMISSION_PATH_RE = re.compile(
    r"/(?:apply|application|how[-_]to[-_]apply"
    r"|open[-_]day|visit|tour"
    r"|student[-_]life|testimonial|career|alumni"
    r"|structure|modules?|curriculum|units?"
    r"|contact|about[-_]us|news|events?"
    r"|login|sign[-_]in|register"
    r")(?:/|$|\?|#)",
    re.IGNORECASE,
)


def _find_admission_links(html: str, base_url: str) -> list[str]:
    """Return prioritised list of admission-relevant internal links found in *html*.

    Priority order:
      tier1 — URL path matches _ADMISSION_PATH_RE (most reliable signal)
      tier2 — anchor text matches _ADMISSION_LINK_TEXT_RE and URL is course-relative

    Only same-domain, course-relative links are returned.  A link is
    "course-relative" when its URL path starts with the base course path,
    meaning it is a sub-page of the same course rather than a university-wide
    central page.
    """
    try:
        from bs4 import BeautifulSoup, Tag  # type: ignore[import]

        soup = BeautifulSoup(html, "html.parser")
        base_parsed = urlparse(base_url)
        base_host = base_parsed.netloc.lower()
        # Normalise: strip trailing slash for prefix comparison.
        base_path = base_parsed.path.rstrip("/")

        seen: set[str] = {base_url}
        tier1: list[str] = []
        tier2: list[str] = []

        for a_tag in soup.find_all("a", href=True):
            if not isinstance(a_tag, Tag):
                continue
            href = (a_tag.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            full_url = urljoin(base_url, href).split("#")[0]
            parsed = urlparse(full_url)

            # Same hostname only.
            if parsed.netloc.lower() != base_host:
                continue

            # Skip duplicates and the base URL itself.
            if not full_url or full_url in seen:
                continue

            # Skip paths that are explicitly non-admission.
            if _NON_ADMISSION_PATH_RE.search(parsed.path):
                continue

            # "Course-relative" check: the link must live under the same course
            # path prefix (base_path), ensuring we follow per-course sub-pages
            # rather than university-wide fee/requirements pages.
            link_path = parsed.path.rstrip("/")
            is_course_relative = (
                link_path.startswith(base_path)
                and len(link_path) > len(base_path)
            )

            anchor_text = a_tag.get_text(" ", strip=True)
            path_match = bool(_ADMISSION_PATH_RE.search(parsed.path))
            text_match = bool(_ADMISSION_LINK_TEXT_RE.search(anchor_text))

            # Both tiers require is_course_relative to avoid pulling in
            # university-wide central pages (those are handled by central_pages.py).
            # path_match vs text_match only controls priority ordering.
            if is_course_relative:
                if path_match:
                    tier1.append(full_url)
                elif text_match:
                    tier2.append(full_url)
                seen.add(full_url)

        return tier1 + tier2

    except Exception as exc:
        log.debug("[LINKED-PAGES] link detection failed for %s: %s", base_url, exc)
        return []


async def fetch_linked_pages_text(
    course_url: str,
    html: str,
    *,
    max_pages: int = 4,
    emit: Any = None,
) -> str:
    """Detect and fetch admission-relevant sub-pages from the course detail page.

    Returns the combined plain-text content of all successfully fetched pages,
    separated by blank lines and prefixed with a [Source: <url>] label.
    Returns empty string when no relevant links are found or every fetch fails.

    The returned text is appended to the HTML that Gemini receives so it can
    extract fee / IELTS / intake data from pages the main course URL doesn't
    contain.

    Args:
        course_url:  Canonical URL of the main course detail page.
        html:        Fetched HTML of that page (used for link detection).
        max_pages:   Maximum number of sub-pages to fetch (default 4).
        emit:        Optional SSE/Celery emit callback for status messages.
    """
    if not html:
        return ""

    links = _find_admission_links(html, course_url)
    if not links:
        return ""

    links = links[:max_pages]
    log.info(
        "[LINKED-PAGES] %s — %d candidate admission link(s) found: %s",
        course_url,
        len(links),
        [u[-60:] for u in links],
    )

    from app.services.scraper.http_fetcher import fetch_html  # lazy import
    from app.services.scraper.extractors._text import html_to_text  # lazy import
    from app.services.scraper.admission_text_filter import (  # lazy import
        filter_admission_html,
    )

    async def _fetch_one(url: str) -> tuple[str, str]:
        """Return (url, text) for a linked page; text='' on failure."""
        try:
            page_html = await asyncio.wait_for(fetch_html(url), timeout=15.0)
            if not page_html:
                return url, ""
            filtered = filter_admission_html(page_html, url=url)
            text = html_to_text(filtered).strip()
            return url, text
        except asyncio.TimeoutError:
            log.debug("[LINKED-PAGES] timeout for %s", url)
            return url, ""
        except Exception as exc:
            log.debug("[LINKED-PAGES] fetch error for %s: %s", url, exc)
            return url, ""

    results = await asyncio.gather(*[_fetch_one(u) for u in links])

    parts: list[str] = []
    for fetched_url, text in results:
        if text:
            parts.append(f"[Source: {fetched_url}]\n{text}")
            if emit:
                try:
                    await emit(
                        "status",
                        f"[LINKED-PAGES] +{len(text)} chars from {fetched_url[-70:]}",
                        phase="extract",
                        kind="linked_page_fetched",
                        url=fetched_url,
                    )
                except Exception:
                    pass

    if parts:
        log.info(
            "[LINKED-PAGES] %s — appending %d/%d linked page(s) (%d chars total)",
            course_url,
            len(parts),
            len(links),
            sum(len(p) for p in parts),
        )

    return "\n\n".join(parts)

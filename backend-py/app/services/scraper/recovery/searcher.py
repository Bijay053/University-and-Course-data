"""Recovery searcher — true BFS search for candidate pages on the same domain.

Given a university scrape_url and the set of fields that need recovery, this
module performs a bounded BFS crawl of the domain and returns a ranked list of
(url, category) pairs to pass to the extractor.

BFS rules:
- Starts at scrape_url (seed page).
- Follows links within the same apex domain.
- Visits at most MAX_BFS_PAGES pages total across all hops.
- Scores every discovered link for each needed category.
- Returns at most MAX_CANDIDATE_PAGES total candidate (url, category) pairs,
  balanced across categories.

Phase 1 categories:
    "fees"          → international_fee
    "english"       → ielts_overall
    "intakes"       → intake_months
    "location"      → course_location
    "requirements"  → other_requirement
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# Maximum pages the BFS will visit (fetch) in total.
MAX_BFS_PAGES = 30
# Maximum candidate (url, category) pairs to return in total.
MAX_CANDIDATE_PAGES = 8

# Keyword → field-category mapping.  Multiple keywords can map to the same
# category; a page matching ANY keyword is included as a candidate.
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "fees": [
        "tuition", "fee", "fees", "cost", "costs", "pricing",
        "international fee", "tuition fee",
    ],
    "english": [
        "ielts", "english requirement", "english language", "english proficiency",
        "language requirement", "entry requirement", "admission requirement",
        "toefl", "pte",
    ],
    "intakes": [
        "intake", "start date", "semester", "trimester", "enrolment",
        "when to apply", "application deadline", "open date",
    ],
    "location": [
        "campus", "location", "study location", "where to study",
        "on campus", "study site",
    ],
    "requirements": [
        "entry requirement", "admission requirement", "academic requirement",
        "prerequisite", "eligibility", "minimum requirement", "entry criteria",
        "how to apply", "selection criteria",
    ],
}

# URL path segments that strongly suggest a given category.
_PATH_SEGMENT_SCORES: dict[str, list[str]] = {
    "fees": ["fee", "fees", "tuition", "cost", "costs", "pricing"],
    "english": ["ielts", "english", "language", "requirements", "admission"],
    "intakes": ["intake", "dates", "calendar", "semester", "trimester", "start"],
    "location": ["campus", "location", "campuses", "study-site"],
    "requirements": ["requirements", "entry", "admission", "prerequisite", "eligibility", "apply"],
}

# Field name → category
FIELD_TO_CATEGORY: dict[str, str] = {
    "international_fee": "fees",
    "ielts_overall": "english",
    "intake_months": "intakes",
    "course_location": "location",
    "other_requirement": "requirements",
}


def _apex_domain(url: str) -> str:
    """Return the apex domain (no www.) for a URL."""
    try:
        host = urlparse(url).netloc.lower()
        return re.sub(r"^www\.", "", host)
    except Exception:
        return ""


def _path_score(path: str, category: str) -> int:
    """Score how well a URL path matches the category (higher = better)."""
    segments = [s.lower() for s in path.split("/") if s]
    score = 0
    for seg_kw in _PATH_SEGMENT_SCORES.get(category, []):
        if any(seg_kw in seg for seg in segments):
            score += 2
    return score


def _text_score(text: str, category: str) -> int:
    """Score how well anchor text matches the category."""
    low = text.lower()
    score = 0
    for kw in _CATEGORY_KEYWORDS.get(category, []):
        if kw in low:
            score += 1
    return score


def _score_link(href: str, anchor_text: str, needed_categories: set[str]) -> dict[str, int]:
    """Return per-category scores for a discovered link. Zero-score categories are omitted."""
    parsed = urlparse(href)
    scores: dict[str, int] = {}
    for cat in needed_categories:
        ps = _path_score(parsed.path, cat)
        ts = _text_score(anchor_text, cat)
        total = ps + ts
        if total > 0:
            scores[cat] = total
    return scores


def _same_apex(link_url: str, apex: str) -> bool:
    """True when link_url is on the apex domain OR any of its subdomains.

    Examples (apex = "university.edu.au"):
      "https://university.edu.au/fees"        → True  (exact match)
      "https://www.university.edu.au/fees"    → True  (www stripped by _apex_domain)
      "https://handbook.university.edu.au/"   → True  (subdomain)
      "https://other.edu.au/"                 → False (different apex)
    """
    link_host = urlparse(link_url).netloc.lower()
    # Strip leading www.
    link_host = re.sub(r"^www\.", "", link_host)
    return link_host == apex or link_host.endswith("." + apex)


def _extract_links(html: str, base_url: str, apex: str) -> list[tuple[str, str]]:
    """Return (full_url, anchor_text) pairs for same-apex-domain links on the page.

    Accepts links on the exact apex host AND any subdomains (e.g. handbook.*,
    study.*) so that universities whose fee/requirement pages live on a
    subdomain are still reachable during recovery.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        full_url = urljoin(base_url, href).split("#")[0].rstrip("/")
        if not full_url.startswith("http"):
            continue
        if not _same_apex(full_url, apex):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        anchor_text = link.get_text(" ", strip=True)
        out.append((full_url, anchor_text))

    return out


async def search_candidate_pages(
    scrape_url: str,
    needed_categories: set[str],
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Return ranked candidate pages for the requested categories via BFS.

    Parameters
    ----------
    scrape_url:
        University entry point (e.g. "https://www.acu.edu.au/study").
    needed_categories:
        Set of category names from FIELD_TO_CATEGORY values.
    timeout:
        HTTP timeout per request.

    Returns
    -------
    list of dicts with keys:
        url, category, score, path_score, matched_keyword
    At most MAX_CANDIDATE_PAGES total entries, balanced across categories.
    Sorted by score descending.
    """
    if not scrape_url or not needed_categories:
        return []

    apex = _apex_domain(scrape_url)
    if not apex:
        log.warning("[RECOVERY:search] cannot determine apex domain for %r", scrape_url)
        return []

    log.info(
        "[RECOVERY:search] starting BFS from %r for categories=%s apex=%r max_pages=%d",
        scrape_url, needed_categories, apex, MAX_BFS_PAGES,
    )

    try:
        import httpx
    except ImportError:
        log.warning("[RECOVERY:search] httpx not available — skipping search")
        return []

    # Import PDF-specific scorer for use as a fallback when the standard HTML
    # scorer gives 0.  The PDF scorer uses a broader keyword list (e.g.
    # "international", "schedule") that catches PDFs whose URL or anchor text
    # don't contain the HTML page keywords.  Non-fatal if extractor unavailable.
    try:
        from app.services.scraper.recovery.extractor import (
            _score_pdf_link as _pdf_link_scorer,
        )
    except ImportError:
        _pdf_link_scorer = None  # type: ignore[assignment]

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # (url, category) → best score so far
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    visited: set[str] = set()
    pages_fetched = 0

    # BFS frontier: list of (url, depth)
    frontier: deque[tuple[str, int]] = deque()
    frontier.append((scrape_url, 0))
    visited.add(scrape_url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        while frontier and pages_fetched < MAX_BFS_PAGES:
            url, depth = frontier.popleft()

            try:
                r = await client.get(url, headers=_HEADERS, timeout=timeout)
                if r.status_code >= 400:
                    log.debug("[RECOVERY:search] HTTP %d for %r", r.status_code, url)
                    continue
                html = r.text
                pages_fetched += 1
                log.debug("[RECOVERY:search] fetched page %d/%d: %r", pages_fetched, MAX_BFS_PAGES, url)
            except Exception as exc:
                log.debug("[RECOVERY:search] fetch error %r: %s", url, exc)
                continue

            # Score all links discovered on this page
            links = _extract_links(html, url, apex)
            for full_url, anchor_text in links:
                scores = _score_link(full_url, anchor_text, needed_categories)

                # PDF fallback: if the standard HTML scorer gives 0 for every
                # category, try the broader PDF-specific keyword scorer from
                # extractor.py.  It covers tokens like "international" and
                # "schedule" that appear in _PDF_CATEGORY_KEYWORDS but not in
                # the HTML-page scoring dicts.  This ensures PDFs are surfaced
                # from *any* BFS-visited page, not only from pages that are
                # themselves high-scoring HTML candidates.
                if full_url.lower().endswith(".pdf") and not scores and _pdf_link_scorer is not None:
                    for cat in needed_categories:
                        ps = _pdf_link_scorer(full_url, anchor_text, {cat})
                        if ps > 0:
                            scores[cat] = ps

                for cat, score in scores.items():
                    key = (full_url, cat)
                    if key not in candidates or score > candidates[key]["score"]:
                        parsed = urlparse(full_url)
                        ps = _path_score(parsed.path, cat)
                        matched_kw = next(
                            (
                                kw for kw in _CATEGORY_KEYWORDS.get(cat, [])
                                if kw in anchor_text.lower() or kw in parsed.path.lower()
                            ),
                            anchor_text[:60],
                        )
                        candidates[key] = {
                            "url": full_url,
                            "category": cat,
                            "score": score,
                            "path_score": ps,
                            "matched_keyword": matched_kw,
                        }

                # Add high-scoring links to BFS frontier for deeper traversal.
                # PDFs are skipped here — they cannot be crawled for more links and
                # their binary content wastes a BFS page slot.  They are still added
                # to `candidates` above and will be processed by _extract_from_pdf.
                if depth < 2 and not full_url.lower().endswith(".pdf"):
                    total_score = sum(
                        _path_score(urlparse(full_url).path, cat)
                        + _text_score(anchor_text, cat)
                        for cat in needed_categories
                    )
                    if total_score > 0 and full_url not in visited:
                        visited.add(full_url)
                        frontier.append((full_url, depth + 1))
                elif full_url.lower().endswith(".pdf") and scores:
                    log.debug(
                        "[RECOVERY:search] PDF candidate found: url=%r categories=%s score=%s",
                        full_url, list(scores.keys()), scores,
                    )

    log.info(
        "[RECOVERY:search] BFS complete — visited %d pages, found %d raw candidates",
        pages_fetched, len(candidates),
    )

    # Sort all candidates by score desc, pick top MAX_CANDIDATE_PAGES globally,
    # balanced so each category gets at least one slot if it has any candidates.
    all_sorted = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)

    final: list[dict[str, Any]] = []
    per_category: dict[str, int] = {}
    # First pass: up to (MAX_CANDIDATE_PAGES // len(needed_categories)) per category
    slots_per_cat = max(1, MAX_CANDIDATE_PAGES // max(len(needed_categories), 1))
    for item in all_sorted:
        cat = item["category"]
        if per_category.get(cat, 0) < slots_per_cat:
            per_category[cat] = per_category.get(cat, 0) + 1
            final.append(item)
        if len(final) >= MAX_CANDIDATE_PAGES:
            break

    # Second pass: fill remaining slots with highest scorers regardless of category
    if len(final) < MAX_CANDIDATE_PAGES:
        in_final = {(x["url"], x["category"]) for x in final}
        for item in all_sorted:
            key = (item["url"], item["category"])
            if key not in in_final:
                final.append(item)
                in_final.add(key)
            if len(final) >= MAX_CANDIDATE_PAGES:
                break

    for item in final:
        log.info(
            "[RECOVERY:search] candidate url=%r category=%r score=%d keyword=%r",
            item["url"], item["category"], item["score"], item["matched_keyword"],
        )

    log.info(
        "[RECOVERY:search] returning %d candidates for categories=%s",
        len(final), needed_categories,
    )
    return final

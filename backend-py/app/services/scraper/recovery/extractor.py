"""Recovery extractor — fetch a candidate page and run the appropriate extractor.

For each (url, categories) pair from the searcher, this module:
1. Fetches the page once (HTTP).
2. Runs ALL matching category extractors on the same HTML.
3. Also checks for linked PDFs and extracts from those.
4. Returns structured results keyed by field name.

Phase 1 extractors:
    fees         → extractors.fee.extract()
    english      → extractors.english_test.extract()
    intakes      → extractors.intake.extract()
    location     → extractors.location.extract()
    requirements → extractors.academic_requirement (entry requirements text)
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Category → field names that will be populated by the extractor
_CATEGORY_FIELDS: dict[str, list[str]] = {
    "fees": ["international_fee", "fee_term", "currency"],
    "english": ["ielts_overall", "ielts_listening", "ielts_reading",
                "ielts_writing", "ielts_speaking", "pte_overall", "toefl_overall"],
    "intakes": ["intake_months"],
    "location": ["course_location"],
    "requirements": ["other_requirement"],
}


def _html_has_meaningful_content(html: str) -> bool:
    """True when the HTML looks like a rendered page rather than a JS shell.

    A JS shell typically has < 2000 characters and very few semantic tags
    before the JS has run.  If the static fetch gives us something with real
    content we skip the expensive browser fallback.
    """
    if not html or len(html) < 800:
        return False
    # Look for paragraph / list / table content — absent in pure JS shells
    import re
    tag_hits = len(re.findall(r"<(?:p|li|td|th|h[1-6])[^>]*>", html, re.I))
    return tag_hits >= 5


async def _browser_fetch_html(url: str, timeout_ms: int = 25_000) -> str | None:
    """Fetch a page using the Playwright browser pool.

    Mirrors the pattern in central_pages._fetch_with_browser_fallback.
    Returns rendered HTML or None on any failure.  Always non-fatal.
    """
    import asyncio as _asyncio

    async def _do_browser() -> str | None:
        # Try stealth browser first if enabled (handles Cloudflare-protected pages)
        try:
            from app.services.scraper.stealth_browser import (
                stealth_fetch_html,
                stealth_required,
            )
            if stealth_required():
                stealth_html = await stealth_fetch_html(url, timeout_ms=30_000)
                if stealth_html and _html_has_meaningful_content(stealth_html):
                    log.info("[RECOVERY:extract] stealth fetch OK for %r (%d chars)", url, len(stealth_html))
                    return stealth_html
        except Exception as exc:
            log.debug("[RECOVERY:extract] stealth fetch failed for %r: %s", url, exc)

        # Fall through to the regular browser pool
        try:
            from app.services.scraper.browser_pool import pool as browser_pool
            async with browser_pool.page() as page:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(3_000)
                rendered = await page.content()
                if rendered and len(rendered) > 500:
                    return rendered
        except Exception as exc:
            log.debug("[RECOVERY:extract] browser pool fetch failed for %r: %s", url, exc)

        return None

    try:
        return await _asyncio.wait_for(_do_browser(), timeout=60)
    except _asyncio.TimeoutError:
        log.warning("[RECOVERY:extract] browser fetch timed out (60s) for %r", url)
    except Exception as exc:
        log.warning("[RECOVERY:extract] browser fetch failed for %r: %s", url, exc)
    return None


async def _fetch_html(url: str, timeout: float = 12.0) -> tuple[str, str]:
    """Fetch a URL and return (html, source_type).

    Strategy:
    1. Static HTTP — fast, handles most static-HTML universities.
    2. Browser fallback (Playwright) — if static fetch returns a JS shell
       (< 800 chars or < 5 semantic tags).  Sets source_type='browser'.

    source_type is one of: 'html', 'browser', 'pdf_direct', 'pdf_content_type'.
    """
    if url.lower().endswith(".pdf"):
        return ("", "pdf_direct")

    html_static: str | None = None
    try:
        import httpx
        _HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout
        ) as client:
            r = await client.get(url, headers=_HEADERS)
            if r.status_code < 400:
                ct = r.headers.get("content-type", "")
                if "pdf" in ct:
                    return ("", "pdf_content_type")
                html_static = r.text
    except Exception as exc:
        log.debug("[RECOVERY:extract] HTTP fetch error %r: %s", url, exc)

    # If static HTML has meaningful content, use it immediately
    if html_static and _html_has_meaningful_content(html_static):
        log.debug("[RECOVERY:extract] HTTP fetch OK for %r (%d chars)", url, len(html_static))
        return (html_static, "html")

    # Static returned a JS shell or nothing — try browser fallback
    log.info(
        "[RECOVERY:extract] %r — static fetch %s; trying browser fallback",
        url,
        f"returned {len(html_static)} chars (JS shell)" if html_static else "failed",
    )
    browser_html = await _browser_fetch_html(url)
    if browser_html:
        log.info("[RECOVERY:extract] browser fallback OK for %r (%d chars)", url, len(browser_html))
        return (browser_html, "browser")

    # Both failed — return whatever static gave us (may be empty)
    log.info("[RECOVERY:extract] %r — both HTTP and browser fetch failed; proceeding with empty HTML", url)
    return (html_static or "", "html_empty")


async def _run_extractor(
    html: str, url: str, category: str, country: str | None = None
) -> list[dict[str, Any]]:
    """Run the appropriate extractor for the category. Returns list of result dicts."""
    results: list[dict[str, Any]] = []
    if not html:
        return results

    try:
        if category == "fees":
            from app.services.scraper.extractors import fee
            raw = await fee.extract(html, url, country=country)
        elif category == "english":
            from app.services.scraper.extractors import english_test
            raw = await english_test.extract(html, url)
        elif category == "intakes":
            from app.services.scraper.extractors import intake
            raw = await intake.extract(html, url)
        elif category == "location":
            from app.services.scraper.extractors import location
            raw = await location.extract(html, url)
        elif category == "requirements":
            # Extract entry/academic requirements text from page
            raw = await _extract_requirements_text(html, url)
        else:
            log.warning("[RECOVERY:extract] unknown category %r", category)
            return results

        allowed_fields = _CATEGORY_FIELDS.get(category, [])
        for er in raw:
            field_key = (
                getattr(er, "field_key", None)
                if not isinstance(er, dict)
                else er.get("field_key")
            )
            if field_key not in allowed_fields:
                continue
            value = getattr(er, "value", None) if not isinstance(er, dict) else er.get("value")
            normalized = getattr(er, "normalized", None) if not isinstance(er, dict) else er.get("normalized")
            confidence = getattr(er, "confidence", None) if not isinstance(er, dict) else er.get("confidence")
            snippet = getattr(er, "snippet", None) if not isinstance(er, dict) else er.get("snippet")
            method = getattr(er, "method", "unknown") if not isinstance(er, dict) else er.get("method", "unknown")

            log.info(
                "[RECOVERY:extract] source=%r category=%r field=%r value=%r confidence=%s method=%r",
                url, category, field_key, value, confidence, method,
            )
            results.append({
                "field": field_key,
                "value": value,
                "normalized": normalized,
                "confidence": float(confidence) if confidence is not None else None,
                "snippet": str(snippet)[:500] if snippet else None,
                "method": method,
                "source_url": url,
                "source_type": "html",
            })
    except Exception as exc:
        log.warning(
            "[RECOVERY:extract] extractor error for category=%r url=%r: %s",
            category, url, exc,
        )
    return results


async def _extract_requirements_text(html: str, url: str) -> list[dict[str, Any]]:
    """Extract entry/academic requirements text from page HTML.

    Uses heuristic heading + paragraph extraction to find the main entry
    requirements section. Returns a list of one result dict with field_key
    'other_requirement'.
    """
    import re
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    _HEADING_PATTERNS = re.compile(
        r"(entry|admission|academic|prerequisite|eligibility|selection|requirement)",
        re.I,
    )
    _EXCLUDE_PATTERNS = re.compile(r"(english|language|ielts|toefl|pte|fee|cost|tuition)", re.I)

    # Find headings that match requirements semantics
    for heading_tag in ("h1", "h2", "h3", "h4"):
        for hdr in soup.find_all(heading_tag):
            hdr_text = hdr.get_text(" ", strip=True)
            if not _HEADING_PATTERNS.search(hdr_text):
                continue
            if _EXCLUDE_PATTERNS.search(hdr_text):
                continue

            # Gather the next few sibling/following paragraphs
            body_parts: list[str] = []
            node = hdr.find_next_sibling()
            depth = 0
            while node and depth < 5:
                tag_name = getattr(node, "name", None)
                if tag_name in ("h1", "h2", "h3", "h4"):
                    break
                txt = node.get_text(" ", strip=True)
                if len(txt) > 20:
                    body_parts.append(txt)
                node = node.find_next_sibling()
                depth += 1

            if body_parts:
                full_text = " ".join(body_parts)[:1000]
                log.info(
                    "[RECOVERY:extract] requirements found under heading %r at %r",
                    hdr_text, url,
                )
                return [{
                    "field_key": "other_requirement",
                    "value": full_text,
                    "normalized": full_text,
                    "confidence": 0.60,
                    "snippet": full_text[:300],
                    "method": "heuristic_heading",
                }]

    log.debug("[RECOVERY:extract] no requirements section found at %r", url)
    return []


_PDF_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "fees": ["fee", "fees", "tuition", "cost", "costs", "pricing", "international", "schedule"],
    "english": ["ielts", "english", "language", "requirement", "toefl", "pte", "admission"],
    "intakes": ["intake", "dates", "calendar", "semester", "trimester", "start"],
    "location": ["campus", "location", "campuses", "study"],
    "requirements": ["requirement", "entry", "admission", "prerequisite", "eligibility"],
}

_MAX_LINKED_PDFS = 5

# Hard cap on total PDFs fetched across ALL candidate pages for a single
# university in one recovery pass.  Prevents a fee-schedule-heavy university
# (e.g. one PDF per faculty) from inflating recovery runtime unpredictably.
MAX_PDFS_PER_RECOVERY_RUN = 10


def _score_pdf_link(pdf_url: str, anchor_text: str, categories: set[str]) -> int:
    """Return a relevance score for a PDF link against the needed categories."""
    combined = (pdf_url.lower() + " " + anchor_text.lower())
    score = 0
    for cat in categories:
        for kw in _PDF_CATEGORY_KEYWORDS.get(cat, []):
            if kw in combined:
                score += 1
    return score


async def _find_linked_pdfs(
    html: str,
    base_url: str,
    categories: set[str] | None = None,
) -> list[str]:
    """Return PDF URLs linked from the page, ranked by relevance to needed categories.

    Scores each PDF link against category keywords (URL path + anchor text).
    Returns at most ``_MAX_LINKED_PDFS`` URLs, highest-scoring first.
    Un-scored links (no category filter) are returned in discovery order.
    """
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        scored: list[tuple[int, str]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.lower().endswith(".pdf"):
                continue
            full = urljoin(base_url, href).split("#")[0]
            if full in seen:
                continue
            seen.add(full)
            anchor_text = a.get_text(" ", strip=True)
            score = _score_pdf_link(full, anchor_text, categories or set()) if categories else 0
            scored.append((score, full))
            log.debug(
                "[RECOVERY:extract] PDF link found: url=%r anchor=%r score=%d",
                full, anchor_text[:80], score,
            )
        if not scored:
            return []
        # Sort by score descending so most-relevant PDFs are fetched first
        scored.sort(key=lambda t: t[0], reverse=True)
        result = [url for _, url in scored[:_MAX_LINKED_PDFS]]
        log.info(
            "[RECOVERY:extract] %d PDF link(s) found at %r; keeping top %d: %s",
            len(scored), base_url, len(result), result,
        )
        return result
    except Exception as exc:
        log.debug("[RECOVERY:extract] _find_linked_pdfs error at %r: %s", base_url, exc)
        return []


async def _extract_from_pdf(pdf_url: str, categories: set[str]) -> list[dict[str, Any]]:
    """Fetch a PDF and run all given category extractors on its text.

    Uses ``pdf_fetcher.download_pdf_text`` to download and parse the PDF,
    wraps the plain text in a minimal HTML shell so the existing extractor
    functions can process it, then tags every result with ``source_type='pdf'``.
    """
    results: list[dict[str, Any]] = []
    try:
        from app.services.scraper.pdf_fetcher import download_pdf_text
        log.info("[RECOVERY:extract] fetching PDF %r for categories=%s", pdf_url, categories)
        pdf_text = await download_pdf_text(pdf_url)
        if not pdf_text:
            log.debug("[RECOVERY:extract] PDF %r returned no text — skipping", pdf_url)
            return results

        log.info(
            "[RECOVERY:extract] PDF %r parsed — %d chars; running extractors for categories=%s",
            pdf_url, len(pdf_text), categories,
        )
        # PDF text is plain, so wrap in minimal HTML so the extractors can parse it
        wrapped = f"<html><body><pre>{pdf_text}</pre></body></html>"
        for cat in categories:
            cat_results = await _run_extractor(wrapped, pdf_url, cat)
            for r in cat_results:
                r["source_type"] = "pdf"
                r["source_url"] = pdf_url
                log.debug(
                    "[RECOVERY:extract] PDF field extracted: pdf=%r field=%r value=%r confidence=%s",
                    pdf_url, r.get("field"), r.get("value"), r.get("confidence"),
                )
            if cat_results:
                log.info(
                    "[RECOVERY:extract] PDF %r category=%r → %d field(s) extracted",
                    pdf_url, cat, len(cat_results),
                )
            results.extend(cat_results)
    except ImportError:
        log.debug("[RECOVERY:extract] pdf_fetcher not available — skipping PDF %r", pdf_url)
    except Exception as exc:
        log.warning("[RECOVERY:extract] PDF extraction error for %r: %s", pdf_url, exc)
    return results


async def extract_from_url(
    url: str,
    categories: set[str],
    *,
    country: str | None = None,
    timeout: float = 12.0,
    metadata: dict | None = None,
    pdf_budget: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch a URL once and run ALL specified category extractors on it.

    This is the primary entry-point for the recovery pipeline.  It avoids
    fetching the same URL multiple times when it is a candidate for more than
    one field category.

    Parameters
    ----------
    url:
        Candidate page URL.
    categories:
        Set of category names to extract from the page (e.g. {"fees", "english"}).
    country:
        Country hint for fee currency detection.
    timeout:
        HTTP fetch timeout in seconds.
    metadata:
        Optional dict that will be updated with ``source_type`` after fetching.
    pdf_budget:
        Optional mutable one-element list ``[remaining]`` shared across all
        ``extract_from_url`` calls for a single university in one recovery pass.
        Each PDF fetched decrements the counter; when it reaches 0 a WARNING is
        logged and no further PDFs are fetched for this university.  If
        ``None``, no cross-URL budget is enforced (``_MAX_LINKED_PDFS`` still
        caps per-page PDF discovery).

    Returns
    -------
    list of extraction result dicts with keys:
        field, value, normalized, confidence, snippet, method,
        source_url, source_type
    """
    log.info("[RECOVERY:extract] fetching url=%r for categories=%s", url, categories)

    html, source_type = await _fetch_html(url, timeout=timeout)
    if metadata is not None:
        metadata["source_type"] = source_type
    results: list[dict[str, Any]] = []

    if html:
        for cat in categories:
            cat_results = await _run_extractor(html, url, cat, country=country)
            results.extend(cat_results)

        # Check for linked PDFs and extract from those too.
        # Pass categories so only relevant PDFs are fetched.
        pdf_links = await _find_linked_pdfs(html, url, categories=categories)
        for pdf_url in pdf_links:
            # Honour the per-university PDF budget when provided.
            if pdf_budget is not None:
                if pdf_budget[0] <= 0:
                    log.warning(
                        "[RECOVERY:extract] PDF budget exhausted (MAX_PDFS_PER_RECOVERY_RUN=%d)"
                        " — skipping remaining PDFs for this university (next skipped: %r)",
                        MAX_PDFS_PER_RECOVERY_RUN,
                        pdf_url,
                    )
                    break
                pdf_budget[0] -= 1
            log.info("[RECOVERY:extract] following linked PDF %r — running extractors", pdf_url)
            pdf_results = await _extract_from_pdf(pdf_url, categories)
            results.extend(pdf_results)

    elif source_type in ("pdf_direct", "pdf_content_type"):
        # The URL itself is a PDF — counts against the budget too.
        if pdf_budget is not None:
            if pdf_budget[0] <= 0:
                log.warning(
                    "[RECOVERY:extract] PDF budget exhausted (MAX_PDFS_PER_RECOVERY_RUN=%d)"
                    " — skipping direct PDF %r",
                    MAX_PDFS_PER_RECOVERY_RUN,
                    url,
                )
                return results
            pdf_budget[0] -= 1
        pdf_results = await _extract_from_pdf(url, categories)
        results.extend(pdf_results)

    if not results:
        log.info(
            "[RECOVERY:extract] no values extracted from %r for categories=%s",
            url, categories,
        )

    return results


async def extract_from_candidate(
    candidate: dict[str, Any],
    *,
    country: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch a candidate page and extract recovery values (single-category shim).

    Prefer using extract_from_url() with a set of categories for efficiency.
    This function is kept for backward compatibility.
    """
    url = candidate["url"]
    category = candidate["category"]
    return await extract_from_url(url, {category}, country=country)

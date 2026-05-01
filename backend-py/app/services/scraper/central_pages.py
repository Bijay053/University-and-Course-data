"""Central-pages pre-fetch (Bug 2 / KBS).

Many universities (KBS, USyd, UNSW, ANU, …) publish tuition fees and English-
language requirements ONCE on a central page rather than repeating them on every
course page.  The per-course extractors therefore find nothing and those fields
stage as NULL even though the data is publicly available.

This module:

1. Reads URLs from ``university.scrape_config['uniPages']``:
   ``feePage``, ``entryPage``, ``requirementsPage``.
2. Fetches each page with the existing HTTP fetcher (same rate-limiting,
   user-agent, and retry logic used throughout the scraper).
3. Parses fees into a list of ``CentralFeeRecord`` dicts keyed by program name
   so downstream code can fuzzy-match them against individual course names.
4. Parses English requirements using the existing ``english_test`` extractor
   (same regexes, sub-band logic, and IELTS/PTE/TOEFL detection).

Public entry-point: :func:`prefetch_central_pages`.

The returned ``CentralData`` dict is passed through the per-course pipeline
and applied as a *last-resort* fallback — lower priority than per-course page
data, AI fill, and uni-PDF data.  Confidence ceiling: 0.45 for fees, 0.50 for
English (IELTS published on a dedicated requirements page is more reliable than
a generic fee table).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.services.scraper.extractors._text import html_to_text
from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# CentralFeeRecord: one row from the fee-schedule page.
#   program_pattern  — raw program name as parsed from the page (used for
#                       fuzzy matching against course_name later).
#   international_fee — numeric fee value, or None if not found.
#   domestic_fee      — numeric domestic fee, or None.
#   currency          — ISO-4217 string e.g. "AUD".
#   per               — "year" | "trimester" | "semester" | "course" | None.
CentralFeeRecord = dict[str, Any]

# CentralData: top-level output of prefetch_central_pages.
#   fees    — list[CentralFeeRecord] (may be empty).
#   english — dict of slot → value for IELTS/PTE/TOEFL (may be empty).
#   fee_page_url     — source URL for provenance.
#   english_page_url — source URL for provenance.
CentralData = dict[str, Any]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CURRENCY_RE = re.compile(
    r"(?:A\$|AUD\s*|NZ\$|CA\$|US\$|\$)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

_PER_TRIMESTER_RE = re.compile(r"\b(per\s*trimester|per\s*tri)\b", re.IGNORECASE)
_PER_SEMESTER_RE = re.compile(r"\b(per\s*semester|per\s*sem)\b", re.IGNORECASE)
_PER_YEAR_RE = re.compile(r"\b(per\s*year|per\s*annum|p\.?a\.?|annual)\b", re.IGNORECASE)

# Sanity range: $2 000 – $200 000 (individual fee amounts, not salary).
_FEE_MIN = 2_000
_FEE_MAX = 200_000

# Degree-level keyword hints used to build a fallback bucket when per-program
# matching fails.  Order matters — postgrad checked before undergrad so a
# "Graduate Certificate" row doesn't accidentally land in undergrad bucket.
_POSTGRAD_TOKENS = (
    "master", "mba", "graduate certificate", "graduate diploma",
    "doctor", "phd", "doctorate", "postgraduate",
)
_UNDERGRAD_TOKENS = (
    "bachelor", "diploma", "certificate", "undergraduate",
    "associate", "foundation", "bridging",
)

_ENGLISH_SLOTS = (
    "ielts_overall",
    "ielts_listening",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)


# ---------------------------------------------------------------------------
# Fee-page parser
# ---------------------------------------------------------------------------

def _parse_fee_amount(text: str) -> float | None:
    """Extract the first plausible fee amount from a text snippet."""
    for m in _CURRENCY_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if _FEE_MIN <= val <= _FEE_MAX:
            return val
    return None


def _infer_per_term(text: str) -> str | None:
    """Return a normalised fee_term enum value or None.

    The returned strings are the same controlled vocabulary used by the
    fee extractor (fee.py _normalize_fee_term) and the UI dropdown:
      "Annual" | "Semester" | "Trimester" | "Full Course" | "Per Unit"

    Returning None means the caller should apply a column-type default.
    """
    if _PER_TRIMESTER_RE.search(text):
        return "Trimester"
    if _PER_SEMESTER_RE.search(text):
        return "Semester"
    if _PER_YEAR_RE.search(text):
        return "Annual"
    return None


def _programme_bucket(name: str) -> str:
    """Map a program name to 'postgraduate' | 'undergraduate' | 'unknown'."""
    low = name.lower()
    for tok in _POSTGRAD_TOKENS:
        if tok in low:
            return "postgraduate"
    for tok in _UNDERGRAD_TOKENS:
        if tok in low:
            return "undergraduate"
    return "unknown"


def _parse_fee_page_html(html: str, page_url: str) -> list[CentralFeeRecord]:
    """Parse a central fee page into a list of CentralFeeRecord dicts.

    Strategy:
    1. Try BeautifulSoup table-row parsing (structured fee table).
    2. Fall back to line-by-line text scan (definition-list / card layout).

    Returns an empty list on parse failure — never raises.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("central_pages: BeautifulSoup not available — fee page parse skipped")
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        log.warning("central_pages: HTML parse error on %s: %s", page_url, exc)
        return []

    records: list[CentralFeeRecord] = []

    # ── Strategy 1: <table> rows ────────────────────────────────────────────
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Identify header row to find "Course", "Fee", "International" cols.
        header_cells = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not header_cells:
            continue

        # ── Multi-row header detection (e.g. KBS) ───────────────────────────
        # Some tables use rows[0] as a title/colspan row ("International Student
        # Tuition Fees") and rows[1] as the actual column header row ("Course",
        # "Subject fee^", "Course fee"). If the first row doesn't contain any
        # fee-signal keywords, check whether rows[1] does, and if so use it as
        # the effective header for column detection. The first data row is then
        # rows[2] instead of rows[1].
        effective_header_cells = header_cells
        data_start = 1
        if len(rows) > 2:
            row1_cells = [c.get_text(" ", strip=True).lower() for c in rows[1].find_all(["th", "td"])]
            row1_text = " ".join(row1_cells)
            row0_has_fee = any(k in " ".join(header_cells) for k in ("fee", "tuition", "international", "domestic"))
            row1_has_fee_col = any(k in row1_text for k in ("subject fee", "course fee", "unit fee", "international", "total fee", "program fee"))
            if row1_has_fee_col and not any(k in " ".join(header_cells) for k in ("subject fee", "course fee", "unit fee", "total fee", "program fee")):
                # rows[0] is a title/label row; rows[1] contains real column headers
                effective_header_cells = row1_cells
                data_start = 2

        # Find relevant column indices.
        prog_col = next(
            (i for i, h in enumerate(effective_header_cells) if any(k in h for k in ("course", "program", "programme", "qualification"))),
            None,
        )
        if prog_col is None:
            prog_col = 0  # first col is usually program name

        intl_col = next(
            (i for i, h in enumerate(effective_header_cells) if any(k in h for k in ("international", "overseas", "intl"))),
            None,
        )
        dom_col = next(
            (i for i, h in enumerate(effective_header_cells) if any(k in h for k in ("domestic", "local", "resident"))),
            None,
        )
        # KBS-style tables use "Subject fee" (per-subject/trimester) and "Course fee"
        # (total program fee).  Prefer total_col because users compare program totals.
        total_col = next(
            (i for i, h in enumerate(effective_header_cells)
             if any(k in h for k in ("course fee", "total fee", "program fee", "full fee", "total cost"))),
            None,
        )
        unit_col = next(
            (i for i, h in enumerate(effective_header_cells)
             if any(k in h for k in ("subject fee", "unit fee", "per subject", "per unit"))),
            None,
        )

        # Column priority: explicit intl > total-course > per-unit > domestic > scan-all
        # KBS: intl_col may match the *title* row "International..." and point to col 0
        # (course name). Guard: if intl_col == prog_col, discard it so we fall through
        # to the more specific total_col / unit_col detection.
        if intl_col is not None and intl_col == prog_col:
            intl_col = None

        primary_fee_col = intl_col if intl_col is not None else (
            total_col if total_col is not None else (
                unit_col if unit_col is not None else None
            )
        )

        # Sniff the per-term from the header row text.
        header_text = " ".join(effective_header_cells)
        per_term = _infer_per_term(header_text)
        # "Course fee" column = full program total → "Full Course";
        # "Subject fee" column = per-unit enrolment → "Per Unit".
        # These match the controlled enum used by fee.py and the UI dropdown.
        if total_col is not None:
            per_term = per_term or "Full Course"
        elif unit_col is not None and intl_col is None:
            per_term = per_term or "Per Unit"

        for row in rows[data_start:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            cell_texts = [c.get_text(" ", strip=True) for c in cells]
            if len(cell_texts) <= prog_col:
                continue

            prog_name = cell_texts[prog_col].strip()
            if not prog_name or len(prog_name) < 3:
                continue

            # Skip header-repeat rows.
            if any(k in prog_name.lower() for k in ("course", "program", "qualification")):
                continue

            intl_fee: float | None = None
            dom_fee: float | None = None

            if primary_fee_col is not None and primary_fee_col < len(cell_texts):
                intl_fee = _parse_fee_amount(cell_texts[primary_fee_col])
            if dom_col is not None and dom_col < len(cell_texts):
                dom_fee = _parse_fee_amount(cell_texts[dom_col])

            # If primary column had no fee, scan remaining cells left-to-right.
            if intl_fee is None and dom_fee is None:
                for ct in cell_texts[prog_col + 1:]:
                    v = _parse_fee_amount(ct)
                    if v is not None:
                        intl_fee = v  # assume international when ambiguous
                        break

            if intl_fee is None and dom_fee is None:
                continue

            # Per-term may also appear in the cell itself.
            row_text = " ".join(cell_texts)
            row_per = _infer_per_term(row_text) or per_term

            records.append({
                "program_pattern": prog_name,
                "international_fee": intl_fee,
                "domestic_fee": dom_fee,
                "currency": "AUD",   # default; improved below if USD/GBP detected
                "per": row_per,
                "bucket": _programme_bucket(prog_name),
                "source_url": page_url,
            })

    if records:
        return records

    # ── Strategy 2: plain-text line scan (card / DL layout) ────────────────
    # Handles two layouts:
    #   (a) Multi-line: program name on one line, fee on the next 1–3 lines.
    #   (b) Same-line:  "Program Name — $fee" or "Program Name: $69,000"
    #       (e.g. AIT /apply page lists total-course fees in this format).
    _SAME_LINE_SPLIT_RE = re.compile(
        r"^(.+?)\s*[-—–:]\s*(?=A?\$|USD\s|\bAUD\s)",
        re.IGNORECASE,
    )
    text = html_to_text(html)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        fee_val = _parse_fee_amount(line)
        if fee_val is None:
            continue
        # Look back up to 3 lines for a program name (heuristic).
        context_lines = lines[max(0, i - 3): i + 1]
        prog_candidate = ""
        for cl in reversed(context_lines[:-1]):
            if _parse_fee_amount(cl) is None and len(cl) > 4:
                prog_candidate = cl
                break
        # Fallback: same-line "Program Name — $fee" format.
        # When both program name and fee appear on a single line (separated by
        # a dash, em-dash, or colon), the backward scan above finds nothing
        # because there is no prior line containing the name.  Extract the
        # program name from the text before the separator instead.
        if not prog_candidate:
            m = _SAME_LINE_SPLIT_RE.match(line)
            if m:
                candidate = m.group(1).strip()
                # Must look like a real program name: > 4 chars, no fee amount.
                if len(candidate) > 4 and _parse_fee_amount(candidate) is None:
                    prog_candidate = candidate
        if not prog_candidate:
            continue
        per_term = _infer_per_term(line) or _infer_per_term(" ".join(context_lines))
        records.append({
            "program_pattern": prog_candidate,
            "international_fee": fee_val,
            "domestic_fee": None,
            "currency": "AUD",
            "per": per_term,
            "bucket": _programme_bucket(prog_candidate),
            "source_url": page_url,
        })

    return records


# ---------------------------------------------------------------------------
# English-requirements parser — flat + level-aware
# ---------------------------------------------------------------------------

# Matches headings that introduce an undergraduate or postgraduate section.
# Used to split central requirements pages that publish separate rows per
# degree level (e.g. ASA's /policies-and-forms page).
_LEVEL_HEADING_RE = re.compile(
    r"\b(undergraduate|postgraduate|under\s*grad(?:uate)?|post\s*grad(?:uate)?)\b",
    re.IGNORECASE,
)


async def _parse_english_by_level_async(
    html: str, page_url: str
) -> dict[str, dict[str, Any]]:
    """Parse a central English requirements page into per-level buckets.

    Looks for ``Undergraduate`` / ``Postgraduate`` heading words in the
    plain-text rendering of *html* and runs the english_test extractor on
    the text window that follows each heading.  Returns a dict keyed by
    ``"undergraduate"`` and/or ``"postgraduate"`` — each value is a flat
    slot dict identical to what :func:`_parse_english_page_html_async`
    returns.

    Returns ``{}`` when no level headings are detected.
    """
    try:
        from app.services.scraper.extractors import english_test

        text = html_to_text(html)
        matches = list(_LEVEL_HEADING_RE.finditer(text))
        if not matches:
            return {}

        out: dict[str, dict[str, Any]] = {}
        for idx, m in enumerate(matches):
            raw_level = m.group(1).lower()
            bucket = "undergraduate" if "under" in raw_level else "postgraduate"
            # Take the text from this heading to just before the next heading
            # (or the next 2 000 chars if there is none) so the extractor
            # doesn't bleed into an adjacent level's section.
            seg_start = m.start()
            seg_end = (
                matches[idx + 1].start() if idx + 1 < len(matches) else seg_start + 2_000
            )
            chunk = text[seg_start:seg_end]

            results = await english_test.extract(chunk, page_url)
            vals: dict[str, Any] = {}
            for r in results:
                if r.normalized:
                    for k, v in r.normalized.items():
                        if k in _ENGLISH_SLOTS and v not in (None, "", 0):
                            vals.setdefault(k, v)

            if vals:
                # If the same level heading appears twice, keep the first hit.
                if bucket not in out:
                    out[bucket] = vals
                else:
                    for k, v in vals.items():
                        out[bucket].setdefault(k, v)

        return out
    except Exception as exc:
        log.warning(
            "central_pages: level-aware english parse failed on %s: %s", page_url, exc
        )
        return {}


async def _fetch_english_with_browser(url: str) -> str | None:
    """Fetch an English-requirements page using Playwright.

    Used when ``central_english_pg_skip`` is True — which signals that the
    page JS-renders its postgraduate section (plain HTTP only exposes the UG
    row).  Browser-first with plain-HTTP fallback.

    Hard 60 s wall-clock timeout on the browser block prevents pool exhaustion
    on servers that accept the TCP handshake but never send data.
    """
    import asyncio as _asyncio

    async def _browser_fetch() -> str | None:
        from app.services.scraper.browser_pool import pool as browser_pool

        async with browser_pool.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            # Give JS time to render the requirements table.
            await page.wait_for_timeout(3_000)
            rendered = await page.content()
            if rendered and len(rendered) > 1_000:
                return rendered
        return None

    try:
        rendered = await _asyncio.wait_for(_browser_fetch(), timeout=60)
        if rendered:
            log.info(
                "central_pages: browser fetch for english page OK (%s, %d bytes)",
                url,
                len(rendered),
            )
            return rendered
    except _asyncio.TimeoutError:
        log.warning(
            "central_pages: browser fetch for english page timed out (60 s) — %s", url
        )
    except Exception as exc:
        log.warning(
            "central_pages: browser fetch for english page failed (%s): %s", url, exc
        )

    # Plain-HTTP fallback — returns the UG-only static HTML at minimum.
    try:
        html = await fetch_html(url)
        log.info(
            "central_pages: HTTP fallback for english page (%s, %d chars)",
            url,
            len(html or ""),
        )
        return html
    except Exception as exc:
        log.warning(
            "central_pages: HTTP fallback for english page failed (%s): %s", url, exc
        )
        return None


def _parse_english_page_html(html: str, page_url: str) -> dict[str, Any]:
    """Run the existing english_test extractor on a central requirements page.

    Returns a flat dict of {slot: value} for any IELTS/PTE/TOEFL/etc. slot
    that was found.  Empty dict on failure.
    """
    try:
        import asyncio
        from app.services.scraper.extractors import english_test

        async def _run() -> dict[str, Any]:
            results = await english_test.extract(html, page_url)
            out: dict[str, Any] = {}
            for r in results:
                if r.normalized:
                    for k, v in r.normalized.items():
                        if k in _ENGLISH_SLOTS and v not in (None, "", 0):
                            out.setdefault(k, v)
            return out

        return asyncio.get_event_loop().run_until_complete(_run())
    except RuntimeError:
        # Already inside an event loop — use nest_asyncio or inline call.
        import asyncio
        from app.services.scraper.extractors import english_test

        async def _run_inline() -> dict[str, Any]:
            results = await english_test.extract(html, page_url)
            out: dict[str, Any] = {}
            for r in results:
                if r.normalized:
                    for k, v in r.normalized.items():
                        if k in _ENGLISH_SLOTS and v not in (None, "", 0):
                            out.setdefault(k, v)
            return out

        return asyncio.ensure_future(_run_inline())  # type: ignore[return-value]
    except Exception as exc:
        log.warning("central_pages: english_test extractor failed on %s: %s", page_url, exc)
        return {}


async def _parse_english_page_html_async(html: str, page_url: str) -> dict[str, Any]:
    """Async-native version of English-requirements parsing."""
    try:
        from app.services.scraper.extractors import english_test

        results = await english_test.extract(html, page_url)
        out: dict[str, Any] = {}
        for r in results:
            if r.normalized:
                for k, v in r.normalized.items():
                    if k in _ENGLISH_SLOTS and v not in (None, "", 0):
                        out.setdefault(k, v)
        return out
    except Exception as exc:
        log.warning("central_pages: english_test extractor failed on %s: %s", page_url, exc)
        return {}


# ---------------------------------------------------------------------------
# Browser-backed fetch (for JS-rendered central pages)
# ---------------------------------------------------------------------------

def _is_soft_404(html: str | None) -> bool:
    """Return True when the page looks like a custom 404 (soft-404) response."""
    if not html:
        return True
    lowered = html[:2000].lower()
    return "page not found" in lowered or "404" in lowered[:500]


def _html_has_fee_signal(html: str | None) -> bool:
    """Return True when the HTML contains at least one plausible fee amount.

    Used to decide whether a plain-HTTP response is rich enough that we
    don't need to spin up a Playwright browser for the central fee page.
    A page with even 3 dollar-sign occurrences almost certainly has a fee
    table in its static HTML.
    """
    if not html:
        return False
    return len(_CURRENCY_RE.findall(html)) >= 3


def _html_has_js_rendering_signal(html: str | None) -> bool:
    """Return True when the HTML page is a JS-rendered shell with little static content.

    Detects DXPR Builder (Drupal), React/Vue shells, and other CMS patterns
    that require Playwright to reveal actual content.  Used to decide whether
    to auto-retry an English-requirements page fetch with the browser when the
    plain-HTTP response returned no English scores.

    Detection signals (any one is sufficient):
    - ``data-az-mode="dynamic"`` — DXPR Builder dynamic content placeholder
    - ``dxpr_builder`` in the page HTML — Drupal DXPR CMS fingerprint
    - ``data-dxpr-builder-libraries`` attribute — DXPR element marker
    - Very sparse text content (< 400 chars of visible text) on a 200 response
    """
    if not html:
        return False
    sample = html[:8000]
    if 'data-az-mode="dynamic"' in sample:
        return True
    if "dxpr_builder" in sample:
        return True
    if "data-dxpr-builder-libraries" in sample:
        return True
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html[:20000], "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        visible = soup.get_text(" ", strip=True)
        if len(visible) < 400:
            return True
    except Exception:
        pass
    return False


async def _fetch_with_browser_fallback(url: str) -> str | None:
    """Fetch a central page, trying plain HTTP first and Playwright as fallback.

    Strategy (HTTP-first, revised from Playwright-first):
    1. Plain HTTP — fast, no browser overhead. The vast majority of central
       fee pages (KBS /international-fees, USyd handbook, UNSW handbook, ANU)
       serve fee tables as static HTML.  If the HTTP response already contains
       fee signals (≥3 currency amounts), return it immediately.
    2. Playwright fallback — used only when HTTP returns a JS-shell (no fee
       signals detected). Adds ~4 s latency but handles true SPAs.

    Never raises — callers always receive either HTML or None.
    """
    # ── 1. Plain HTTP ────────────────────────────────────────────────────────
    try:
        html = await fetch_html(url)
        if _html_has_fee_signal(html):
            log.info("central_pages: HTTP fetch has fee signals for %s (%d chars)", url, len(html or ""))
            return html
        log.info("central_pages: HTTP fetch returned no fee signals for %s — trying browser", url)
    except Exception as exc:
        log.warning("central_pages: HTTP fetch failed for %s: %s", url, exc)
        html = None

    # ── 2. Playwright fallback ───────────────────────────────────────────────
    # Hard 60-second wall-clock timeout on the entire browser block —
    # browser_pool.page() acquisition can itself block indefinitely if the
    # pool is exhausted, which caused 1h+ hangs on the Celery worker.
    import asyncio as _asyncio

    # Hosts whose fee/english pages are React/Vue SPAs that need extra time
    # after DOMContentLoaded before fee tables/cards are injected into the DOM.
    _SLOW_SPA_HOSTS = frozenset({
        "www.torrens.edu.au", "torrens.edu.au",
        # CDU's international fees page is JS-rendered (Angular SPA).
        # The static HTTP response contains no fee signals, so the browser
        # fallback fires; with the default 3 s wait the Angular bundle hasn't
        # finished injecting the fee table.  6 s gives it enough time.
        "www.cdu.edu.au", "cdu.edu.au",
    })
    from urllib.parse import urlparse as _urlparse
    _host = _urlparse(url).netloc
    _extra_wait_ms = 6_000 if _host in _SLOW_SPA_HOSTS else 3_000

    async def _browser_fetch() -> str | None:
        from app.services.scraper.browser_pool import pool as browser_pool
        async with browser_pool.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(_extra_wait_ms)
            rendered = await page.content()
            if rendered and len(rendered) > 1000:
                return rendered
        return None

    try:
        rendered = await _asyncio.wait_for(_browser_fetch(), timeout=60)
        if rendered:
            log.info("central_pages: browser fetch OK for %s (%d bytes)", url, len(rendered))
            return rendered
    except _asyncio.TimeoutError:
        log.warning("central_pages: browser fetch timed out (60s) for %s — using HTTP html", url)
    except Exception as exc:
        log.warning("central_pages: browser fetch failed for %s: %s", url, exc)

    # Return whatever HTTP gave us (may be a JS shell, parser will just find 0 records)
    return html


# ---------------------------------------------------------------------------
# Central-page cache helpers
# ---------------------------------------------------------------------------
#
# Stores per-university, per-page-type parsed data so scrapes that run
# within 30 days of the last fetch skip the network round-trip entirely.
# All functions are fault-tolerant: if the ``central_page_cache`` table
# doesn't exist yet (e.g. DB migration pending) they log a warning and
# return as if the cache were empty — no behaviour change for callers.
#
# page_type constants:
#   "fee_schedule"          — parsed fee records + fee_page_url
#   "english_requirements"  — english slots + english_page_url + english_by_level

_CACHE_TTL_DAYS = 30


async def _cache_get(university_id: int, page_type: str) -> dict[str, Any] | None:
    """Return unexpired cached parsed_data for (university_id, page_type), or None."""
    try:
        from datetime import datetime, timezone

        from sqlalchemy import select

        from app.database import AsyncSessionLocal
        from app.models.central_page_cache import CentralPageCache

        async with AsyncSessionLocal() as session:
            row = await session.scalar(
                select(CentralPageCache).where(
                    CentralPageCache.university_id == university_id,
                    CentralPageCache.page_type == page_type,
                )
            )
            if row is None:
                return None
            # Normalise to UTC-aware before comparison
            expires = row.expires_at
            if expires.tzinfo is None:
                from datetime import timezone as _tz
                expires = expires.replace(tzinfo=_tz.utc)
            if expires > datetime.now(timezone.utc):
                return row.parsed_data
            # Expired — treat as miss (caller will re-fetch and overwrite)
            return None
    except Exception as exc:
        log.warning(
            "central_page_cache: get(%s, %s) failed — %s", university_id, page_type, exc
        )
        return None


def _is_empty_central_result(page_type: str, parsed_data: dict[str, Any]) -> bool:
    """Return True when ``parsed_data`` contains no useful extracted values.

    Prevents cache poisoning: if a central-page fetch succeeds at the HTTP
    level but the parser extracts nothing (e.g. ASA's policies PDF has no
    English values), we must NOT store an empty entry.  A future scrape
    hitting that empty cache row would report ``[CACHE] hit → no values``
    and skip the re-fetch for the full TTL period, permanently suppressing
    any values that could have been obtained by other means.

    Rules per page_type:
    * ``fee_schedule`` — must have at least one fee record in ``fees``.
    * ``english_requirements`` — must have at least one English slot in
      ``english`` OR at least one level bucket in ``english_by_level``.
    * All other page types — always considered non-empty (safe to cache).
    """
    if page_type == "fee_schedule":
        return not bool(parsed_data.get("fees"))
    if page_type == "english_requirements":
        _has_flat = bool(parsed_data.get("english"))
        _by_level = parsed_data.get("english_by_level") or {}
        _has_level = any(bool(v) for v in _by_level.values())
        return not (_has_flat or _has_level)
    return False


async def _cache_set(
    university_id: int,
    page_type: str,
    url: str,
    parsed_data: dict[str, Any],
    ttl_days: int = _CACHE_TTL_DAYS,
) -> None:
    """Upsert a cache entry.  Silently swallows errors so failures never abort scrapes."""
    # Guard: don't poison the cache with empty parse results.
    if _is_empty_central_result(page_type, parsed_data):
        log.info(
            "central_page_cache: skipping empty result for %s/%s — "
            "no useful data extracted, will re-fetch on next scrape",
            university_id,
            page_type,
        )
        return
    try:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.database import AsyncSessionLocal
        from app.models.central_page_cache import CentralPageCache

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=ttl_days)
        async with AsyncSessionLocal() as session:
            stmt = (
                pg_insert(CentralPageCache)
                .values(
                    university_id=university_id,
                    page_type=page_type,
                    url=url,
                    parsed_data=parsed_data,
                    fetched_at=now,
                    expires_at=expires,
                )
                .on_conflict_do_update(
                    index_elements=["university_id", "page_type"],
                    set_={
                        "url": url,
                        "parsed_data": parsed_data,
                        "fetched_at": now,
                        "expires_at": expires,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
        log.info(
            "[CACHE] cached %s/%s for %d days (expires %s)",
            university_id,
            page_type,
            ttl_days,
            expires.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        log.warning(
            "central_page_cache: set(%s, %s) failed — %s", university_id, page_type, exc
        )


async def invalidate_central_cache(university_id: int) -> int:
    """Delete all cache entries for a university.  Returns number of rows deleted."""
    try:
        from sqlalchemy import delete

        from app.database import AsyncSessionLocal
        from app.models.central_page_cache import CentralPageCache

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(CentralPageCache).where(
                    CentralPageCache.university_id == university_id
                )
            )
            await session.commit()
            deleted = result.rowcount  # type: ignore[attr-defined]
            log.info("central_page_cache: invalidated %d row(s) for uni %s", deleted, university_id)
            return deleted
    except Exception as exc:
        log.warning("central_page_cache: invalidate(%s) failed — %s", university_id, exc)
        return 0


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def prefetch_central_pages(
    scrape_config: dict[str, Any] | None,
    *,
    emit=None,
    university_id: int | None = None,
) -> CentralData:
    """Pre-fetch and parse central fee + English-requirements pages.

    Reads URLs from ``scrape_config['uniPages']``:
    - ``feePage``          → fee schedule (list of per-program fee records)
    - ``entryPage``        → English requirements (IELTS/PTE/TOEFL values)
    - ``requirementsPage`` → fallback English source if entryPage absent

    Returns a ``CentralData`` dict:
    ``{"fees": [...], "english": {...}, "fee_page_url": str|None, "english_page_url": str|None}``

    Always returns a valid dict (empty fields, never raises).

    When ``university_id`` is provided the results are cached in
    ``central_page_cache`` for ``_CACHE_TTL_DAYS`` days.  Subsequent calls
    within that window return the cached data immediately without any network
    round-trip.  Cache misses and cache errors are transparent — behaviour is
    identical to the pre-cache implementation.
    """
    empty: CentralData = {
        "fees": [],
        "english": {},
        "fee_page_url": None,
        "english_page_url": None,
    }

    if not scrape_config:
        return empty

    uni_pages: dict[str, str] = scrape_config.get("uniPages") or {}
    fee_url = uni_pages.get("feePage") or uni_pages.get("feesPdf")
    english_url = (
        uni_pages.get("entryPage")
        or uni_pages.get("requirementsPage")
        or uni_pages.get("requirementsPdf")
    )

    # Always preserve the pg_skip flag — single_course.py reads it to gate
    # the PG clear-out pass regardless of whether any central pages exist.
    _pg_skip = bool(scrape_config.get("central_english_pg_skip", False))

    if not fee_url and not english_url:
        return {**empty, "central_english_pg_skip": _pg_skip}

    result: CentralData = {
        "fees": [],
        "english": {},
        "fee_page_url": fee_url,
        "english_page_url": english_url,
        "central_english_pg_skip": _pg_skip,
    }

    # ── Fetch fee page ──────────────────────────────────────────────────────
    if fee_url and not fee_url.endswith(".pdf"):
        # Check cache first when university_id is known
        _fee_cached: dict[str, Any] | None = None
        if university_id is not None:
            _fee_cached = await _cache_get(university_id, "fee_schedule")
            if _fee_cached is not None:
                result["fees"] = _fee_cached.get("fees", [])
                if emit:
                    await emit(
                        "status",
                        f"[CACHE] fee_schedule hit → {len(result['fees'])} record(s) "
                        f"(cached, skipping fetch of {fee_url})",
                        phase="discover",
                        kind="central_fee_cache_hit",
                        count=len(result["fees"]),
                        url=fee_url,
                    )
                log.info(
                    "[CACHE] fee_schedule hit for uni %s (%d records)",
                    university_id, len(result["fees"]),
                )

        if _fee_cached is None:
            try:
                fee_html = await _fetch_with_browser_fallback(fee_url)
                if fee_html:
                    records = _parse_fee_page_html(fee_html, fee_url)
                    result["fees"] = records
                    if emit:
                        await emit(
                            "status",
                            f"[CENTRAL] fee page parsed → {len(records)} program record(s) from {fee_url}",
                            phase="discover",
                            kind="central_fee_parsed",
                            count=len(records),
                            url=fee_url,
                        )
                    log.info("central_pages: %d fee records from %s", len(records), fee_url)

                    # ── PDF fallback: if the HTML fee page has no inline fee
                    # data, look for linked PDFs (e.g. "Fees Schedule.pdf" on
                    # a CDN) and attempt to parse them instead. ──────────────
                    if not records:
                        _pdf_links = _extract_pdf_fee_links(fee_html, fee_url)
                        if _pdf_links:
                            log.info(
                                "central_pages: 0 inline records from %s — "
                                "probing %d PDF fee link(s): %s",
                                fee_url,
                                len(_pdf_links),
                                _pdf_links[:3],
                            )
                        for _pdf_url in _pdf_links[:3]:  # cap at 3 PDFs
                            try:
                                from app.services.scraper.pdf_fetcher import (
                                    download_pdf_text,
                                )

                                _pdf_text = await download_pdf_text(_pdf_url)
                                if not _pdf_text or len(_pdf_text) < 50:
                                    continue
                                # Wrap in <pre> so html_to_text strips tags
                                # correctly and Strategy-2 line scan runs.
                                _pdf_records = _parse_fee_page_html(
                                    f"<pre>{_pdf_text}</pre>", _pdf_url
                                )
                                if _pdf_records:
                                    records = _pdf_records
                                    result["fees"] = records
                                    log.info(
                                        "central_pages: PDF fee parse (%s) → "
                                        "%d record(s)",
                                        _pdf_url,
                                        len(records),
                                    )
                                    if emit:
                                        await emit(
                                            "status",
                                            f"[CENTRAL] PDF fee parsed → "
                                            f"{len(records)} record(s) from "
                                            f"{_pdf_url}",
                                            phase="discover",
                                            kind="central_fee_pdf_parsed",
                                            count=len(records),
                                            url=_pdf_url,
                                        )
                                    break
                            except Exception as _pdf_exc:
                                log.warning(
                                    "central_pages: PDF fee fetch/parse failed "
                                    "(%s): %s",
                                    _pdf_url,
                                    _pdf_exc,
                                )

                        # ── Hub sub-page follower ─────────────────────────
                        # When the fee URL is a navigational hub (0 inline
                        # records AND no PDF found data either), probe child
                        # sub-pages of that hub.  Example: KBS publishes an
                        # /admissions/fees hub that links to
                        # /admissions/fees/international-fees which has the
                        # actual HTML table.  Generic: any same-domain child
                        # path containing "international" and "fee" tokens is
                        # tried (up to 2 candidates, international-first).
                        if not records and fee_html:
                            from urllib.parse import urlparse as _urlparse_hub
                            _hub_parsed = _urlparse_hub(fee_url)
                            _base_domain = (
                                f"{_hub_parsed.scheme}://{_hub_parsed.netloc}"
                            )
                            _sub_candidates = _extract_fee_hub_subpage_links(
                                fee_html, fee_url, _base_domain
                            )
                            if _sub_candidates:
                                log.info(
                                    "central_pages: hub sub-page follower — "
                                    "trying %d candidate(s): %s",
                                    len(_sub_candidates),
                                    _sub_candidates[:2],
                                )
                            for _sub_url in _sub_candidates[:2]:
                                try:
                                    _sub_html = await _fetch_with_browser_fallback(
                                        _sub_url
                                    )
                                    if not _sub_html:
                                        continue
                                    _sub_records = _parse_fee_page_html(
                                        _sub_html, _sub_url
                                    )
                                    if _sub_records:
                                        records = _sub_records
                                        result["fees"] = records
                                        result["fee_page_url"] = _sub_url
                                        log.info(
                                            "central_pages: sub-page fee parse "
                                            "(%s) → %d record(s)",
                                            _sub_url,
                                            len(_sub_records),
                                        )
                                        if emit:
                                            await emit(
                                                "status",
                                                f"[CENTRAL] sub-page fee parsed → "
                                                f"{len(_sub_records)} record(s) "
                                                f"from {_sub_url}",
                                                phase="discover",
                                                kind="central_fee_subpage_parsed",
                                                count=len(_sub_records),
                                                url=_sub_url,
                                            )
                                        break
                                except Exception as _sub_exc:
                                    log.warning(
                                        "central_pages: sub-page fee fetch/parse "
                                        "failed (%s): %s",
                                        _sub_url,
                                        _sub_exc,
                                    )

                    # Store in cache
                    if university_id is not None:
                        await _cache_set(
                            university_id,
                            "fee_schedule",
                            fee_url,
                            {"fees": records, "fee_page_url": fee_url},
                        )
            except Exception as exc:
                log.warning("central_pages: fee page fetch/parse failed (%s): %s", fee_url, exc)
                if emit:
                    await emit(
                        "status",
                        f"[CENTRAL] fee page fetch failed ({fee_url}): {exc}",
                        phase="discover",
                        kind="central_fee_error",
                        level="warn",
                        url=fee_url,
                        error=str(exc)[:200],
                    )

    # ── Fetch English-requirements page ────────────────────────────────────
    # Default: plain HTTP only, bounded at 45 s.  Some servers (ASA, etc.)
    # accept the TCP handshake but never send data — the wait_for cap
    # ensures a single slow host can't stall the Celery worker.
    #
    # Level-aware fetch path: when ``central_english_pg_skip`` is True the
    # page JS-renders its postgraduate section (plain HTTP exposes only the
    # UG row).  We use the browser to render the full page, then run both the
    # flat extractor (for backward compat) and the new level-aware extractor.
    # The level-keyed dict is stored in ``result["english_by_level"]`` and
    # consumed by single_course.py to apply the correct values per degree
    # level instead of falling back to the "same for all" flat dict.
    _use_browser_for_english = _pg_skip

    if english_url and not english_url.endswith(".pdf"):
        # Check cache first when university_id is known
        _eng_cached: dict[str, Any] | None = None
        if university_id is not None:
            _eng_cached = await _cache_get(university_id, "english_requirements")
            if _eng_cached is not None:
                result["english"] = _eng_cached.get("english", {})
                if "english_by_level" in _eng_cached:
                    result["english_by_level"] = _eng_cached["english_by_level"]
                if emit:
                    _cached_slots = ", ".join(
                        f"{k}={v}"
                        for k, v in sorted(result["english"].items())
                    ) or "no values"
                    _by_level_note = (
                        f" + by_level keys: {list(_eng_cached.get('english_by_level', {}).keys())}"
                        if _eng_cached.get("english_by_level")
                        else ""
                    )
                    await emit(
                        "status",
                        f"[CACHE] english_requirements hit → {_cached_slots}"
                        f"{_by_level_note} (cached, skipping fetch of {english_url})",
                        phase="discover",
                        kind="central_english_cache_hit",
                        values=result["english"],
                        url=english_url,
                    )
                log.info(
                    "[CACHE] english_requirements hit for uni %s (%s)",
                    university_id, result["english"],
                )

        if _eng_cached is None:
            try:
                if _use_browser_for_english:
                    # Hard 75 s outer cap — the inner browser fetch has its own
                    # 60 s guard so the total wall time is bounded.
                    eng_html = await asyncio.wait_for(
                        _fetch_english_with_browser(english_url),
                        timeout=75,
                    )
                else:
                    eng_html = await asyncio.wait_for(
                        fetch_html(english_url),
                        timeout=45,
                    )
                if eng_html:
                    # Flat parse — first-seen value wins (typically UG for ASA).
                    english_vals = await _parse_english_page_html_async(eng_html, english_url)
                    result["english"] = english_vals

                    # ── Auto browser-fallback for JS-rendered English pages ──
                    # When plain HTTP returned a JS shell (e.g. Drupal DXPR
                    # Builder) the static response has no IELTS scores.  Detect
                    # that signal and re-fetch with Playwright so the rendered
                    # HTML exposes the actual requirement tables.  Skipped when
                    # we already used the browser path (_use_browser_for_english).
                    if (
                        not english_vals
                        and not _use_browser_for_english
                        and _html_has_js_rendering_signal(eng_html)
                    ):
                        log.info(
                            "central_pages: english page %s looks JS-rendered "
                            "(no scores + JS signal) — retrying with browser",
                            english_url,
                        )
                        if emit:
                            await emit(
                                "status",
                                f"[CENTRAL] english page JS-shell detected — "
                                f"retrying with browser: {english_url}",
                                phase="discover",
                                kind="central_english_browser_retry",
                                url=english_url,
                            )
                        try:
                            _br_eng_html = await asyncio.wait_for(
                                _fetch_english_with_browser(english_url),
                                timeout=75,
                            )
                            if _br_eng_html:
                                _br_vals = await _parse_english_page_html_async(
                                    _br_eng_html, english_url
                                )
                                if _br_vals:
                                    english_vals = _br_vals
                                    eng_html = _br_eng_html
                                    result["english"] = english_vals
                                    # Treat subsequent by_level parse as browser
                                    _use_browser_for_english = True
                                    log.info(
                                        "central_pages: browser retry found "
                                        "english scores for %s: %s",
                                        english_url,
                                        english_vals,
                                    )
                        except Exception as _br_exc:
                            log.warning(
                                "central_pages: english browser retry failed "
                                "(%s): %s",
                                english_url,
                                _br_exc,
                            )

                    # Level-aware parse — only meaningful when the browser
                    # rendered a page with separate UG / PG sections.
                    by_level: dict[str, Any] = {}
                    if _use_browser_for_english:
                        by_level = await _parse_english_by_level_async(eng_html, english_url)
                        if by_level:
                            result["english_by_level"] = by_level
                            if emit:
                                _level_summary = "; ".join(
                                    f"{lvl}: "
                                    + ", ".join(f"{k}={v}" for k, v in sorted(vals.items()))
                                    for lvl, vals in sorted(by_level.items())
                                )
                                await emit(
                                    "status",
                                    f"[CENTRAL] english by level → {_level_summary}",
                                    phase="discover",
                                    kind="central_english_by_level",
                                    values=by_level,
                                    url=english_url,
                                )

                    if emit:
                        slots_found = ", ".join(
                            f"{k}={v}" for k, v in sorted(english_vals.items())
                        )
                        await emit(
                            "status",
                            f"[CENTRAL] english page parsed → {slots_found or 'no values'} from {english_url}",
                            phase="discover",
                            kind="central_english_parsed",
                            values=english_vals,
                            url=english_url,
                        )
                    log.info("central_pages: english slots from %s: %s", english_url, english_vals)

                    # Store in cache
                    if university_id is not None:
                        _to_cache: dict[str, Any] = {
                            "english": english_vals,
                            "english_page_url": english_url,
                        }
                        if by_level:
                            _to_cache["english_by_level"] = by_level
                        await _cache_set(
                            university_id,
                            "english_requirements",
                            english_url,
                            _to_cache,
                        )
            except Exception as exc:
                log.warning("central_pages: english page fetch/parse failed (%s): %s", english_url, exc)
                if emit:
                    await emit(
                        "status",
                        f"[CENTRAL] english page fetch failed ({english_url}): {exc}",
                        phase="discover",
                        kind="central_english_error",
                        level="warn",
                        url=english_url,
                        error=str(exc)[:200],
                    )

    return result


# ---------------------------------------------------------------------------
# Auto-discovery: find a central fee URL from sampled course pages
# ---------------------------------------------------------------------------

# URL path fragments that strongly suggest a centralized fee schedule.
# Ordered from most-specific to most-generic.
_FEE_URL_PATHS = (
    "/international-fees",
    "/fees/international",
    "/tuition-fees/international",
    "/international-tuition",
    "/admissions/fees",
    "/fees/fee-schedule",
    "/fee-schedule",
    "/tuition-fees",
    "/fees",
    "/tuition",
    "/costs",
    "/study-costs",
    # Some smaller institutions (e.g. AIT) publish their complete fee schedule
    # on the application / admissions page rather than a dedicated /fees URL.
    # These paths have low specificity so the specificity scorer still prefers
    # a dedicated /fees or /tuition URL when both are found.
    "/how-to-apply",
    "/apply",
)

# Anchor text snippets that suggest a link leads to a fee page.
_FEE_ANCHOR_TEXT = (
    "international fees",
    "international tuition",
    "tuition fees",
    "fee schedule",
    "view all fees",
    "course fees",
    "program fees",
    "study costs",
    # Application pages that also list tuition (e.g. AIT /apply).
    "how to apply",
    "apply now",
)


def _extract_pdf_fee_links(html: str, base_url: str) -> list[str]:
    """Find PDF links on a fee page that may contain fee data not inline in HTML.

    Unlike :func:`_extract_fee_link_candidates` this function allows cross-domain
    URLs (e.g. CDN-hosted PDFs on ``cdn.prod.website-files.com``) because
    institutions routinely publish fee schedules as third-party-hosted PDFs.

    Selection criteria:
    - href ends with ``.pdf`` (case-insensitive)
    - anchor text OR URL path contains a fee-related keyword

    Returns absolute URLs, deduplicated.
    """
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse
    except ImportError:
        return []

    _PDF_FEE_ANCHOR_TOKENS = (
        "fee",
        "tuition",
        "cost",
        "schedule",
        "international student",
        "rate",
        "price list",
        "pricing",
    )

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    seen: set[str] = set()
    results: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        path_lower = parsed.path.lower()
        if not path_lower.endswith(".pdf"):
            continue
        anchor_text = a.get_text(" ", strip=True).lower()
        # Match on anchor text or URL path tokens.
        if not any(tok in anchor_text or tok in path_lower for tok in _PDF_FEE_ANCHOR_TOKENS):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean not in seen:
            seen.add(clean)
            results.append(clean)

    return results


def _extract_fee_link_candidates(html: str, base_domain: str) -> list[str]:
    """Extract anchor hrefs from *html* that look like centralized fee pages.

    Only returns same-domain URLs (prevents picking up CRICOS or government
    fee-comparison sites).  Normalises relative hrefs to absolute URLs using
    *base_domain* (e.g. ``"https://www.kbs.edu.au"``).
    """
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse
    except ImportError:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    parsed_base = urlparse(base_domain)
    base_netloc = parsed_base.netloc.lower()

    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href: str = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue

        # Resolve relative URLs.
        abs_url = urljoin(base_domain, href)
        parsed = urlparse(abs_url)

        # Same-domain check.
        if parsed.netloc.lower() != base_netloc:
            continue

        path = parsed.path.lower()
        anchor_text = a.get_text(" ", strip=True).lower()

        # Path-based signal.
        path_match = any(path.endswith(fragment) or fragment in path for fragment in _FEE_URL_PATHS)
        # Anchor-text signal.
        text_match = any(token in anchor_text for token in _FEE_ANCHOR_TEXT)

        if path_match or text_match:
            # Normalise: drop query strings and fragments, keep path only.
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            candidates.append(clean)

    return candidates


def _extract_fee_hub_subpage_links(
    html: str, hub_url: str, base_domain: str
) -> list[str]:
    """Extract sub-page links from a hub-style fee page that yielded 0 records.

    When a central fee page is a navigational hub (e.g. KBS ``/admissions/fees``
    links to ``/admissions/fees/international-fees``), the top-level HTML table
    parser finds no parseable fee data.  This function finds child links under
    the hub URL and ranks ones mentioning "international" first so the caller
    can try fetching the richer sub-page instead.

    Only returns same-domain URLs whose path starts with the hub URL's path
    followed by ``/`` (i.e. genuine child pages, not siblings or parents).
    Links that are identical to the hub URL are excluded.

    Args:
        html: HTML content of the hub page already fetched.
        hub_url: The hub URL that returned 0 fee records.
        base_domain: Scheme + netloc (e.g. ``"https://www.kbs.edu.au"``).

    Returns:
        Deduplicated list, "international" child pages first, at most 4 items.
    """
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin, urlparse
    except ImportError:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    hub_parsed = urlparse(hub_url)
    hub_path = hub_parsed.path.rstrip("/").lower()
    parsed_base = urlparse(base_domain)
    base_netloc = parsed_base.netloc.lower()

    _INTL_TOKENS = ("international", "intl", "overseas", "offshore")
    _FEE_TOKENS = ("fee", "tuition", "cost", "schedule", "rate")

    seen: set[str] = set()
    intl: list[str] = []
    other: list[str] = []

    for a in soup.find_all("a", href=True):
        href: str = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        abs_url = urljoin(hub_url, href)
        parsed = urlparse(abs_url)

        if parsed.netloc.lower() != base_netloc:
            continue

        child_path = parsed.path.rstrip("/").lower()

        # Must be a genuine child page of the hub (path starts with hub_path/)
        if not child_path.startswith(hub_path + "/"):
            continue

        # Skip exact-same URL as hub
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean.rstrip("/") == hub_url.rstrip("/"):
            continue
        if clean in seen:
            continue
        seen.add(clean)

        anchor_text = a.get_text(" ", strip=True).lower()
        is_intl = any(t in child_path or t in anchor_text for t in _INTL_TOKENS)
        has_fee = any(t in child_path or t in anchor_text for t in _FEE_TOKENS)

        if is_intl and has_fee:
            intl.append(clean)
        elif is_intl or has_fee:
            other.append(clean)

    return (intl + other)[:4]


def _fee_url_specificity(url: str) -> int:
    """Tiebreaker score: higher = more likely to be the international fee page.

    Used when multiple candidate URLs tie on vote count so that
    ``/admissions/fees/international-fees`` wins over ``/admissions/fees``.

    ACU regression note: both the fees page and the scholarships page sit
    under ``/fees-and-scholarships/`` so both contain "fee" and "international"
    in their paths and previously tied on score.  The path-length tiebreaker
    then selected the (longer) scholarships URL over the (shorter) fees URL.
    A heavy penalty for "scholarship" in the path ensures the real fees page
    always outscores the scholarships page.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    score = 0
    if "international" in path:
        score += 10   # highest signal — explicitly for international students
    if "fee" in path:
        score += 5
    score += len(path) // 10  # longer, more-specific paths preferred
    # Strong penalty: scholarship pages are never the fee schedule source.
    # Without this, /fees-and-scholarships/international-student-scholarships
    # outscores /fees-and-scholarships/international-student-fees because it
    # contains both "fee" (from "fees-and-scholarships") and is longer.
    if "scholarship" in path:
        score -= 50
    return score


async def discover_fee_url_from_course_pages(
    course_urls: list[str],
    base_domain: str,
    *,
    max_pages: int = 5,
) -> str | None:
    """Sample up to *max_pages* course pages and vote for the most-cited fee URL.

    Uses plain HTTP only (fast, no browser spin-up). Returns the URL that
    appears most frequently across the sampled pages, or ``None`` when no
    fee-link candidates are found.

    Each page casts at most one vote per unique candidate URL (deduplication
    within the page) so a single nav bar can't inflate counts.  When multiple
    URLs tie on vote count, ``_fee_url_specificity`` breaks the tie in favour
    of paths containing "international".

    Called by the orchestrator BEFORE ``prefetch_central_pages`` when
    ``scrape_config.uniPages.feePage`` is not manually configured.
    """
    from collections import Counter

    votes: Counter[str] = Counter()
    sample = course_urls[:max_pages]

    for url in sample:
        try:
            html = await fetch_html(url)
            if html:
                found = _extract_fee_link_candidates(html, base_domain)
                # Deduplicate within a single page so nav bars don't inflate counts.
                votes.update(set(found))
        except Exception as exc:
            log.debug("discover_fee_url: fetch failed for %s: %s", url, exc)

    if not votes:
        return None

    # Primary sort: vote count descending; secondary: specificity descending.
    winner = max(votes.keys(), key=lambda u: (votes[u], _fee_url_specificity(u)))
    log.info(
        "central_pages: auto-discovered fee URL '%s' (votes=%d from %d pages, specificity=%d)",
        winner, votes[winner], len(sample), _fee_url_specificity(winner),
    )
    return winner


# ---------------------------------------------------------------------------
# Matching helper (used by single_course.py)
# ---------------------------------------------------------------------------

def match_central_fee(
    course_name: str,
    central_fees: list[CentralFeeRecord],
    degree_level: str | None = None,
    *,
    threshold: float = 80.0,
) -> tuple[CentralFeeRecord | None, str]:
    """Find the best-matching fee record for a course name.

    Returns ``(record, confidence)`` where *confidence* is one of:
      - ``"exact"``       — normalised names match exactly (score=100).
      - ``"high"``        — WRatio score ≥ 85 (very likely the same program).
      - ``"medium"``      — WRatio score ≥ 80 (plausible; used with a warning).
      - ``"bucket"``      — degree-level bucket fallback only (low precision).
      - ``"none"``        — no match found.

    **Algorithm change (was token_set_ratio):**
    ``token_set_ratio`` rewards any subset relationship with score=100
    (e.g. "Bachelor of Business" → 100 against every "Bachelor of Business
    Specialisation in …" record).  The first record encountered then wins
    every course, giving every course the same fee.

    We now use ``token_sort_ratio`` (rapidfuzz) which sorts tokens
    alphabetically before comparing — giving ~35 for unrelated programs
    ("Diploma of Business" vs "Master of IT") and ~88 for genuinely similar
    names ("Bachelor of Business" vs "Bachelor of Business (BBus)").
    WRatio and token_set_ratio are avoided — both include
    partial_token_set_ratio which inflates unrelated matches via common
    stop-words like "of" and causes the "same fee for all courses" bug.
    Threshold raised from 65 → 80 so only genuine name matches are applied.

    Bucket fallback is returned with confidence="bucket" so callers can
    attach a scrape warning instead of silently using a bad fee.
    """
    if not central_fees or not course_name:
        return None, "none"

    try:
        from rapidfuzz import fuzz as _rfuzz
        use_rapidfuzz = True
    except ImportError:
        use_rapidfuzz = False

    _norm_course = re.sub(r"\s+", " ", course_name).strip().lower()

    def _score(pattern: str, name: str) -> float:
        p = re.sub(r"\s+", " ", pattern).strip().lower()
        n = re.sub(r"\s+", " ", name).strip().lower()
        if use_rapidfuzz:
            # token_sort_ratio sorts tokens alphabetically before comparing.
            # This correctly rejects unrelated programs ("Diploma of Business"
            # vs "Master of Information Technology" → ~35) while accepting
            # near-identical names ("Bachelor of Business" vs "Bachelor of
            # Business (BBus)" → ~88).
            #
            # WRatio and token_set_ratio are intentionally avoided: both
            # include partial_token_set_ratio which rewards the common word
            # "of" being in both strings and inflates unrelated matches to
            # score ≥ 85, causing the same "all courses get one fee" bug.
            return float(_rfuzz.token_sort_ratio(p, n))
        return 100.0 if p == n else (60.0 if p in n or n in p else 0.0)

    best_record: CentralFeeRecord | None = None
    best_score: float = 0.0

    # ── Pass 1: exact normalised name match (fast path) ─────────────────────
    for rec in central_fees:
        pattern = rec.get("program_pattern") or ""
        if not pattern:
            continue
        if re.sub(r"\s+", " ", pattern).strip().lower() == _norm_course:
            log.info(
                "[FEE match] course=%r matched_row=%r fee=%s confidence=exact",
                course_name,
                pattern,
                rec.get("international_fee"),
            )
            return rec, "exact"

    # ── Pass 2: WRatio fuzzy match ───────────────────────────────────────────
    for rec in central_fees:
        pattern = rec.get("program_pattern") or ""
        if not pattern:
            continue
        score = _score(pattern, course_name)
        if score > best_score:
            best_score = score
            best_record = rec

    if best_score >= threshold and best_record is not None:
        confidence = "high" if best_score >= 85 else "medium"
        log.info(
            "[FEE match] course=%r matched_row=%r fee=%s confidence=%s score=%.0f",
            course_name,
            best_record.get("program_pattern"),
            best_record.get("international_fee"),
            confidence,
            best_score,
        )
        return best_record, confidence

    if best_record is not None:
        log.info(
            "[FEE match] course=%r best_row=%r score=%.0f — below threshold (%.0f), fee skipped",
            course_name,
            best_record.get("program_pattern"),
            best_score,
            threshold,
        )

    # ── Bucket fallback ────────────────────────────────────────────────────
    # Last-resort path: fee page only lists level buckets ("Postgraduate:
    # A$X") with no per-program name.  Returned with confidence="bucket"
    # so callers can attach a scrape warning.
    if degree_level:
        course_bucket = _programme_bucket(degree_level)
        for rec in central_fees:
            if rec.get("bucket") == course_bucket:
                log.info(
                    "[FEE match] course=%r bucket=%s fee=%s confidence=bucket (name match failed, score=%.0f)",
                    course_name,
                    course_bucket,
                    rec.get("international_fee"),
                    best_score,
                )
                return rec, "bucket"

    return None, "none"

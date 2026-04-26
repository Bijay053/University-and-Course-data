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

    async def _browser_fetch() -> str | None:
        from app.services.scraper.browser_pool import pool as browser_pool
        async with browser_pool.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(3_000)
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
# Public entry-point
# ---------------------------------------------------------------------------

async def prefetch_central_pages(
    scrape_config: dict[str, Any] | None,
    *,
    emit=None,
) -> CentralData:
    """Pre-fetch and parse central fee + English-requirements pages.

    Reads URLs from ``scrape_config['uniPages']``:
    - ``feePage``          → fee schedule (list of per-program fee records)
    - ``entryPage``        → English requirements (IELTS/PTE/TOEFL values)
    - ``requirementsPage`` → fallback English source if entryPage absent

    Returns a ``CentralData`` dict:
    ``{"fees": [...], "english": {...}, "fee_page_url": str|None, "english_page_url": str|None}``

    Always returns a valid dict (empty fields, never raises).
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

    if not fee_url and not english_url:
        # Preserve the pg_skip flag even when there are no central pages to
        # fetch — single_course.py reads it AFTER all extractors have run to
        # perform the PG clear-out pass.  Returning `empty` verbatim would
        # silently drop the flag, causing vision-OCR scores to survive for PG
        # courses on universities where the flag should suppress them.
        return {
            **empty,
            "central_english_pg_skip": bool(
                scrape_config.get("central_english_pg_skip", False)
            ),
        }

    result: CentralData = {
        "fees": [],
        "english": {},
        "fee_page_url": fee_url,
        "english_page_url": english_url,
        # Per-university opt-in: when True, the central English page values are
        # NOT applied to postgraduate courses (Master's, Graduate Certificate,
        # Graduate Diploma, Doctorate).  Use when a university's central English
        # page is fetched via plain HTTP and the PG row is JS-rendered (e.g. ASA).
        # Default False — apply central English values to all degree levels.
        "central_english_pg_skip": bool(scrape_config.get("central_english_pg_skip", False)),
    }

    # ── Fetch fee page ──────────────────────────────────────────────────────
    if fee_url and not fee_url.endswith(".pdf"):
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
    _use_browser_for_english = bool(scrape_config.get("central_english_pg_skip", False))

    if english_url and not english_url.endswith(".pdf"):
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

                # Level-aware parse — only meaningful when the browser
                # rendered a page with separate UG / PG sections.
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
)


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


def _fee_url_specificity(url: str) -> int:
    """Tiebreaker score: higher = more likely to be the international fee page.

    Used when multiple candidate URLs tie on vote count so that
    ``/admissions/fees/international-fees`` wins over ``/admissions/fees``.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    score = 0
    if "international" in path:
        score += 10   # highest signal — explicitly for international students
    if "fee" in path:
        score += 5
    score += len(path) // 10  # longer, more-specific paths preferred
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
    threshold: float = 65.0,
) -> CentralFeeRecord | None:
    """Find the best-matching fee record for a course name.

    Uses rapidfuzz ``token_set_ratio`` for fuzzy program-name matching.
    Falls back to degree-level bucket matching when no name score meets the
    threshold.  Returns ``None`` when nothing plausible is found.

    ``threshold`` (0-100) controls how lenient the name match is.  65 is
    deliberately permissive because program names on fee pages are often
    abbreviated ("MBA" vs "Master of Business Administration") while course
    names in the catalogue are long-form.
    """
    if not central_fees or not course_name:
        return None

    try:
        from rapidfuzz import fuzz
        use_rapidfuzz = True
    except ImportError:
        use_rapidfuzz = False

    def _score(pattern: str, name: str) -> float:
        if use_rapidfuzz:
            # token_set_ratio handles "MBA" vs "Master of Business Administration"
            # better than simple ratio because it ignores token ordering and
            # subset relationships.
            return float(fuzz.token_set_ratio(pattern.lower(), name.lower()))
        # stdlib fallback: basic substring check
        return 100.0 if pattern.lower() in name.lower() else 0.0

    best_record: CentralFeeRecord | None = None
    best_score: float = 0.0

    for rec in central_fees:
        pattern = rec.get("program_pattern") or ""
        if not pattern:
            continue
        sc = _score(pattern, course_name)
        # Also try the reverse (course_name as query, pattern as corpus) because
        # "Bachelor of Business" scores higher against "Bachelor of Business
        # (Marketing)" in reverse.
        sc_rev = _score(course_name, pattern)
        score = max(sc, sc_rev)
        if score > best_score:
            best_score = score
            best_record = rec

    if best_score >= threshold:
        log.debug(
            "central_pages: fee match '%s' → '%s' (score=%.0f)",
            course_name,
            best_record.get("program_pattern"),
            best_score,
        )
        return best_record

    # ── Bucket fallback ────────────────────────────────────────────────────
    # If no name matched but the caller supplies a degree_level, return the
    # first record whose bucket matches.  This is a last-resort path used
    # when course names and program names share no common tokens (e.g. a
    # fee page that just says "Postgraduate" without a program name).
    if degree_level:
        course_bucket = _programme_bucket(degree_level)
        for rec in central_fees:
            if rec.get("bucket") == course_bucket:
                log.debug(
                    "central_pages: bucket fallback '%s' (bucket=%s)",
                    course_name,
                    course_bucket,
                )
                return rec

    return None

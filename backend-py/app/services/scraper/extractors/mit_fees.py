"""MIT (Melbourne Institute of Technology) per-course fee table extractor.

MIT publishes its complete international fee schedule as a single
HTML table at::

    https://www.mit.edu.au/study-with-us/tuition-fees

The page has Domestic / International accordion sections.  The
international section contains a real ``<table class="table-mit-new">``
with one row per course::

    <tr>
      <td><a href="/node/21">Bachelor of Business, major in Accounting</a></td>
      <td>A$9,600.00 per trimester</td>   <!-- 2026 fee -->
      <td>A$9,900.00 per trimester</td>   <!-- 2027 fee -->
    </tr>

The static per-course page (e.g.
``/study-with-us/programs/bachelor-business/accounting``) does NOT
expose the international fee anywhere — only Gemini sees the
"DomesticInternational" toggle text and returns ``international_fee:
null`` because the actual A$ value is rendered client-side.  Without
this override, the central-page parser falls back to a single broadcast
fee (A$13,320 = 2027 Master of ICT Research per-trimester) and stamps
it onto every course as ``Full Course``, producing 22 wrong rows.

This module fetches the central tuition-fees page once per scrape
(cached on the per-uni context), parses the international table, and
provides a per-course lookup keyed on a normalised course title.
Hostname-gated → no-op for every other uni.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from html import unescape
from typing import Any
from urllib.parse import urlparse

from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger("uniportal.scraper.mit_fees")


CENTRAL_FEES_URL = "https://www.mit.edu.au/study-with-us/tuition-fees"

# In-process cache: parse the central fees table once per worker.
# Key = central URL, value = mapping of normalised course title → fee dict.
# Successful parses are cached for the worker lifetime; transient
# failures are cached only briefly (FAILURE_TTL_SECONDS) so a single
# blip early in the worker's life doesn't permanently disable MIT
# overrides for every subsequent course in the worker.
_FEE_TABLE_CACHE: dict[str, dict[str, Any] | None] = {}
_FAILURE_TIMESTAMP: dict[str, float] = {}
_FAILURE_TTL_SECONDS = 60.0
# An asyncio.Lock per central URL to coalesce concurrent fetches: when
# 27 MIT course pages all hit apply_overrides() in parallel, only one
# of them issues the HTTP request and the rest await the result.
# Lazy-initialised because we can't construct the lock at import time
# (no running event loop yet).
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(url: str) -> asyncio.Lock:
    lock = _LOCKS.get(url)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[url] = lock
    return lock


# ── Host gate ────────────────────────────────────────────────────────────
def is_mit_host(url: str | None) -> bool:
    """Strict netloc check — only ``mit.edu.au`` and its subdomains."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "mit.edu.au" or host.endswith(".mit.edu.au")


# ── Title normalisation ──────────────────────────────────────────────────
# Matching needs to be tolerant of:
#   - case differences ("Bachelor of Business" vs "bachelor of business")
#   - punctuation ("," vs no comma)
#   - the brand-tail suffix the staged course_name sometimes carries
#     ("| Melbourne Institute of Technology")
#   - "major in" vs "majoring in" vs ":" separators
#   - en-dash / hyphen / em-dash variants
_BRAND_TAIL_RE = re.compile(
    r"\s*[|–-]\s*melbourne institute of technology\s*$", re.I
)
_PUNCT_RE = re.compile(r"[,.:;|()\[\]\-–—]+")
_WS_RE = re.compile(r"\s+")
_MAJOR_VARIANTS_RE = re.compile(r"\b(majoring in|major in|major)\b", re.I)


def _norm_title(title: str) -> str:
    """Normalise a course title for fuzzy lookup.  Lower-cases, strips
    the MIT brand suffix, collapses punctuation/spacing, and rewrites
    "majoring in" → "major in" so both phrasings collide on the same
    key."""
    if not title:
        return ""
    t = unescape(title)
    t = _BRAND_TAIL_RE.sub("", t)
    t = _MAJOR_VARIANTS_RE.sub("major in", t)
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip().lower()
    return t


# ── URL slug → title hint ────────────────────────────────────────────────
# Per-course page URLs look like
# ``/study-with-us/programs/bachelor-business/accounting`` — the major
# slug is the last path segment.  Used as a tie-breaker when the
# extracted course_name is the bare program name without the major.
def _slug_hint(url: str | None) -> str:
    if not url:
        return ""
    try:
        path = urlparse(url).path or ""
    except (ValueError, AttributeError):
        return ""
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        return ""
    return segs[-1].replace("-", " ").lower().strip()


# ── HTML table parser ────────────────────────────────────────────────────
# The international section starts with a heading containing
# "Course fees - International" and ends at the next "Course fees -"
# heading (Domestic / Other / etc.).  Inside it sits a single
# ``<table class="table-mit-new">`` with a 3-column thead
# (Course | <year1> Fees | <year2> Fees).
_INTL_SECTION_START_RE = re.compile(
    r"course fees\s*[-–]\s*international", re.I
)
_NEXT_SECTION_RE = re.compile(r"course fees\s*[-–]\s*", re.I)
# Extracts course name + 2026 + 2027 cells from each <tr>.  Optional
# anchor wrapper around the course name handled with a non-capturing
# group.
_ROW_RE = re.compile(
    r"<tr>\s*<td>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*</td>"
    r"\s*<td>\s*([^<]+?)\s*</td>"
    r"\s*<td>\s*([^<]+?)\s*</td>",
    re.I | re.S,
)
# A$ amount + optional cents.  Allow decimal point or comma thousand
# separators.  Captures the integer-part number for staging.
_FEE_AMOUNT_RE = re.compile(
    r"A?\$\s*([0-9][0-9,]*)(?:\.\d+)?",
    re.I,
)
# Term label ("per trimester" / "per semester" / "per year" / "per
# annum").  Falls back to "Full Course" if nothing matches.
_TERM_RE = re.compile(
    r"per\s+(trimester|semester|year|annum|month)", re.I
)


def _parse_fee_cell(cell: str) -> tuple[float | None, str | None]:
    """Return (amount, term) parsed from a fee cell.

    Returns (None, None) for non-numeric cells (e.g. the printing /
    photocopy rows at the bottom of the table where the second
    column is "Per Print" rather than an A$ amount).
    """
    if not cell:
        return (None, None)
    txt = unescape(cell)
    amt_m = _FEE_AMOUNT_RE.search(txt)
    if not amt_m:
        return (None, None)
    try:
        amount = float(amt_m.group(1).replace(",", ""))
    except (ValueError, TypeError):
        return (None, None)
    term_m = _TERM_RE.search(txt)
    if term_m:
        unit = term_m.group(1).lower()
        # Map MIT's vocabulary to the canonical fee_term values used
        # elsewhere in the system.
        term_map = {
            "trimester": "Trimester",
            "semester": "Semester",
            "year": "Annual",
            "annum": "Annual",
            "month": "Month",
        }
        term = term_map.get(unit, "Full Course")
    else:
        term = "Full Course"
    return (amount, term)


def parse_intl_fee_table(html: str) -> dict[str, dict[str, Any]]:
    """Parse the international section of the MIT central fees page.

    Returns a dict keyed on the normalised course title with values:
        {
            "course_name": str,        # original (un-normalised) title
            "fee_2026": float | None,
            "fee_2027": float | None,
            "fee_term": str | None,    # canonical term ("Trimester", …)
        }
    Non-course rows (printing, photocopy, library, etc. — second
    column is "Per Print" rather than "A$X.XX per …") are skipped.
    """
    if not html:
        return {}
    start_m = _INTL_SECTION_START_RE.search(html)
    if not start_m:
        return {}
    section = html[start_m.end():]
    # Cut at the next "Course fees - " heading so we don't leak Domestic
    # rows into the international lookup.
    next_m = _NEXT_SECTION_RE.search(section)
    if next_m:
        section = section[: next_m.start()]
    out: dict[str, dict[str, Any]] = {}
    for row in _ROW_RE.finditer(section):
        raw_name = unescape(row.group(1)).strip()
        cell_2026 = row.group(2)
        cell_2027 = row.group(3)
        fee_2026, term_2026 = _parse_fee_cell(cell_2026)
        fee_2027, term_2027 = _parse_fee_cell(cell_2027)
        # Skip header rows ("Course"/"2026 Fees"/...) and non-course
        # rows that lack both numeric cells.
        if fee_2026 is None and fee_2027 is None:
            continue
        if not raw_name or raw_name.lower() in ("course", "fees"):
            continue
        key = _norm_title(raw_name)
        if not key:
            continue
        # First-row wins per key (the table is currently flat, no dupes,
        # but be defensive).
        out.setdefault(
            key,
            {
                "course_name": raw_name,
                "fee_2026": fee_2026,
                "fee_2027": fee_2027,
                "fee_term": term_2026 or term_2027,
            },
        )
    return out


async def _load_table_cached() -> dict[str, dict[str, Any]] | None:
    """Fetch + parse the MIT central fees page, memoised across the
    worker.  Returns the lookup dict, or ``None`` if fetch/parse failed
    (in which case callers fall back to existing extractors).

    Concurrency-safe: an asyncio lock coalesces parallel callers so the
    27 MIT course pages issue exactly one fetch among them rather than
    27 simultaneous ones.

    Failure-recovery-safe: a transient fetch failure is cached for
    ``_FAILURE_TTL_SECONDS`` only.  After the TTL expires the next
    caller will retry the fetch instead of being stuck with a
    permanently negative result for the worker's lifetime.
    """
    cached = _FEE_TABLE_CACHE.get(CENTRAL_FEES_URL, "missing")
    # Successful parse → return immediately, no lock needed.
    if isinstance(cached, dict):
        return cached
    # Failure cached within TTL → also return without re-fetching.
    if cached is None and CENTRAL_FEES_URL in _FAILURE_TIMESTAMP:
        if (
            time.monotonic() - _FAILURE_TIMESTAMP[CENTRAL_FEES_URL]
            < _FAILURE_TTL_SECONDS
        ):
            return None
    async with _lock_for(CENTRAL_FEES_URL):
        # Re-check inside the lock — another coroutine may have
        # populated the cache while we were waiting.
        cached = _FEE_TABLE_CACHE.get(CENTRAL_FEES_URL, "missing")
        if isinstance(cached, dict):
            return cached
        if cached is None and CENTRAL_FEES_URL in _FAILURE_TIMESTAMP:
            if (
                time.monotonic() - _FAILURE_TIMESTAMP[CENTRAL_FEES_URL]
                < _FAILURE_TTL_SECONDS
            ):
                return None
        try:
            html = await fetch_html(CENTRAL_FEES_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("MIT central fees fetch failed: %s", exc)
            _FEE_TABLE_CACHE[CENTRAL_FEES_URL] = None
            _FAILURE_TIMESTAMP[CENTRAL_FEES_URL] = time.monotonic()
            return None
        if not html:
            _FEE_TABLE_CACHE[CENTRAL_FEES_URL] = None
            _FAILURE_TIMESTAMP[CENTRAL_FEES_URL] = time.monotonic()
            return None
        table = parse_intl_fee_table(html)
        if table:
            _FEE_TABLE_CACHE[CENTRAL_FEES_URL] = table
            _FAILURE_TIMESTAMP.pop(CENTRAL_FEES_URL, None)
            log.info("MIT central fees parsed: %d course rows", len(table))
            return table
        # 0-row parse → treat as a transient failure (HTML structure
        # may have changed; let the next worker retry rather than
        # poison-cache permanently).
        _FEE_TABLE_CACHE[CENTRAL_FEES_URL] = None
        _FAILURE_TIMESTAMP[CENTRAL_FEES_URL] = time.monotonic()
        log.warning("MIT central fees parse returned 0 rows (HTML changed?)")
        return None


def _lookup(
    table: dict[str, dict[str, Any]],
    course_name: str | None,
    url: str | None,
) -> dict[str, Any] | None:
    """Match a course against the parsed table.  Tries (in order):

    1. exact normalised match on ``course_name``
    2. normalised ``course_name`` is a prefix of a table key, and the
       URL slug appears in that key (disambiguates "Bachelor of
       Business" → ".../bachelor-business/accounting" → "Bachelor of
       Business, major in Accounting")
    3. URL slug appears in a table key

    Returns ``None`` when no unambiguous match is found.
    """
    if not table:
        return None
    name_key = _norm_title(course_name or "")
    slug = _slug_hint(url)
    # Pass 1: exact match on the staged course_name.
    if name_key and name_key in table:
        return table[name_key]
    # Pass 2: course_name is a program-level prefix; URL slug picks the major.
    if name_key and slug:
        slug_norm = _norm_title(slug)
        candidates = [
            v for k, v in table.items()
            if k.startswith(name_key) and slug_norm and slug_norm in k
        ]
        if len(candidates) == 1:
            return candidates[0]
    # Pass 3: URL-slug-only match (last-resort, must be unambiguous).
    if slug:
        slug_norm = _norm_title(slug)
        if slug_norm:
            candidates = [v for k, v in table.items() if slug_norm in k]
            if len(candidates) == 1:
                return candidates[0]
    return None


async def apply_overrides(
    payload: dict[str, Any],
    *,
    url: str,
    evidence: list[dict[str, Any]],
    current_year: int = 2026,
) -> bool:
    """If the URL is an MIT course page and the central fee table has a
    matching row, override ``international_fee``, ``fee_term``, and
    ``currency`` with the per-course value.  Returns True when an
    override was applied.

    The override is REPLACE rather than fill-only because the existing
    pipeline currently broadcasts a single wrong fee to every MIT
    course (the central-page generic parser stamps A$13,320 = 2027
    Master of ICT Research per-trimester onto every row).  REPLACE is
    safe here because the per-course HTML page genuinely contains no
    international fee — there is no in-page value worth preserving.
    """
    if not is_mit_host(url):
        return False
    table = await _load_table_cached()
    if not table:
        return False
    row = _lookup(table, payload.get("course_name"), url)
    if not row:
        return False
    # Pick the current-year fee, fall back to the next-year value if the
    # current year is missing (table only has 2026 + 2027 today; this
    # makes the extractor forward-compatible when MIT rolls the table
    # over to 2027 + 2028).
    fee = row.get(f"fee_{current_year}")
    if fee is None:
        # Scan every fee_<year> key in the row, prefer the closest
        # year ≥ current_year (the most-future-proof choice) and fall
        # back to the next-most-recent past year otherwise.
        years_in_row = sorted(
            int(k.split("_", 1)[1])
            for k in row
            if k.startswith("fee_") and row[k] is not None
            and k.split("_", 1)[1].isdigit()
        )
        future = [y for y in years_in_row if y >= current_year]
        past = [y for y in years_in_row if y < current_year]
        if future:
            fee = row[f"fee_{future[0]}"]
        elif past:
            fee = row[f"fee_{past[-1]}"]
    if fee is None:
        return False
    term = row.get("fee_term") or "Trimester"
    payload["international_fee"] = fee
    payload["fee_term"] = term
    payload["currency"] = "AUD"
    evidence.append(
        {
            "field_key": "international_fee",
            "value": fee,
            "confidence": 0.95,
            "method": "mit_fees_table",
            "source_url": CENTRAL_FEES_URL,
            "snippet": (
                f"MIT central fee table: {row['course_name']} → "
                f"A${fee:,.0f} per {term}"
            ),
        }
    )
    return True


def _reset_cache_for_tests() -> None:
    """Test-only helper to clear the in-process cache between test cases."""
    _FEE_TABLE_CACHE.clear()
    _FAILURE_TIMESTAMP.clear()
    _LOCKS.clear()

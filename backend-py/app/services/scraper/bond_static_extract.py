"""Bond University program-page extractor.

Bond University (bond.edu.au) publishes all real courses under the path
``/program/<slug>``.  Dynamic fields (fees, English scores, intake calendar)
are rendered client-side, but Bond exposes undocumented JSON APIs that can be
discovered from ``data-*`` attributes embedded in the static HTML.

Discovered API endpoints
------------------------
All endpoints are publicly accessible (no auth/cookies required):

* **Program details** — duration, offerings (intakes), study areas, degree type::

      GET /api/program-details/{numeric_id}
      → { "programs": [{ "id", "duration", "type", "studyAreas", "offerings" }] }

* **Fees** — per-semester and total fee for domestic and international students::

      GET /api/program-fees/{numeric_id}/{program_code}
      → { "fees": [{ "year", "international": { "semester", "total" } }] }

* **International requirements** — per-country academic entry requirements
  (per-entry IELTS data not available; parse static HTML of /entry_requirements
  subpage instead).

Both numeric_id and program_code are embedded in the static HTML of every
``/program/`` page as ``data-program-detail-url`` and ``data-program-code``
data attributes on the main program container element.

This module provides:

``is_bond_program_url(url)``
    Returns True for bond.edu.au/program/* URLs.

``apply_bond_extraction(url, html)``
    Pre-seeds the per-course payload with Bond-authoritative values by:

    1. Parsing ``data-program-detail-url`` and ``data-program-code`` from the
       static HTML.
    2. Calling ``/api/program-details/{id}`` for duration, intake months (from
       offerings), study area (category), and degree type.
    3. Calling ``/api/program-fees/{id}/{code}`` for the international semester
       fee.  Annual fee = semester × 3 (Bond operates three semesters per year:
       January, May, September).
    4. Fetching ``/program/{slug}/entry_requirements`` and parsing the IELTS
       overall band score from the plain-text content.

    Falls back gracefully when any API call fails — the existing
    ``has_central_fee_page = True`` strategy keeps the course staged for human
    review whenever fee extraction fails.

Design note
-----------
Called as a *pre-seed* inside ``single_course.extract_course`` before the
``_EXTRACTORS`` loop.  Uses direct assignment for location, study_mode, and
has_central_fee_page.  All other keys use ``setdefault`` so the generic
extractors can still win when they find real values first.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host / path detection
# ---------------------------------------------------------------------------

_BOND_HOSTS = frozenset({"bond.edu.au", "www.bond.edu.au"})


def is_bond_program_url(url: str) -> bool:
    """Return True when *url* is a Bond University ``/program/<slug>`` page."""
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return host in _BOND_HOSTS and p.path.lower().startswith("/program/")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}
_API_TIMEOUT = 10  # seconds per request


def _get_json(url: str) -> dict | list | None:
    """GET *url* and return parsed JSON, or None on any error."""
    try:
        import requests  # lazy import — keeps module usable in test contexts
        r = requests.get(url, headers=_HEADERS, timeout=_API_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug("[BOND] JSON fetch failed for %s: %s", url, exc)
        return None


def _get_html(url: str) -> str | None:
    """GET *url* and return response text, or None on any error."""
    try:
        import requests
        r = requests.get(url, headers={**_HEADERS, "Accept": "text/html,*/*"},
                         timeout=_API_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as exc:
        log.debug("[BOND] HTML fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Static HTML: fee extraction (fallback when API ids are absent)
# ---------------------------------------------------------------------------

_STATIC_FEE_PATTERNS: list[re.Pattern[str]] = [
    # "International students: A$28,320" / "International student A$28,320"
    re.compile(
        r"(?:international|overseas)\s+student[s]?[^$\n]{0,40}(?:A\$|\$)\s*([\d,]+)",
        re.IGNORECASE,
    ),
    # "Annual tuition fee: $32,600 AUD" / "Annual tuition fee $32,600"
    re.compile(
        r"(?:annual|yearly|total)\s+(?:tuition\s+)?fee[s]?[:\s]*(?:A\$|\$)\s*([\d,]+)",
        re.IGNORECASE,
    ),
    # Generic "tuition fee: A$XX,XXX"
    re.compile(
        r"tuition\s+fee[s]?[:\s]*(?:A\$|\$)\s*([\d,]+)",
        re.IGNORECASE,
    ),
]

_FEE_TERM_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bper\s+semester\b", re.IGNORECASE), "semester"),
    (re.compile(r"\bper\s+trimester\b", re.IGNORECASE), "trimester"),
    (re.compile(r"\bper\s+(?:year|annum)\b|annually\b|annual\b", re.IGNORECASE), "year"),
]

_FEE_MIN = 1_000
_FEE_MAX = 200_000

# ---------------------------------------------------------------------------
# Static HTML: intake month extraction (fallback when API ids are absent)
# ---------------------------------------------------------------------------

_MONTH_NAMES: list[str] = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NAME_RE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES) + r")\b",
    re.IGNORECASE,
)
_INTAKE_CONTEXT_RE = re.compile(
    r"(?:intake[s]?|start[s]?|commenc|semester\s+start|enrollment|enrolment)"
    r".{0,120}",
    re.IGNORECASE | re.DOTALL,
)


def _extract_intake_from_static_html(plain_text: str) -> list[str]:
    """Extract unique intake month names from plain text near intake keywords.

    Returns an ordered, deduplicated list of month names (title-case) when
    an intake-context phrase is found, otherwise ``[]``.
    """
    context_match = _INTAKE_CONTEXT_RE.search(plain_text)
    if not context_match:
        return []
    context = context_match.group(0)
    seen: set[str] = set()
    months: list[str] = []
    for m in _MONTH_NAME_RE.finditer(context):
        name = m.group(1).title()
        if name not in seen:
            seen.add(name)
            months.append(name)
    return months


def _extract_fee_from_static_html(plain_text: str) -> dict[str, Any]:
    """Try to extract international fee from stripped plain text.

    Returns a dict with ``international_fee`` and ``fee_term`` when a
    plausible amount (1,000–200,000 AUD) is found, otherwise ``{}``.
    """
    for pattern in _STATIC_FEE_PATTERNS:
        m = pattern.search(plain_text)
        if not m:
            continue
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if not (_FEE_MIN <= amount <= _FEE_MAX):
            continue
        result: dict[str, Any] = {"international_fee": amount}
        # Determine fee term from surrounding context (±200 chars)
        start = max(0, m.start() - 50)
        end = min(len(plain_text), m.end() + 150)
        context = plain_text[start:end]
        fee_term = "year"  # default
        for term_re, term_name in _FEE_TERM_MAP:
            if term_re.search(context):
                fee_term = term_name
                break
        result["fee_term"] = fee_term
        return result
    return {}


# ---------------------------------------------------------------------------
# Static HTML: data-* attribute parsing
# ---------------------------------------------------------------------------

_DETAIL_URL_RE = re.compile(
    r'data-program-detail-url=["\']\/api\/program-details\/(\d+)["\']'
)
_PROG_CODE_RE = re.compile(r'data-program-code=["\']([A-Z0-9\-]+)["\']')


def _extract_program_ids(html: str) -> tuple[str | None, str | None]:
    """Return (numeric_id, program_code) parsed from Bond course-page HTML.

    Both are embedded as data-* attributes on the main program container::

        data-program-detail-url="/api/program-details/432"
        data-program-code="HS-20003"
    """
    m_id = _DETAIL_URL_RE.search(html)
    m_code = _PROG_CODE_RE.search(html)
    numeric_id = m_id.group(1) if m_id else None
    prog_code = m_code.group(1) if m_code else None
    return numeric_id, prog_code


# ---------------------------------------------------------------------------
# Location / mode
# ---------------------------------------------------------------------------

_BOND_LOCATION: str = "Gold Coast, Queensland"

_ONLINE_MODE_RE = re.compile(
    r"\b(online(?:\s+delivery)?|fully\s+online|distance\s+learning|"
    r"external\s+study|study\s+online)\b",
    re.IGNORECASE,
)
_CAMPUS_MENTION_RE = re.compile(
    r"\b(gold\s+coast|robina|on.campus|on\s+campus|residential)\b",
    re.IGNORECASE,
)


def _derive_study_mode(plain_text: str) -> str | None:
    has_online = bool(_ONLINE_MODE_RE.search(plain_text))
    has_campus = bool(_CAMPUS_MENTION_RE.search(plain_text))
    if has_online and has_campus:
        return "Blended"
    if has_online:
        return "Online"
    return None


# ---------------------------------------------------------------------------
# /api/program-details/{id}  — duration, intakes, category, degree type
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+years?", re.IGNORECASE)
_MONTH_RE = re.compile(r"(\d+)\s+months?", re.IGNORECASE)

_OFFERING_MONTH_MAP: dict[str, str] = {
    "jan": "January", "feb": "February", "mar": "March",
    "apr": "April",   "may": "May",       "jun": "June",
    "jul": "July",    "aug": "August",    "sep": "September",
    "oct": "October", "nov": "November",  "dec": "December",
}


def _parse_duration_years(duration_str: str) -> float | None:
    """Convert 'N years (M semesters)' or 'N months' to a year float."""
    m = _YEAR_RE.search(duration_str)
    if m:
        return float(m.group(1))
    m = _MONTH_RE.search(duration_str)
    if m:
        return round(int(m.group(1)) / 12, 2)
    return None


def _parse_offerings_intakes(offerings: list[dict]) -> list[str]:
    """Extract unique month names from offering semester strings like 'May 2026'."""
    months: list[str] = []
    seen: set[str] = set()
    for offering in offerings:
        sem = offering.get("semester", "")
        key = sem[:3].lower()
        name = _OFFERING_MONTH_MAP.get(key)
        if name and name not in seen:
            months.append(name)
            seen.add(name)
    return months


def _enrich_from_details_api(numeric_id: str) -> dict[str, Any]:
    """Call /api/program-details/{id} and return extracted fields."""
    data = _get_json(f"https://bond.edu.au/api/program-details/{numeric_id}")
    if not data or not isinstance(data, dict):
        return {}
    programs = data.get("programs", [])
    if not programs:
        return {}
    prog = programs[0]

    result: dict[str, Any] = {}

    # Duration
    dur_str = prog.get("duration", "")
    if dur_str:
        dur_years = _parse_duration_years(dur_str)
        if dur_years is not None:
            result["duration"] = dur_years

    # Intake months from offerings
    offerings = prog.get("offerings", [])
    if offerings:
        months = _parse_offerings_intakes(offerings)
        if months:
            result["intake_months"] = months

    # Category from first study area
    study_areas = prog.get("studyAreas", [])
    if study_areas and study_areas[0].get("label"):
        result["category"] = study_areas[0]["label"]

    log.info(
        "[BOND] program-details API → duration=%s intake_months=%s category=%s",
        result.get("duration"), result.get("intake_months"), result.get("category"),
    )
    return result


# ---------------------------------------------------------------------------
# /api/program-fees/{id}/{code}  — international fee
# ---------------------------------------------------------------------------

# Bond runs 3 semesters per year (January, May, September).
# Annual fee = per-semester fee × 3.
_BOND_SEMESTERS_PER_YEAR = 3
# Prefer 2026 fees; fall back to first available year.
_PREFERRED_FEE_YEAR = "2026"


def _enrich_from_fees_api(numeric_id: str, program_code: str) -> dict[str, Any]:
    """Call /api/program-fees/{id}/{code} and return international_fee (annual)."""
    url = f"https://bond.edu.au/api/program-fees/{numeric_id}/{program_code}"
    data = _get_json(url)
    if not data or not isinstance(data, dict):
        return {}
    fees_list = data.get("fees", [])
    if not fees_list:
        return {}

    # Prefer the target year; fall back to first entry.
    fee_entry = next(
        (f for f in fees_list if str(f.get("year", "")) == _PREFERRED_FEE_YEAR),
        fees_list[0],
    )
    intl = fee_entry.get("international", {})
    semester_fee = intl.get("semester")
    if not semester_fee or not isinstance(semester_fee, (int, float)):
        return {}

    annual_fee = float(semester_fee) * _BOND_SEMESTERS_PER_YEAR
    log.info(
        "[BOND] fees API → semester=%s × %d = annual %.0f (year=%s)",
        semester_fee, _BOND_SEMESTERS_PER_YEAR, annual_fee,
        fee_entry.get("year"),
    )
    return {
        "international_fee": annual_fee,
        "fee_term": "year",
    }


# ---------------------------------------------------------------------------
# /program/{slug}/entry_requirements — IELTS from static HTML
# ---------------------------------------------------------------------------

# Matches: "Overall score 6.5" or "overall: 6.5"
_IELTS_OVERALL_RE = re.compile(
    r"[Oo]verall\s+(?:band\s+)?(?:score\s+)?(\d+(?:\.\d+)?)", re.IGNORECASE
)
# Matches: "no sub score less than 6.0" / "minimum 6.0 in each" / "not less than 6.0"
_IELTS_SUB_RE = re.compile(
    r"(?:sub\s*score|band|each\s+(?:sub)?skill|each\s+component|minimum(?:\s+band)?)"
    r"\s+(?:(?:less\s+than\s+|not\s+less\s+than\s+|of\s+)?(?:at\s+least\s+)?)"
    r"(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _enrich_from_entry_requirements(course_url: str) -> dict[str, Any]:
    """Fetch /entry_requirements subpage and parse IELTS bands from plain text."""
    # Construct subpage URL: strip trailing slash, append /entry_requirements
    base = course_url.rstrip("/")
    er_url = f"{base}/entry_requirements"

    html = _get_html(er_url)
    if not html:
        return {}

    # Strip tags and normalise whitespace for regex matching
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    result: dict[str, Any] = {}

    m_overall = _IELTS_OVERALL_RE.search(text)
    if m_overall:
        try:
            result["ielts_overall"] = float(m_overall.group(1))
        except ValueError:
            pass

    m_sub = _IELTS_SUB_RE.search(text)
    if m_sub:
        try:
            sub = float(m_sub.group(1))
            for band in ("ielts_writing", "ielts_reading",
                         "ielts_listening", "ielts_speaking"):
                result[band] = sub
        except ValueError:
            pass

    if result:
        log.info("[BOND] entry_requirements → %s", result)
    return result


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def apply_bond_extraction(url: str, html: str) -> dict[str, Any]:
    """Return a pre-seed dict for Bond University ``/program/`` pages.

    Enrichment order:
    1. Parse ``data-*`` attributes from static HTML to get numeric_id and code.
    2. Call ``/api/program-details`` for duration, intakes, category.
    3. Call ``/api/program-fees`` for annual international fee.
    4. Fetch ``/entry_requirements`` subpage for IELTS bands.

    Falls back gracefully: any failed API call is skipped and the
    ``bond_fee_js_rendered`` warning is added when no fee is found so the
    Review UI surfaces the incomplete row for human follow-up.

    Hard-set keys (direct assignment, cannot be overwritten by generic
    extractors): ``has_central_fee_page``, ``course_location``.

    Soft-set keys (``setdefault`` semantics, extractors may win):
    ``study_mode``, ``duration``, ``intake_months``, ``category``,
    ``international_fee``, ``fee_term``, ``ielts_*``.
    """
    result: dict[str, Any] = {}

    # ── Always-set (hard block on extractor mis-fires) ─────────────────────
    result["has_central_fee_page"] = True
    result["course_location"] = _BOND_LOCATION

    # Study mode from static HTML keywords
    plain_text = re.sub(r"<[^>]+>", " ", html or "")
    plain_text = re.sub(r"\s+", " ", plain_text)
    _mode = _derive_study_mode(plain_text)
    if _mode is not None:
        result["study_mode"] = _mode

    # ── API enrichment ──────────────────────────────────────────────────────
    numeric_id, program_code = _extract_program_ids(html or "")

    if numeric_id:
        details = _enrich_from_details_api(numeric_id)
        # Use setdefault for everything from the API so generic extractors
        # can still win if they found values first.
        for k, v in details.items():
            result.setdefault(k, v)

        if program_code:
            fees = _enrich_from_fees_api(numeric_id, program_code)
            for k, v in fees.items():
                result.setdefault(k, v)
        else:
            log.warning("[BOND] %s — program_code not found in HTML, skipping fees API", url)
    else:
        log.warning("[BOND] %s — numeric_id not found in HTML, API enrichment skipped", url)

    # ── IELTS from entry_requirements subpage ───────────────────────────────
    ielts = _enrich_from_entry_requirements(url)
    for k, v in ielts.items():
        result.setdefault(k, v)

    # ── Static HTML intake fallback (when API ids absent or API returned no months) ─
    if "intake_months" not in result:
        static_months = _extract_intake_from_static_html(plain_text)
        if static_months:
            result["intake_months"] = static_months
            log.info("[BOND] %s — intake_months extracted from static HTML: %s", url, static_months)

    # ── Static HTML fee fallback (when API ids absent or API returned no fee) ─
    if "international_fee" not in result:
        static_fee = _extract_fee_from_static_html(plain_text)
        for k, v in static_fee.items():
            result.setdefault(k, v)
        if static_fee:
            log.info("[BOND] %s — fee extracted from static HTML: %.0f", url, static_fee["international_fee"])

    # ── Fee warning when still missing ─────────────────────────────────────
    if "international_fee" not in result:
        result["scrape_warnings"] = ["bond_fee_js_rendered"]
        log.info(
            "[BOND] %s — fee not resolved from API; "
            "staging with has_central_fee_page=True for human review", url,
        )
    else:
        log.info(
            "[BOND] %s — fully enriched: fee=%.0f ielts=%s",
            url, result["international_fee"], result.get("ielts_overall"),
        )

    return result

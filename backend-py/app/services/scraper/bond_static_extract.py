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
``_EXTRACTORS`` loop.  Only ``has_central_fee_page`` is a direct assignment.
Location and study mode remain owned by the ordinary course-page extractors;
page-wide Bond defaults caused every program to inherit the same values.
"""
from __future__ import annotations

import logging
import re
import time
from html import unescape
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
_API_ATTEMPTS = 3


def _get_json(url: str) -> dict | list | None:
    """GET *url* and return parsed JSON, or None on any error."""
    import requests  # lazy import — keeps module usable in test contexts
    for attempt in range(_API_ATTEMPTS):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=_API_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.debug("[BOND] JSON fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
            if attempt + 1 < _API_ATTEMPTS:
                time.sleep(0.25 * (attempt + 1))
    return None


def _get_html(url: str) -> str | None:
    """GET *url* and return response text, or None on any error."""
    import requests
    for attempt in range(_API_ATTEMPTS):
        try:
            r = requests.get(
                url,
                headers={**_HEADERS, "Accept": "text/html,*/*"},
                timeout=_API_TIMEOUT,
            )
            r.raise_for_status()
            return r.text
        except Exception as exc:
            log.debug("[BOND] HTML fetch attempt %d failed for %s: %s", attempt + 1, url, exc)
            if attempt + 1 < _API_ATTEMPTS:
                time.sleep(0.25 * (attempt + 1))
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
_FEE_SLOT_KEYS = frozenset(
    {"international_fee", "domestic_fee", "currency", "fee_term", "fee_year"}
)


def suppress_authoritative_fee_omission(
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> None:
    """Remove all downstream fee guesses after Bond returns ``fees: []``."""
    for key in _FEE_SLOT_KEYS:
        payload[key] = None
    evidence[:] = [
        ev for ev in evidence
        if not isinstance(ev, dict) or ev.get("field_key") not in _FEE_SLOT_KEYS
    ]
    warnings = list(payload.get("scrape_warnings") or [])
    if "bond_fee_source_empty" not in warnings:
        warnings.append("bond_fee_source_empty")
    payload["scrape_warnings"] = warnings


def suppress_authoritative_delivery_omission(
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> None:
    """Clear page-wide location/mode guesses when Bond has no current offering."""
    for key in ("course_location", "study_mode"):
        payload[key] = None
    evidence[:] = [
        ev for ev in evidence
        if not isinstance(ev, dict)
        or ev.get("field_key") not in {"course_location", "study_mode"}
    ]
    warnings = list(payload.get("scrape_warnings") or [])
    if "bond_no_current_offerings" not in warnings:
        warnings.append("bond_no_current_offerings")
    payload["scrape_warnings"] = warnings

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
    r'data-program-detail-url\s*=\s*["\']'
    r'(?:https?://(?:www\.)?bond\.edu\.au)?/api/program-details/(\d+)["\']',
    re.IGNORECASE,
)
_PROG_CODE_RE = re.compile(
    r'data-program(?:-code)?\s*=\s*["\']([A-Z0-9\-]+)["\']',
    re.IGNORECASE,
)
_SAFE_PROGRAM_CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$", re.IGNORECASE)


def _extract_program_ids(html: str) -> tuple[str | None, str | None]:
    """Return (numeric_id, program_code) parsed from Bond course-page HTML.

    Both are embedded as data-* attributes on the main program container::

        data-program-detail-url="/api/program-details/432"
        data-program-code="HS-20003"
    """
    tags = re.findall(r"<[^>]*data-program-detail-url[^>]*>", html, re.I | re.S)
    candidates: list[tuple[str, str | None, bool]] = []
    for tag in tags:
        m_id = _DETAIL_URL_RE.search(tag)
        if not m_id:
            continue
        m_code = _PROG_CODE_RE.search(tag)
        is_main = bool(re.search(
            r'class\s*=\s*["\'][^"\']*\bprogram-detail\b',
            tag,
            re.IGNORECASE,
        ))
        candidates.append((
            m_id.group(1),
            m_code.group(1) if m_code else None,
            is_main,
        ))

    main = [(numeric_id, code) for numeric_id, code, is_main in candidates if is_main]
    if len(main) == 1:
        return main[0]
    if len(candidates) == 1:
        numeric_id, code, _is_main = candidates[0]
        return numeric_id, code
    # Multiple unscoped components cannot safely prove which identifier pair
    # belongs to the current course. Fail closed instead of borrowing a card.
    return None, None


def _extract_program_keys(html: str) -> tuple[str | None, list[str]]:
    """Return the shared details id and every course-owned program code.

    Packaged Bond programs intentionally render multiple ``program-detail``
    elements with one details id (one element per component).  Treating that
    as ambiguous discarded the package duration and every component fee.
    """
    tags = re.findall(r"<[^>]*data-program-detail-url[^>]*>", html, re.I | re.S)
    pairs: list[tuple[str, str]] = []
    for tag in tags:
        if not re.search(
            r'class\s*=\s*["\'][^"\']*\bprogram-detail\b', tag, re.IGNORECASE
        ):
            continue
        id_match = _DETAIL_URL_RE.search(tag)
        code_match = _PROG_CODE_RE.search(tag)
        if id_match and code_match:
            pair = (id_match.group(1), code_match.group(1))
            if pair not in pairs:
                pairs.append(pair)
    if not pairs:
        numeric_id, code = _extract_program_ids(html)
        return numeric_id, [code] if code else []
    ids = {numeric_id for numeric_id, _code in pairs}
    if len(ids) != 1:
        return None, []
    return pairs[0][0], [code for _numeric_id, code in pairs]


# ---------------------------------------------------------------------------
# /api/program-details/{id}  — duration, intakes, category, degree type
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+years?", re.IGNORECASE)
_MONTH_RE = re.compile(r"(\d+)\s+months?", re.IGNORECASE)
_SEMESTER_RE = re.compile(r"(\d+(?:\.\d+)?)\s+semesters?", re.IGNORECASE)

_OFFERING_MONTH_MAP: dict[str, str] = {
    "jan": "January", "feb": "February", "mar": "March",
    "apr": "April",   "may": "May",       "jun": "June",
    "jul": "July",    "aug": "August",    "sep": "September",
    "oct": "October", "nov": "November",  "dec": "December",
}


def _parse_duration(duration_str: str) -> tuple[float, str] | None:
    """Return Bond's authoritative duration as an atomic value/unit pair."""
    years = _YEAR_RE.search(duration_str)
    months = _MONTH_RE.search(duration_str)
    if years and months:
        return float(years.group(1)) * 12 + float(months.group(1)), "Month"
    if years:
        return float(years.group(1)), "Year"
    if months:
        return float(months.group(1)), "Month"
    semesters = _SEMESTER_RE.search(duration_str)
    if semesters:
        return float(semesters.group(1)), "Semester"
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
    result: dict[str, Any] = {}
    valid_programs = [prog for prog in programs if isinstance(prog, dict)]
    detail_codes = [
        code.strip()
        for prog in valid_programs
        if isinstance((code := prog.get("id")), str)
        and _SAFE_PROGRAM_CODE_RE.fullmatch(code.strip())
    ]
    if detail_codes:
        result["_program_codes"] = detail_codes
        if len(detail_codes) == 1:
            result["_program_code"] = detail_codes[0]

    # A packaged program's standard duration is the sum of its consecutive
    # component programs. Normalise to months only when the units differ.
    durations = [
        parsed
        for prog in valid_programs
        if (parsed := _parse_duration(str(prog.get("duration") or ""))) is not None
    ]
    if len(durations) == 1:
        result["duration"], result["duration_term"] = durations[0]
    elif durations:
        months = sum(
            value * (12 if unit == "Year" else 4 if unit == "Semester" else 1)
            for value, unit in durations
        )
        if months % 12 == 0:
            result["duration"], result["duration_term"] = months / 12, "Year"
        else:
            result["duration"], result["duration_term"] = months, "Month"

    # Intake months from offerings
    offerings = [
        offering
        for prog in valid_programs
        for offering in (prog.get("offerings") or [])
        if isinstance(offering, dict)
    ]
    if offerings:
        months = _parse_offerings_intakes(offerings)
        if months:
            result["intake_months"] = months
        locations = list(dict.fromkeys(
            str(o.get("location")).strip()
            for o in offerings if o.get("location")
        ))
        modes = list(dict.fromkeys(
            str(o.get("deliveryMode")).strip()
            for o in offerings if o.get("deliveryMode")
        ))
        if locations:
            result["course_location"] = ", ".join(locations)
        if modes:
            normalised = {
                "on-campus": "On Campus",
                "online": "Online",
            }
            mapped = list(dict.fromkeys(normalised.get(m.casefold(), m) for m in modes))
            result["study_mode"] = mapped[0] if len(mapped) == 1 else "Blended"
    else:
        result["_authoritative_delivery_omission"] = True

    # Category from first study area
    study_areas = valid_programs[0].get("studyAreas", []) if valid_programs else []
    if study_areas and study_areas[0].get("label"):
        result["category"] = study_areas[0]["label"]

    log.info(
        "[BOND] program-details API → duration=%s %s intake_months=%s category=%s",
        result.get("duration"), result.get("duration_term"),
        result.get("intake_months"), result.get("category"),
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


def _select_fee_entry(fees_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [entry for entry in fees_list if isinstance(entry, dict)]
    if not valid:
        return None
    preferred = next(
        (entry for entry in valid if str(entry.get("year", "")) == _PREFERRED_FEE_YEAR),
        None,
    )
    if preferred is not None:
        return preferred
    numeric = [
        (int(str(entry.get("year"))), entry)
        for entry in valid
        if str(entry.get("year", "")).isdigit()
    ]
    return max(numeric, key=lambda pair: pair[0])[1] if numeric else valid[0]


def _positive_amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _enrich_from_fees_api(numeric_id: str, program_code: str) -> dict[str, Any]:
    """Return Bond's best authoritative international fee and its real period."""
    url = f"https://bond.edu.au/api/program-fees/{numeric_id}/{program_code}"
    data = _get_json(url)
    if not data or not isinstance(data, dict):
        return {}
    fees_list = data.get("fees", [])
    if not fees_list:
        # This is an authoritative 200 response saying Bond publishes no fee
        # for this program. Keep it distinct from transport/identifier failure
        # so downstream HTML/AI fallbacks cannot invent a tuition amount.
        return {"_authoritative_fee_omission": True}

    fee_entry = _select_fee_entry(fees_list)
    if fee_entry is None:
        return {}
    intl = fee_entry.get("international")
    if not isinstance(intl, dict):
        return {}
    annual_fee = _positive_amount(intl.get("annual"))
    semester_fee = _positive_amount(intl.get("semester"))
    total_fee = _positive_amount(intl.get("total"))
    fee_year = int(fee_entry["year"]) if str(fee_entry.get("year", "")).isdigit() else None
    if annual_fee is not None:
        amount, term, origin = annual_fee, "Annual", "annual"
    elif semester_fee is not None:
        amount, term, origin = (
            semester_fee * _BOND_SEMESTERS_PER_YEAR,
            "Annual",
            "semester_x_3",
        )
    elif total_fee is not None:
        amount, term, origin = total_fee, "Full Course", "total"
    else:
        return {}
    log.info(
        "[BOND] fees API → %s %.0f term=%s (year=%s)",
        origin, amount, term, fee_entry.get("year"),
    )
    result: dict[str, Any] = {
        "international_fee": amount,
        "currency": "AUD",
        "fee_term": term,
    }
    if fee_year is not None:
        result["fee_year"] = fee_year
    return result


def _enrich_from_packaged_fees_api(
    numeric_id: str,
    program_codes: list[str],
) -> dict[str, Any]:
    """Return the exact combined full-course fee for a packaged program."""
    if len(program_codes) == 1:
        return _enrich_from_fees_api(numeric_id, program_codes[0])
    totals: list[float] = []
    years: list[int] = []
    for code in program_codes:
        data = _get_json(f"https://bond.edu.au/api/program-fees/{numeric_id}/{code}")
        if not isinstance(data, dict):
            return {}
        fees_list = data.get("fees") or []
        if not fees_list:
            return {"_authoritative_fee_omission": True}
        entry = _select_fee_entry(fees_list)
        international = entry.get("international") if entry else None
        total = _positive_amount(
            international.get("total") if isinstance(international, dict) else None
        )
        if total is None:
            # Different component semester prices cannot safely be presented as
            # one annual figure. Fail closed unless every full-course total exists.
            return {}
        totals.append(total)
        if str(entry.get("year", "")).isdigit():
            years.append(int(entry["year"]))
    result: dict[str, Any] = {
        "international_fee": sum(totals),
        "currency": "AUD",
        "fee_term": "Full Course",
    }
    if years and len(set(years)) == 1:
        result["fee_year"] = years[0]
    return result


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
_PTE_RE = re.compile(
    r"Pearson Test of English \(PTE\) Academic.*?Overall score\s+(\d+)"
    r"(?:\s+with no Communicative Scores below\s+(\d+))?",
    re.IGNORECASE,
)
_TOEFL_RE = re.compile(
    r"TOEFL iBT\).*?Overall score\s+(\d+)",
    re.IGNORECASE,
)


def _extract_admission_text(text: str) -> str | None:
    section = re.search(
        r"Admission criteria\s+(.*?)\s+(?:Find your qualification|"
        r"English language proficiency requirements|Alternative entry options)",
        text,
        re.IGNORECASE,
    )
    source = section.group(1).strip() if section else ""
    degree = re.search(
        r"(Successful completion of a recognised Bachelor degree "
        r"\(or equivalent qualification\) in any field(?:\s*\([^)]{1,220}\))?\.?)",
        source,
        re.IGNORECASE,
    )
    if degree:
        return " ".join(degree.group(1).split())[:300]
    atar = re.search(
        r"ATAR\s*-\s*Australian Tertiary Admissions Rank\s*(\d+).*?"
        r"IB\s*-\s*International Baccalaureate Diploma\s*(\d+)",
        source,
        re.IGNORECASE,
    )
    parts: list[str] = []
    if atar:
        parts.append(
            f"ATAR {atar.group(1)} (Australian Year 12 qualification or "
            f"equivalent Selection Rank); IB Diploma {atar.group(2)}"
        )
    prerequisite = re.search(
        r"Program prerequisites\s+(Successfully complete .*?)"
        r"(?:See the equivalent|Some programs have|$)",
        text,
        re.IGNORECASE,
    )
    if prerequisite:
        parts.append("Prerequisites: " + " ".join(prerequisite.group(1).split()))
    return "; ".join(parts)[:300] or None


def _enrich_from_entry_requirements(course_url: str) -> dict[str, Any]:
    """Fetch /entry_requirements subpage and parse IELTS bands from plain text."""
    # Construct subpage URL: strip trailing slash, append /entry_requirements
    base = course_url.rstrip("/")
    er_url = f"{base}/entry_requirements"

    html = _get_html(er_url)
    if not html:
        return {}

    # Strip tags and normalise whitespace for regex matching
    text = unescape(re.sub(r"<[^>]+>", " ", html))
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

    pte = _PTE_RE.search(text)
    if pte:
        result["pte_overall"] = float(pte.group(1))
        if pte.group(2):
            for skill in ("writing", "reading", "listening", "speaking"):
                result[f"pte_{skill}"] = float(pte.group(2))
    toefl = _TOEFL_RE.search(text)
    if toefl:
        result["toefl_overall"] = float(toefl.group(1))
    admission = _extract_admission_text(text)
    if admission:
        result["other_requirement"] = admission

    if result:
        log.info("[BOND] entry_requirements → %s", result)
    return result


_CENTRAL_IELTS_URL = (
    "https://bond.edu.au/entry-to-bond/entry-requirements/"
    "international-entry-requirements/english-language-requirements/ielts"
)


def _enrich_from_central_ielts(course_url: str) -> dict[str, Any]:
    """Fill only exact Bond program gaps from the official program-keyed table."""
    slug = urlparse(course_url).path.rstrip("/").split("/")[-1].lower()
    if slug in {
        "master-of-occupational-therapy",
        "bachelor-of-health-sciences-master-of-occupational-therapy",
    }:
        marker = "Master of Occupational Therapy"
    elif slug in {
        "master-of-philosophy",
        "doctor-of-philosophy",
        "doctor-of-legal-science-research",
        "master-of-laws-by-research",
    }:
        marker = "All higher degree by research (HDR) programs"
    else:
        return {}
    html = _get_html(_CENTRAL_IELTS_URL)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return {}
    requirement_text = ""
    for table in soup.find_all("table"):
        active_requirement = ""
        active_rows = 0
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            if len(cells) >= 2:
                requirement_cell = cells[1]
                active_requirement = " ".join(
                    requirement_cell.get_text(" ", strip=True).split()
                )
                try:
                    active_rows = max(1, int(requirement_cell.get("rowspan", 1)))
                except (TypeError, ValueError):
                    active_rows = 1
            first_cell = " ".join(cells[0].get_text(" ", strip=True).split())
            current_requirement = active_requirement if active_rows > 0 else ""
            if first_cell.casefold() == marker.casefold():
                requirement_text = current_requirement
                break
            if active_rows > 0:
                active_rows -= 1
                if active_rows == 0:
                    active_requirement = ""
        if requirement_text:
            break
    requirement = re.search(
        r"Overall\s+(\d(?:\.\d+)?)\s+with\s+no\s+sub-?score\s+less\s+than\s+"
        r"(\d(?:\.\d+)?)",
        requirement_text,
        re.IGNORECASE,
    )
    if not requirement:
        return {}
    overall, floor = map(float, requirement.groups())
    if not (4.0 <= floor <= overall <= 9.0):
        return {}
    return {
        "ielts_overall": overall,
        "ielts_writing": floor,
        "ielts_reading": floor,
        "ielts_listening": floor,
        "ielts_speaking": floor,
    }


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
    extractors): ``has_central_fee_page``.

    Soft-set keys (``setdefault`` semantics, extractors may win):
    ``intake_months``, ``category``,
    ``international_fee``, ``fee_term``, ``ielts_*``.

    The details API's ``duration`` and ``duration_term`` form one authoritative
    pair and must be applied together by the caller.
    """
    result: dict[str, Any] = {}
    source_urls: dict[str, str] = {}
    authoritative_fee_omission = False

    # ── Always-set (hard block on extractor mis-fires) ─────────────────────
    result["has_central_fee_page"] = True
    plain_text = re.sub(r"<[^>]+>", " ", html or "")
    plain_text = re.sub(r"\s+", " ", plain_text)

    # ── API enrichment ──────────────────────────────────────────────────────
    numeric_id, program_codes = _extract_program_keys(html or "")

    if numeric_id:
        details = _enrich_from_details_api(numeric_id)
        details_program_code = details.pop("_program_code", None)
        details_program_codes = details.pop("_program_codes", [])
        if not details_program_codes and details_program_code:
            details_program_codes = [details_program_code]
        authoritative_delivery_omission = bool(
            details.pop("_authoritative_delivery_omission", False)
        )
        if authoritative_delivery_omission:
            result["_authoritative_delivery_omission"] = True
        # Use setdefault for everything from the API so generic extractors
        # can still win if they found values first.
        for k, v in details.items():
            result.setdefault(k, v)
            source_urls.setdefault(k, f"https://bond.edu.au/api/program-details/{numeric_id}")

        if (
            program_codes
            and details_program_codes
            and {code.casefold() for code in program_codes}
            != {code.casefold() for code in details_program_codes}
        ):
            log.warning(
                "[BOND] %s — HTML program code %s disagrees with details API "
                "code %s; skipping fees API",
                url,
                program_codes,
                details_program_codes,
            )
            resolved_program_codes = []
        else:
            resolved_program_codes = (
                program_codes or details_program_codes
                or ([details_program_code] if details_program_code else [])
            )
        if resolved_program_codes:
            fees_url = (
                f"{url.rstrip('/')}/fees"
                if len(resolved_program_codes) > 1
                else f"https://bond.edu.au/api/program-fees/"
                f"{numeric_id}/{resolved_program_codes[0]}"
            )
            fees = _enrich_from_packaged_fees_api(
                numeric_id, resolved_program_codes
            )
            authoritative_fee_omission = bool(
                fees.pop("_authoritative_fee_omission", False)
            )
            for k, v in fees.items():
                result.setdefault(k, v)
                source_urls.setdefault(k, fees_url)
        else:
            log.warning("[BOND] %s — program_code not found in HTML, skipping fees API", url)
    else:
        log.warning("[BOND] %s — numeric_id not found in HTML, API enrichment skipped", url)

    # ── IELTS from entry_requirements subpage ───────────────────────────────
    entry_ielts = _enrich_from_entry_requirements(url)
    central_ielts = _enrich_from_central_ielts(url)
    ielts = dict(entry_ielts)
    for k, v in central_ielts.items():
        ielts.setdefault(k, v)
    for k, v in ielts.items():
        result.setdefault(k, v)
        source_urls.setdefault(
            k,
            f"{url.rstrip('/')}/entry_requirements"
            if k in entry_ielts
            else _CENTRAL_IELTS_URL,
        )

    # ── Static HTML intake fallback (when API ids absent or API returned no months) ─
    if "intake_months" not in result:
        static_months = _extract_intake_from_static_html(plain_text)
        if static_months:
            result["intake_months"] = static_months
            log.info("[BOND] %s — intake_months extracted from static HTML: %s", url, static_months)

    # ── Static HTML fee fallback (when API ids absent or API returned no fee) ─
    if "international_fee" not in result and not authoritative_fee_omission:
        static_fee = _extract_fee_from_static_html(plain_text)
        for k, v in static_fee.items():
            result.setdefault(k, v)
        if static_fee:
            log.info("[BOND] %s — fee extracted from static HTML: %.0f", url, static_fee["international_fee"])

    # ── Fee warning when still missing ─────────────────────────────────────
    if "international_fee" not in result:
        result["scrape_warnings"] = [
            "bond_fee_source_empty"
            if authoritative_fee_omission
            else "bond_fee_js_rendered"
        ]
        if authoritative_fee_omission:
            result["_authoritative_fee_omission"] = True
        log.info(
            "[BOND] %s — fee not resolved from API; "
            "staging with has_central_fee_page=True for human review", url,
        )
    else:
        log.info(
            "[BOND] %s — fully enriched: fee=%.0f ielts=%s",
            url, result["international_fee"], result.get("ielts_overall"),
        )

    result["_source_urls"] = source_urls
    return result

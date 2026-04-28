"""Bond University program-page extractor.

Bond University (bond.edu.au) publishes all real courses under the path
``/program/<slug>`` and renders all dynamic fields (fees, English scores,
intake calendar) via client-side JavaScript. The standard Playwright browser
pass returns ``filled=[]`` for every Bond program page because the fee and
English tables are not present in the initial HTML payload — they require
a second XHR/fetch round-trip that the extractor's settle window misses.

This module provides:

``is_bond_program_url(url)``
    Returns True for bond.edu.au/program/* URLs.

``apply_bond_extraction(url, html)``
    Pre-seeds the per-course payload with Bond-authoritative values:

    * ``has_central_fee_page = True``
        Bypasses the ``no_international_fee`` staging gate so every real
        Bond program page is staged for human review rather than silently
        discarded. Operators see the fee-blank row in the Review UI and
        can supply the correct fee manually.

    * ``course_location = "Gold Coast, Queensland"``
        Bond has a single residential campus at Robina (Gold Coast, QLD).
        All ``/program/`` pages are physically delivered there.
        Written directly (not via setdefault) so the standard location
        extractor's footer-derived garbage ("University Club (Building 6),
        Bond University", or random sentence fragments) cannot win.

    * ``study_mode = "On Campus"``
        Bond's standard delivery mode is on-campus.  The extractor also
        looks for explicit "online" delivery keywords in the static HTML;
        if found it switches to "Blended" (Bond may offer selected programs
        online as well as on campus).  Written directly so the fallback
        derive_mode_from_location path in single_course.py is suppressed.

    * ``intake_months``
        Bond operates a tri-semester model: January, May, September.
        The extractor first tries to read real intake months from the page;
        if nothing is found, these three are used as the known-good fallback.

    * ``scrape_warnings``
        Appends ``"bond_fee_js_rendered"`` when no international fee could
        be found in the static HTML. This surfaced as an amber badge in the
        Review UI so operators know why the fee field is empty.

    All other fields (course_name, degree_level, English scores, duration,
    domestic_fee, international_fee) are left to the standard extractor chain
    and the AI fallback. The Bond extractor NEVER overwrites a non-empty slot.

Design note
-----------
Called as a *pre-seed* inside ``single_course.extract_course`` before the
``_EXTRACTORS`` loop.  Uses direct assignment (``payload[k] = v``) — not
``setdefault`` — for location, study_mode, and has_central_fee_page so
these values cannot be overwritten by the generic extractors' first-write-wins
``setdefault`` calls.  All other keys use ``setdefault`` to let the
extractors win when they do find real values.
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
# Location / mode
# ---------------------------------------------------------------------------

_BOND_LOCATION: str = "Gold Coast, Queensland"

# Explicit online-delivery keywords that override the default On Campus mode.
# Bond has historically offered selected programs via online delivery as well.
_ONLINE_MODE_RE = re.compile(
    r"\b(online(?:\s+delivery)?|fully\s+online|distance\s+learning|"
    r"external\s+study|study\s+online)\b",
    re.IGNORECASE,
)

# Location keywords that indicate a physical campus presence — used to
# distinguish "Online and On Campus" (Blended) from fully online delivery.
_CAMPUS_MENTION_RE = re.compile(
    r"\b(gold\s+coast|robina|on.campus|on\s+campus|residential)\b",
    re.IGNORECASE,
)


def _derive_study_mode(html_text: str) -> str:
    """Return the most appropriate study_mode for a Bond program page.

    Priority:
    1. Explicit "online" keyword found AND campus mention found → "Blended"
    2. Explicit "online" keyword found but NO campus mention → "Online"
    3. Default → "On Campus"
    """
    has_online = bool(_ONLINE_MODE_RE.search(html_text))
    has_campus = bool(_CAMPUS_MENTION_RE.search(html_text))
    if has_online and has_campus:
        return "Blended"
    if has_online:
        return "Online"
    return "On Campus"


# ---------------------------------------------------------------------------
# Intake months
# ---------------------------------------------------------------------------

# Bond's standard tri-semester calendar.  The extractor looks for explicit
# month names first; this is the fallback used when none are found.
_BOND_DEFAULT_INTAKES: list[str] = ["January", "May", "September"]

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
# Title-cased month list for the fallback evidence snippet.
_MONTH_TITLE = {m: m.title() for m in _MONTH_NAMES}

_INTAKE_CONTEXT_RE = re.compile(
    r"(?:intakes?|semesters?|sessions?|start\s+dates?|commenc(?:e|ing)|"
    r"enroll(?:ment)?|enrol(?:ment)?)\s*:?\s*([^\n<]{5,120})",
    re.IGNORECASE,
)


def _extract_intake_months(html: str) -> list[str] | None:
    """Try to pull intake months from an intake/session context block.

    Returns a deduplicated ordered list of title-cased month names, or None
    when no intake-context block is found so the caller can apply the Bond
    default fallback.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    months: list[str] = []
    seen: set[str] = set()
    for m in _INTAKE_CONTEXT_RE.finditer(text):
        snippet = m.group(1).lower()
        for month in _MONTH_NAMES:
            if month in snippet and month not in seen:
                months.append(_MONTH_TITLE[month])
                seen.add(month)
    return months if months else None


# ---------------------------------------------------------------------------
# Fee extraction (best-effort from static HTML)
# ---------------------------------------------------------------------------

# Bond fee labels seen in static HTML:
#   "International students: A$28,320 per year"
#   "Total program fee: $85,440"
#   "Annual tuition fee: $28,320"
_INTL_FEE_RE = re.compile(
    r"(?:international(?:\s+students?)?|intl)\s*:?\s*[A-Z]?\$\s*([\d,]+)",
    re.IGNORECASE,
)
_ANNUAL_FEE_RE = re.compile(
    r"(?:annual\s+tuition|tuition\s+fee|per\s+year|annual\s+fee)\s*:?\s*[A-Z]?\$\s*([\d,]+)",
    re.IGNORECASE,
)


def _extract_international_fee(html: str) -> float | None:
    """Try to pull a numeric international fee from Bond's static HTML.

    Returns a float (AUD) or None when not found.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    for pattern in (_INTL_FEE_RE, _ANNUAL_FEE_RE):
        m = pattern.search(text)
        if m:
            raw = m.group(1).replace(",", "")
            try:
                val = float(raw)
                if 1_000 <= val <= 200_000:
                    return val
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def apply_bond_extraction(url: str, html: str) -> dict[str, Any]:
    """Return a pre-seed dict for Bond University ``/program/`` pages.

    Called before the standard extractor loop in single_course.extract_course.
    Values that must block extractor mis-fires are returned unconditionally
    (location, study_mode, has_central_fee_page).  All other values are only
    returned when the extractor found something concrete so the caller can
    decide whether to use setdefault or direct assignment.

    Return keys
    -----------
    has_central_fee_page : True  (always)
    course_location      : "Gold Coast, Queensland"  (always for /program/)
    study_mode           : "On Campus" | "Blended" | "Online"
    intake_months        : list[str]  (extracted or Bond tri-semester default)
    international_fee    : float | absent  (only when found in static HTML)
    scrape_warnings      : list[str]  (appended "bond_fee_js_rendered" when fee absent)
    """
    result: dict[str, Any] = {}

    # ── Always-set keys (direct write, block extractor mis-fires) ──────────
    result["has_central_fee_page"] = True
    result["course_location"] = _BOND_LOCATION

    # Study mode — parse from static HTML then fall back to On Campus default.
    plain_text = re.sub(r"<[^>]+>", " ", html or "")
    plain_text = re.sub(r"\s+", " ", plain_text)
    result["study_mode"] = _derive_study_mode(plain_text)

    # ── Intake months ─────────────────────────────────────────────────────
    found_months = _extract_intake_months(html or "")
    result["intake_months"] = found_months if found_months else _BOND_DEFAULT_INTAKES[:]

    # ── Best-effort fee extraction from static HTML ──────────────────────
    intl_fee = _extract_international_fee(html or "")
    if intl_fee is not None:
        result["international_fee"] = intl_fee
        result["fee_term"] = "year"
        log.info("[BOND] %s — fee extracted from static HTML: %.0f", url, intl_fee)
    else:
        # Fee is JS-rendered — not available in static HTML. Flag for review.
        result["scrape_warnings"] = ["bond_fee_js_rendered"]
        log.info(
            "[BOND] %s — fee not in static HTML (JS-rendered); "
            "staging with has_central_fee_page=True for human review",
            url,
        )

    return result

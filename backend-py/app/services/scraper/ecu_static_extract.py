"""ECU (Edith Cowan University) program-page extractor.

ECU (ecu.edu.au) publishes all individual course pages under the path
``/degrees/courses/<slug>``. Other paths under the same domain are
article pages, news, study-experience pages, and hub/category pages —
those must be filtered out at discovery time (see discovery.py).

This module provides:

``is_ecu_course_url(url)``
    Returns True for ecu.edu.au/degrees/courses/<slug> pages (not the
    /all listing or /postgraduate hub — those are discovery seeds, not
    individual course pages).

``apply_ecu_extraction(url, html)``
    Pre-seeds the per-course payload with ECU-authoritative values:

    * ``has_central_fee_page = True``
        ECU's fee schedule is published centrally.  Without this flag
        every ECU course would be hard-rejected by the no_international_fee
        staging gate even though the course is real — the fee just lives on
        a different page.  Operators see the fee-blank row in the Review UI
        and can supply the correct fee or link it from the fee schedule.

    * ``course_location``
        ECU operates four physical campuses:
            Joondalup  (main campus, north Perth metro)
            Mount Lawley (inner Perth, arts/education hub)
            South West  (Bunbury regional campus)
            Perth City  (small CBD presence)
        The extractor scans static HTML for these campus names and returns
        the ones it finds. If no campus mention is found, it defaults to
        "Perth, Australia" — which is correct (all ECU campuses are in WA,
        Australia) and prevents non-Australian locations (e.g. "Sri Lanka"
        from international-student marketing text) from leaking through.

    * ``scrape_warnings``
        Appends "ecu_fee_review" when no international fee is found in
        the static HTML — surfaced as an amber badge in the Review UI.

Design note
-----------
Called as a *pre-seed* inside ``single_course.extract_course`` before the
``_EXTRACTORS`` loop.  ``has_central_fee_page`` uses direct assignment so
the staging gate cannot overwrite it.  ``course_location`` also uses direct
assignment to prevent the generic location extractor from winning with
footer-derived garbage (ECU's footer contains every campus name + "Sri
Lanka" from marketing links — the extractor must not use that).  All other
keys use ``setdefault`` so standard extractors can override them.
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

_ECU_HOSTS = frozenset({"ecu.edu.au", "www.ecu.edu.au"})

# Article / non-course path fragments that must be rejected during discovery
# even if they superficially match a course-URL pattern.
_ECU_NON_COURSE_PATHS = (
    "/degrees/courses/all",
    "/degrees/courses/search",
    "/degrees/postgraduate",
    "/degrees/undergraduate",
    "/study/extra/",
    "/study/articles/",
    "/news/",
    "/research/",
    "/about/",
    "/staff/",
    "/students/",
    "/services/",
    "/events/",
    "/contact",
    "/international/",
    "/future-students/",
    "/current-students/",
    "/industry/",
    "/our-research/",
    "/scholarships",
)


def is_ecu_course_url(url: str) -> bool:
    """Return True when *url* is a real ECU course page.

    A real ECU course page lives at:
        https://www.ecu.edu.au/degrees/courses/<slug>

    where <slug> is NOT "all", "search", or a pagination segment.
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host not in _ECU_HOSTS:
            return False
        path = p.path.lower().rstrip("/")
        # Must start with /degrees/courses/ and have at least one slug segment.
        if not path.startswith("/degrees/courses/"):
            return False
        # Strip the prefix and check the remaining slug is non-empty and
        # not one of the known listing pages.
        slug = path[len("/degrees/courses/"):]
        if not slug or slug in ("all", "search", "postgraduate", "undergraduate"):
            return False
        # Reject paths with further sub-directories (category hubs)
        # e.g. /degrees/courses/health-sciences/bachelor-of-nursing
        # ECU individual course pages are always one segment deep.
        if "/" in slug:
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Campus location detection
# ---------------------------------------------------------------------------

# ECU's four real physical campuses.  Any combination of these may appear on
# a course page — the extractor collects all that it finds.
_ECU_CAMPUS_NAMES: tuple[tuple[str, str], ...] = (
    # (search_pattern_lower, canonical_name)
    ("joondalup",      "Joondalup"),
    ("mount lawley",   "Mount Lawley"),
    ("south west",     "South West"),
    ("bunbury",        "South West"),   # South West campus is in Bunbury
    ("perth city",     "Perth City"),
    ("cbd",            "Perth City"),
)

# Regex that detects non-Australian locations leaking from marketing text.
# These appear in ECU's "where our students come from" sections.
_NON_AU_LOCATION_NOISE_RE = re.compile(
    r"\b(sri lanka|india|china|malaysia|vietnam|indonesia|nepal|pakistan|"
    r"bangladesh|kenya|nigeria|ghana|zimbabwe|uganda|ethiopia|cambodia|"
    r"myanmar|singapore|hong kong|philippines)\b",
    re.IGNORECASE,
)

_DEFAULT_ECU_LOCATION = "Perth, Australia"


def _extract_ecu_location(html: str) -> str:
    """Detect ECU campus names from static HTML.

    Returns a comma-separated string of found campus names, or the default
    "Perth, Australia" when nothing ECU-specific is detected.

    Strips non-Australian location noise that appears in marketing sections
    (e.g. "Students from Sri Lanka may apply…").
    """
    # Strip HTML tags and collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).lower()

    found: list[str] = []
    seen: set[str] = set()
    for pattern, canonical in _ECU_CAMPUS_NAMES:
        if pattern in text and canonical not in seen:
            found.append(canonical)
            seen.add(canonical)

    if found:
        return ", ".join(found)

    # No ECU campus found — use the safe default.
    return _DEFAULT_ECU_LOCATION


# ---------------------------------------------------------------------------
# Fee extraction (best-effort from static HTML)
# ---------------------------------------------------------------------------

# ECU sometimes renders fees in static HTML before JS hydration.
_INTL_FEE_RE = re.compile(
    r"(?:international(?:\s+students?)?|tuition)\s*(?:fee|fees?)?\s*:?\s*"
    r"[A-Z]?\$\s*([\d,]+)",
    re.IGNORECASE,
)
_ANNUAL_FEE_RE = re.compile(
    r"(?:annual\s+tuition|per\s+year|fee\s+per\s+year|annual\s+fee)\s*:?\s*"
    r"[A-Z]?\$\s*([\d,]+)",
    re.IGNORECASE,
)


def _extract_international_fee(html: str) -> float | None:
    """Try to extract a numeric international fee from ECU static HTML."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    for pat in (_INTL_FEE_RE, _ANNUAL_FEE_RE):
        m = pat.search(text)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 1_000 <= val <= 200_000:
                    return val
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def apply_ecu_extraction(url: str, html: str) -> dict[str, Any]:
    """Return a pre-seed dict for ECU ``/degrees/courses/`` pages.

    Called before the standard extractor loop in single_course.extract_course.

    Return keys
    -----------
    has_central_fee_page : True  (always — bypasses no_international_fee gate)
    course_location      : ECU campus name(s) or "Perth, Australia"  (always)
    international_fee    : float | absent  (only when found in static HTML)
    scrape_warnings      : list[str]  (appends "ecu_fee_review" when fee absent)
    """
    result: dict[str, Any] = {}

    # Always-set: bypass the no_international_fee hard rejection.
    result["has_central_fee_page"] = True

    # Always-set: clean location derived from page content only.
    # Uses direct assignment (not setdefault) so the generic location
    # extractor — which grabs footer text containing "Sri Lanka" etc. —
    # cannot overwrite this authoritative value.
    result["course_location"] = _extract_ecu_location(html or "")

    # Best-effort fee from static HTML.
    intl_fee = _extract_international_fee(html or "")
    if intl_fee is not None:
        result["international_fee"] = intl_fee
        result["fee_term"] = "year"
        log.info("[ECU] %s — fee extracted from static HTML: %.0f", url, intl_fee)
    else:
        result["scrape_warnings"] = ["ecu_fee_review"]
        log.info(
            "[ECU] %s — fee not in static HTML; staging with has_central_fee_page=True",
            url,
        )

    return result

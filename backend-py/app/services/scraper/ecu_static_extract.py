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

_DEFAULT_ECU_LOCATION = "Perth"  # 2026-05-13: dropped ", Australia" suffix per user preference (every ECU campus is in WA)


# ---------------------------------------------------------------------------
# Availability & Campus grid parser (2026-05-12)
# ---------------------------------------------------------------------------
#
# ECU course pages render a structured table at the "Availability & Campus"
# panel:
#
#     <table class="info-table info-table-availability">
#       <thead>
#         <tr>
#           <th>Location</th>
#           <th>Semester 1</th>
#           <th>Semester 2</th>
#         </tr>
#       </thead>
#       <tbody>
#         <tr>
#           <th scope="row">Joondalup</th>
#           <td><span title="Full Time Available">FT</span></td>
#           <td><span title="Part Time Available">PT</span></td>
#         </tr>
#         …
#       </tbody>
#     </table>
#
# Bug history (2026-05-12 user report):
#   - The substring-based ``_extract_ecu_location`` was returning every
#     campus name that appeared anywhere in the page, including row labels
#     for empty rows (City Campus / South West / Sri Lanka), so e.g.
#     Bachelor of Arts (only Joondalup + Online have FT/PT) was staged as
#     "Joondalup, South West".
#   - The text-only intake extractor (``intake.ecu_semester`` in
#     extractors/intake.py, conf=0.85) sees both "Semester 1" and "Semester
#     2" column headers regardless of which one actually has FT/PT cells,
#     so every ECU course was staged with intake_months=["February","July"].
#     E.g. Master of Nursing (Graduate Entry) only offers Joondalup S1 FT
#     PT — should be ["February"], not ["February","July"].
#
# This parser walks the table HTML, ties each non-empty FT/PT cell back to
# its (campus, semester) pair, and returns:
#   - campuses with at least one FT/PT cell anywhere in their row
#   - intake months mapped from semesters with at least one FT/PT cell
#     anywhere in their column
#
# When the grid is found but EVERY cell is empty (e.g. Master of Nursing
# (Nurse Practitioner) on the international tab — paired with the
# domestic-only banner "This course is not offered for study on-campus in
# Australia to international students"), both lists are empty and the
# pre-seed leaves location/intake unset so the global domestic-only filter
# in single_course.py can drop the page.

_AVAIL_TABLE_RE = re.compile(
    # Accept both double- and single-quoted class attributes — ECU itself uses
    # double quotes today, but unquoted/single-quoted variants must not silently
    # disable the parser if the template ever changes.
    r"<table\b[^>]*\bclass\s*=\s*[\"'][^\"']*\binfo-table-availability\b[^\"']*[\"'][^>]*>"
    r"(?P<body>.*?)</table>",
    re.S | re.I,
)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.S | re.I)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.S | re.I)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.S | re.I)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_TAGS_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SEM_HEADER_RE = re.compile(r"\bSemester\s+([1-3])\b", re.I)
_FT_PT_RE = re.compile(
    r"\b(?:FT|PT|Full[\s-]?Time|Part[\s-]?Time)\b",
    re.I,
)

# Maps Australian academic semester → start month (ECU follows the standard
# AU calendar — Sem 1 = Feb, Sem 2 = Jul, Sem 3 = Oct).
_ECU_SEMESTER_TO_MONTH: dict[int, str] = {
    1: "February",
    2: "July",
    3: "October",
}

# Canonical names for the row labels that appear in ECU's availability grid.
# Sri Lanka is intentionally excluded — it's an offshore partner campus
# that the scraper has historically filtered out of location extraction
# (see ``_NON_AU_LOCATION_NOISE_RE`` above).  Online IS included because
# it represents a delivery option ECU exposes per-course.
_ECU_GRID_CAMPUS_NORM: dict[str, str] = {
    "joondalup": "Joondalup",
    "mount lawley": "Mount Lawley",
    "south west": "South West",
    "bunbury": "South West",
    "perth city": "Perth City",
    "city campus": "Perth City",
    "online": "Online",
}


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAGS_RE.sub(" ", s)).strip()


def parse_availability_grid(html: str) -> dict[str, list[str]] | None:
    """Parse ECU's <table class="info-table-availability"> grid.

    Returns a dict ``{"campuses": [...], "intake_months": [...]}`` where
    each list contains only entries with at least one FT/PT cell.  Returns
    ``None`` when no such table is present (e.g. non-course pages, or ECU
    pages that pre-date the current template).  Returns the dict with
    BOTH lists empty when the table exists but every cell is blank — the
    caller should treat that as a strong "no offering on this tab" signal.
    """
    if not html:
        return None
    table_m = _AVAIL_TABLE_RE.search(html)
    if not table_m:
        return None
    table_html = table_m.group("body")

    # Resolve column → semester number from the <thead> row.
    sem_cols: list[int | None] = []
    thead_m = _THEAD_RE.search(table_html)
    if thead_m:
        head_row_m = _TR_RE.search(thead_m.group(1))
        if head_row_m:
            for th in _TH_RE.findall(head_row_m.group(1)):
                hm = _SEM_HEADER_RE.search(_strip_tags(th))
                sem_cols.append(int(hm.group(1)) if hm else None)
    if not any(sem_cols):
        # No recognisable semester headers — abort and let the caller fall
        # back to the legacy substring extractor.
        return None

    # Collect FT/PT presence per (campus_row, semester_col).
    tbody_m = _TBODY_RE.search(table_html)
    body_html = tbody_m.group(1) if tbody_m else table_html
    rows = _TR_RE.findall(body_html)

    campuses_with_offer: list[str] = []
    seen_campuses: set[str] = set()
    sems_with_offer: set[int] = set()

    for row_html in rows:
        first_th = _TH_RE.search(row_html)
        if not first_th:
            continue
        row_label = _strip_tags(first_th.group(1)).lower()
        canonical = _ECU_GRID_CAMPUS_NORM.get(row_label)
        cells = _TD_RE.findall(row_html)
        any_offer = False
        for td_idx, cell_html in enumerate(cells):
            cell_text = _strip_tags(cell_html)
            if not cell_text or not _FT_PT_RE.search(cell_text):
                continue
            any_offer = True
            # td index N corresponds to thead column N+1 (the first thead
            # column is "Location" which has no <td> in body rows).
            sem_thead_idx = td_idx + 1
            if 0 <= sem_thead_idx < len(sem_cols):
                sem_num = sem_cols[sem_thead_idx]
                if sem_num:
                    sems_with_offer.add(sem_num)
        if any_offer and canonical and canonical not in seen_campuses:
            campuses_with_offer.append(canonical)
            seen_campuses.add(canonical)

    months: list[str] = []
    for n in sorted(sems_with_offer):
        month = _ECU_SEMESTER_TO_MONTH.get(n)
        if month and month not in months:
            months.append(month)

    return {"campuses": campuses_with_offer, "intake_months": months}


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

    # Authoritative location + intake months from the structured
    # Availability & Campus grid (2026-05-12 fix).  When the grid is
    # present we ALWAYS prefer it: it ties FT/PT presence to
    # (campus, semester) pairs so empty rows / columns no longer leak
    # into the staged values.  Falls back to the legacy substring scan
    # only when no grid is found (older template / non-standard pages).
    grid = parse_availability_grid(html or "")
    if grid is not None:
        if grid["campuses"]:
            result["course_location"] = ", ".join(grid["campuses"])
        else:
            # Grid exists but EVERY cell is empty — do not write a default
            # location.  Two cases reach here:
            #   1. Page also carries the domestic-only banner (e.g. Master
            #      of Nursing (Nurse Practitioner)) — the global filter in
            #      single_course.py drops the page before staging anyway.
            #   2. Page has no banner but no offerings either (rare; usually
            #      a template glitch or a course that's between intakes) —
            #      we surface "ecu_no_offerings" as a scrape warning so
            #      operators can review in the UI rather than silently
            #      seeding "Perth, Australia" / wrong intakes.
            warns = list(result.get("scrape_warnings") or [])
            if "ecu_no_offerings" not in warns:
                warns.append("ecu_no_offerings")
            result["scrape_warnings"] = warns
        if grid["intake_months"]:
            # Higher priority than intake.ecu_semester (conf=0.85) so this
            # wins the merge in single_course.py.
            result["intake_months"] = grid["intake_months"]
        log.info(
            "[ECU grid] %s — campuses=%s intakes=%s",
            url,
            grid["campuses"],
            grid["intake_months"],
        )
    else:
        # Pre-2026-05 template / non-standard pages: best-effort substring scan.
        result["course_location"] = _extract_ecu_location(html or "")

    # Best-effort fee from static HTML.
    intl_fee = _extract_international_fee(html or "")
    if intl_fee is not None:
        result["international_fee"] = intl_fee
        result["fee_term"] = "year"
        log.info("[ECU] %s — fee extracted from static HTML: %.0f", url, intl_fee)
    else:
        warns = list(result.get("scrape_warnings") or [])
        if "ecu_fee_review" not in warns:
            warns.append("ecu_fee_review")
        result["scrape_warnings"] = warns
        log.info(
            "[ECU] %s — fee not in static HTML; staging with has_central_fee_page=True",
            url,
        )

    return result

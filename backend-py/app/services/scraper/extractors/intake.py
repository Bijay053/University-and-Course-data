"""Intake-month extractor.

Ported from Node ``extractIntakeMonths`` in
``artifacts/api-server/src/routes/scrape.ts`` (lines 3175-3296).
Strategy in passes:
  1. Look for full date forms ("15 February 2025" / "20 Jul").
  2. Look near keywords like "applications open", "next intake",
     "study period", "course start".
  3. Fall back to month names inside short windows around the word
     "intake" itself.
"""
from __future__ import annotations

import re

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult


field_key = "intake_months"

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_FULL = "|".join(_MONTHS)
_MONTH_ABBR = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
_MONTH_ANY = f"{_MONTH_FULL}|{_MONTH_ABBR}"

_KEYWORD_WINDOW = re.compile(
    r"(applications?\s*(?:open|close|closing|date|opening\s*date)|"
    r"next\s*(?:available\s*)?intake|available\s*intakes?|"
    r"study\s*(?:period|periods?|start|begins?)|"
    r"course\s*(?:start|commencement)|class\s*starts?|"
    r"start\s*date(?:s)?|commencement(?:\s*date)?|"
    r"entry\s*point|intake(?:s)?)",
    re.I,
)

# ── Start-dates-section anchor (YAML start_dates_only mode) ────────────────
# Matches a "Start dates" / "Next start date" / "Start dates for YYYY" heading
# as it appears in flattened page text.  The trailing [\s\S]{0,40}? allows
# for "Start dates for 2027 Semester One" before the newline/colon delimiter.
# Used by the start_dates_only pass to anchor the extraction window so that
# only months from the authoritative start-dates block are captured.
_START_DATES_SECTION_RE = re.compile(
    r"(?:next\s+)?start\s+dates?\b[^\n.!?]{0,60}?(?=\n|\r|:\s*\n|Starts?\b|Semester\b)",
    re.I,
)

# Matches "Semester One – 1 March", "Semester 2 – 20 July", "Starts – March"
# as they appear in Auckland-style "Start dates for YYYY" sections.
# Named group ``month`` captures the month name/abbreviation.
_SEMESTER_DATE_RE = re.compile(
    r"(?:Semester\s+(?:One|Two|Three|Four|1|2|3|4)|Starts?)"
    r"\s*[-\u2013\u2014]\s*"
    r"(?:\d{1,2}\s+)?"
    r"(?P<month>"
    + (_MONTH_FULL + "|" + _MONTH_ABBR)
    + r")",
    re.I,
)

# Months appearing near research-candidature language are NOT course
# intake months — they're HDR enrollment windows, research-period
# admission dates, or thesis submission deadlines.  Chunks containing
# any of these phrases are excluded from Passes 1 & 2 so that, e.g.,
# "Research candidature commencing January, May or August" on a Master
# of Research page doesn't produce intake_months=[Jan, May, Aug] when
# the real coursework intakes are February / May / June.
_INTAKE_RESEARCH_REJECT_RE = re.compile(
    r"\b(?:candidature|research\s+(?:period|enrollment|enrolment|"
    r"commencement|training|degree|program(?:me)?)|"
    r"hdr\b|higher\s+degree\s+by\s+research|"
    r"thesis\s+(?:submission|completion|milestone)|"
    r"maximum\s+(?:candidature|completion|time)|"
    r"doctoral\s+(?:candidature|program(?:me)?)|"
    r"research\s+candidacy|graduate\s+research)\b",
    re.I,
)

# ── Bug 2a / 2b: View-dates table and application-timeline contamination ──
#
# Weekday abbreviations — the key discriminator between:
#   summary field:       "Start Feb, Jun, Sep"   (month immediately after Start)
#   View-dates row:      "Start Mon 16 Feb"       (weekday immediately after Start)
# We use this to guard the _SUMMARY_START_RE below AND to reject View-dates
# table chunks inside _scoped_chunks.
_WEEKDAY_ABBR_STR = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"

# Labeled summary "Start [months]" extractor.
# Matches the short, consistent "Start Feb, Jun, Sep" (or "Start Jan") entry
# in the course overview / summary block.  Negative lookahead ensures that
# "Start Mon 16 Feb" (a View-dates table row) does NOT match.  The capture
# group reads only the bare month list — it stops at the first non-month
# token so it can never bleed into adjacent fields or the View-dates table.
_SUMMARY_START_RE = re.compile(
    rf"\bStart\s+(?!{_WEEKDAY_ABBR_STR}\b)"
    rf"((?:{_MONTH_ANY})(?:[,\s/]+(?:{_MONTH_ANY}))*)",
    re.I,
)

# Application process / timeline reject.
# Pages like UniSQ Master of Professional Psychology embed a multi-step
# application workflow that triggers _KEYWORD_WINDOW via "applications open":
#   Applications open:   Tuesday 5 August 2025
#   Applications close:  Monday 15 September 2025
#   Interviews from:     Monday 22 September 2025
#   Outcomes from:       17 October 2025
#   Block 1 study period start: Monday 19 January 2026
# The months Aug/Sep/Oct in these chunks are deadline dates, not intake
# months.  Any _scoped_chunks hit whose text contains these timeline signals
# is discarded.
_APPLICATION_TIMELINE_REJECT_RE = re.compile(
    r"\b(?:"
    r"applications?\s+close[sd]?\b|"
    r"interview[s]?\s+from\b|"
    r"outcomes?\s+from\b|"
    r"block\s+\d+\s+study\s+period\s+start"
    r")\b",
    re.I,
)

# View-dates orientation row reject.
# The "View dates" expanded table on UniSQ course pages has rows of the form:
#   "Trimester 2, 2026  Orientation Mon 25 May  Start Mon 1 Jun"
# The Orientation month (May) precedes the actual intake month (June) and
# MUST NOT be captured as an intake month.  Any chunk that contains an
# "Orientation Mon/Tue/…" pattern is a View-dates table slice — discard it.
_ORIENTATION_DATE_REJECT_RE = re.compile(
    rf"\bOrientation\s+{_WEEKDAY_ABBR_STR}\b",
    re.I,
)
_FULL_DATE = re.compile(
    rf"\b(\d{{1,2}})(?:\s+|-|/)+({_MONTH_ANY})(?:(?:\s+|-|/)\d{{2,4}})?\b", re.I
)
_MONTH_RE = re.compile(rf"\b({_MONTH_ANY})\b", re.I)

# Mirrors `study_mode._extract_strong_label_value`: a structural pre-pass
# that reads the value cell directly out of the DOM so the same
# flattened-text boundary-collision bug class can't bleed an adjacent
# field's value into the intake capture (e.g. ASA-style
# `<div><strong>Location</strong></div><div>Sydney, March</div>
# <div><strong>Intake</strong></div><div>February, July</div>` —
# tag-stripping concatenates "March" and "Intake" and the keyword
# window would then walk forward from the wrong offset).
_SEMESTER_MONTH_MAP: dict[str, str] = {
    "1": "February",
    "2": "July",
    "3": "October",
}
_SEMESTER_RE = re.compile(r"\bSemester\s+([1-3])\b", re.I)

# Australian university session names → canonical start month.
# UOW (and similar institutions) use "Autumn Session" / "Spring Session"
# instead of Semester 1/2.  Maps case-insensitively; only fires as a
# last-resort fallback (Pass 4) when passes 1-3 found nothing.
_SESSION_MONTH_MAP: dict[str, str] = {
    "autumn": "March",
    "spring": "July",
    "summer": "November",
    "winter": "June",
}
_SESSION_RE = re.compile(
    r"\b(autumn|spring|summer|winter)\s+session\b", re.I
)

_INTAKE_LABEL_RE = re.compile(
    r"(?:intakes?|intake\s+(?:dates?|months?|periods?)|"
    r"next\s+(?:available\s+)?intakes?|available\s+intakes?|"
    r"start\s+dates?|commencement(?:\s+dates?)?|"
    r"course\s+(?:start\s+dates?|commencement|starts?)|"
    r"study\s+(?:periods?|start)|class\s+starts?|"
    r"applications?\s+(?:open|close|closing|opening\s+date)|"
    r"entry\s+points?)"
    # Optional trailing campus parenthetical, e.g. "Start dates (Newcastle)",
    # "Start dates (Central Coast)", "Start dates (Sydney)".  Without this,
    # the fullmatch in `_extract_strong_label_value` rejects the label and
    # the extractor falls through to greedy text passes that pick up
    # cross-campus dates from the international-toggle expanded view —
    # the 2026-05-15 Newcastle bug where Bachelor of Physiotherapy staged
    # intake_months=["January","February"] instead of the correct ["January"]
    # (the only displayed start date is "Semester 1 — 27 Jan 2026 (Newcastle)").
    # Bounded to 40 chars so a value mis-tagged as <strong> can't slip through.
    r"(?:\s*\([^)]{1,40}\))?",
    re.IGNORECASE,
)
_STRONG_VALUE_CHAR_CAP = 300


def _normalise_month(raw: str) -> str | None:
    """'Jan' / 'jan.' / 'JANUARY' → 'January'."""
    m = (raw or "").strip(" ,.;:").lower()[:4]
    for full in _MONTHS:
        if full.lower().startswith(m):
            return full
    return None


def _classify_intake_value(value: str) -> tuple[list[str], int | None] | None:
    """Parse months (and a leading day-of-month, if present) from a raw
    label-value string. Returns ``(months, day)`` or ``None`` when no
    month name is recoverable. Mirrors the two-pass strategy in
    :func:`extract` (full ``day Month`` dates first, bare month names
    as a fallback) but constrained to a single value cell so we never
    bleed adjacent paragraphs into the result."""
    months: list[str] = []
    day: int | None = None
    for m in _FULL_DATE.finditer(value):
        d = int(m.group(1))
        if 1 <= d <= 31:
            mo = _normalise_month(m.group(2))
            if mo:
                if day is None:
                    day = d
                if mo not in months:
                    months.append(mo)
    if not months:
        for raw in _MONTH_RE.findall(value):
            mo = _normalise_month(raw)
            if mo and mo not in months:
                months.append(mo)
    if not months:
        return None
    return months, day


def _extract_strong_label_value(
    html: str,
) -> tuple[tuple[list[str], int | None] | None, str | None]:
    """Structural pre-pass for label/value idioms in the DOM. See
    :func:`study_mode._extract_strong_label_value` for the full
    rationale — this is the same idea, restricted to intake labels.

    Recognised idioms (all read the value from the DOM rather than
    from a flattened tag-stripped token run):

    * ``<strong>Intake</strong>`` / ``<b>Start dates:</b>`` — value
      either inline after the bold tag or in a sibling element. Walks
      forward in document order until the next labelled boundary.
    * ``<dt>Intake</dt><dd>February, July</dd>`` — definition lists.
    * ``<th>Intake</th><td>February, July</td>`` — table key/value rows.
    """
    if not html:
        return None, None
    try:
        from bs4 import BeautifulSoup
        from bs4.element import NavigableString, Tag
    except ImportError:  # pragma: no cover - bs4 is a hard dep
        return None, None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None, None

    for label_tag in soup.find_all(("strong", "b", "dt", "th")):
        label_raw = label_tag.get_text(" ", strip=True).rstrip(":").strip()
        if not label_raw or not _INTAKE_LABEL_RE.fullmatch(label_raw):
            continue

        value_text: str | None = None
        if label_tag.name == "dt":
            sibling = label_tag.find_next_sibling("dd")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        elif label_tag.name == "th":
            sibling = label_tag.find_next_sibling("td")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        else:
            parts: list[str] = []
            char_count = 0
            for node in label_tag.next_elements:
                if isinstance(node, Tag):
                    if node is label_tag:
                        continue
                    if node.name in ("strong", "b", "h1", "h2", "h3",
                                     "h4", "h5", "h6", "dt", "th",
                                     "tr"):
                        break
                    continue
                if isinstance(node, NavigableString):
                    text = str(node).strip()
                    if not text:
                        continue
                    parts.append(text)
                    char_count += len(text) + 1
                    if char_count >= _STRONG_VALUE_CHAR_CAP:
                        break
            value_text = " ".join(parts)

        if not value_text:
            continue
        value_text = value_text.lstrip(":-– ").strip()
        if not value_text:
            continue
        parsed = _classify_intake_value(value_text)
        if parsed is not None:
            snippet = (
                f"<{label_tag.name}>{label_raw}</{label_tag.name}> -> "
                f"{value_text[:80]}"
            )
            return parsed, snippet
    return None, None


_CAMPUS_TABLE_LABEL_RE = re.compile(
    r"start\s+dates?\s+(?:and\s+)?campus(?:es)?|"
    r"availability\s+(?:&|and)\s+campus(?:es)?",
    re.I,
)
_CAMPUS_PERIOD_COL_RE = re.compile(
    r"(?:Semester|Trimester|Term|Quarter)\s+\d+"
    r"(?:\s*[-–—]\s*(?P<month>"
    + "|".join(_MONTHS) +
    r"))?",
    re.I,
)
_ONLINE_RE = re.compile(r"^online\b", re.I)


def _extract_campus_table_intake(html: str) -> list[str] | None:
    """Parse a 'Start dates and campus' pivot table (as used by UNE) and
    return only months where at least one physical (non-Online) campus row
    has a detectable checkmark.  Returns None when no such table found."""
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    from app.services.scraper.extractors.location import _cell_availability

    soup = BeautifulSoup(html, "html.parser")
    for th in soup.find_all(["th", "td"]):
        if not _CAMPUS_TABLE_LABEL_RE.search(th.get_text(strip=True)):
            continue
        parent_table = th.find_parent("table")
        if not parent_table:
            continue
        header_row = th.find_parent("tr")
        if not header_row:
            continue
        header_cells = header_row.find_all(["th", "td"])
        if len(header_cells) < 2:
            continue
        # Build a mapping: col_index → month extracted from header text
        col_month: dict[int, str] = {}
        for i, hcell in enumerate(header_cells[1:], start=1):
            htext = hcell.get_text(strip=True)
            m = _CAMPUS_PERIOD_COL_RE.search(htext)
            if m:
                mo_str = m.group("month")
                if mo_str:
                    mo = _normalise_month(mo_str)
                    if mo:
                        col_month[i] = mo
                else:
                    # No explicit month in header — extract from column text anyway
                    for raw in _MONTH_RE.findall(htext):
                        mo = _normalise_month(raw)
                        if mo:
                            col_month[i] = mo
                            break
        if not col_month:
            return None
        # Walk data rows and collect months with at least one physical campus ✓
        confirmed: set[str] = set()
        for row in parent_table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells or len(cells) < 2:
                continue
            row_label = cells[0].get_text(strip=True)
            if not row_label or _CAMPUS_TABLE_LABEL_RE.search(row_label):
                continue
            if _ONLINE_RE.search(row_label):
                continue  # skip Online rows
            for col_idx, month in col_month.items():
                if col_idx >= len(cells):
                    continue
                avail = _cell_availability(cells[col_idx])
                if avail == "yes":
                    confirmed.add(month)
        if confirmed:
            # Preserve calendar order
            return [m for m in _MONTHS if m in confirmed]
        # Availability opaque (all icons undetectable) — fall through
        return None


def _scoped_chunks(text: str, max_chunks: int = 16) -> list[str]:
    out: list[str] = []
    for hit in _KEYWORD_WINDOW.finditer(text):
        start = max(0, hit.start() - 24)
        end = min(len(text), hit.end() + 260)
        chunk = text[start:end].strip()
        if not chunk or chunk in out:
            continue
        # Reject chunks that describe HDR research-period enrollment windows,
        # thesis submission dates, or candidature commencements — these are NOT
        # coursework intake months (e.g. "Research candidature commencing Jan,
        # May or Aug" on a UniSQ Master of Research page).
        if _INTAKE_RESEARCH_REJECT_RE.search(chunk):
            continue
        # Reject application process / timeline chunks (Bug 2b).
        # A chunk triggered by "applications open" that also contains
        # "applications close", "interviews from", or "outcomes from" is a
        # multi-step deadline timeline — the months in it are closing/interview
        # dates, NOT intake months (e.g. UniSQ Master of Professional Psychology
        # produces "Aug, Sep, Oct, Jan" from its application timeline).
        if _APPLICATION_TIMELINE_REJECT_RE.search(chunk):
            continue
        # Reject View-dates orientation table chunks (Bug 2a).
        # UniSQ "View dates" rows look like:
        #   "Trimester 2, 2026  Orientation Mon 25 May  Start Mon 1 Jun"
        # The orientation month (May) must not be captured as an intake month.
        # Any chunk containing "Orientation Mon/Tue/…" is from that table.
        if _ORIENTATION_DATE_REJECT_RE.search(chunk):
            continue
        out.append(chunk)
        if len(out) >= max_chunks:
            break
    return out


# "Recently viewed" sidebar widget that appears on many university sites
# (UniSQ and others) lists other courses' names, locations, and Start dates.
# When the intake keyword-window scan runs on the full page text, the word
# "Start" (or "intakes") in the sidebar triggers _KEYWORD_WINDOW, and the
# month names from those OTHER courses are captured as intake months for the
# CURRENT course. Strip this block from the plain text before any pass so
# only the course's own content is scanned.
#
# Pattern: everything from a "Recently viewed" heading to the end of the
# text, or to the next heading-level boundary (capped at 3 000 chars to
# avoid stripping large legitimate sections on pathological pages).
_RECENTLY_VIEWED_RE = re.compile(
    r"recently\s+viewed\b.{0,3000}",
    re.IGNORECASE | re.DOTALL,
)


def _strip_recently_viewed(text: str) -> str:
    """Remove 'Recently viewed' sidebar sections from plain text.

    Prevents intake months (and campus names) from sibling-course listings
    in the 'Recently viewed' widget from being captured as this course's
    own intake dates.
    """
    return _RECENTLY_VIEWED_RE.sub("", text)


_UOW_HOSTS: frozenset[str] = frozenset({"www.uow.edu.au", "uow.edu.au"})

# ECU (Edith Cowan University) — every coursework programme page publishes
# its intake calendar as a "Semester availability" / "Availability & Campus
# Location" pivot whose column headers are literally "Semester 1" and
# "Semester 2" (and occasionally "Semester 3").  No explicit month names
# appear in those sections, so the regex passes below fall through to the
# rest of the page text and pick up:
#   - "may" (lowercase) from sentences like "Applicants may apply…"
#   - "August / September" from the "Information Sessions" events sidebar
#     ("Tue 11 Aug Tuesday, 11 August South West Teacher Education
#     Information Session…  Mon 07 Sep Monday, 07 September …")
# Net effect on the 2026-05-11 ECU scrape (job_2c8b501ed7d3): ~80% of
# bachelor courses were staged with intake_months=["May"] and the research
# masters / cyber-security cert were staged with ["August","September"] —
# every one of them should be Feb+Jul.  The fix mirrors the UOW pattern:
# detect the ECU semester anchors and map directly to the AU academic
# calendar (Semester 1 → Feb, Semester 2 → Jul, Semester 3 → Oct), short-
# circuiting the noisy Pass-1/2 raw-month scan.
_ECU_HOSTS: frozenset[str] = frozenset({"www.ecu.edu.au", "ecu.edu.au"})

# Anchors that confirm we are looking at ECU's canonical course-page
# semester pivot, not an incidental "Semester 1" mention elsewhere on the
# site.  Either anchor is sufficient — both appear on every ECU coursework
# page sampled (Bachelor / Master / Graduate Certificate / research masters).
_ECU_SEMESTER_ANCHOR_RE = re.compile(
    r"(?:Semester\s+availability|Availability\s*(?:&amp;|&)\s*Campus)",
    re.IGNORECASE,
)

# Labels that indicate a nearby month is a real intake/session, not a
# deadline or key-date.  Used by the UOW-specific session guard below.
_SESSION_LABEL_RE = re.compile(
    r"(?:intake|session|commenc(?:ing|ement)|start\s+date|course\s+start"
    r"|starts?|study\s+period)",
    re.I,
)


def _nz_semester_supplement(months: list[str], text: str) -> list[str]:
    """Bug 11: AUT (and other NZ unis) have two intakes per year — Semester 1
    (February) and Semester 2 (July) — but Quick Facts shows only the *next*
    start date. When the page body contains course listings for BOTH Semester 1
    AND Semester 2 (e.g. "Year 1 Semester 1: ARCH800" and "Year 1 Semester 2:
    ARCH801"), the programme accepts mid-year entry and both months must appear.

    Safe for any host — returns ``months`` unchanged when the page does not
    mention both semesters (single-intake programmes are unaffected).
    """
    sem1 = bool(re.search(r"\bSemester\s+1\b", text, re.I))
    sem2 = bool(re.search(r"\bSemester\s+2\b", text, re.I))
    if not (sem1 and sem2):
        return months
    supplemented: list[str] = list(months)
    if "February" not in supplemented:
        supplemented.append("February")
    if "July" not in supplemented:
        supplemented.append("July")
    return [mo for mo in _MONTHS if mo in set(supplemented)]


def _select_intake_candidate(
    candidates: list[ExtractionResult],
) -> list[ExtractionResult]:
    """Pick the winning intake candidate from all content-gated passes.

    Selection rule mirrors the system-wide arbitration in
    ``_finalize_evidence_selection``: highest confidence wins, ties broken
    by first-write (insertion order is preserved by ``sorted``'s stability).

    This replaces the previous behaviour where each content-gated pass did
    an early ``return``. With early returns, a pass added or loosened to fix
    one university could intercept another university's page upstream and
    silently change its result. By accumulating candidates and selecting on
    confidence, a lower-confidence pass can no longer pre-empt a
    higher-confidence one just because it appears earlier in the function.
    """
    if not candidates:
        return []
    # sorted() is stable, so equal-confidence candidates keep insertion
    # order — i.e. first-write-wins at equal confidence, matching the
    # documented arbitration rule.
    best = sorted(candidates, key=lambda r: r.confidence, reverse=True)[0]
    return [best]


async def _extract_raw(html: str, url: str) -> list[ExtractionResult]:
    from urllib.parse import urlparse as _up
    _host = (_up(url).netloc or "").lower()
    _is_uow = _host in _UOW_HOSTS
    _is_ecu = _host in _ECU_HOSTS

    # ── Candidate accumulator (regression fix 2026-05-28) ────────────────
    # Previously each content-gated pass below (campus-pivot, structural,
    # summary-start) did an early `return` the moment it matched. Because
    # these passes fire on PAGE CONTENT (not host), a pass added/loosened to
    # fix university B would intercept university A's page upstream of the
    # generic Pass 1-4 that produced A's correct months — silently breaking
    # A. The fix: content-gated passes now APPEND to `candidates` instead of
    # returning, and the highest-confidence candidate is selected at the end.
    # The HOST-gated blocks (UOW / ECU) keep their early returns — they are
    # netloc-scoped and must win for their own host, and cannot bleed.
    candidates: list[ExtractionResult] = []

    # Campus-pivot pass: handles UNE "Start dates and campus" table where
    # months appear in column headers (e.g. "Trimester 1 – February 2026")
    # and availability is indicated by checkmarks in data rows.  Only
    # physical (non-Online) campus rows contribute months.
    pivot_months = _extract_campus_table_intake(html)
    if pivot_months:
        candidates.append(
            ExtractionResult(
                field_key="intake_months",
                value=pivot_months,
                normalized={"intake_months": pivot_months, "intake_days": None},
                confidence=0.85,
                snippet="campus-pivot-table",
                method="intake.campus_pivot",
            )
        )

    # Structural pre-pass FIRST — see _extract_strong_label_value for
    # the rationale. When the page publishes intake months as a
    # `<strong>Intake</strong>` / `<dt>/<dd>` / `<th>/<td>` pair, read
    # the value cell out of the DOM directly so a flattened-text
    # boundary collision with the previous field's value can't pollute
    # the result.
    structural, snippet = _extract_strong_label_value(html)
    if structural is not None:
        months, day = structural
        candidates.append(
            ExtractionResult(
                field_key="intake_months",
                value=months,
                normalized={
                    "intake_months": months,
                    "intake_days": day,
                },
                confidence=0.8,
                snippet=snippet,
                method="intake.structural",
            )
        )

    text = compact(html_to_text(html))
    if not text:
        # No page text to run the remaining passes against, but a structural
        # or campus-pivot candidate may already have been found above.
        return _select_intake_candidate(candidates)

    # Strip "Recently viewed" sidebar before ANY keyword or month scan.
    # Sites like UniSQ embed sibling-course names + Start dates in this
    # widget; leaving it in causes months from unrelated courses to be
    # captured as this course's own intake dates.
    text = _strip_recently_viewed(text)

    # ── Pass 0a: Start-dates-section anchor (YAML start_dates_only) ───────
    # When extraction.intake.start_dates_only=True in the per-uni YAML, only
    # extract months from the dedicated "Start dates" / "Next start date"
    # section on the course page.  This prevents exam calendars, deadlines,
    # and academic-calendar tables from adding spurious months to the list.
    # Confidence 0.92 beats every other text pass so it always wins.
    # Falls through when the anchor heading is not found (safety net).
    try:
        from app.services.scraper.config.context import require_uni_config as _ruc
        _uc_i = _ruc()
        _cfg_sd_only: bool = bool(
            getattr(getattr(getattr(_uc_i, "extraction", None), "intake", None), "start_dates_only", False)
        )
        _cfg_sd_win: int = int(
            getattr(getattr(getattr(_uc_i, "extraction", None), "intake", None), "start_dates_window_chars", 600)
        )
    except Exception:
        _cfg_sd_only = False
        _cfg_sd_win = 600

    if _cfg_sd_only:
        _anchor_m = _START_DATES_SECTION_RE.search(text)
        if _anchor_m:
            _section = text[_anchor_m.start(): _anchor_m.start() + _cfg_sd_win]
            _sd_months: list[str] = []
            # First: "Semester X – DD Month" or "Starts – DD Month" patterns
            for _sm in _SEMESTER_DATE_RE.finditer(_section):
                _mn = _normalise_month(_sm.group("month"))
                if _mn and _mn not in _sd_months:
                    _sd_months.append(_mn)
            # Second: bare "DD Month" full-date patterns in the section
            if not _sd_months:
                for _fm in _FULL_DATE.finditer(_section):
                    _mn = _normalise_month(_fm.group(2))
                    if _mn and _mn not in _sd_months:
                        _sd_months.append(_mn)
            # Third: bare month names anywhere in the section
            if not _sd_months:
                for _raw in _MONTH_RE.findall(_section):
                    _mn = _normalise_month(_raw)
                    if _mn and _mn not in _sd_months:
                        _sd_months.append(_mn)
            if _sd_months:
                _ordered_sd = [mo for mo in _MONTHS if mo in set(_sd_months)]
                return [
                    ExtractionResult(
                        field_key="intake_months",
                        value=_ordered_sd,
                        normalized={"intake_months": _ordered_sd, "intake_days": None},
                        confidence=0.92,
                        snippet=f"start_dates_section: {_section[:120].strip()}",
                        method="intake.start_dates_section",
                    )
                ]
            # Anchor found but no months in section — return empty rather than
            # falling through to greedy passes that would give wrong months.
            return []
        # Anchor not found — fall through to standard passes as safety net.

    # ── Pass 0b: labeled summary "Start [months]" field ───────────────────
    # The structural DOM pass above catches <strong>/<dt>/<th> patterns, but
    # UniSQ and similar universities render the course summary "Start" field
    # as a bare label (e.g. inside a <span> or <div>) that _INTAKE_LABEL_RE
    # does not match, so it falls through to the greedy text passes where the
    # View-dates table and application timeline contaminate the result.
    #
    # _SUMMARY_START_RE looks for "\bStart [months]" with a negative lookahead
    # that rejects weekday abbreviations immediately after "Start" (which
    # signals a View-dates row: "Start Mon 16 Feb"), so only the terse summary
    # entry ("Start Feb, Jun, Sep") fires.  The capture group stops at the
    # first non-month token — it cannot bleed into adjacent fields.
    #
    # Priority: higher than _scoped_chunks / Pass 1-2 so the contaminated
    # text passes are skipped entirely when the summary field is found.
    _summary_hit = _SUMMARY_START_RE.search(text)
    if _summary_hit:
        raw_months = [_normalise_month(r) for r in _MONTH_RE.findall(_summary_hit.group(1))]
        _summary_months = [m for m in _MONTHS if m in set(filter(None, raw_months))]
        if _summary_months:
            candidates.append(
                ExtractionResult(
                    field_key="intake_months",
                    value=_summary_months,
                    normalized={"intake_months": _summary_months, "intake_days": None},
                    confidence=0.82,
                    snippet=f"summary-start: {_summary_hit.group(0)[:60]}",
                    method="intake.summary_start",
                )
            )

    # ── ECU-specific: Semester N → month mapping takes priority ──────────
    # See _ECU_SEMESTER_ANCHOR_RE doc above.  ECU course pages do NOT carry
    # explicit month names in their intake widget — only "Semester 1" /
    # "Semester 2" column headers — so the raw Pass-1/2 month scan locks
    # onto incidental tokens (lowercase "may" inside body sentences, "Aug"
    # / "Sep" from the Information-Sessions events sidebar) and produces
    # uniformly wrong intake months across the catalogue.  We run the
    # Semester-N scan FIRST when an ECU semester anchor is present and
    # return immediately, skipping the noisy passes entirely.  Conservative:
    # if neither anchor is present (unusual ECU pages: doctorates, study-
    # abroad blurbs, news articles) we fall through to the default chain.
    if _is_ecu:
        # Scope the Semester-N scan to a tight window around each anchor
        # match.  Without this, curriculum tables that mention "Year 1
        # Semester 3" (or unit guides referencing Sem 3) inflate the intake
        # list with months not actually offered as intakes.  ±400 chars is
        # enough to cover the entire "Semester availability" / "Availability
        # & Campus Location" pivot block on every ECU page sampled while
        # excluding curriculum, FAQ, and footer mentions far from the
        # anchor.
        _ecu_anchor_hits = list(_ECU_SEMESTER_ANCHOR_RE.finditer(text))
    else:
        _ecu_anchor_hits = []
    if _is_ecu and _ecu_anchor_hits:
        _ECU_WIN = 400
        scope_chunks: list[str] = []
        for am in _ecu_anchor_hits:
            lo = max(0, am.start() - _ECU_WIN)
            hi = min(len(text), am.end() + _ECU_WIN)
            scope_chunks.append(text[lo:hi])
        scope_text = "\n".join(scope_chunks)
        ecu_months: list[str] = []
        for m in _SEMESTER_RE.finditer(scope_text):
            mapped = _SEMESTER_MONTH_MAP.get(m.group(1))
            if mapped and mapped not in ecu_months:
                ecu_months.append(mapped)
        if ecu_months:
            ordered = [mo for mo in _MONTHS if mo in set(ecu_months)]
            return [
                ExtractionResult(
                    field_key="intake_months",
                    value=ordered,
                    normalized={"intake_months": ordered, "intake_days": None},
                    confidence=0.85,
                    snippet=f"ECU semester: {', '.join(ordered)}",
                    method="intake.ecu_semester",
                )
            ]

    # ── UOW-specific: session-name extraction takes priority ──────────────
    # UOW uses "Autumn session" (→ March) and "Spring session" (→ July)
    # instead of explicit month dates. The keyword-window passes (Passes 1-2
    # below) are too greedy on UOW pages: they pick up months from
    # application-deadline paragraphs, key-dates tables, and previous-year
    # admission notices, producing spurious 5-6 month lists. For UOW we run
    # the session-name scan FIRST and return immediately when it fires — the
    # raw-month passes are skipped entirely to avoid deadline contamination.
    if _is_uow:
        session_months: list[str] = []
        for m in _SESSION_RE.finditer(text):
            mapped = _SESSION_MONTH_MAP.get(m.group(1).lower())
            if mapped and mapped not in session_months:
                session_months.append(mapped)
        if session_months:
            # Preserve calendar order (March before July, etc.)
            ordered = [mo for mo in _MONTHS if mo in session_months]
            return [
                ExtractionResult(
                    field_key="intake_months",
                    value=ordered,
                    normalized={"intake_months": ordered, "intake_days": None},
                    confidence=0.85,
                    snippet=f"UOW session: {', '.join(ordered)}",
                    method="intake.session_names",
                )
            ]
        # No session names found — fall through to semester mapping only.
        # Skip Passes 1-2 (raw month scan) to avoid picking up deadline
        # months and key-date entries that are not course intakes.
        uow_months: list[str] = []
        for m in _SEMESTER_RE.finditer(text):
            mapped = _SEMESTER_MONTH_MAP.get(m.group(1))
            if mapped and mapped not in uow_months:
                uow_months.append(mapped)
        if uow_months:
            ordered = [mo for mo in _MONTHS if mo in set(uow_months)]
            return [
                ExtractionResult(
                    field_key="intake_months",
                    value=ordered,
                    normalized={"intake_months": ordered, "intake_days": None},
                    confidence=0.75,
                    snippet=f"UOW semester: {', '.join(ordered)}",
                    method="intake.semester",
                )
            ]
        # Nothing found for UOW — return empty rather than wrong months.
        return []

    chunks = _scoped_chunks(text)
    search = " | ".join(chunks) if chunks else text[:12000]

    months: list[str] = []
    days: list[int] = []

    # Pass 1: full "day Month" dates.
    for m in _FULL_DATE.finditer(search):
        day = int(m.group(1))
        if 1 <= day <= 31:
            month = _normalise_month(m.group(2))
            if month:
                if day not in days:
                    days.append(day)
                if month not in months:
                    months.append(month)

    # Pass 2: month names anywhere in scoped chunks.
    if not months:
        for raw in _MONTH_RE.findall(search):
            month = _normalise_month(raw)
            if month and month not in months:
                months.append(month)

    # Pass 3: Semester N → month mapping (Australian academic calendar).
    # Fires only when passes 1 & 2 found nothing — handles pages that expose
    # "Semester 1" / "Semester 2" availability labels with no explicit dates
    # (e.g. ECU's "Availability & Campus" pivot table).
    if not months:
        for m in _SEMESTER_RE.finditer(text):
            mapped = _SEMESTER_MONTH_MAP.get(m.group(1))
            if mapped and mapped not in months:
                months.append(mapped)

    # Pass 4: named-session → month mapping (UOW-style "Autumn Session" /
    # "Spring Session").  Fires only when passes 1-3 found nothing.
    if not months:
        for m in _SESSION_RE.finditer(text):
            mapped = _SESSION_MONTH_MAP.get(m.group(1).lower())
            if mapped and mapped not in months:
                months.append(mapped)

    if months:
        candidates.append(
            ExtractionResult(
                field_key="intake_months",
                value=months,
                normalized={"intake_months": months, "intake_days": days[0] if days else None},
                confidence=0.7 if chunks else 0.4,
                snippet=search[:240],
                method="regex",
            )
        )

    return _select_intake_candidate(candidates)


# ── QUT structural reader (qut.edu.au) ─────────────────────────────────────
# QUT's per-course pages publish the canonical intake months for international
# applicants inside a sidebar UL of the shape::
#
#     <b>Entry</b>
#     <ul data-course-map-key="quickBoxCourseStartsINT">
#       <li>January</li>
#       <li>July</li>
#     </ul>
#
# The generic regex cascade reads the full course prose and pulls in
# unrelated month tokens from "applications open", testimonial copy, and
# scholarship windows, producing 5–6 month lists that don't match the
# canonical 2-month intake the live page actually shows.  This structural
# reader is host-gated to qut.edu.au and runs BEFORE _extract_raw so the
# canonical INT entry always wins.
# Verified 2026-05-17 on Graduate Diploma in Legal Practice
# (university_id QUT, course id 22761; stored ["January","March","May",
# "July","August","October"] vs page-canonical ["January","July"]).
def _from_qut_quickbox(html: str, url: str) -> list[str] | None:
    from urllib.parse import urlparse as _urlparse
    host = (_urlparse(url or "").hostname or "").lower()
    if not (host == "qut.edu.au" or host.endswith(".qut.edu.au")):
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    # Preference: INT (canonical international value) → DOM (fallback).
    for preferred in ("quickBoxCourseStartsINT", "quickBoxCourseStartsDOM"):
        for el in soup.find_all(attrs={"data-course-map-key": True}):
            key = el.get("data-course-map-key") or ""
            # Exact-match (not substring) — matches the duration reader's
            # tightened key-precedence guard.  Prevents future variant
            # keys (e.g. a hypothetical `quickBoxCourseStartsINTAlt`) from
            # accidentally satisfying the INT precedence pass.
            if not isinstance(key, str) or key.strip() != preferred:
                continue
            raw_items: list[str] = []
            lis = el.find_all("li") if el.name in ("ul", "ol") else []
            if lis:
                for li in lis:
                    t = compact(li.get_text(" ", strip=True))
                    if t:
                        raw_items.append(t)
            else:
                t = compact(el.get_text(" ", strip=True))
                if t:
                    raw_items.append(t)
            months: list[str] = []
            for item in raw_items:
                for tok in _MONTH_RE.findall(item):
                    mo = _normalise_month(tok)
                    if mo and mo not in months:
                        months.append(mo)
            if months:
                return [mo for mo in _MONTHS if mo in set(months)]
    return None


async def extract(html: str, url: str) -> list[ExtractionResult]:
    """Public entry point. Runs all extraction passes via ``_extract_raw``,
    then applies the NZ-semester supplement (Bug 11) for ``.ac.nz`` hosts.

    The supplement adds February and July whenever the page body contains
    course listings for BOTH Semester 1 AND Semester 2, ensuring that
    programmes with mid-year intake are not reported as February-only.
    """
    # QUT structural pre-pass — see _from_qut_quickbox above.  Runs BEFORE
    # _extract_raw because the canonical `quickBoxCourseStartsINT` UL is the
    # authoritative source for QUT intake months; the generic regex cascade
    # otherwise pulls in extra months from unrelated page chrome.
    _qut_months = _from_qut_quickbox(html, url)
    if _qut_months:
        return [
            ExtractionResult(
                field_key="intake_months",
                value=_qut_months,
                normalized={"intake_months": _qut_months, "intake_days": None},
                confidence=0.95,
                snippet=f"qut.quickbox: {', '.join(_qut_months)}",
                method="intake.qut_quickbox",
            )
        ]

    results = await _extract_raw(html, url)
    from urllib.parse import urlparse as _up

    _host = (_up(url).netloc or "").lower()
    if results and _host.endswith(".ac.nz"):
        from app.services.scraper.extractors._text import compact, html_to_text

        _full_text = compact(html_to_text(html)) or ""
        _r = results[0]
        _months = list(_r.normalized.get("intake_months") or [])
        _supplemented = _nz_semester_supplement(_months, _full_text)
        if len(_supplemented) > len(_months):
            results = [
                ExtractionResult(
                    field_key="intake_months",
                    value=_supplemented,
                    normalized={
                        "intake_months": _supplemented,
                        "intake_days": _r.normalized.get("intake_days"),
                    },
                    confidence=_r.confidence,
                    snippet=_r.snippet,
                    method=_r.method + "+nz_semester_supplement",
                )
            ]
    return results

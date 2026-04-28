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
    r"entry\s+points?)",
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
        if chunk and chunk not in out:
            out.append(chunk)
        if len(out) >= max_chunks:
            break
    return out


_UOW_HOSTS: frozenset[str] = frozenset({"www.uow.edu.au", "uow.edu.au"})

# Labels that indicate a nearby month is a real intake/session, not a
# deadline or key-date.  Used by the UOW-specific session guard below.
_SESSION_LABEL_RE = re.compile(
    r"(?:intake|session|commenc(?:ing|ement)|start\s+date|course\s+start"
    r"|starts?|study\s+period)",
    re.I,
)


async def extract(html: str, url: str) -> list[ExtractionResult]:
    from urllib.parse import urlparse as _up
    _host = (_up(url).netloc or "").lower()
    _is_uow = _host in _UOW_HOSTS

    # Campus-pivot pass: handles UNE "Start dates and campus" table where
    # months appear in column headers (e.g. "Trimester 1 – February 2026")
    # and availability is indicated by checkmarks in data rows.  Only
    # physical (non-Online) campus rows contribute months.
    pivot_months = _extract_campus_table_intake(html)
    if pivot_months:
        return [
            ExtractionResult(
                field_key="intake_months",
                value=pivot_months,
                normalized={"intake_months": pivot_months, "intake_days": None},
                confidence=0.85,
                snippet="campus-pivot-table",
                method="intake.campus_pivot",
            )
        ]

    # Structural pre-pass FIRST — see _extract_strong_label_value for
    # the rationale. When the page publishes intake months as a
    # `<strong>Intake</strong>` / `<dt>/<dd>` / `<th>/<td>` pair, read
    # the value cell out of the DOM directly so a flattened-text
    # boundary collision with the previous field's value can't pollute
    # the result.
    structural, snippet = _extract_strong_label_value(html)
    if structural is not None:
        months, day = structural
        return [
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
        ]

    text = compact(html_to_text(html))
    if not text:
        return []

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

    if not months:
        return []
    return [
        ExtractionResult(
            field_key="intake_months",
            value=months,
            normalized={"intake_months": months, "intake_days": days[0] if days else None},
            confidence=0.7 if chunks else 0.4,
            snippet=search[:240],
            method="regex",
        )
    ]

"""VIT-specific static fallback for duration / intakes / location (T003).

Mirrors Node ``applyVitSummaryExtraction``
(``artifacts/api-server/src/routes/scrape.ts`` lines 3619-3735).

When the per-course browser pass clicks the "International students"
toggle on a VIT course page, the international panel sometimes does
NOT contain the same duration / intake / locations summary block as
the static (server-rendered) HTML. The toggle removes the
``<p><strong>Duration:</strong> Usually a 3 year course...</p>``
narrative paragraph from the rendered DOM. Without a static fallback,
courses like Bachelor of Business, Diploma of Business, MBA Finance,
and BBus - HR Specialisation lose all three fields.

This module re-fetches the static HTML over plain HTTP (no JS, no
toggle) and harvests duration / intake / location values from the
``<strong>``-labelled paragraphs and the ``rbt-list-style-3`` lists
VIT publishes on every course page.

Public entry-point: :func:`apply_vit_summary_extraction`. Returns a
dict of fields to merge into the payload via ``setdefault``.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)


# Label regexes lifted directly from Node lines 3651-3669. Loose enough
# to catch the variations VIT uses across course types (BBus vs MBA vs
# Diploma) but tight enough to skip unrelated headings.
_INTAKE_LABEL_STRICT = re.compile(
    r"^(?:20\d{2}\s+)?(?:intakes?|course\s+intakes?|start\s+dates?|commencement)$",
    re.I,
)
_LOCATION_LABEL_STRICT = re.compile(
    r"^(?:campus(?:es)?|locations?|study\s+locations?|"
    r"course\s+locations?|available\s+at)$",
    re.I,
)
_DURATION_LABEL_STRICT = re.compile(
    r"^(?:course\s+)?(?:duration|course\s+length|program\s+length)$",
    re.I,
)
_INTAKE_LABEL_LOOSE = re.compile(
    r"^\s*(?:20\d{2}\s+)?"
    r"(?:intakes?|course\s+intakes?|available\s+intakes?|"
    r"start\s+dates?|class\s+start\s+dates?|commencement\s+dates?|"
    r"next\s+intakes?)\s*:?\s*$",
    re.I,
)
_LOCATION_LABEL_LOOSE = re.compile(
    r"^\s*(?:campus(?:es)?|locations?|study\s+locations?|"
    r"delivery\s+locations?|course\s+locations?|available\s+at)\s*:?\s*$",
    re.I,
)
_DURATION_LABEL_LOOSE = re.compile(
    r"^\s*(?:course\s+)?(?:duration|course\s+length|program\s+length|"
    r"study\s+duration)\s*:?\s*$",
    re.I,
)

# Months we recognise — full names + 3-4 letter abbreviations.
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b",
    re.I,
)
_DURATION_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|weeks?|trimesters?|semesters?)\b",
    re.I,
)
_VIT_CITIES = (
    "Melbourne", "Sydney", "Brisbane", "Adelaide", "Perth",
    "Canberra", "Geelong", "Gold Coast", "Hobart",
)


def is_vit_url(url: str) -> bool:
    """Return True iff the host is a VIT property."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host.endswith("vit.edu.au")


def _cell_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _normalise_month(token: str) -> str | None:
    t = (token or "").strip(" ,.;:").lower()[:4]
    for full in _MONTHS:
        if full.lower().startswith(t):
            return full
    return None


def _harvest_label_buckets(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Walk every label-like element and bucket the next list/paragraph
    items by which field the label refers to (intake / location /
    duration). Mirrors Node strategies 0-5 (lines 3644-3710)."""
    buckets: dict[str, list[str]] = {"intake": [], "location": [], "duration": []}

    # ── Strategy 0: <strong>/<b> label inside a <p>, value is the
    # surrounding text ("<p><strong>Duration:</strong> Usually a 3 year
    # course...</p>"). ──────────────────────────────────────────────────
    for el in soup.find_all(["strong", "b"]):
        label_text = _cell_text(el).rstrip(":").strip()
        if not label_text or len(label_text) > 40:
            continue
        bucket: str | None = None
        if _INTAKE_LABEL_STRICT.match(label_text):
            bucket = "intake"
        elif _LOCATION_LABEL_STRICT.match(label_text):
            bucket = "location"
        elif _DURATION_LABEL_STRICT.match(label_text):
            bucket = "duration"
        if not bucket or buckets[bucket]:
            continue
        parent = el.parent
        if parent is None:
            continue
        full_text = _cell_text(parent)
        strong_text = _cell_text(el)
        remainder = full_text.replace(strong_text, "", 1).lstrip(" :,-").strip()
        if remainder and len(remainder) < 400:
            buckets[bucket] = [remainder]

    # ── Strategies 1-5: any label-like element with a list / inline
    # value next to it. ─────────────────────────────────────────────────
    label_tags = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "label", "div", "span")
    for el in soup.find_all(label_tags):
        label = _cell_text(el)
        if not label or len(label) > 120:
            continue
        bucket = None
        if _INTAKE_LABEL_LOOSE.match(label):
            bucket = "intake"
        elif _LOCATION_LABEL_LOOSE.match(label):
            bucket = "location"
        elif _DURATION_LABEL_LOOSE.match(label):
            bucket = "duration"
        if not bucket or buckets[bucket]:
            continue

        items: list[str] = []

        # Strategy 1: next sibling ul/ol.
        next_list = None
        for sib in el.next_siblings:
            if isinstance(sib, Tag) and sib.name in ("ul", "ol"):
                next_list = sib
                break
        if next_list is not None:
            items = [
                _cell_text(li)
                for li in next_list.find_all("li", recursive=False) or next_list.find_all("li")
                if _cell_text(li)
            ]

        # Strategy 2: parent's next sibling ul/ol.
        if not items and el.parent is not None:
            for sib in el.parent.next_siblings:
                if isinstance(sib, Tag) and sib.name in ("ul", "ol"):
                    items = [
                        _cell_text(li)
                        for li in sib.find_all("li", recursive=False) or sib.find_all("li")
                        if _cell_text(li)
                    ]
                    break

        # Strategy 3: any ul/ol inside the same parent.
        if not items and el.parent is not None:
            sib_list = el.parent.find(["ul", "ol"])
            if sib_list is not None and sib_list.find_all("li"):
                items = [_cell_text(li) for li in sib_list.find_all("li") if _cell_text(li)]

        # Strategy 4: next sibling paragraph/div/span.
        if not items:
            for sib in el.next_siblings:
                if isinstance(sib, Tag) and sib.name in ("p", "div", "span"):
                    txt = _cell_text(sib)
                    if txt and len(txt) < 240 and txt != label:
                        items = [txt]
                    break

        # Strategy 5: same-element inline value, e.g. "Duration: 3 Years".
        if not items:
            inline = re.match(r"^[^:]+:\s*(.{1,200})$", label)
            if inline and inline.group(1).strip():
                items = [inline.group(1).strip()]

        if items:
            buckets[bucket] = items

    return buckets


def _extract_intake_months(joined: str) -> str | None:
    """From a free-text intake string, return a comma-joined list of
    month names (de-duplicated, in calendar order). Returns ``None``
    when no months parse out."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _MONTH_RE.finditer(joined):
        norm = _normalise_month(match.group(1))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered.append(norm)
    if not ordered:
        return None
    # Calendar-sort for stable output.
    by_month = {name: idx for idx, name in enumerate(_MONTHS)}
    ordered.sort(key=lambda m: by_month.get(m, 99))
    return ",".join(ordered)


def _extract_location(joined: str) -> str | None:
    matched = [
        c for c in _VIT_CITIES if re.search(rf"\b{re.escape(c)}\b", joined, re.I)
    ]
    if matched:
        return ", ".join(matched)
    return None


def _extract_duration(items: list[str]) -> tuple[float, str] | None:
    """From a list of duration strings ("Usually a 3 year course..."),
    return ``(value, unit)`` or ``None``. Unit is normalised to one of
    Year / Month / Week / Trimester / Semester."""
    for item in items:
        m = _DURATION_VALUE_RE.search(item)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            unit_raw = m.group(2).lower().rstrip("s")
            mapping = {
                "year": "Year",
                "yr": "Year",
                "month": "Month",
                "week": "Week",
                "trimester": "Trimester",
                "semester": "Semester",
            }
            unit = mapping.get(unit_raw)
            if unit:
                return value, unit
    return None


def apply_vit_summary_extraction(
    url: str,
    html: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Extract VIT duration / intake / location from ``html`` and return
    a dict of NEW fields to merge into ``payload``.

    Caller is expected to merge with ``setdefault`` so any value the
    primary extractors already produced wins. Returns ``{}`` when the
    URL is not a VIT page or the extractor finds nothing usable.
    """
    if not is_vit_url(url) or not html:
        return {}

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001
        log.warning("vit_static_extract: failed to parse %s: %s", url, exc)
        return {}

    buckets = _harvest_label_buckets(soup)
    out: dict[str, Any] = {}

    if buckets["intake"] and "intake_text" not in payload:
        joined = " ".join(buckets["intake"])
        intakes = _extract_intake_months(joined)
        if intakes:
            out["intake_text"] = intakes
            out["intake_months"] = intakes  # secondary slot some consumers read

    if buckets["location"] and "location_text" not in payload:
        joined = " ".join(buckets["location"])
        loc = _extract_location(joined)
        if loc:
            out["location_text"] = loc
        elif buckets["location"]:
            # Fall back to the raw string, capped, when no whitelisted
            # city matched (matches Node line 3726).
            out["location_text"] = ", ".join(buckets["location"])[:200]

    if buckets["duration"] and (payload.get("duration") is None or not payload.get("duration_term")):
        dur = _extract_duration(buckets["duration"])
        if dur:
            value, unit = dur
            if payload.get("duration") is None:
                out["duration"] = value
            if not payload.get("duration_term"):
                out["duration_term"] = unit

    return out


__all__ = ("apply_vit_summary_extraction", "is_vit_url")

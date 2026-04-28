"""Course location extractor.

Ported from the Node ``extractCourseLocation`` cascade in
``artifacts/api-server/src/routes/scrape.ts``. Tries (in order):
  1. Definition lists  (``<dl><dt>Location</dt><dd>Sydney</dd></dl>``)
  2. Tables            (``<tr><th>Campus</th><td>…</td></tr>``)
  3. Heading + sibling (``<h3>Locations</h3><ul><li>…</li></ul>``)
  4. Free-text window  (regex around the keyword "campus location")

Output is normalised + sanitised the same way the Node code does it
(strip marketing copy, drop junk like "online/virtual", dedupe).
"""
from __future__ import annotations

import re
from typing import List

from bs4 import BeautifulSoup

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult

LOCATION_LABEL = re.compile(
    r"^\s*(?:campus(?:\s*locations?)?|location|locations|"
    r"start\s+dates?\s+(?:and\s+)?campus(?:es)?|"
    r"availability\s+(?:&|and)\s+campus(?:es)?|"
    r"where\s+(?:can\s+)?(?:i|you)\s+study|delivery\s+location)\s*:?\s*$",
    re.I,
)
_MARKETING_HINTS = re.compile(
    r"\b(?:focuses on|knowledge and skills|this (?:course|program|degree|qualification)|our (?:courses?|programs?))\b",
    re.I,
)
_JUNK = re.compile(
    r"\b(?:https?://|www\.|src=|href=|style=|googletagmanager|qtac|cricos|step\s*\d+\s*of|student\s*type|fee\s*type|study\s*mode|reset\s*fee\s*calculator)\b",
    re.I,
)
_TRAILING_KEYS = re.compile(
    r"\b(?:delivery\s*mode|delivery\s*method|study\s*mode|course\s*structure|intakes?|course\s*length|duration|cricos\s*code|fees?"
    r"|view\s+dates|start\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
    re.I,
)
_REMOVE_VIRTUAL = re.compile(
    r"\b(?:online|virtual|remote|distance(?:\s*learning)?|off[-\s]?campus|external)\b",
    re.I,
)
_LOCATION_WINDOW = re.compile(
    r"\b(?:campus\s+)?locations?\s*[:\-]?\s*([^\n]{0,220}?)(?=\b(?:intakes?|duration|fees?|student\s*type|learning\s*mode|study\s*mode|delivery|attendance)\b|$)",
    re.I,
)
_COMMON_CITIES = (
    "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Canberra",
    "Darwin", "Hobart", "Gold Coast", "Geelong", "Newcastle", "Wollongong",
    "Cairns", "Townsville", "Ballarat", "Bendigo", "Launceston",
    "Auckland", "Wellington", "Christchurch", "Dunedin", "Hamilton",
    "Palmerston North", "Tauranga", "Rotorua", "Bathurst", "Albury", "Wodonga",
    "Port Macquarie", "Toowoomba",
    "Ipswich", "Springfield",
)

# ── Country suffix maps ───────────────────────────────────────────────────
# Used by _append_country_suffix to determine what country tag to add to a
# location string made entirely of cities from the same country.
_AU_CITIES: frozenset[str] = frozenset({
    "Sydney", "Melbourne", "Brisbane", "Adelaide", "Perth", "Canberra",
    "Darwin", "Hobart", "Gold Coast", "Geelong", "Newcastle", "Wollongong",
    "Cairns", "Townsville", "Ballarat", "Bendigo", "Launceston",
    "Bathurst", "Albury", "Wodonga", "Port Macquarie", "Toowoomba",
    "Ipswich", "Springfield", "Manly", "Parramatta", "Rockingham",
    "Joondalup", "Fremantle", "Tweed Heads",
    # ECU / Bond campuses
    "Mount Lawley", "South West", "Perth City",
})

_NZ_CITIES: frozenset[str] = frozenset({
    "Auckland", "Wellington", "Christchurch", "Dunedin", "Hamilton",
    "Palmerston North", "Tauranga", "Rotorua",
})


def _append_country_suffix(display: str) -> str:
    """Append ', Australia' or ', New Zealand' to a location string when
    every city token belongs unambiguously to the same country.

    Preserves the original string when:
      • The location already contains a country word (Australia, New Zealand, …)
      • Some tokens belong to different countries or are unrecognised
      • The string contains a state / territory indicator (NSW, VIC, QLD …)

    Examples
    --------
    >>> _append_country_suffix("Sydney")
    'Sydney, Australia'
    >>> _append_country_suffix("Sydney, Melbourne")
    'Sydney, Melbourne, Australia'
    >>> _append_country_suffix("Auckland")
    'Auckland, New Zealand'
    >>> _append_country_suffix("Sydney, Auckland")
    'Sydney, Auckland'   # mixed — no suffix
    >>> _append_country_suffix("Sydney, NSW")
    'Sydney, NSW'        # already contextualised — leave as-is
    """
    if not display:
        return display

    _ALREADY_HAS_COUNTRY = re.compile(
        r"\b(?:australia|new zealand|nz|united states|usa|uk|united kingdom|"
        r"canada|india|china|nsw|vic|qld|sa|wa|nt|act|tas)\b",
        re.IGNORECASE,
    )
    if _ALREADY_HAS_COUNTRY.search(display):
        return display

    tokens = [t.strip() for t in display.split(",") if t.strip()]
    if not tokens:
        return display

    # Determine which country every token belongs to (if any).
    au_count = sum(1 for t in tokens if t in _AU_CITIES)
    nz_count = sum(1 for t in tokens if t in _NZ_CITIES)
    unknown_count = len(tokens) - au_count - nz_count

    if unknown_count > 0:
        return display  # don't guess when not all tokens are known

    if au_count > 0 and nz_count == 0:
        return display + ", Australia"
    if nz_count > 0 and au_count == 0:
        return display + ", New Zealand"
    # mixed AU+NZ — leave as-is
    return display

# Campus short-code → full city name mapping.
# Universities (e.g. APIC College) publish location as 3-letter codes
# ("SYD | MEL | BNE") rather than full city names. This map expands
# those codes so the stored location is always human-readable.
_CAMPUS_CODE_MAP: dict[str, str] = {
    "SYD": "Sydney",
    "MEL": "Melbourne",
    "BNE": "Brisbane",
    "PER": "Perth",
    "ADL": "Adelaide",
    "CBR": "Canberra",
    "DAR": "Darwin",
    "HOB": "Hobart",
    "GC":  "Gold Coast",
    "OOL": "Gold Coast",
    "TWD": "Tweed Heads",
    "GEE": "Geelong",
    "NEW": "Newcastle",
    "WOL": "Wollongong",
    "MAN": "Manly",
    "PARR": "Parramatta",
    "ROCK": "Rockingham",
    "JOON": "Joondalup",
    "FREM": "Fremantle",
    # NZ codes
    "AKL": "Auckland",
    "WLG": "Wellington",
    "CHC": "Christchurch",
}

# Separators used between campus codes: " | ", " / ", ", ", "-"
_CAMPUS_CODE_SEP_RE = re.compile(r"\s*[|/,\-–—]\s*")


def _expand_campus_codes(text: str) -> str:
    """Replace campus short codes with full city names.

    Handles "SYD | MEL | BNE" → "Sydney, Melbourne, Brisbane".
    Leaves values that are already full city names unchanged.
    Only expands when EVERY non-empty token is a known code or a
    recognised city name — avoids mangling arbitrary text that
    happens to contain a 3-letter substring.
    """
    if not text:
        return text
    parts = [p.strip() for p in _CAMPUS_CODE_SEP_RE.split(text) if p.strip()]
    if not parts or len(parts) < 2:
        # Single token: try a direct code lookup but only apply if it's a pure code
        single = text.strip().upper()
        if single in _CAMPUS_CODE_MAP:
            return _CAMPUS_CODE_MAP[single]
        return text

    expanded: list[str] = []
    all_known = True
    for part in parts:
        upper = part.upper()
        if upper in _CAMPUS_CODE_MAP:
            expanded.append(_CAMPUS_CODE_MAP[upper])
        else:
            # Already a city name or unknown token — keep as-is
            expanded.append(part)
            # If it's not a recognised city and not a code, mark as "unknown"
            # so we don't blindly expand partial matches.
            if part not in _COMMON_CITIES:
                all_known = False

    # Only substitute when all tokens were resolved (either code→city or
    # already a city name). If we see truly unknown tokens the input is
    # probably not a code-list and should be left unchanged.
    if all_known:
        # De-dup while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for city in expanded:
            if city.lower() not in seen:
                seen.add(city.lower())
                out.append(city)
        return ", ".join(out)
    return text

_PERIOD_LABEL_RE = re.compile(
    r"^(?:Semester|Trimester|Term|Quarter|S|T)\s*\d+"
    r"(?:\s*[-–—]\s*.{0,50})?$",
    re.I,
)


_CHECKMARK_CHARS = frozenset("✓✔✅√☑")
_CROSS_CHARS = frozenset("✗✘✕✖❌")
_AVAIL_KEYWORDS = frozenset(("available", "yes", "tick", "check", "offered", "offered here"))
_UNAVAIL_KEYWORDS = frozenset(("not available", "no", "cross", "unavailable"))
_AVAIL_CLASS_FRAGMENTS = ("check", "tick", "yes", "available", "success", "positive", "offered")
_UNAVAIL_CLASS_FRAGMENTS = ("cross", "no-", "unavailable", "not-available", "negative")


def _cell_availability(td) -> str:
    """Return 'yes', 'no', or 'unknown' based on icon/aria-label/text in a table cell."""
    text = td.get_text(strip=True)
    if any(c in text for c in _CHECKMARK_CHARS):
        return "yes"
    if any(c in text for c in _CROSS_CHARS):
        return "no"
    for el in td.find_all(True):
        label = (el.get("aria-label") or el.get("title") or "").lower().strip()
        # Check unavailable FIRST — "not available" contains "available" as
        # a substring so we must reject the negative case before the positive.
        if any(k in label for k in _UNAVAIL_KEYWORDS):
            return "no"
        if any(k in label for k in _AVAIL_KEYWORDS):
            return "yes"
        cls_str = " ".join(el.get("class") or []).lower()
        if any(f in cls_str for f in _UNAVAIL_CLASS_FRAGMENTS):
            return "no"
        if any(f in cls_str for f in _AVAIL_CLASS_FRAGMENTS):
            return "yes"
    return "unknown"


def _looks_marketing(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if _MARKETING_HINTS.search(t):
        return True
    return len(t.split()) > 16


_NON_LOCATION_PHRASES: frozenset[str] = frozenset({
    # Delivery / mode / format labels — never a location.
    "delivery method",
    "delivery mode",
    "delivery format",
    "study mode",
    "attendance mode",
    "attendance pattern",
    "mode of study",
    "mode of delivery",
    # Action verbs / button labels picked up by sloppy DOM walks.
    "view dates",
    "view date",
    "view all",
    "start",
    "start date",
    "start dates",
    "starts",
    "apply",
    "apply now",
    "enquire",
    "enquire now",
    "enrol",
    "enrol now",
    "save",
    "saved",
    "compare",
    "more info",
    # Audience / fee-type labels — also not a location.
    "domestic",
    "international",
    "domestic students",
    "international students",
    "domestic and international",
})


def _is_only_delivery_method(text: str) -> bool:
    """True when ``text`` reduces to nothing once delivery-method words
    (online / virtual / remote / external / ...) and punctuation are
    stripped, OR the text exactly matches one of the non-location
    phrases (Delivery method, View dates, Start, Apply, Domestic,
    International, ...).

    Defence in depth so a location field never gets saved as
    "Online" / "External" / "Delivery method" / "View dates" / "Start"
    / "Apply" / "Domestic" / "International" / etc., regardless of which
    extractor cascade method produced the value.
    """
    if not text:
        return True
    # Pass 1 — exact-phrase rejection (case-insensitive, normalised
    # whitespace).  Catches single-word labels like "Start" / "Apply"
    # that survive _REMOVE_VIRTUAL stripping below.
    norm = re.sub(r"\s+", " ", text).strip().lower().rstrip(":")
    if norm in _NON_LOCATION_PHRASES:
        return True
    # Pass 2 — strip delivery-mode tokens and check what's left.
    stripped = _REMOVE_VIRTUAL.sub("", text)
    stripped = re.sub(r"[\s,;/&\-–—]+", "", stripped).strip()
    return not stripped


def _normalise(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).replace(" , ", ", ").strip()
    # Expand campus short-codes (e.g. "SYD | MEL | BNE" → "Sydney, Melbourne, Brisbane")
    # before any marketing / junk checks so the expanded text can be validated normally.
    cleaned = _expand_campus_codes(cleaned)
    if _looks_marketing(cleaned):
        return None
    head = _TRAILING_KEYS.split(cleaned, maxsplit=1)[0].strip() or cleaned
    if len(head) <= 2 or "<" in head or ">" in head:
        return None
    if _JUNK.search(head):
        return None
    # Reject bare period/semester labels (e.g. "Semester 1", "Trimester 2") —
    # these appear as ECU-style pivot-table column headers and must never be
    # returned as a campus location.
    if _PERIOD_LABEL_RE.match(head):
        return None
    # Phase A.5 — never accept a value that is only delivery-method
    # words.  Stops "Online" / "External" / "Online, Distance" / etc.
    # from being saved as a course location even if a future cascade
    # method bypasses the _sanitise_for_display strip.
    if _is_only_delivery_method(head):
        return None
    return head[:120]


def _sanitise_for_display(raw: str | None) -> str | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip() and not _REMOVE_VIRTUAL.search(p)]
    if parts:
        # de-dup preserving order
        seen: set[str] = set()
        out: List[str] = []
        for p in parts:
            k = p.lower()
            if k not in seen:
                seen.add(k)
                out.append(p)
        return ", ".join(out)
    cleaned = _REMOVE_VIRTUAL.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(", ").strip()
    return cleaned or None


# Mirrors `study_mode._extract_strong_label_value`: a structural
# pre-pass that walks the DOM looking for `<strong>Location</strong>`
# style labels and reads the value out of the next text nodes /
# sibling cells. The existing `_from_dl` and `_from_tables` already
# cover `<dt>/<dd>` and `<th>/<td>`; this catches the ASA-style
# adjacent-div idiom (`<div><strong>Location</strong></div><div>
# Sydney</div>`) where the value lives in the parent's next sibling
# rather than the strong tag's own next sibling — `_from_headings`
# misses that because it walks `find_next_sibling()` on the strong
# tag only and never bubbles up to the parent.
_LOCATION_LABEL_TAG_RE = re.compile(
    r"(?:campus(?:\s+locations?)?|locations?|"
    r"where\s+(?:can\s+)?(?:i|you)\s+study|"
    r"delivery\s+location)",
    re.IGNORECASE,
)
_STRONG_VALUE_CHAR_CAP = 300


def _classify_location_value(value: str) -> str | None:
    """Run the value text through the existing normalise/sanitise
    pipeline so the structural pre-pass returns the same shape as
    the rest of the cascade. Returns ``None`` when the value is
    rejected (marketing copy, junk, virtual-only)."""
    normalised = _normalise(value)
    if not normalised:
        return None
    display = _sanitise_for_display(normalised)
    if not display:
        return None
    return display


def _from_strong_dom_walk(soup: BeautifulSoup) -> str | None:
    """Structural pre-pass for `<strong>Location</strong>` /
    `<b>Campus</b>` idioms whose value lives in the parent's next
    sibling element. Walks forward from the strong/b tag in document
    order until the next labelled boundary, mirroring
    `study_mode._extract_strong_label_value`."""
    try:
        from bs4.element import NavigableString, Tag
    except ImportError:  # pragma: no cover - bs4 is a hard dep
        return None
    for label_tag in soup.find_all(("strong", "b")):
        label_raw = label_tag.get_text(" ", strip=True).rstrip(":").strip()
        if not label_raw or not _LOCATION_LABEL_TAG_RE.fullmatch(label_raw):
            continue
        # Skip the label tag's own descendants (its own text would
        # otherwise be appended in front of the value, e.g.
        # `Location Sydney` for `<strong>Location</strong>` followed
        # by `<div>Sydney</div>`). The other extractors' classifiers
        # ignore unknown leading words by design, so they're fine
        # without this guard — for location the label word can look
        # exactly like a city name to the normaliser.
        descendant_ids = {id(d) for d in label_tag.descendants}
        parts: list[str] = []
        char_count = 0
        for node in label_tag.next_elements:
            if isinstance(node, Tag):
                if node is label_tag or id(node) in descendant_ids:
                    continue
                if node.name in ("strong", "b", "h1", "h2", "h3",
                                 "h4", "h5", "h6", "dt", "th",
                                 "tr"):
                    break
                continue
            if isinstance(node, NavigableString):
                if id(node) in descendant_ids:
                    continue
                text = str(node).strip()
                if not text:
                    continue
                parts.append(text)
                char_count += len(text) + 1
                if char_count >= _STRONG_VALUE_CHAR_CAP:
                    break
        if not parts:
            continue
        value_text = " ".join(parts).lstrip(":-– ").strip()
        if not value_text:
            continue
        v = _classify_location_value(value_text)
        if v:
            return v
    return None


def _from_dl(soup: BeautifulSoup) -> str | None:
    for dt in soup.find_all("dt"):
        if not LOCATION_LABEL.match(dt.get_text(strip=True)):
            continue
        dd = dt.find_next_sibling("dd")
        if dd:
            v = _normalise(dd.get_text(" ", strip=True))
            if v:
                return v
    return None


def _from_tables(soup: BeautifulSoup) -> str | None:
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        if not LOCATION_LABEL.match(cells[0].get_text(strip=True)):
            continue
        c1_text = cells[1].get_text(strip=True)

        # ECU / UNE-style pivot table:
        #   ECU: "Location | Semester 1 | Semester 2"
        #   UNE: "Start dates and campus | Trimester 1 – Feb 2026 | …"
        # The header row's second+ cells are period/date labels.  Real campus
        # names live in the first column of subsequent data rows.  We use
        # _cell_availability() to detect checkmarks (unicode, aria-label, or
        # CSS class) so that only campuses with at least one available
        # trimester/semester are included.  When no availability signal is
        # detected (icons totally opaque) we include ALL non-Online rows to
        # avoid returning null and falling through to the city-text extractor.
        if _PERIOD_LABEL_RE.match(c1_text):
            parent_table = tr.find_parent("table")
            if not parent_table:
                continue
            header_col_count = len(cells)
            locations: list[str] = []
            seen_locs: set[str] = set()
            for data_tr in parent_table.find_all("tr"):
                dcells = data_tr.find_all(["th", "td"])
                # Skip the header row itself
                if not dcells:
                    continue
                if LOCATION_LABEL.match(dcells[0].get_text(strip=True)):
                    continue
                # Skip group-header rows spanning multiple columns
                try:
                    if int(dcells[0].get("colspan") or 1) > 1:
                        continue
                except (ValueError, TypeError):
                    pass
                # Accept only rows that span the full table width
                # (data rows vs single-cell sub-section headers)
                if len(dcells) < max(2, header_col_count - 1):
                    continue
                # Detect availability: at least one value cell must be
                # available (or availability is unknown — icon not parseable).
                statuses = [
                    _cell_availability(dcells[i])
                    for i in range(1, len(dcells))
                ]
                has_yes = any(s == "yes" for s in statuses)
                all_no = all(s == "no" for s in statuses)
                all_unknown = all(s == "unknown" for s in statuses)
                # Skip rows where we can confirm all periods are unavailable
                if all_no:
                    continue
                # Include if available, or if detection is fully opaque
                if not (has_yes or all_unknown):
                    continue
                loc_text = dcells[0].get_text(strip=True)
                # Exclude "Online *" / "Online only" rows from physical campus list
                if _REMOVE_VIRTUAL.search(loc_text):
                    continue
                if loc_text and loc_text.lower() not in seen_locs:
                    seen_locs.add(loc_text.lower())
                    locations.append(loc_text)
            if locations:
                v = _normalise(", ".join(locations))
                if v:
                    return v
            continue  # don't fall through to the normal single-cell path

        v = _normalise(c1_text)
        if v:
            return v
    return None


def _from_headings(soup: BeautifulSoup) -> str | None:
    for el in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "label"]):
        label = compact(el.get_text(" ", strip=True))
        if not LOCATION_LABEL.match(label):
            continue
        nxt = el.find_next_sibling()
        candidate: str | None = None
        if nxt is None:
            continue
        if nxt.name == "p":
            candidate = compact(nxt.get_text(" ", strip=True))
        elif nxt.name in ("ul", "ol"):
            items = [compact(li.get_text(" ", strip=True)) for li in nxt.find_all("li")]
            candidate = ", ".join(filter(None, items))
        else:
            candidate = compact(nxt.get_text(" ", strip=True))
        v = _normalise(candidate)
        if v:
            return v
    return None


# Pattern used by Flinders University (and similar) to encode the
# delivery campus inside the delivery-mode field:
#   <div class="international_content_marker">In person (Bedford Park, City)</div>
# We extract the campus name(s) from inside the parentheses.
_IN_PERSON_RE = re.compile(r"\bIn\s+person\s*\(([^)]+)\)", re.I)


def _from_delivery_mode_inperson(soup: BeautifulSoup) -> str | None:
    """Extract campus from 'In person (Campus, ...)' delivery-mode markers.

    First preference: elements with class ``international_content_marker``
    (Flinders, and others that distinguish domestic vs international delivery).
    Second preference: any element whose text matches the pattern.
    """
    # Prefer international-specific elements
    for cls in ("international_content_marker", "delivery_mode"):
        for el in soup.find_all(class_=cls):
            text = el.get_text(separator=" ", strip=True)
            m = _IN_PERSON_RE.search(text)
            if m:
                campuses_raw = m.group(1)
                # Split on commas, strip, filter blanks
                parts = [p.strip() for p in campuses_raw.split(",") if p.strip()]
                # Drop pure "Online" / "remote" variants
                parts = [p for p in parts if not _REMOVE_VIRTUAL.search(p)]
                if parts:
                    return _normalise(", ".join(parts))
    return None


def _from_text_block(text: str) -> str | None:
    text = compact(text)
    if not text:
        return None
    m = _LOCATION_WINDOW.search(text)
    window = m.group(1) if m else text
    matched = [c for c in _COMMON_CITIES if re.search(rf"\b{re.escape(c)}\b", window, re.I)]
    if matched:
        seen: set[str] = set()
        out: List[str] = []
        for c in matched:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return _normalise(", ".join(out))
    return _normalise(window.replace(" / ", ", "))


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cascade = (
        # Structural pre-pass FIRST — see _from_strong_dom_walk for the
        # rationale. Reads `<strong>Location</strong>` style values out
        # of the DOM directly, including the ASA-style adjacent-div
        # idiom that the heading walker misses.
        ("strong", _from_strong_dom_walk(soup), 0.9),
        ("dl", _from_dl(soup), 0.9),
        ("table", _from_tables(soup), 0.85),
        ("heading", _from_headings(soup), 0.7),
        # "In person (CampusName)" delivery-mode pattern (Flinders, etc.)
        ("delivery_inperson", _from_delivery_mode_inperson(soup), 0.85),
        ("text_block", _from_text_block(html_to_text(html)), 0.5),
    )
    for method, raw, conf in cascade:
        if not raw:
            continue
        display = _sanitise_for_display(raw)
        if not display:
            continue
        # Append country suffix when all tokens are unambiguous AU/NZ cities.
        display = _append_country_suffix(display)
        return [
            ExtractionResult(
                field_key="course_location",
                value=display,
                normalized={"course_location": display},
                confidence=conf,
                method=f"location.{method}",
                snippet=raw[:200],
            )
        ]
    return []

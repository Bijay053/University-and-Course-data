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
    r"where\s+(?:can\s+)?(?:i|you)\s+study|delivery\s+location|"
    r"available\s+at)\s*[*†^‡]?\s*:?\s*$",
    re.I,
)
# Strips trailing footnote markers (*, †, ^, ‡) before matching LOCATION_LABEL.
_FOOTNOTE_TRAILER_RE = re.compile(r"[*†^‡\u2020\u2021]+$")
_MARKETING_HINTS = re.compile(
    r"\b(?:focuses on|knowledge and skills|this (?:course|program|degree|qualification)|our (?:courses?|programs?))\b"
    r"|study\s+in\s+our\b"
    r"|(?:£|€|\$)\s*\d+(?:\.\d+)?\s*(?:m(?:illion)?|bn|billion)\b"
    r"|(?:state[-\s]of[-\s]the[-\s]art|world[-\s]class)\s+facilit"
    r"|town[-\s]cent(?:re|er)\s+campus"
    r"|million(?:\s+pound)?\s+(?:invest|redevelop|campus|facilit)"
    r"|friendly\s+(?:town|city|campus)"
    r"|\d+m\s+(?:invest|redevelop|campus|facilit)",
    re.I,
)
_JUNK = re.compile(
    r"\b(?:https?://|www\.|src=|href=|style=|googletagmanager|qtac|satac|cricos|step\s*\d+\s*of|student\s*type|fee\s*type|study\s*mode|reset\s*fee\s*calculator)\b",
    re.I,
)
_NON_LOCATION_VALUE_RE = re.compile(
    r"^(?:(?:domestic|international|home|overseas|students?|"
    r"teaching|study|academic|period|online|virtual|remote|external)\s*)+$"
    r"|^(?:may|might|some|please|select|choose|view|see)\b",
    re.IGNORECASE,
)
_TRAILING_KEYS = re.compile(
    r"\b(?:delivery\s*mode|delivery\s*method|study\s*mode|course\s*structure|intakes?|course\s*length|duration|cricos\s*code|fees?"
    r"|view\s+dates|start\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|scroll\s+to\s+top)\b",
    re.I,
)
# Strip leading section markers injected by some CMSes into page text.
# Canterbury's Contensis CMS emits "(s)" before section headings which
# bleeds into Gemini's location_text extraction: "(s)Canterbury Scroll to top".
_LEADING_SECTION_MARKER_RE = re.compile(r"^\([a-z]\)\s*", re.I)

# Strip institutional label prefixes that some CMSes (e.g. Wolverhampton)
# prepend to campus names.  Gemini faithfully copies these from the page,
# producing values like "University: City Campus" or "University:".
# Applied per comma-separated part so "University: City Campus,
# University: Springfield Campus" → "City Campus, Springfield Campus".
_INST_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:university|college|institution|campus)\s*:\s*",
    re.IGNORECASE,
)
_REMOVE_VIRTUAL = re.compile(
    r"\b(?:online|virtual|remote|distance(?:\s*learning)?|off[-\s]?campus|external)\b",
    re.I,
)

# Navigation / menu text that is NEVER a campus name.
# These keywords appear in site-wide header/footer navigation blocks that
# sometimes bleed into location extraction when the page structure is flat
# (e.g. JCU Wayback HTML).  If the extracted string contains any of these,
# discard it entirely rather than storing garbage as a campus location.
_NAV_TEXT_LOCATION_RE = re.compile(
    r"\b(?:scholarship|global\s+rankings?|accessibility\s+support|"
    r"admissions?\s+and\s+entry|apply\s+to\s+\w|entry\s+options?|"
    r"application\s+due\s+dates?|how\s+to\s+apply|"
    r"fees?\s+calculator|student\s+(?:life|hub|portal|login)|"
    r"international\s+students?\s+home|visit\s+(?:us|the\s+campus)|"
    r"career\s+(?:outcomes?|services?)|related\s+(?:courses?|programs?)|"
    r"contact\s+us|news\s+and\s+events|open\s+day|"
    r"research\s+(?:degrees?|programs?)|find\s+a\s+course|"
    r"ucas\s*(?:code|tariff|points?)|enrolments?)\b",
    re.IGNORECASE,
)
# Country names that sometimes appear as standalone comma-split parts in a
# location string (e.g. "Sydney, Melbourne, Brisbane, Australia").  They are
# not campus names and must be stripped from the parts list in
# _sanitise_for_display.  _append_country_suffix may add a country suffix later
# when appropriate — but only when the raw string did NOT already carry one
# (see cascade guard using _COUNTRY_WORD_IN_RAW_RE below).
_COUNTRY_NAME_PARTS_LC: frozenset[str] = frozenset({
    "australia", "new zealand", "nz",
    "uk", "united kingdom",
    "usa", "united states", "united states of america",
    "canada", "india", "china", "malaysia", "singapore",
})
_COUNTRY_WORD_IN_RAW_RE = re.compile(
    r"\b(?:australia|new\s+zealand|united\s+kingdom|united\s+states)\b",
    re.I,
)
# JCU pages render "Course available at" as an uppercase header followed by
# "Notes" and then space-separated "JCU {Campus}" tokens (no commas).
# Example raw value: "COURSE AVAILABLE AT NOTES JCU Townsville JCU Cairns"
# Clean this into a normal comma-separated campus list.
_COURSE_AVAIL_NOTES_RE = re.compile(
    r"^course\s+available\s+at\s+notes?\s*", re.IGNORECASE
)
_JCU_CAMPUS_TOKEN_RE = re.compile(
    r"\bJCU\s+([A-Za-z][A-Za-z]+(?: [A-Za-z][A-Za-z]+)?)", re.IGNORECASE
)

_LOCATION_WINDOW = re.compile(
    r"\b(?:(?:campus\s+)?locations?|available\s+at)\s*[:\-]?\s*([^\n]{0,220}?)"
    r"(?=\b(?:intakes?|duration|fees?|student\s*type|learning\s*mode|study\s*modes?|delivery|attendance)\b|$)",
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
    r"^(?:(?:Semester|Trimester|Term|Quarter|S|T)\s*\d+|"
    r"(?:Teaching|Study|Academic)\s+Period(?:\s*\d+)?)"
    r"(?:\s*[-–—]\s*.{0,50})?$",
    re.I,
)

# UTAS (and similar) panel divs concatenate availability schedules onto campus
# names in a single text node:
#   "Hobart Semester 1, Semester 2 Launceston Semester 1"
# Each matched label (with its optional leading comma+space) is replaced with
# a § sentinel so that multi-word campus names like "Cradle Coast" survive
# intact while the labels act as campus-name separators.
_PERIOD_ANY_RE = re.compile(
    r",?\s*\b(?:"
    r"Half\s+Year\s+Period\s*\d+|"
    r"Semester\s*\d+|"
    r"Trimester\s*\d+|"
    r"Term\s*\d+|"
    r"Quarter\s*\d+|"
    r"Study\s+Period\s*\d*|"
    r"Teaching\s+Period\s*\d*|"
    r"Academic\s+Period\s*\d*|"
    # Research Period N — used by UTAS for HDR / postgrad research courses
    # e.g. "Hobart Research Period 1, Research Period 2" → "Hobart"
    r"Research\s+Period\s*\d*|"
    # Season-based study periods (UTAS uses "Spring", "Summer" etc. without
    # a number: "Launceston, Spring" → the season is a study-period label,
    # not a second campus name).  Allow an optional trailing year.
    r"Spring(?:\s+\d{4})?|"
    r"Summer(?:\s+\d{4})?|"
    r"Autumn(?:\s+\d{4})?|"
    r"Winter(?:\s+\d{4})?"
    r")\b",
    re.IGNORECASE,
)


def _strip_period_labels(text: str) -> str:
    """Strip inline period/semester availability labels from a location string.

    Returns the cleaned campus-name string, or an empty string when the entire
    text was availability labels (e.g. "Study Period" with no campus name).

    Uses a § (U+00A7) sentinel to preserve multi-word campus names while
    turning each label into a campus-name boundary::

        "Hobart Semester 1, Semester 2 Launceston Semester 1"
        → "Hobart, Launceston"

        "Cradle Coast Semester 1 Hobart Semester 1, Semester 2 Launceston"
        → "Cradle Coast, Hobart, Launceston"

        "Study Period"
        → ""  (caller should return None)
    """
    replaced = _PERIOD_ANY_RE.sub("\u00a7", text)
    parts = [p.strip().strip(",").strip() for p in replaced.split("\u00a7")]
    parts = [p for p in parts if p]
    return ", ".join(parts)


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
    # Bare "Mode" label (Wolverhampton CMS emits this as a standalone value
    # adjacent to the delivery-method section — not a campus name).
    "mode",
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
    "local students",
    "local student",
    "domestic and international",
    # Fee-table column headers and UI chrome that bleed into location extraction.
    # Malaysian universities (INTI, etc.) use "Campus | Local Students | International Students"
    # as a fee-table header row; the DOM walk picks up "Local Students" as a campus name.
    "select a campus",
    "select campus",
    "choose campus",
    "all campuses",
    "campus",
    # UTAS "Study period" panel value — availability label, not a campus name.
    "study period",
    # Newcastle (and similar) sidebar headings that the DOM-walk pass would
    # otherwise capture as a location when no real campus label is present.
    # The "Admission info" panel header sits next to selection rank / fees /
    # start dates and was being staged as course_location="Admission info"
    # or "Admission info Selection rank" on every Newcastle course where the
    # rule pass found no campus chip (2026-05-15 fleet-wide bug — Master of
    # Nurse Practitioner, Master of Midwifery, Diploma in Information
    # Technology, Diploma in Media and Visual Communication, etc.).
    "admission info",
    "admission info selection rank",
    "selection rank",
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
    # Strip leading CMS section markers like "(s)" that Contensis / similar
    # CMSes inject into page headings.  These bleed into Gemini's location_text
    # as "(s)Canterbury Scroll to top" — strip before any other cleaning.
    raw = _LEADING_SECTION_MARKER_RE.sub("", raw)
    cleaned = re.sub(r"\s+", " ", raw).replace(" , ", ", ").strip()
    if _NON_LOCATION_VALUE_RE.match(cleaned):
        return None
    # Normalise slash-separated city lists to comma-separated.
    # KBS (and some others) publish location as "Adelaide / Brisbane / Melbourne /".
    # _from_text_block already does this via window.replace(" / ", ", "); applying
    # it here ensures all cascade paths (dl, table, strong, headings) see the same
    # normalised form.  Strip any trailing slash/comma/space left by the conversion.
    if " / " in cleaned:
        cleaned = re.sub(r"\s+/\s+", ", ", cleaned).strip(" ,/")
    # Strip inline period/semester availability labels before any other checks.
    # UTAS panel divs concatenate schedule onto campus names:
    #   "Hobart Semester 1, Semester 2 Launceston Semester 1" → "Hobart, Launceston"
    #   "Study Period" (no campus at all) → "" → return None below
    cleaned = _strip_period_labels(cleaned)
    if not cleaned:
        return None
    # Expand campus short-codes (e.g. "SYD | MEL | BNE" → "Sydney, Melbourne, Brisbane")
    # before any marketing / junk checks so the expanded text can be validated normally.
    cleaned = _expand_campus_codes(cleaned)
    # Strip institutional label prefixes (e.g. "University: City Campus" → "City Campus").
    # Some CMSes (Wolverhampton) prefix every campus name with "University:".
    # Gemini faithfully copies the label, producing values like:
    #   "University: City Campus, University: Springfield Campus"
    # Apply per comma-separated part and drop any parts that become empty
    # (e.g. bare "University:" with no campus name following it).
    if _INST_LABEL_PREFIX_RE.search(cleaned):
        stripped_parts = [
            _INST_LABEL_PREFIX_RE.sub("", p).strip()
            for p in cleaned.split(",")
        ]
        stripped_parts = [p for p in stripped_parts if p]
        cleaned = ", ".join(stripped_parts)
        if not cleaned:
            return None
    # JCU "COURSE AVAILABLE AT NOTES JCU Townsville JCU Cairns" → "Townsville, Cairns"
    if _COURSE_AVAIL_NOTES_RE.match(cleaned):
        cities = _JCU_CAMPUS_TOKEN_RE.findall(cleaned)
        cleaned = ", ".join(cities) if cities else _COURSE_AVAIL_NOTES_RE.sub("", cleaned).strip()
    # Generic "JCU Townsville" → "Townsville" (e.g. when the full label isn't present
    # but the campus is prefixed with the university short code).
    cleaned = _JCU_CAMPUS_TOKEN_RE.sub(r"\1", cleaned).strip(", ")
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
    # Reject navigation / menu text captured from page headers or footers.
    # Any location longer than 80 chars that also contains a navigation
    # keyword is guaranteed to be nav text, not a campus.  Discard it so
    # garbage like "Global rankings Scholarships Accessibility support …"
    # never reaches the staging queue.
    if _NAV_TEXT_LOCATION_RE.search(head):
        return None
    if len(head) > 80 and head.count(",") < 3:
        # Long string with few comma-separated campus names = likely a
        # prose sentence or nav block, not a location list.
        return None
    return head[:120]


_CAMPUS_AVAILABILITY_SUFFIX_RE = re.compile(
    r"\s+(?:not\s+(?:offered|available|applicable)|not|available|offered|applicable)\s*$",
    re.IGNORECASE,
)


def _sanitise_for_display(raw: str | None) -> str | None:
    if not raw:
        return None
    if _NON_LOCATION_VALUE_RE.match(raw):
        return None
    # Strip CMS section markers (e.g. "(s)") and trailing page-chrome
    # suffixes (e.g. "Scroll to top") before any other processing so that
    # Gemini-sourced location_text values like "(s)Canterbury Scroll to top"
    # are cleaned even when _normalise() was not called first.
    raw = _LEADING_SECTION_MARKER_RE.sub("", raw).strip()
    raw = _TRAILING_KEYS.split(raw, maxsplit=1)[0].strip() or raw
    if not raw:
        return None
    if raw.lower().strip(" :") in _FIELD_LABEL_TOKENS:
        return None
    # Reject bare delivery-method / non-location labels (e.g. "Mode") before
    # any further processing so they never reach the staging queue regardless
    # of which extraction path produced them (structural, Gemini, AI fallback).
    if _is_only_delivery_method(raw):
        return None
    # Strip institutional label prefixes that some CMSes prepend to campus
    # names.  Gemini faithfully copies these, producing values like
    # "University: City Campus" or "University: City, University: Walsall".
    # Apply per comma-separated part BEFORE the split loop so that multi-
    # campus strings are cleaned as a unit.  Empty parts (bare "University:")
    # are dropped.
    if _INST_LABEL_PREFIX_RE.search(raw):
        _pre_parts = [_INST_LABEL_PREFIX_RE.sub("", p).strip() for p in raw.split(",")]
        _pre_parts = [p for p in _pre_parts if p]
        raw = ", ".join(_pre_parts)
        if not raw:
            return None
    # Strip trailing availability qualifiers from each campus part before
    # any other processing.  JCU course pages render availability as a table
    # whose cells end up as "Townsville Not offered", "Cairns Not", etc. after
    # html_to_text flattening.  Stripping these suffixes avoids storing
    # "Townsville Not" as the campus location.
    def _strip_avail(part: str) -> str:
        return _CAMPUS_AVAILABILITY_SUFFIX_RE.sub("", part).strip()

    # Build the parts list by stripping virtual-delivery tokens from within
    # each comma-separated segment rather than discarding the whole segment.
    # Without this, a string like "Bristol, London Moorgate and Online" only
    # yields "Bristol": the second comma-part ("London Moorgate and Online")
    # matches _REMOVE_VIRTUAL and was previously dropped wholesale, losing
    # the valid physical campus "London Moorgate".
    parts = []
    for _raw_part in raw.split(","):
        _q = _REMOVE_VIRTUAL.sub("", _raw_part)
        # Strip dangling conjunctions left after removing a virtual token at
        # the end ("London Moorgate and ") or the start (" and London Moorgate").
        _q = re.sub(r"\s*\b(?:and|or)\b\s*$", "", _q, flags=re.IGNORECASE)
        _q = re.sub(r"^\s*\b(?:and|or)\b\s*", "", _q, flags=re.IGNORECASE)
        _q = _strip_avail(_q.strip())
        if _q and _q.lower() not in _COUNTRY_NAME_PARTS_LC:
            parts.append(_q)
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
    if not cleaned or cleaned.lower() in _COUNTRY_NAME_PARTS_LC:
        return None
    return cleaned


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
    r"delivery\s+location|available\s+at)",
    re.IGNORECASE,
)
_STRONG_VALUE_CHAR_CAP = 300


_FIELD_LABEL_TOKENS: frozenset[str] = frozenset({
    # Field-label words that can appear as raw DOM text immediately after
    # a <strong>Location</strong> tag when the adjacent cell is empty or
    # when the DOM walker accidentally captures the next label instead of
    # the value.  Rejecting these prevents literal "Location", "Duration",
    # "Fees" etc. from being stored as a campus location (ACU Bug 2).
    "location",
    "locations",
    "campus",
    "campuses",
    "duration",
    "fees",
    "fee",
    "intake",
    "intakes",
    "entry",
    "delivery",
    "mode",
    "study",
    "type",
    "course",
    "length",
    "domestic",
    "international",
    "teaching period",
    "study period",
    "academic period",
})


def _classify_location_value(value: str) -> str | None:
    """Run the value text through the existing normalise/sanitise
    pipeline so the structural pre-pass returns the same shape as
    the rest of the cascade. Returns ``None`` when the value is
    rejected (marketing copy, junk, virtual-only, or a bare field
    label word like "Location" / "Duration")."""
    normalised = _normalise(value)
    if not normalised:
        return None
    # Reject single-word (or single-phrase) values that are just field
    # label tokens — these arise when the DOM walker captures the next
    # adjacent label element instead of the actual location value
    # (e.g. ACU pages: "Offered at 0 locations Location Duration …").
    if normalised.lower().strip() in _FIELD_LABEL_TOKENS:
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
        from bs4.element import Comment, NavigableString, Tag
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
                # Skip HTML comments — they're a NavigableString subclass so
                # the previous isinstance(NavigableString) check accepted
                # them.  London Metropolitan University (uni 6, 2026-05-13)
                # placed an author comment between the <strong>Location</strong>
                # label and the value div:
                #   <strong>Location</strong>
                #   <!-- display after selection, but leave  t4 content here for SEO -->
                #   <div class="variable-data-item"><a>London Metropolitan University</a></div>
                # which leaked the SEO note into the location value as
                # "display after selection, but leave t4 content here for SEO
                # London Metropolitan University".
                if isinstance(node, Comment):
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
        is_header_row = bool(tr.find_parent("thead")) or all(
            cell.name == "th" for cell in cells
        )

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

        # Generic table headers such as
        #   Location | Domestic | International
        # describe columns; they are never campus values. Real key/value rows
        # use a <th>/<td> shape and continue to the normal path below.
        if is_header_row:
            continue

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
            # Collect ALL consecutive <p> siblings and normalise each one
            # individually before joining.  Some CMSes (e.g. Oxford Brookes)
            # render each location on its own <p> tag:
            #   <p>Distance learning</p>
            #   <p>City of Oxford College (part of Activate Learning)</p>
            #   <p>Reading College (part of Activate Learning)</p>
            # When the first <p> is a virtual-only delivery method ("Distance
            # learning"), _normalise() correctly returns None for that part.
            # Joining all parts first and then calling _normalise() can fail
            # the `len > 80 and commas < 3` guard even when the individual
            # physical campus names are valid.  Normalising per-part and
            # joining the survivors avoids that false rejection.
            normed_parts: list[str] = []
            sib = nxt
            while sib is not None and getattr(sib, "name", None) == "p":
                t = compact(sib.get_text(" ", strip=True))
                if t:
                    v_part = _normalise(t)
                    if v_part:
                        normed_parts.append(v_part)
                sib = sib.find_next_sibling()
            if normed_parts:
                return ", ".join(normed_parts)
            continue
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


def _from_newcastle_toggles(soup: BeautifulSoup) -> str | None:
    """Newcastle (newcastle.edu.au) ``Study location`` radio toggle group.

    Newcastle's per-degree page has no labelled "Location:" / "Campus:" cell.
    Instead the campus list is rendered as an HTML radio-button group::

        <h6>Study location</h6>
        <div class="uon-option-toggles" id="degree-location-toggles">
          <div class="uon-option-toggle">
            <input type="radio" id="degree-location-online"
                   data-display-label="Online">
            <label for="degree-location-online">Online</label>
          </div>
          <div class="uon-option-toggle">
            <input type="radio" id="degree-location-newcastle"
                   data-display-label="Newcastle">
            <label for="degree-location-newcastle">Newcastle</label>
          </div>
        </div>

    Without a structural reader for this idiom, the location extractor
    cascade returned None for every Newcastle Master / Diploma course
    that did not happen to expose a campus name elsewhere in the DOM
    (observed 2026-05-15: Master of Nursing, Master of Mental Health
    Nursing, etc. all staged with course_location blank).

    We prefer the ``data-display-label`` attribute on each radio input
    (the canonical, locale-stable label) and fall back to the visible
    ``<label>`` text if the attribute is missing.

    Pure "Online" entries are KEPT in the comma-joined raw value here —
    downstream ``_sanitise_for_display`` strips them via
    ``_REMOVE_VIRTUAL`` so courses with both Online + a physical campus
    correctly stage just the physical campus, while online-only courses
    return None (campus-less) so the ``online_only`` guard in
    ``guards.py`` can evaluate them.

    Returns ``None`` when the ``#degree-location-toggles`` div is absent
    or has no toggle labels.
    """
    container = soup.find(id="degree-location-toggles")
    if container is None:
        # Newcastle sometimes wraps the toggles in a different parent;
        # fall back to a class-based search for the toggle items directly.
        toggles = soup.select(".uon-option-toggles .uon-option-toggle")
        if not toggles:
            return None
    else:
        toggles = container.select(".uon-option-toggle")
        if not toggles:
            # The container exists but uses a flat layout; treat the
            # container's direct radio inputs as the toggle list.
            toggles = container.find_all("input", attrs={"type": "radio"})
    items: list[str] = []
    for tog in toggles:
        # Prefer the radio input's data-display-label attribute.
        label = None
        if tog.name == "input":
            label = tog.get("data-display-label")
        else:
            inp = tog.find("input")
            if inp is not None:
                label = inp.get("data-display-label")
            if not label:
                lab_el = tog.find("label")
                if lab_el is not None:
                    label = lab_el.get_text(" ", strip=True)
        if not label or not isinstance(label, str):
            continue
        label = compact(label)
        if not label:
            continue
        items.append(label)
    if not items:
        return None
    # De-dup while preserving order (Newcastle pages occasionally repeat
    # the same label across hidden + visible toggles).
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = it.lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return _normalise(", ".join(out))


def deakin_banner_tab_values(soup: BeautifulSoup) -> list[str]:
    """Return Deakin's authoritative audience-specific location tab labels."""
    tabs = soup.select(
        "#banner .banner-course__locations "
        ".banner-course__tabs [role='tab']"
    )
    values: list[str] = []
    seen: set[str] = set()
    for tab in tabs:
        value = compact(tab.get_text(" ", strip=True))
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def _from_deakin_banner_tabs(soup: BeautifulSoup) -> str | None:
    """Read Deakin's audience-specific course-location tabs.

    Deakin course pages expose the authoritative delivery locations as tab
    buttons inside the course banner. Generic heading extraction instead finds
    the footer's university-wide "Locations" navigation and returns
    "Campuses, Corporate centres, International offices".
    """
    physical = [
        value
        for value in deakin_banner_tab_values(soup)
        if not _REMOVE_VIRTUAL.search(value)
    ]
    if physical:
        return _normalise(", ".join(physical))
    return None


def _from_qut_quickbox(soup: BeautifulSoup) -> str | None:
    """QUT-specific structural extractor for the rendered ``Explore this course``
    quick-box sidebar.

    QUT (qut.edu.au) is fully Cloudflare-walled and JS-rendered; the rendered
    DOM contains a sidebar block of the shape::

        <b>Delivery</b>
        <ul data-course-map-key="quickBoxDeliveryINT">
          <li>Gardens Point</li>
          <li>Kelvin Grove</li>
        </ul>

    There are *two* such ULs per course: ``quickBoxDeliveryDOM`` (domestic
    students) and ``quickBoxDeliveryINT`` (international students).  Their
    contents differ on cross-jurisdictional courses (e.g. EMBA delivers in
    Brisbane to domestic students but in additional cities to international
    students), so we prefer ``...INT`` when present and fall back to
    ``...DOM`` only if INT is missing or empty.

    Without this structural read, QUT pages have NO recognisable
    ``<strong>Location</strong>`` / ``<dl>`` / ``<table>`` location node, the
    full-page text-block fallback hits page chrome (footer city lists), and
    the orchestrator marks ``location_text`` as missing — at which point the
    AI fallback is invoked and Gemini hallucinates plausible-but-wrong
    multi-city values like ``Brisbane, Canberra`` and
    ``Brisbane, Canberra, Gold Coast`` (QUT only operates in Brisbane).

    Returns ``None`` when no quickBoxDelivery* UL is present or when the
    selected UL has no non-blank list items.
    """
    selected = None
    for ul in soup.find_all("ul"):
        key = ul.get("data-course-map-key") or ""
        if not isinstance(key, str):
            continue
        if "quickBoxDeliveryINT" in key:
            selected = ul
            break
    if selected is None:
        for ul in soup.find_all("ul"):
            key = ul.get("data-course-map-key") or ""
            if isinstance(key, str) and "quickBoxDeliveryDOM" in key:
                selected = ul
                break
    if selected is None:
        return None
    items: list[str] = []
    for li in selected.find_all("li"):
        text = compact(li.get_text(" ", strip=True))
        if not text:
            continue
        # Drop pure online / virtual mentions — those are study_mode signals,
        # not physical campuses.  A combined "Online, Gardens Point" entry
        # from a single LI is still kept (it carries a real campus name);
        # _normalise / _sanitise_for_display will scrub the ``Online`` token.
        if _REMOVE_VIRTUAL.fullmatch(text):
            continue
        items.append(text)
    if not items:
        return None
    return _normalise(", ".join(items))


def _from_unisq_quickfacts(soup: BeautifulSoup) -> str | None:
    """UniSQ (unisq.edu.au) per-degree quick-facts panel.

    UniSQ renders the quick-facts strip directly under the page hero as a
    horizontal flex panel with six icon-labelled columns::

        Entry requirements  Duration  Location   Start         Fees      CRICOS
        View full details   2 years   Ipswich    Feb, Jun, Sep AUD …     078596M
                                      Toowoomba  View dates    (Indic…)
                                      External
                                      Online

    Each column header (``Location``, ``Duration``, ``Start`` …) is a plain
    text node inside a small container; the values are stacked beneath as
    sibling text nodes / `<p>` / `<div>` elements.  The generic
    `_from_strong_dom_walk`, `_from_dl`, `_from_panel_divs`, `_from_tables`
    and `_from_headings` walkers all miss this idiom because:

      * The label is not a `<strong>` / `<dt>` / `<th>` / `<h*>`
      * There is no labelled "Location:" pair — just a label followed by
        a stack of sibling text nodes
      * `_from_panel_divs` only reads a single next-sibling value (it would
        return ``"Ipswich"`` and drop Toowoomba)

    Without this reader, the cascade falls through to `_from_text_block`
    which scans the *whole page text* and false-matches the homepage-footer
    quick-links column ("Accommodation UniSQ Events Contributing to our
    communities") — observed fleet-wide on every UniSQ course staged
    2026-05-17.

    We anchor on an element whose own (label-only) text is exactly
    ``"Location"`` (case-insensitive, optional trailing colon) and then
    collect adjacent / sibling text nodes within the same small container
    until we hit another quick-fact label.  Pure "Online" / "External" /
    "Distance" entries are KEPT in the comma-joined raw value here —
    downstream `_sanitise_for_display` strips them via `_REMOVE_VIRTUAL`
    so courses with both Online + a physical campus stage just the
    physical campus, while online-only courses return None (campus-less)
    so the `online_only` guard in `guards.py` can fire downstream.

    Returns ``None`` when no exact "Location" label is found or the
    harvested value set has no recognisable campus tokens.
    """
    from bs4.element import NavigableString, Tag

    _LABEL_RE = re.compile(r"^\s*location\s*:?\s*$", re.I)
    # Sibling text matching any of these patterns means we've crossed
    # into the next quick-fact column — stop harvesting.
    _NEXT_LABEL_RE = re.compile(
        r"^\s*(?:entry\s+requirements?|view\s+full\s+details|duration"
        r"|start|view\s+dates|fees?|cricos|note|how\s+to\s+apply"
        r"|domestic\s+student|international\s+student|compare)\b",
        re.I,
    )
    # Hard sanity bound: a single quick-fact column never carries more
    # than ~8 stacked entries (UniSQ has 3 physical campuses + 3 modes
    # at most).  Caps runaway harvesting on unusual DOM shapes.
    _MAX_VALUES = 12

    label_els: list[Tag] = []
    for el in soup.find_all(["p", "div", "span", "strong", "b", "h5", "h6", "dt"]):
        own_text = el.get_text(" ", strip=True)
        if not own_text or not _LABEL_RE.match(own_text):
            continue
        # Must be a leaf-ish label: no block children carrying the label
        # text (otherwise we'd match e.g. a paragraph that opens with
        # "Location: ..." which we want the strong/dl walkers to handle).
        if el.find(["div", "table", "ul", "ol", "p"]):
            continue
        label_els.append(el)

    for label_el in label_els:
        values: list[str] = []
        # Strategy A: same-parent text harvest — collect sibling content
        # AFTER the label until the next quick-fact label is hit.
        parent = label_el.parent
        if parent is not None:
            after_label = False
            for child in parent.children:
                if isinstance(child, Tag) and child is label_el:
                    after_label = True
                    continue
                if not after_label:
                    continue
                if isinstance(child, NavigableString):
                    txt = str(child).strip()
                else:
                    txt = child.get_text(" ", strip=True)
                if not txt:
                    continue
                if _NEXT_LABEL_RE.search(txt):
                    break
                # Split inline-text harvests on newlines so a single
                # `<p>Ipswich\nToowoomba\nExternal\nOnline</p>` yields
                # four parts.
                for part in re.split(r"[\r\n]+|\s*,\s*", txt):
                    part = part.strip(" \t-•·|")
                    if not part:
                        continue
                    if _NEXT_LABEL_RE.search(part):
                        break
                    values.append(part)
                    if len(values) >= _MAX_VALUES:
                        break
                if len(values) >= _MAX_VALUES:
                    break
        # Strategy B: if same-parent harvest yielded nothing, walk the
        # label's next siblings at the parent level (icon-label idiom
        # where label and values are siblings of the icon container).
        if not values:
            for sib in label_el.find_next_siblings():
                txt = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
                if not txt:
                    continue
                if _NEXT_LABEL_RE.search(txt):
                    break
                for part in re.split(r"[\r\n]+|\s*,\s*", txt):
                    part = part.strip(" \t-•·|")
                    if not part:
                        continue
                    if _NEXT_LABEL_RE.search(part):
                        break
                    values.append(part)
                    if len(values) >= _MAX_VALUES:
                        break
                if len(values) >= _MAX_VALUES:
                    break
        if not values:
            continue
        # De-dup while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            k = v.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(v)
        if not out:
            continue
        # Require at least one token to be a recognised AU city (the three
        # UniSQ campuses are Toowoomba, Springfield, Ipswich — all in
        # _COMMON_CITIES).  This guards against the label appearing in
        # some unrelated panel further down the page that happens to
        # carry the literal word "Location".
        common_lc = {c.lower() for c in _COMMON_CITIES}
        if not any(v.lower() in common_lc for v in out):
            # Allow pure-virtual results to pass through too — the
            # downstream _sanitise_for_display strip will return None
            # and the online_only guard takes over.
            if not any(_REMOVE_VIRTUAL.fullmatch(v) for v in out):
                continue
        return _normalise(", ".join(out))
    return None


def _from_panel_divs(soup: BeautifulSoup) -> str | None:
    """Handle div-label / div-value panel idiom (e.g. Torrens ``course-card-panel``).

    Finds any ``<div>`` or ``<span>`` whose short text matches LOCATION_LABEL
    (after stripping trailing footnote markers like ``*``), then reads the
    next sibling div/span as the value.  Covers patterns like::

        <div class="course-card-panel__label">Campus locations*</div>
        <div class="course-card-panel__value">Sydney, Melbourne, Brisbane</div>

    Short-text guard (≤ 60 chars) prevents content divs from triggering.
    The element must also have no block-level children (it must be a leaf label).
    """
    try:
        from bs4.element import Tag  # noqa: F401 — just checking import health
    except ImportError:  # pragma: no cover
        return None

    for el in soup.find_all(("div", "span")):
        raw_text = el.get_text(" ", strip=True)
        if not raw_text or len(raw_text) > 60:
            continue
        # Strip footnote markers before checking the label regex.
        cleaned = _FOOTNOTE_TRAILER_RE.sub("", raw_text).strip()
        if not LOCATION_LABEL.match(cleaned):
            continue
        # Require leaf-like label: no block-level children.
        if el.find(("div", "table", "ul", "ol", "p")):
            continue
        # Try the label's next sibling first, then the parent's next sibling
        # (ASA-style adjacent parent containers).
        nxt = el.find_next_sibling(("div", "span"))
        if nxt is None:
            parent = el.parent
            if parent:
                nxt = parent.find_next_sibling(("div", "span"))
        if nxt is None:
            continue
        val = compact(nxt.get_text(" ", strip=True))
        # Guard: when the value div contains city names mixed with non-geographic
        # text (e.g. UTAS "Hobart Legal Practice" where "Legal Practice" is the
        # qualification area, not a campus) and no comma/slash separators are
        # present (which would indicate an already-structured list), strip the
        # non-city noise and keep only the recognised city names.
        # Does NOT fire for "Hobart, Launceston" (commas → structured list, safe).
        # Does NOT fire for "Cradle Coast, Hobart" (commas present).
        if val and "," not in val and "/" not in val and "|" not in val:
            _city_hits = [
                c for c in _COMMON_CITIES
                if re.search(rf"\b{re.escape(c)}\b", val, re.I)
            ]
            if _city_hits:
                _remainder = val
                for _c in _city_hits:
                    _remainder = re.sub(rf"\b{re.escape(_c)}\b", "", _remainder, flags=re.I)
                _remainder = _remainder.strip(" ,;/|").strip()
                if _remainder:
                    # Non-city content mixed in — keep only the clean city names.
                    val = ", ".join(_city_hits)
        result = _classify_location_value(val)
        if result:
            return result
    return None


def _from_utas_intl_panel(soup: BeautifulSoup) -> str | None:
    """UTAS-specific: extract location from the hidden #tabInternational panel.

    UTAS course pages use a two-tab layout (Domestic / International) rendered
    as ``<div id="tabDomestic">`` and ``<div id="tabInternational" hidden="">``.
    The international panel is in the DOM from the start (just CSS-hidden) so
    BeautifulSoup can read it without needing a browser tab-click.

    Scoping the cascade to only the international panel ensures the Location
    section returned reflects what international students actually see — not the
    domestic section that appears earlier in the HTML and would otherwise win the
    cascade first-match race.

    The element ID matching is case-insensitive and also tries ``tabintl`` and
    ``international-tab`` aliases so minor UTAS template changes don't break it.
    """
    intl_panel = (
        soup.find(id="tabInternational")
        or soup.find(id="tabintl")
        or soup.find(attrs={"id": re.compile(r"tab.?international", re.I)})
    )
    if not intl_panel:
        return None

    # Run the whole cascade on the restricted panel soup so we reuse all
    # existing normalisation / sanity-checking logic without duplicating it.
    panel_soup = BeautifulSoup(str(intl_panel), "html.parser")
    for fn in (
        _from_strong_dom_walk,
        _from_dl,
        _from_panel_divs,
        _from_tables,
        _from_headings,
        _from_delivery_mode_inperson,
    ):
        result = fn(panel_soup)
        if result:
            return result
    # Do NOT fall back to text-block here.  The panel's full text contains
    # page-chrome headings ("Key Information", "Entry requirements",
    # "Course rules") that share the same parent section as the Location
    # heading, causing _from_text_block's keyword window to capture those
    # phrases instead of a campus name.  Return None and let the outer
    # cascade continue — structural methods on the full page are safer.
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


def _from_uwl_jsonld(soup: BeautifulSoup) -> str | None:
    """UWL-specific: extract Location from JSON-LD ``CourseInstance.location.name``.

    UWL Angular SPA pages embed structured data in a
    ``<script type="application/ld+json">`` block::

        {
          "@context": "https://schema.org",
          "@graph": [
            {
              "hasCourseInstance": [
                {
                  "@type": "CourseInstance",
                  "location": {
                    "@type": "place",
                    "name": "West London Campus",
                    "address": "GB, London, W5 5RF, ..."
                  },
                  ...
                },
                ...
              ]
            }
          ]
        }

    The Angular ``<input aria-label="Location">`` carries the same value as its
    ``value`` attribute, but BeautifulSoup's ``get_text()`` skips input values so
    this structured data is the authoritative machine-readable source.

    All unique non-empty ``location.name`` values are collected, deduplicated
    (preserving order), joined with " / " and normalised.
    """
    import json as _json

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
        except Exception:
            continue
        graph = data.get("@graph") or [data]
        for node in graph:
            instances = node.get("hasCourseInstance") or []
            if not instances:
                continue
            seen: list[str] = []
            seen_lower: set[str] = set()
            for inst in instances:
                loc = inst.get("location") or {}
                name = (loc.get("name") or "").strip()
                if name and name.lower() not in seen_lower:
                    seen_lower.add(name.lower())
                    seen.append(name)
            if seen:
                v = _normalise(" / ".join(seen))
                if v:
                    return v
    return None


def _from_aria_input_value(soup: BeautifulSoup) -> str | None:
    """Generic fallback: read ``<input aria-label="Location" value="...">`` attribute.

    Angular / React SPAs sometimes bind the location value to an ``<input>``
    element's ``value`` attribute that is present in the rendered HTML but
    invisible to ``get_text()`` calls.  This function reads the attribute
    directly so the value is not lost.
    """
    for inp in soup.find_all("input"):
        aria = (inp.get("aria-label") or "").strip()
        if re.fullmatch(r"location", aria, re.I):
            val = (inp.get("value") or "").strip()
            if val:
                return _normalise(val)
    return None


def _from_bcu_keyfacts(soup: BeautifulSoup) -> str | None:
    """BCU-specific extractor: reads Location directly from the structured
    course facts panel ``div.course__key-info__inner``.

    BCU course pages render the facts panel as:

        <div class="course__key-info__inner">
          <div class="course__key-info__box-side">
            <ul class="course__key-info__list">
              <li>
                <span class="title">Location</span>
                <span class="value"><a href="...">City Centre</a></span>
              </li>
            </ul>
          </div>
        </div>

    Every other page section (testimonials, graduate stories, marketing
    quotes) is deliberately excluded — this extractor reads ONLY from
    the panel div, so names like "Lauren Redfern" or "Ben Stones" are
    never candidates.
    """
    panel = soup.select_one("div.course__key-info__inner")
    if not panel:
        return None
    for li in panel.select("li"):
        title_el = li.select_one("span.title")
        value_el = li.select_one("span.value")
        if (
            title_el
            and value_el
            and title_el.get_text(strip=True).lower() == "location"
        ):
            loc = value_el.get_text(strip=True)
            return _classify_location_value(loc) if loc else None
    return None


def _from_swinburne_international_hero(soup: BeautifulSoup) -> str | None:
    """Read Swinburne's audience-scoped campus hero fact.

    The generic div-label walker can pair a key-dates header with the adjacent
    ``Last date to apply`` column.  Swinburne's authoritative campus is already
    present in the course hero, with an international child when audiences
    differ and a shared value otherwise.
    """
    panel = soup.select_one(
        ".course-details__summary-item.course-details__campus"
    )
    if panel is None:
        return None
    audience_value = panel.select_one(".international")
    raw = (audience_value or panel).get_text(" ", strip=True)
    return _classify_location_value(raw)


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    # Per-uni text-cleaning strip_patterns (Option C).
    # Loaded from the contextvar set by set_uni_config() before extraction.
    # Fail-open: if the contextvar is unset (tests / ad-hoc calls), no stripping.
    from app.services.scraper.config.context import get_uni_config  # local import avoids circular dep
    _uni_cfg = get_uni_config()
    _strip_patterns: list[re.Pattern[str]] = []
    _allowed_values: list[str] = []
    if _uni_cfg:
        for _pat_str in _uni_cfg.extraction.text_cleaning.location.strip_patterns:
            try:
                _strip_patterns.append(re.compile(_pat_str, re.IGNORECASE))
            except re.error:
                pass  # bad pattern in YAML — skip rather than crash
        _allowed_values = [
            v.lower()
            for v in _uni_cfg.extraction.text_cleaning.location.allowed_values
        ]

    # ── UTAS page detection ──────────────────────────────────────────────────
    # Detect the hidden #tabInternational panel that UTAS injects into every
    # course page.  Two consequences:
    #
    # 1. _from_text_block is SKIPPED for UTAS pages.
    #    The full-page text contains page-chrome headings ("Key Information",
    #    "Entry requirements", "Course rules") immediately after the "Location"
    #    heading inside the panel.  _from_text_block's keyword window captures
    #    those phrases and returns them verbatim when no common city is found,
    #    producing "Key Information Entry requirements Course rules" as location.
    #    Courses whose international panel has no structural location node are
    #    better served by FALLBACK AI enrichment, which reads the whole page
    #    correctly.
    #
    # 2. _append_country_suffix is SKIPPED for all UTAS results.
    #    UTAS campus names include non-standard tokens ("Cradle Coast",
    #    "Melbourne Study Centre", "Ultimo Study Centre") and overseas partner
    #    institutions ("Hong Kong Universal Ed", "Shanghai Ocean University").
    #    These are unknown to _AU_CITIES so suffix is applied inconsistently —
    #    some rows get ", Australia", others don't.  Omitting it entirely keeps
    #    format stable across all UTAS courses.
    _has_utas_panel: bool = bool(
        soup.find(id="tabInternational")
        or soup.find(id="tabintl")
        or soup.find(attrs={"id": re.compile(r"tab.?international", re.I)})
    )
    # Parsed-hostname check — a naive substring (`"utas.edu.au" in url`)
    # also matches `notutas.edu.au` and `utas.edu.au.evil.com`, which would
    # mis-route those hosts into the UTAS-only cascade.  Use urlparse +
    # exact host / .suffix match instead.
    from urllib.parse import urlparse as _urlparse
    _parsed_host: str = (_urlparse(url or "").hostname or "").lower()
    _is_utas_host: bool = (
        _parsed_host == "utas.edu.au" or _parsed_host.endswith(".utas.edu.au")
    )

    # For UTAS pages that have a #tabInternational panel: scope the cascade
    # ENTIRELY to that panel.  Do NOT run any full-page structural method as a
    # fallback.
    #
    # Root cause of the leak: the full-page methods (strong, dl, div_panel,
    # table, heading) run on the whole HTML source, which includes the
    # *domestic* tab.  When the international panel has no physical campus
    # (course is Online-only for international students) _from_utas_intl_panel
    # returns None, and the cascade then finds the domestic campus (e.g.
    # "Hobart") from the full-page strong/dl/table scan.  That non-empty
    # course_location prevents the UTAS-specific online_only guard in
    # guards.py from firing, so the course is incorrectly staged.
    #
    # Correct behaviour: if the international panel has no physical location,
    # course_location should be blank → UTAS guard rejects the course.
    _is_qut_host: bool = "qut.edu.au" in (url or "").lower()
    _is_swinburne_host: bool = (
        _parsed_host == "swinburne.edu.au"
        or _parsed_host.endswith(".swinburne.edu.au")
    )

    # IMPORTANT — the UTAS-panel branch MUST be gated on the UTAS host.
    # Without the host guard, the `tab.?international` regex (where `.` is
    # a wildcard, not a literal) false-matches non-UTAS pages that happen
    # to ship a `<div id="tab-international">` marketing widget — most
    # notably Newcastle (newcastle.edu.au) per-degree pages, whose footer
    # carries exactly such a div.  When that happens the cascade collapses
    # to `[_from_utas_intl_panel]` only, which returns None on Newcastle
    # pages, and `course_location` stages blank fleet-wide.  Verified
    # 2026-05-17 on Master of Nursing, Master of Leadership and Management
    # in Education, and Master of Health Management and Policy (Global).
    if _is_swinburne_host:
        cascade_list = [
            ("swinburne_international_hero", _from_swinburne_international_hero(soup), 0.98),
        ]
    elif _has_utas_panel and _is_utas_host:
        cascade_list: list[tuple[str, str | None, float]] = [
            ("utas_intl_panel", _from_utas_intl_panel(soup), 0.95),
        ]
    elif (
        _parsed_host == "deakin.edu.au"
        or _parsed_host.endswith(".deakin.edu.au")
    ):
        # Deakin's generic "Locations" heading belongs to footer navigation.
        # Scope extraction to the course banner tabs and fail closed if those
        # are absent rather than staging university-wide chrome as a campus.
        cascade_list = [
            ("deakin_banner_tabs", _from_deakin_banner_tabs(soup), 0.98),
        ]
    elif "newcastle.edu.au" in (url or "").lower():
        # Newcastle (newcastle.edu.au): the per-degree page renders the
        # campus list as a radio-toggle group under <h6>Study location</h6>.
        # The structural reader for this idiom (_from_newcastle_toggles)
        # runs FIRST at confidence 0.95 — it reads the canonical
        # data-display-label attributes and is more reliable than any of
        # the generic strong/dl/heading walkers, which all miss the
        # toggle-button DOM shape.
        cascade_list: list[tuple[str, str | None, float]] = [
            ("newcastle_toggles", _from_newcastle_toggles(soup), 0.95),
            ("strong", _from_strong_dom_walk(soup), 0.9),
            ("dl", _from_dl(soup), 0.9),
            ("div_panel", _from_panel_divs(soup), 0.88),
            ("table", _from_tables(soup), 0.85),
            ("heading", _from_headings(soup), 0.7),
            ("delivery_inperson", _from_delivery_mode_inperson(soup), 0.85),
            ("text_block", _from_text_block(html_to_text(html)), 0.5),
        ]
    elif "unisq.edu.au" in (url or "").lower():
        # UniSQ (unisq.edu.au): the per-degree page renders the campus list
        # as a stacked text column inside a horizontal icon-labelled
        # quick-facts panel under <… >Location</…>.  The structural reader
        # for this idiom (_from_unisq_quickfacts) runs FIRST at confidence
        # 0.95 — without it the cascade falls through to _from_text_block
        # and reads homepage-footer quick-links ("Accommodation UniSQ
        # Events Contributing to our communities") as the "location".
        # Observed 2026-05-17 on every staged UniSQ course
        # (Master of Science, Bachelor of Nursing, Master of Information
        # Technology, etc.) — fleet-wide blank/wrong course_location.
        cascade_list: list[tuple[str, str | None, float]] = [
            ("unisq_quickfacts", _from_unisq_quickfacts(soup), 0.95),
            ("strong", _from_strong_dom_walk(soup), 0.9),
            ("dl", _from_dl(soup), 0.9),
            ("div_panel", _from_panel_divs(soup), 0.88),
            ("table", _from_tables(soup), 0.85),
            ("heading", _from_headings(soup), 0.7),
            ("delivery_inperson", _from_delivery_mode_inperson(soup), 0.85),
            # Critically, NO text_block fallback for UniSQ — the page
            # chrome (footer quick-links: "Accommodation UniSQ Events
            # Contributing to our communities") seeds the exact junk
            # that caused this bug.  If the structural readers all miss,
            # leave course_location blank so the AI fallback / review
            # queue can flag it instead of staging garbage.
        ]
    elif _is_qut_host:
        # QUT cascade: scope to the structural quickBoxDelivery* sidebar.
        # Falls through to the generic strong/dl/etc. cascade only when the
        # quick-box ULs are absent (very rare — present on every per-course
        # page in the rendered DOM).  Critically, we do NOT run
        # ``_from_text_block`` for QUT: the page chrome (footer "Our
        # campuses" mega-menu, partner-city links) seeds false positives
        # like "Brisbane, Sydney, Melbourne" / "Brisbane, Canberra" that
        # have no relationship to the course's actual delivery campus.
        cascade_list = [
            ("qut_quickbox", _from_qut_quickbox(soup), 0.95),
            ("strong", _from_strong_dom_walk(soup), 0.9),
            ("dl", _from_dl(soup), 0.9),
            ("div_panel", _from_panel_divs(soup), 0.88),
            ("table", _from_tables(soup), 0.85),
            ("heading", _from_headings(soup), 0.7),
            ("delivery_inperson", _from_delivery_mode_inperson(soup), 0.85),
        ]
    elif "bcu.ac.uk" in (url or "").lower():
        # BCU (Birmingham City University): the course facts panel is
        # div.course__key-info__inner → span.title="Location" + span.value.
        # All other page sections (testimonials, graduate stories, marketing
        # quotes) contain person names that look like valid text to the generic
        # strong/heading/text_block extractors — "Lauren Redfern",
        # "Ben Stones, Station Sound", "Jocelyn Bennett" etc.
        # Scoping extraction strictly to the keyfacts panel is the only correct
        # fix; post-filters cannot distinguish person names from campus names.
        # NO text_block or heading fallback — if the panel is absent (very
        # rare) the field stays blank and routes to AI/review queue.
        cascade_list = [
            ("bcu_keyfacts", _from_bcu_keyfacts(soup), 0.98),
        ]
    elif "uwl.ac.uk" in (url or "").lower():
        # UWL (University of West London): Angular SPA.
        #
        # Root cause of the junk extraction ("UCAS code Overview Degree
        # details Our students Research Entry and"):
        #
        #   1. The visible course summary bar renders location as an Angular
        #      <input aria-label="Location" value="West London Campus"> —
        #      BeautifulSoup's get_text() silently skips input[value], so no
        #      structural method reads the value from the DOM text.
        #
        #   2. _from_panel_divs finds <div>Location</div> whose next sibling
        #      is <div class="u-hidden">UCAS code</div> (the UCAS code bar
        #      item immediately follows Location in the summary row).
        #
        #   3. _from_text_block's regex window captures "Location" → up to
        #      the "fees?" lookahead stop-word, yielding
        #      "UCAS code Overview Degree details Our students Research Entry
        #      and" (the tab navigation text that immediately follows the
        #      summary bar in the rendered text stream).
        #
        # Fix: scope the cascade to two authoritative machine-readable
        # sources that ARE present in the rendered HTML and bypass all
        # text-content methods:
        #
        #   1. JSON-LD structured data (<script type="application/ld+json">)
        #      contains CourseInstance.location.name = "West London Campus"
        #      for every instance — this is the primary, most reliable source.
        #
        #   2. <input aria-label="Location" value="West London Campus"> —
        #      the value attribute (not text content) is readable via
        #      BeautifulSoup's .get("value"), so _from_aria_input_value
        #      provides a redundant fallback.
        #
        # NO text_block, strong, dl, div_panel, table, or heading fallbacks —
        # all generic methods read corrupted data on this Angular SPA.
        # If both structured sources miss (should not happen on live pages),
        # the field stays blank and routes to AI/review queue.
        cascade_list = [
            ("uwl_jsonld", _from_uwl_jsonld(soup), 0.98),
            ("aria_input", _from_aria_input_value(soup), 0.93),
        ]
    else:
        cascade_list = [
            # Structural pre-pass FIRST — see _from_strong_dom_walk for the
            # rationale. Reads `<strong>Location</strong>` style values out
            # of the DOM directly, including the ASA-style adjacent-div
            # idiom that the heading walker misses.
            ("strong", _from_strong_dom_walk(soup), 0.9),
            ("dl", _from_dl(soup), 0.9),
            # Div-label / div-value panel idiom (Torrens course-card-panel, etc.)
            ("div_panel", _from_panel_divs(soup), 0.88),
            ("table", _from_tables(soup), 0.85),
            ("heading", _from_headings(soup), 0.7),
            # "In person (CampusName)" delivery-mode pattern (Flinders, etc.)
            ("delivery_inperson", _from_delivery_mode_inperson(soup), 0.85),
            # text_block runs on non-UTAS pages only — see comment above.
            ("text_block", _from_text_block(html_to_text(html)), 0.5),
        ]

    for method, raw, conf in cascade_list:
        if not raw:
            continue
        # Apply per-uni strip_patterns before sanitise so cruft (e.g. ACAP
        # footnote annotations "^ ^Available in Perth from Trimester 3, 2026")
        # is removed from the raw location text before any normalisation check.
        for _pat in _strip_patterns:
            raw = _pat.sub("", raw).strip()
        if not raw:
            continue
        # Strip study-period labels (Semester 1, Spring, Autumn, Term 2 …)
        # from ALL cascade results.  Some extractors (headings, text_block,
        # utas_intl_panel) emit strings like "Launceston, Spring" when the
        # UTAS page lists availability periods inline with campus names.
        raw = _strip_period_labels(raw)
        if not raw:
            continue
        display = _sanitise_for_display(raw)
        if not display:
            continue
        # 2026-05-22 — Site-chrome chokepoint (post-mortem on UniSQ uni 562).
        # Every cascade method (strong/dl/div_panel/table/heading/
        # delivery_inperson/text_block/utas_intl_panel/newcastle_toggles/
        # unisq_quickfacts/qut_quickbox) passes through this loop. Any of
        # them can read a site-footer link block as a "Location:" value
        # when the page emits a labelled list-like DOM shape inside the
        # footer (UniSQ's `<dl>`/`<strong>` footer quick-links column
        # "Accommodation UniSQ Events Contributing to our communities"
        # matched method=strong/method=dl on ~25 staged courses 2026-05-22).
        # The pipeline-side `_is_location_chrome` guards (single_course.py
        # lines 2434/2584/3711) only protect the Gemini PRIMARY / FALLBACK
        # writes — the structural cascade had no equivalent gate, so the
        # chrome value entered `payload[course_location]` with method=
        # `location.<method>` and propagated unchecked.  This is the
        # single chokepoint that catches every structural source.
        try:
            from app.services.scraper.pipelines.single_course import (
                _is_location_chrome as _loc_chrome_guard,
            )
            if _loc_chrome_guard(display):
                continue  # site-chrome string — try the next cascade method
        except Exception:  # noqa: BLE001 — never abort extraction on import error
            pass
        # Per-uni allowed_values allowlist (YAML extraction.text_cleaning.location.allowed_values).
        # Applied after sanitise so we compare normalised display values.
        # Substring match (case-insensitive) so "City Centre" matches "City Centre, City South".
        if _allowed_values:
            if not any(av in display.lower() for av in _allowed_values):
                continue  # extracted value not in allowlist — try next cascade method
        # Append country suffix when all tokens are unambiguous AU/NZ cities.
        # Skip for: (a) utas_intl_panel results, (b) any UTAS-domain page,
        # and (c) when the raw string already contained a country word — in
        # that case _sanitise_for_display already stripped the bare country
        # token and we must not re-add it as a suffix (e.g. a page that lists
        # "Sydney, Melbourne, Brisbane, Australia" should yield
        # "Sydney, Melbourne, Brisbane", not "Sydney, Melbourne, Brisbane,
        # Australia" again).
        # 2026-05-13: country-suffix appending disabled per user preference.
        # The dashboard previously displayed locations like
        # "Sydney, Melbourne, Brisbane, Adelaide, Australia" — the trailing
        # ", Australia" is noise (every Australian uni page is implicitly AU).
        # The strip path (_sanitise_for_display) still removes country tokens
        # that appear in the raw HTML; we just no longer re-add a suffix.
        # _append_country_suffix is kept for any caller that may still want
        # to opt in explicitly, but the structural cascade no longer calls it.
        _raw_had_country = bool(_COUNTRY_WORD_IN_RAW_RE.search(raw))  # noqa: F841 — kept for future per-uni opt-in
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

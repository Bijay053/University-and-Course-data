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
    r"^\s*(?:campus(?:\s*locations?)?|location|locations|where\s+(?:can\s+)?(?:i|you)\s+study|delivery\s+location)\s*:?\s*$",
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
    r"\b(?:delivery\s*mode|study\s*mode|course\s*structure|intakes?|course\s*length|duration|cricos\s*code|fees?)\b",
    re.I,
)
_REMOVE_VIRTUAL = re.compile(
    r"\b(?:online|virtual|remote|distance(?:\s*learning)?|off[-\s]?campus)\b",
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
)


def _looks_marketing(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if _MARKETING_HINTS.search(t):
        return True
    return len(t.split()) > 16


def _normalise(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).replace(" , ", ", ").strip()
    if _looks_marketing(cleaned):
        return None
    head = _TRAILING_KEYS.split(cleaned, maxsplit=1)[0].strip() or cleaned
    if len(head) <= 2 or "<" in head or ">" in head:
        return None
    if _JUNK.search(head):
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
        v = _normalise(cells[1].get_text(" ", strip=True))
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
        ("text_block", _from_text_block(html_to_text(html)), 0.5),
    )
    for method, raw, conf in cascade:
        if not raw:
            continue
        display = _sanitise_for_display(raw)
        if not display:
            continue
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

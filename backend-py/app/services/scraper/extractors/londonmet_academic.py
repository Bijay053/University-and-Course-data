"""Deterministic London Met academic-entry extraction.

London Met course pages keep course-owned requirements in one accordion item.
This extractor deliberately refuses to inspect page-wide text: navigation and
related-course copy contain many unrelated numbers and qualifications.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.services.scraper.extractors.base import ExtractionResult

_HOSTS = {"londonmet.ac.uk", "www.londonmet.ac.uk"}
_MISSING = (None, "", [])
_MAX_OTHER_LENGTH = 600

_UCAS_AFTER_NUMBER_RE = re.compile(
    r"\b(?P<low>\d{1,3})(?:\s*(?:-|–|—|to)\s*(?P<high>\d{1,3}))?"
    r"\s+UCAS(?:\s+tariff)?\s+points?\b",
    re.IGNORECASE,
)
_UCAS_BEFORE_NUMBER_RE = re.compile(
    r"\bUCAS(?:\s+tariff)?\s+points?\s*(?:of|:)?\s*"
    r"(?P<low>\d{1,3})(?:\s*(?:-|–|—|to)\s*(?P<high>\d{1,3}))?\b",
    re.IGNORECASE,
)
_STOP_HEADING_RE = re.compile(
    r"^(?:accelerated study|accreditation of prior learning|"
    r"english language requirements?|qualification requirements? for)",
    re.IGNORECASE,
)
_YEAR_12_RE = re.compile(
    r"\b(?:A[\s-]?levels?|UCAS(?:\s+tariff)?\s+points?|"
    r"Level\s+3\s+qualification|GCSEs?)\b",
    re.IGNORECASE,
)
_BACHELOR_RE = re.compile(
    r"\b(?:honours?\s+degree|bachelor(?:'s|\u2019s)?\s+degree|"
    r"undergraduate\s+degree|first\s+degree)\b",
    re.IGNORECASE,
)
_MASTER_RE = re.compile(
    r"\b(?:master(?:'s|\u2019s)?\s+degree|postgraduate\s+degree)\b",
    re.IGNORECASE,
)
_UCAS_PAREN_RE = re.compile(r"\s*\([^()]*\bUCAS\b[^()]*\)", re.IGNORECASE)


def is_londonmet_url(url: str) -> bool:
    """Return whether *url* belongs to London Metropolitan University."""
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".") in _HOSTS
    except (TypeError, ValueError):
        return False


def _entry_panel(html: str) -> Tag | None:
    """Locate the bounded entry-requirements body in current/legacy markup."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Some templates use the direct panel id.
    direct = soup.select_one("#entry-requirements")
    if isinstance(direct, Tag):
        return direct.select_one(".accordion-body") or direct

    # Current pages put the id on the accordion button and the body in the
    # button's data-bs-target. Do not fall back to a heading/text search.
    trigger = soup.select_one("#entry-requirements-section")
    if not isinstance(trigger, Tag):
        return None
    target_id = str(trigger.get("data-bs-target") or "").strip()
    if target_id.startswith("#") and len(target_id) > 1:
        target = soup.select_one(target_id)
        if isinstance(target, Tag):
            return target.select_one(".accordion-body") or target
    item = trigger.find_parent(class_="accordion-item")
    if isinstance(item, Tag):
        return item.select_one(".accordion-body")
    return None


def _core_requirement_items(panel: Tag) -> list[str]:
    """Read list requirements before unrelated accordion subsections."""
    root = panel.select_one(".accordion-body") or panel
    items: list[str] = []
    for child in root.find_all(recursive=False):
        if child.name in {"h2", "h3", "h4", "h5", "h6"}:
            if _STOP_HEADING_RE.search(child.get_text(" ", strip=True)):
                break
        if child.name in {"ul", "ol"}:
            for li in child.find_all("li", recursive=False):
                text = " ".join(li.get_text(" ", strip=True).split())
                if text:
                    items.append(text)
    return items


def _ucas_score(text: str) -> float | None:
    """Extract only an explicitly labelled UCAS-points score."""
    match = _UCAS_AFTER_NUMBER_RE.search(text) or _UCAS_BEFORE_NUMBER_RE.search(text)
    if match is None:
        return None
    low = int(match.group("low"))
    high_raw = match.groupdict().get("high")
    high = int(high_raw) if high_raw else low
    score = min(low, high)
    # Current UCAS Tariff values are comfortably within this bound. The bound
    # also prevents years and arbitrary page numbers from becoming scores.
    if not 1 <= score <= 200:
        return None
    return float(score)


def _academic_level(text: str, url: str) -> str | None:
    path = (urlparse(url).path if url else "").lower()
    if _MASTER_RE.search(text):
        return "Master's degree"
    if _BACHELOR_RE.search(text):
        return "Bachelor's degree"
    if _YEAR_12_RE.search(text) or "/undergraduate/" in path:
        return "Year 12"
    return None


def _other_requirement(items: list[str]) -> str | None:
    cleaned: list[str] = []
    for item in items:
        # The numeric score has its own typed columns. Keep the alternative
        # A-level grades and all other genuine requirements as prose.
        text = _UCAS_PAREN_RE.sub("", item)
        if _UCAS_AFTER_NUMBER_RE.fullmatch(text.strip()) or _UCAS_BEFORE_NUMBER_RE.fullmatch(
            text.strip()
        ):
            continue
        text = re.sub(r"\s+([,.;:])", r"\1", " ".join(text.split())).strip(" ;")
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        return None
    value = "; ".join(cleaned)
    if len(value) <= _MAX_OTHER_LENGTH:
        return value
    return value[: _MAX_OTHER_LENGTH - 3].rstrip(" ,;:-") + "..."


def extract_fields(html: str, url: str) -> dict[str, Any]:
    """Return verified academic fields from the bounded entry panel."""
    if not is_londonmet_url(url):
        return {}
    panel = _entry_panel(html)
    if panel is None:
        return {}
    panel_text = " ".join(panel.get_text(" ", strip=True).split())
    if not panel_text:
        return {}

    fields: dict[str, Any] = {}
    level = _academic_level(panel_text, url)
    if level:
        fields["academic_level"] = level
    score = _ucas_score(panel_text)
    if score is not None:
        fields["academic_score"] = score
        fields["score_type"] = "UCAS Points"
    other = _other_requirement(_core_requirement_items(panel))
    if other:
        fields["other_requirement"] = other
    return fields


def apply_fill_only(
    payload: dict[str, Any],
    html: str,
    *,
    url: str,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fill missing payload fields without replacing an earlier extraction."""
    fields = extract_fields(html, url)
    applied: dict[str, Any] = {}
    snippet = ""
    panel = _entry_panel(html)
    if panel is not None:
        snippet = " ".join(panel.get_text(" ", strip=True).split())[:500]

    deterministic_score = fields.get("academic_score")
    for field, value in fields.items():
        if field == "score_type" and payload.get("academic_score") not in _MISSING:
            try:
                if float(payload["academic_score"]) != float(deterministic_score):
                    continue
            except (TypeError, ValueError):
                continue
        if payload.get(field) not in _MISSING:
            continue
        old = payload.get(field)
        payload[field] = value
        applied[field] = {"old": old, "new": value}
        if evidence is not None:
            evidence.append(
                {
                    "field_key": field,
                    "value": value,
                    "confidence": 0.99,
                    "method": "londonmet_academic:entry_panel",
                    "source_url": url,
                    "snippet": snippet,
                }
            )
    return applied


async def extract(html: str, url: str) -> list[ExtractionResult]:
    """Extractor-protocol wrapper around :func:`extract_fields`."""
    fields = extract_fields(html, url)
    panel = _entry_panel(html)
    snippet = (
        " ".join(panel.get_text(" ", strip=True).split())[:500]
        if panel is not None
        else None
    )
    return [
        ExtractionResult(
            field_key=field,
            value=value,
            normalized={field: value},
            confidence=0.99,
            snippet=snippet,
            method="londonmet_academic:entry_panel",
        )
        for field, value in fields.items()
    ]
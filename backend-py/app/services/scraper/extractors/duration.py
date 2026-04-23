"""Course-duration extractor.

Ported from Node ``extractDurationFromTextBlock`` /
``extractDurationFromDom`` in ``artifacts/api-server/src/routes/scrape.ts``
(lines 3453-3556).

Returns one ExtractionResult with the course duration plus its term
(Year / Semester / Trimester / Month / Week). Excludes accelerated /
fast-track variants — they should not overwrite the standard duration
(real-world bug at CSU "Bachelor of Business Studies").
"""
from __future__ import annotations

import re

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult


field_key = "duration"

_LABELS = (
    r"course\s*duration|duration|course\s*length|program\s*length|"
    r"study\s*duration|full[- ]?time\s*duration"
)
_UNIT = r"years?|yrs?|months?|weeks?|trimesters?|semesters?"

_PATTERNS = (
    re.compile(rf"\b(?:{_LABELS})\b[\s:.\-]{{0,40}}(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I),
    re.compile(rf"\bfull[- ]?time\b[\s:.\-]{{0,20}}(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I),
    re.compile(rf"\b(\d+(?:\.\d+)?)\s*({_UNIT})\s*(?:full[- ]?time)?\b", re.I),
)
_ACCELERATED = re.compile(
    r"\b(accelerat(?:ed|ion)|fast[- ]?track|condensed|intensive\s+(?:mode|stream|study)|"
    r"advanced\s+standing|recognition\s+of\s+prior\s+learning|RPL|"
    r"credit\s+for\s+previous\s+study)\b",
    re.I,
)
_UNIT_RANK = {"Year": 4, "Semester": 3, "Trimester": 3, "Month": 2, "Week": 1}
_WEEKS = {"Year": 52, "Semester": 20, "Trimester": 14, "Month": 4, "Week": 1}


def _normalise_unit(raw: str) -> str | None:
    raw = raw.lower()
    if "year" in raw or "yr" in raw:
        return "Year"
    if "month" in raw:
        return "Month"
    if "week" in raw:
        return "Week"
    if "trimester" in raw:
        return "Trimester"
    if "semester" in raw:
        return "Semester"
    return None


async def extract(html: str, url: str) -> list[ExtractionResult]:
    text = compact(html_to_text(html))
    if not text:
        return []

    # Build candidate sentences (skip accelerated callouts entirely).
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    parsed: list[tuple[float, float, str, str]] = []  # (weight, amount, unit, snippet)
    for s in sentences:
        if _ACCELERATED.search(s):
            continue
        for pat in _PATTERNS:
            m = pat.search(s)
            if not m:
                continue
            try:
                amount = float(m.group(1))
            except ValueError:
                continue
            unit = _normalise_unit(m.group(2))
            if not unit:
                continue
            # Cap depending on unit so we reject only true outliers
            # (e.g. "120 weeks" is 2 years, fine; "200 years" is junk).
            cap = {"Year": 12, "Semester": 24, "Trimester": 36, "Month": 96, "Week": 416}[unit]
            if not (0 < amount <= cap):
                continue
            weeks = amount * _WEEKS[unit]
            parsed.append((weeks * 100 + _UNIT_RANK[unit], amount, unit, s.strip()[:240]))
            break  # one match per sentence is enough

    if not parsed:
        return []
    parsed.sort(key=lambda t: t[0], reverse=True)
    _, amount, unit, snippet = parsed[0]
    return [
        ExtractionResult(
            field_key="duration",
            value=amount,
            normalized={"duration": amount, "duration_term": unit},
            confidence=0.75,
            snippet=snippet,
            method="regex",
        )
    ]

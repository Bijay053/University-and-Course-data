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


def _normalise_month(raw: str) -> str | None:
    """'Jan' / 'jan.' / 'JANUARY' → 'January'."""
    m = (raw or "").strip(" ,.;:").lower()[:4]
    for full in _MONTHS:
        if full.lower().startswith(m):
            return full
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


async def extract(html: str, url: str) -> list[ExtractionResult]:
    text = compact(html_to_text(html))
    if not text:
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

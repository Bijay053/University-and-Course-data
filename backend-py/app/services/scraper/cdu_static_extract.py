"""Authoritative extraction from CDU's current-course international blocks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

_CDU_HOSTS = frozenset({"cdu.edu.au", "www.cdu.edu.au"})
_ANNUAL_FEE_RE = re.compile(
    r"annual\s+tuition\s+fee\s+for\s+"
    r"(?:full\s+time\s+study|commencing\s+student\s+visa\s+holders)\s+in\s+"
    r"(?P<year>20\d{2})\s+is\s+(?P<currency>AUD)\s*\$\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_FULL_TIME_YEARS_RE = re.compile(
    r"(?P<years>\d+(?:\.\d+)?)\s*year(?:/s|s)?\s*full[\s-]*time",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(
    r"(?P<years>\d+(?:\.\d+)?)\s*year(?:/s|s)?\b",
    re.IGNORECASE,
)


def is_cdu_url(url: str) -> bool:
    return urlparse(url).hostname in _CDU_HOSTS


def ensure_cdu_catalogue_year(url: str, *, year: int | None = None) -> str:
    """Add CDU's active catalogue year to course URLs when it is absent."""
    parsed = urlparse(url)
    if (
        parsed.hostname not in _CDU_HOSTS
        or not parsed.path.lower().startswith("/study/course/")
    ):
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query.get("year"):
        return url
    query["year"] = str(year or datetime.now(timezone.utc).year)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _fee(soup: BeautifulSoup) -> dict[str, Any]:
    summary = soup.select_one("summary#accordion-fees")
    details = summary.find_parent("details") if summary else None
    if details is None:
        return {}

    for block in details.select('[data-student-type="international"]'):
        text = " ".join(block.get_text(" ", strip=True).split())
        match = _ANNUAL_FEE_RE.search(text)
        if not match:
            continue
        return {
            "international_fee": float(match.group("amount").replace(",", "")),
            "fee_term": "Annual",
            "fee_year": int(match.group("year")),
            "currency": match.group("currency").upper(),
        }
    return {}


def _location_and_mode(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    block = soup.select_one(
        '.block-course-key-fact-location [data-student-type="international"]'
    )
    if block is None:
        return None, None

    raw = " ".join(block.get_text(" ", strip=True).split())
    if not raw or "not available to international" in raw.lower():
        return None, None

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    online = any(part.lower() == "online" for part in parts)
    physical = [part for part in parts if part.lower() != "online"]

    if physical and online:
        mode = "Blended"
    elif physical:
        mode = "On Campus"
    elif online:
        mode = "Online"
    else:
        mode = None

    return ", ".join(physical) or None, mode


def _current_vet_duration_block(soup: BeautifulSoup) -> Any | None:
    block = soup.select_one(
        "#key-details .block-course-key-fact-duration-vet"
    )
    if block is not None:
        return block

    candidates = []
    for candidate in soup.select(".block-course-key-fact-duration-vet"):
        related_parent = candidate.find_parent(
            class_=lambda classes: classes
            and any(
                value == "mx-tile"
                or value == "related-course"
                or value.startswith("related-")
                for value in (
                    classes if isinstance(classes, list) else str(classes).split()
                )
            )
        )
        if related_parent is None:
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _duration(soup: BeautifulSoup) -> dict[str, Any]:
    block = soup.select_one(
        '.block-course-key-fact-duration [data-student-type="international"]'
    )
    match = None
    if block is not None:
        text = " ".join(block.get_text(" ", strip=True).split())
        match = _FULL_TIME_YEARS_RE.search(text)

    if match is None:
        vet_block = _current_vet_duration_block(soup)
        international = (
            vet_block.select_one('[data-student-type="international"]')
            if vet_block is not None
            else None
        )
        international_text = (
            " ".join(international.get_text(" ", strip=True).split())
            if international is not None
            else ""
        )
        # CDU's VET template places the current course's headline duration in
        # the unscoped key-fact value, while the international child confirms
        # that student-visa holders take the full-time route.
        if (
            vet_block is None
            or "student visa holders must study internally full time"
            not in international_text.lower()
        ):
            return {}
        heading = vet_block.find(["h2", "h3", "h4"])
        value = heading.find_next_sibling() if heading is not None else None
        value_text = (
            " ".join(value.get_text(" ", strip=True).split())
            if value is not None
            else ""
        )
        match = _YEARS_RE.fullmatch(value_text)
        if match is None:
            return {}

    return {
        "duration": float(match.group("years")),
        "duration_term": "Year",
    }


def apply_cdu_static_extraction(url: str, html: str) -> dict[str, Any]:
    """Return CDU fields scoped to the current international course."""
    if not is_cdu_url(url) or not html:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    location, mode = _location_and_mode(soup)
    result: dict[str, Any] = {
        # Always guard these fields on CDU pages. Unscoped generic extraction
        # can otherwise select domestic fees or locations from related cards.
        "international_fee": None,
        "course_location": location,
        "study_mode": mode,
        "duration": None,
        "duration_term": None,
    }
    result.update(_fee(soup))
    result.update(_duration(soup))
    return result
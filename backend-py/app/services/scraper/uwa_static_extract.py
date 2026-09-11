"""Authoritative UWA course-page and fee-calculator extractor.

UWA's course pages contain a labelled ``Campus location`` card and a
``Course Code`` card in the server-rendered HTML.  Generic text extraction
also sees campus names in navigation, marketing and related-course cards,
which was the cause of values such as ``Sydney, Perth`` being attached to
unrelated courses. This module scopes location to the current course's
labelled card and resolves its exact course code through UWA's official
international fee calculator.
"""
from __future__ import annotations

import html as _html
import re
import time
import httpx
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

_HOST = "uwa.edu.au"
_FEE_ENDPOINT = "https://www.fees.uwa.edu.au/Calculator/GetCourseFee"
_FEE_YEAR = "2026"


def is_uwa_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == _HOST or host.endswith("." + _HOST)


def _card_value(page: str, label: str) -> str | None:
    """Return the value following a labelled card-details field."""
    pattern = (
        r'<div[^>]*class=["\'][^"\']*card-details-label[^"\']*["\'][^>]*>'
        r"\s*" + re.escape(label) +
        r"\s*</div>\s*<div[^>]*class=[\"'][^\"']*card-details-value[^\"']*[\"'][^>]*>"
        r"(.*?)</div>"
    )
    match = re.search(pattern, page, re.I | re.S)
    if not match:
        return None
    text = re.sub(r"<[^>]+>", " ", match.group(1))
    text = _html.unescape(text)
    values = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"[\r\n]+", text)]
    values = [x for x in values if x]
    return ", ".join(dict.fromkeys(values)) or None


def _location_card_value(page: str) -> str | None:
    """Read UWA's current-course campus card across its template labels."""
    for label in ("Campus location", "Locations", "Course location", "Location"):
        value = _card_value(page, label)
        if value:
            return value
    return None


@lru_cache(maxsize=512)
def _fee_for_code(code: str, category: str) -> dict[str, Any]:
    """Read one exact course amount from UWA's official calculator.

    Cache is process-local and bounded; failed requests are cached as empty for
    this scrape process, preventing a 426-course run from retrying an outage.
    """
    body = {
        "feeCategory": category, "feeYear": _FEE_YEAR,
        "courseCode": code, "year": f"Starting {_FEE_YEAR}",
    }
    for attempt in range(2):
        try:
            response = httpx.post(_FEE_ENDPOINT, json=body, timeout=6.0)
            if response.status_code != 200:
                return {}
            data = response.json()
            row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
            amount = re.sub(r"[^\d.]", "", str(row.get("fee") or ""))
            if not amount or not re.fullmatch(r"\d+(?:\.\d+)?", amount):
                return {}
            return {
                "international_fee": float(amount),
                "fee_currency": "AUD",
                "fee_term": "Annual",
                "fee_year": int(_FEE_YEAR),
                "uwa_fee_source_url": _FEE_ENDPOINT,
            }
        except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError):
            if attempt == 0:
                time.sleep(0.1)
    return {}


def apply_uwa_static_extraction(
    url: str, page: str, course_name: str | None = None
) -> dict[str, Any]:
    """Extract only values proven by the current UWA course card."""
    if not is_uwa_url(url):
        return {}
    location = _location_card_value(page)
    code = _card_value(page, "Course Code")
    result: dict[str, Any] = {
        # Always write location, including None, so noisy regex fallback cannot
        # replace an absent authoritative campus with a navigation city.
        "course_location": location,
        "location_text": location,
        # If the exact calculator lookup fails, keep the fee blank and make
        # that visible rather than guessing a default.
        "scrape_warnings": ["uwa_fee_calculator_required"],
    }
    if code and re.fullmatch(r"[A-Z0-9]{4,8}", code.replace(",", "").strip()):
        result["uwa_course_code"] = code.strip()
        name = (course_name or "").lower()
        if not name:
            title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.I | re.S)
            name = re.sub(r"<[^>]+>", " ", title.group(1)).lower() if title else ""
        category = "INTUG" if re.search(r"\b(?:bachelor|undergraduate)\b", name) else "INTPG"
        fee = _fee_for_code(code.strip(), category)
        result.update(fee)
        if fee.get("international_fee") is not None:
            result["scrape_warnings"] = []
    return result
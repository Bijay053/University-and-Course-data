"""Curtin course-page facts.

Curtin's ``/study/offering/course-*`` documents contain several repeated
course cards (including majors and recommendations).  The normal flattened
text extractors therefore frequently select a neighbouring card: credit
counts become durations and the university default campus becomes the
course campus.  This deliberately small provider only accepts values in the
current offering's labelled fact blocks and never invents a campus or score.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def is_curtin_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "curtin.edu.au" or host.endswith(".curtin.edu.au")


def _text(source: str) -> str:
    source = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", source)
    source = re.sub(r"(?s)<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", _html.unescape(source)).strip()


def _information_value(soup: BeautifulSoup, label: str) -> str | None:
    """Read one current-course key-information block, excluding its tooltip."""
    wanted = label.casefold()
    for block in soup.select("div.information"):
        heading = block.select_one("div.information__title > h3")
        if not heading or _text(str(heading)).casefold() != wanted:
            continue
        value = block.select_one(":scope > div.information__content")
        if value:
            return _text(str(value)) or None
    return None


def _international_annual_offer(soup: BeautifulSoup) -> dict[str, Any]:
    """Return the newest exact international year-1 offer from JSON-LD."""
    matches: list[tuple[int, dict[str, Any]]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            root = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = root if isinstance(root, list) else [root]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            name = str(item.get("name") or "")
            if (
                item.get("@type") == "Offer"
                and re.search(r"\bInternational\b", name, re.I)
                and re.search(r"\bIndicative\s+year\s*1\s+fee\b", name, re.I)
                and str(item.get("priceCurrency") or "").upper() == "AUD"
            ):
                year_match = re.search(r"\b(20\d{2})\b", name)
                price = item.get("price")
                if year_match and isinstance(price, (int, float)) and price >= 1000:
                    matches.append((int(year_match.group(1)), item))
    return max(matches, key=lambda pair: pair[0])[1] if matches else {}


def _number(value: str | None, pattern: str) -> float | None:
    if not value:
        return None
    m = re.search(pattern, value, re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n if n > 0 else None


def apply_curtin_static_extraction(url: str, html: str) -> dict[str, Any]:
    """Return only explicit international/current-offering facts."""
    result: dict[str, Any] = {
        "course_location": None,
        "location_text": None,
        "study_mode": None,
        "duration": None,
        "duration_term": None,
    }
    if not is_curtin_url(url) or not html:
        return result
    soup = BeautifulSoup(html, "html.parser")
    text = _text(html)

    # A major page is not an award offering.  This is intentionally limited to
    # Curtin's URL shape; no global title or degree filtering is performed.
    if re.search(r"/course-[^/]*\bmajor\b", url, re.I):
        result["scrape_warnings"] = ["curtin_non_award_major"]
        return result

    duration_value = _information_value(soup, "Duration")
    # Never treat credit points/units as time.  The old loose matcher turned
    # "66 credit points" into 66 years/months on combined degrees.
    if duration_value:
        years = _number(duration_value, r"(\d+(?:\.\d+)?)\s*years?\b")
        months = _number(duration_value, r"(\d+(?:\.\d+)?)\s*months?\b")
        if years is not None:
            result["duration"] = years + ((months or 0) / 12)
            result["duration_term"] = "Year"
        elif months is not None:
            result["duration"] = months
            result["duration_term"] = "Month"
        else:
            m = re.search(
                r"(?<![\d.])(\d+(?:\.\d+)?)\s*(weeks?|semesters?|trimesters?)\b",
                duration_value,
                re.I,
            )
            if m:
                result["duration"] = float(m.group(1))
                result["duration_term"] = m.group(2).title().rstrip("s")

    offer = _international_annual_offer(soup)
    if offer:
        result["international_fee"] = float(offer["price"])
        result["fee_term"] = "Annual"
        result["fee_currency"] = "AUD"
        year_match = re.search(r"\b(20\d{2})\b", str(offer.get("name") or ""))
        if year_match:
            result["fee_year"] = int(year_match.group(1))

    location = _information_value(soup, "Location")
    if location:
        location = re.split(r"\b(?:Duration|Study mode|International|English)\b", location, flags=re.I)[0].strip(" :-|")
        if location and len(location) < 100 and not re.search(r"start dates|academic calendar", location, re.I):
            result["course_location"] = location
            result["location_text"] = location

    mode = _information_value(soup, "Attendance mode")
    if mode:
        m = re.search(r"\b(On\s+campus|Online|Blended|External|Part[- ]?time|Full[- ]?time)\b", mode, re.I)
        if m:
            result["study_mode"] = m.group(1).title().replace("  ", " ")

    english_match = re.search(
        r"(?is)English\s+(?:language\s+)?requirements?.{0,800}?"
        r"(IELTS.{0,160}(?:PTE|TOEFL|</(?:p|li|div)>))",
        html,
    )
    english = _text(english_match.group(1)) if english_match else None
    ielts = _number(english, r"\bIELTS(?:\s+overall)?\s*[:\-]?\s*(\d(?:\.\d)?)")
    if ielts is not None and 1 <= ielts <= 9:
        result["ielts_overall"] = ielts
    pte = _number(english, r"\bPTE(?:\s+academic)?(?:\s+overall)?\s*[:\-]?\s*(\d{2,3})")
    if pte is not None and 10 <= pte <= 90:
        result["pte_overall"] = pte
    toefl = _number(english, r"\bTOEFL(?:\s+overall)?\s*[:\-]?\s*(\d{2,3})")
    if toefl is not None and 10 <= toefl <= 120:
        result["toefl_overall"] = toefl

    entry = _information_value(soup, "Entry requirements")
    if entry:
        result["other_requirement"] = entry
    return result
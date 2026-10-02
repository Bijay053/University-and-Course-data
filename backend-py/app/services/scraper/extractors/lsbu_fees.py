"""Course-owned LSBU international tuition, before page-wide fee scanning."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.services.scraper.extractors.base import ExtractionResult

METHOD = "fee.lsbu_international_card"
_PRICE = re.compile(r"£\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _amount(text: str) -> float | None:
    match = _PRICE.fullmatch(text.strip())
    if not match:
        return None
    value = float(match[1].replace(",", ""))
    return value if value > 0 else None


def course_fee(html: str, url: str) -> tuple[bool, ExtractionResult | None]:
    """Return (owned template, fee); an empty international price fails closed.

    The headline card is annual tuition. Year-labelled full-time fee tables
    repeat that price beside a separate, explicitly labelled course total.
    Read only the international card/cell, never the neighbouring UK column.
    """
    parsed = urlparse(url or "")
    if (
        parsed.hostname not in {"www.lsbu.ac.uk", "lsbu.ac.uk"}
        or not re.fullmatch(r"/study/course-finder/[^/]+/?", parsed.path)
        or not html
    ):
        return False, None
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one("#fees")
    if panel is None:
        return False, None

    cards = panel.select(".fees__card")
    tables = panel.select("table.fees__table")
    if not cards and not tables:
        return False, None
    prices: set[float] = set()
    international_card = False
    for card in cards:
        header = card.select_one(".fees__card-header-title")
        if not header or header.get_text(" ", strip=True).casefold() != "international":
            continue
        international_card = True
        for item in card.select(".fees__card-content-item"):
            info = item.select_one(".fees__card-info")
            price = item.select_one(".fees__card-subtitle")
            if (
                info is not None and price is not None
                and re.search(r"tuition fees for\s+international students", info.get_text(" ", strip=True), re.I)
            ):
                value = _amount(price.get_text(" ", strip=True))
                if value is not None:
                    prices.add(value)

    # Year-one full-time cells are the bounded fallback when the headline
    # card is absent. Do not use extension/writing-up prices or course totals.
    year_one: list[tuple[float, int | None]] = []
    for table in tables:
        wrapper = table.find_parent(class_="course-table-wrapper")
        if wrapper is None:
            continue
        context = wrapper.get_text(" ", strip=True)
        if (
            not re.search(r"\(FT\)\s*[-–—]\s*Year\s+1\b", context, re.I)
            or re.search(r"Extension|Writing Up", context, re.I)
        ):
            continue
        year_match = re.search(r"entry\s+(20\d{2})(?:/\d{2,4})?", table.get_text(" ", strip=True), re.I)
        year = int(year_match[1]) if year_match else None
        for cell in table.select("td"):
            text = cell.get_text(" ", strip=True)
            match = re.fullmatch(r"International fee:\s*(£\s*[\d,]+(?:\.\d{1,2})?)", text, re.I)
            if match:
                value = _amount(match[1])
                if value is not None:
                    year_one.append((value, year))
    if not international_card and year_one:
        latest = max((year or 0 for _, year in year_one), default=0)
        prices = {value for value, year in year_one if (year or 0) == latest}
    # A blank/zero international headline is unpublished, not permission to
    # substitute a domestic, historical, part-time or AI-generated price.
    if len(prices) != 1:
        return True, None
    value = next(iter(prices))
    year = max((year for amount, year in year_one if amount == value and year), default=None)
    snippet = f"International tuition fees: £{value:g} per year"
    if year:
        snippet += f"; entry {year}/{str(year + 1)[-2:]}"
    return True, ExtractionResult(
        field_key="international_fee",
        value=value,
        normalized={
            "international_fee": value, "currency": "GBP",
            "fee_term": "Annual", "fee_year": year,
        },
        confidence=1.0,
        snippet=snippet,
        method=METHOD,
    )


def apply_course_fee_authority(html: str, url: str, payload: dict, evidence: list) -> None:
    """Keep the audience/amount/period tuple intact after all fallback passes."""
    known, result = course_fee(html, url)
    if not known:
        return
    for row in evidence:
        if row.get("field_key") == "international_fee":
            row["decision_status"] = "needs_review"
    for key in ("international_fee", "currency", "fee_term", "fee_year"):
        payload[key] = result.normalized.get(key) if result else None
    if result:
        evidence.append({
            "field_key": result.field_key, "value": result.value,
            "confidence": result.confidence, "method": result.method,
            "source_url": url, "snippet": result.snippet,
        })
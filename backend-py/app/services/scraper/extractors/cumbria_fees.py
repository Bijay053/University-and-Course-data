"""Course-owned international undergraduate fee cards, retaining entry years."""
from urllib.parse import urlsplit
import re

from bs4 import BeautifulSoup

from app.services.scraper.extractors.base import ExtractionResult


def course_fee(html: str, url: str) -> tuple[bool, ExtractionResult | None]:
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.cumbria.ac.uk", "cumbria.ac.uk"} or not parsed.path.startswith(
        "/study/courses/undergraduate/"
    ):
        return False, None
    soup = BeautifulSoup(html, "html.parser")
    # Both responsive facts cards and the Fees tab publish the same schedule.
    # Never flatten the neighbouring Home rows into this audience's context.
    panels = soup.select("#intFeesInfo, #intFeesDetails")
    if not panels:
        return False, None
    candidates: dict[int, dict[int, str]] = {}
    for panel in panels:
        for row in panel.select(".internationalfees, .leftFeesColumn, .rightFeesColumn"):
            text = row.get_text(" ", strip=True)
            years = re.findall(r"\bEntry in academic year\s+(20\d{2})\s*/\s*\d{2,4}\b", text, re.I)
            amounts = re.findall(r"£\s*([\d,]+(?:\.\d{2})?)", text)
            if len(years) != 1 or len(amounts) != 1:
                continue
            amount = float(amounts[0].replace(",", ""))
            if amount <= 0 or not amount.is_integer():
                continue
            candidates.setdefault(int(years[0]), {})[int(amount)] = text
    if not candidates:
        return True, None
    year = max(candidates)
    # Conflicting responsive copies are not permission to guess a price.
    if len(candidates[year]) != 1:
        return True, None
    amount, text = next(iter(candidates[year].items()))
    return True, ExtractionResult(
        field_key="international_fee",
        value=amount,
        normalized={"international_fee": amount, "currency": "GBP",
                    "fee_term": "Annual", "fee_year": year},
        confidence=1.0,
        snippet=f"International tuition: {text}",
        method="fee.cumbria_latest_international_year",
    )

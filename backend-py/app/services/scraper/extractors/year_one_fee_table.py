"""Course-owned year-one tuition must not be replaced by placement-year prices."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from .base import ExtractionResult

_AUDIENCE = re.compile(
    r"(?:international|overseas|non[- ]UK)\s+students?"
    r"(?:\s*\(?\s*(20\d{2})(?:\s*/\s*\d{2,4})?\s*\)?)?", re.I)
_FIRST_YEAR = re.compile(r"(?:first\s+year|year\s*1|annual)\s+tuition\s+fees?\s*:?", re.I)
_POUNDS = re.compile(r"£\s*(\d[\d,]*(?:\.\d{2})?)\s*\*{0,3}")


def extract_year_one_fee(html: str, url: str) -> tuple[bool, ExtractionResult | None]:
    if not (urlsplit(url).hostname or "").lower().endswith(".ac.uk"):
        return False, None
    if not re.search(r"(?:first\s+year|year\s*1|annual)\s+tuition", html, re.I):
        return False, None
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    handled = False
    for table in soup.find_all("table"):
        if not table.find_parent(["main", "article"]):
            continue
        ancestors = list(table.parents)
        if any(node.name in {"aside", "nav", "footer", "header"} or re.search(
            r"(?:^|[-_\s])(?:related|recommended|other-courses)(?:[-_\s]|$)",
            " ".join([str(node.get("id", "")), *node.get("class", [])]), re.I
        ) for node in ancestors if getattr(node, "attrs", None) is not None):
            continue
        heading = table.find_previous_sibling()
        if not heading or heading.name not in {"p", "h2", "h3", "h4", "h5", "h6"}:
            continue
        audience = _AUDIENCE.fullmatch(heading.get_text(" ", strip=True))
        if not audience:
            continue
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != 2 or not _FIRST_YEAR.fullmatch(cells[0].get_text(" ", strip=True)):
                continue
            handled = True
            match = _POUNDS.fullmatch(cells[1].get_text(" ", strip=True))
            if not match:
                return True, None
            amount = float(match.group(1).replace(",", ""))
            if amount <= 0:
                return True, None
            year = int(audience.group(1)) if audience.group(1) else None
            snippet = heading.get_text(" ", strip=True) + " | " + row.get_text(" ", strip=True)
            candidates.append((year, amount, snippet))
    if not candidates:
        return handled, None
    years = {entry[0] for entry in candidates}
    if None in years and len(years) > 1:
        return True, None
    latest = max(years) if None not in years else None
    selected = [entry for entry in candidates if entry[0] == latest]
    if len({entry[1] for entry in selected}) != 1:
        return True, None
    year, amount, snippet = selected[0]
    normalized = {"international_fee": amount, "currency": "GBP", "fee_term": "Annual"}
    if year:
        normalized["fee_year"] = year
    return True, ExtractionResult(
        field_key="international_fee", value=amount, normalized=normalized,
        confidence=0.99, snippet=snippet, method="fee.labelled_international_year_one")

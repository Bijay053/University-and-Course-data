"""Bind TRU's Open Learning undergraduate credit rate to a selected programme."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

TUITION = "https://www.tru.ca/distance/services/tuition.html"
METHOD = "fee.tru_open_learning_undergraduate_per_credit"


def programme_title(programme, url):
    from app.services.scraper.pipelines.single_course import _is_not_accepting_page

    parsed = urlsplit(url)
    if parsed.hostname != "www.tru.ca" or not parsed.path.startswith("/distance/programs/"):
        return None
    soup = BeautifulSoup(programme, "html.parser")
    for node in soup.select("script, style"):
        node.decompose()
    titles = [h.get_text(" ", strip=True) for h in soup.select("h1") if h.get_text(strip=True)]
    if len(titles) != 1:
        return None
    title = titles[0]
    if (not re.search(r"\b(?:Bachelor|Diploma|Certificate|Associate Degree)\b", title, re.I)
            or re.search(r"\b(?:Master|Doctor|Graduate|Postgraduate|Post[- ]Baccalaureate|Apprenticeship)\b", title, re.I)):
        return None
    text = soup.get_text(" ", strip=True)
    if _is_not_accepting_page(str(soup)):
        return None
    if TUITION not in {urljoin(url, a["href"]) for a in soup.select("a[href]")}:
        return None
    # A selected programme's own price or exception needs its own comparison.
    if re.search(r"\btuition\b.{0,100}(?:\$|CAD)\s*\d", text, re.I):
        return None
    return title


def parse(programme, schedule, url):
    from .base import ExtractionResult
    title = programme_title(programme, url)
    if not title:
        return None
    fees = BeautifulSoup(schedule, "html.parser")
    main = fees.select_one("main") or fees
    currency = re.search(r"in\s+Canadian\s+funds", main.get_text(" ", strip=True), re.I)
    if not currency:
        return None
    candidates = []
    for table in main.select("table"):
        heading = table.find_previous(["h2", "h3", "h4"])
        undergraduate = bool(heading and heading.get_text(" ", strip=True).lower() == "undergraduate tuition")
        for row in table.select("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"], recursive=False)]
            if any(c.lower() == "undergraduate tuition" for c in cells):
                undergraduate = True
            if any(c.lower() == "graduate tuition" for c in cells):
                undergraduate = False
            if not undergraduate or len(cells) != 2 or cells[0].lower() != "international students":
                continue
            match = re.fullmatch(r"\$([\d,]+\.\d{2})\s+per credit", cells[1], re.I)
            if match:
                candidates.append((float(match[1].replace(",", "")), row.get_text(" ", strip=True)))
    if len(candidates) != 1 or not 0 < candidates[0][0] < 10000:
        return None
    amount, quote = candidates[0]
    return ExtractionResult(
        field_key="international_fee", value=amount,
        normalized={"international_fee":amount, "currency":"CAD", "fee_term":"Per Credit", "fee_year":None},
        confidence=0.95, method=METHOD, snippet=quote,
        extras={"tuition_source_url":TUITION, "course_url":url, "course_title":title,
                "applicability":"Open Learning undergraduate tuition", "currency_quote":currency[0]},
    )


async def extract(html, url):
    from .tru_calculator import read_dependency
    if not programme_title(html, url):
        return None
    schedule = await asyncio.wait_for(read_dependency(TUITION, url), timeout=10)
    return parse(html, schedule, url)

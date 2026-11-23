"""Bedfordshire admission/audience panels must not share another award's fees."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import ExtractionResult


def course_fee(html, url):
    target = urlsplit(url)
    if target.hostname not in {"beds.ac.uk", "www.beds.ac.uk"} or not target.path.startswith("/courses/"):
        return False, None
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.select("[data-course-admission][data-study-type]")
    if not panels:
        return False, None
    # Unknown selectable options must not silently inherit the base course fee.
    if target.query:
        return True, None
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if re.search(r"\b(?:FdA|FdSc|foundation degree|PhD|MPhil|MRes)\b|by research", title, re.I):
        return True, None
    if re.search(r"\b(?:BSc|BA|BEng|BEd|LLB|MEng|MSci|Bachelor)\b", title, re.I):
        qualification = re.compile(r"\bundergraduate\b", re.I)
    elif re.search(r"\bMBA\b", title, re.I):
        qualification = re.compile(r"\bMBA\b|Master of Business Administration", re.I)
    elif re.search(r"\b(?:MSc|MA|LLM|Master)\b", title, re.I):
        qualification = re.compile(r"\bpostgraduate\b", re.I)
    else:
        return True, None
    candidates = []
    for panel in panels:
        # These exact labels are published on the selected admission's panel.
        # "The UK", mixed audiences, and nested panels are never international.
        if str(panel.get("data-study-type", "")).strip().casefold() != "international":
            continue
        for paragraph in panel.find_all("p"):
            if (paragraph.find_parent(attrs={"data-study-type": True}) is not panel
                    or paragraph.find_parent(attrs={"data-course-admission": True}) is not panel
                    or paragraph.find(attrs={"data-study-type": True})
                    or paragraph.find(attrs={"data-course-admission": True})):
                continue
            text = paragraph.get_text(" ", strip=True)
            if (not qualification.search(text)
                    or not re.search(r"\btuition\s+fees?\b", text, re.I)
                    or re.search(r"foundation degree|scholarship|bursary|discount|deposit|"
                                 r"not available|cannot apply|not published", text, re.I)):
                continue
            amounts = re.findall(r"£\s*(\d[\d,]*(?:\.\d{2})?)", text)
            year = re.search(r"\bacademic year\s+(20\d{2})(?:\s*/\s*\d{2,4})?\b", text, re.I)
            annual = bool(re.search(r"\bper (?:year|annum)\b", text, re.I))
            total = bool(re.search(r"\b(?:full|whole|complete) course\b", text, re.I))
            if len(amounts) != 1 or not year or annual == total:
                continue
            amount = float(amounts[0].replace(",", ""))
            if amount <= 0:
                continue
            candidates.append((int(year[1]), amount, "Annual" if annual else "Full Course", text))
    if not candidates:
        return True, None
    latest = max(item[0] for item in candidates)
    selected = [item for item in candidates if item[0] == latest]
    # Repeated February/October admissions can agree. Conflicting offerings
    # require an exact option comparison, not a maximum/minimum-price guess.
    if len({(item[1], item[2]) for item in selected}) != 1:
        return True, None
    year, amount, term, text = selected[0]
    return True, ExtractionResult(
        field_key="international_fee", value=amount,
        normalized={"international_fee": amount, "currency": "GBP",
                    "fee_year": year, "fee_term": term},
        confidence=0.99, method="fee.bedfordshire_admission_audience",
        snippet=text,
    )

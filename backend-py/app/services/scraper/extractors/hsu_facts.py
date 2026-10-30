"""Course-owned HSU location and audience/year-scoped tuition."""
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def is_hsu_course(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"www.hsu.ac.uk", "hsu.ac.uk"} and bool(
        re.fullmatch(r"/course/[^/]+/?", parsed.path)
    )


def apply_course_authority(html: str, url: str, payload: dict, evidence: list) -> None:
    if not is_hsu_course(url):
        return
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("nav, header, footer, script, style"):
        element.decompose()
    locations = set()
    for label in soup.select("article .label"):
        if label.get_text(" ", strip=True).lower() != "location":
            continue
        value = label.find_next_sibling(class_="value")
        if value and value.get_text(" ", strip=True):
            locations.add(value.get_text(" ", strip=True))
    # Never retain the concatenated navigation campuses when course facts
    # are absent or conflicting.
    location = next(iter(locations)) if len(locations) == 1 else None
    payload["course_location"] = location
    for row in evidence:
        if row.get("field_key") == "course_location":
            row["decision_status"] = "needs_review"
    if location:
        evidence.append({
            "field_key": "course_location", "value": location,
            "method": "location.hsu_course_fact", "confidence": 1.0,
            "source_url": url, "snippet": f"Location: {location}",
        })

    sections = []
    for heading in soup.find_all(["h2", "h3"]):
        title = heading.get_text(" ", strip=True)
        if not re.search(r"\btuition fees?\b", title, re.I):
            continue
        year_match = re.search(r"\b(20\d{2})", title)
        year = int(year_match[1]) if year_match else 0
        international = False
        prices = set()
        snippets = []
        for node in heading.find_next_siblings():
            if node.name in {"h2", "h3"}:
                break
            text = node.get_text(" ", strip=True)
            if node.name == "h4":
                international = bool(re.fullmatch(
                    r"(?:international|overseas)(?: students?)?(?: fees?)?", text, re.I
                ))
                continue
            if not international or re.search(
                r"part[\s-]*time|per\s+\d+\s+credits?|deposit|additional|"
                r"to be confirmed|not yet|scholarship", text, re.I
            ):
                continue
            amounts = re.findall(r"£\s*(\d+(?:,\s*\d{3})*(?:\.\d{1,2})?)", text)
            if len(amounts) != 1:
                continue
            amount = float(re.sub(r"[,\s]", "", amounts[0]))
            if amount <= 0:
                continue
            term = "Full Course" if re.search(
                r"full course|whole course|total(?: course)? (?:fee|cost)", text, re.I
            ) else "Annual" if year or re.search(r"per annum|per year|annual", text, re.I) else None
            if term:
                prices.add((amount, term))
                snippets.append(f"{title}; International: {text}")
        sections.append((year, prices, snippets))
    # Missing audience/year evidence is not permission to retain an AI or
    # generic domestic price. Keep the whole international tuple consistent.
    newest = max((year for year, _, _ in sections), default=0)
    prices = set().union(*(p for y, p, _ in sections if y == newest))
    for row in evidence:
        if row.get("field_key") == "international_fee":
            row["decision_status"] = "needs_review"
    payload.update(international_fee=None, currency="GBP", fee_term=None, fee_year=None)
    if len(prices) == 1:
        amount, term = next(iter(prices))
        payload.update(international_fee=amount, fee_term=term, fee_year=newest or None)
        snippet = " ".join(s for y, _, ss in sections if y == newest for s in ss)
        evidence.append({
            "field_key": "international_fee", "value": amount,
            "method": "fee.hsu_international_tuition", "confidence": 1.0,
            "source_url": url, "snippet": snippet,
        })

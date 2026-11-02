"""Course-owned HSU location and audience/year-scoped tuition."""
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def is_hsu_course(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"www.hsu.ac.uk", "hsu.ac.uk"} and bool(
        re.fullmatch(r"/course/[^/]+/?", parsed.path)
    )


def _apply_english(soup, url, payload, evidence):
    """Read course-owned international accordions, never navigation defaults."""
    scores = set()
    snippets = []
    sections = list(soup.select("details"))
    sections += [h.parent for h in soup.find_all(["h4", "h5"])
                 if re.search(r"international students|english language", h.get_text(), re.I)]
    for section in sections:
        summary = section.find("summary")
        if summary and not re.search(r"international|english|required|grades|entry", summary.get_text(), re.I):
            continue
        for paragraph in section.find_all(["p", "li"]):
            text = paragraph.get_text(" ", strip=True)
            if not re.search(r"IELTS|ILETS|International.*(?:Testing System|Test Score)|Undergraduate standard", text, re.I):
                continue
            overall = re.search(
                r"(?:overall(?:\s+score)?(?:\s+of)?|Undergraduate standard:)\s*(\d(?:\.\d)?)"
                r"|(\d(?:\.\d)?)\s*(?:overall|or above)"
                r"|(?:score of|level)\s*(\d(?:\.\d)?)", text, re.I)
            floor = re.search(
                r"(?:no less than|no (?:component|element|band|section) below)\s*(\d(?:\.\d)?)", text, re.I)
            if not overall or not floor:
                continue
            value = float(next(g for g in overall.groups() if g))
            band = float(floor[1])
            if 0 < band <= value <= 9:
                scores.add((value, band))
                snippets.append(text)
    if len(scores) != 1:
        return
    overall, band = next(iter(scores))
    for field, value in {"ielts_overall": overall, **{
        f"ielts_{part}": band for part in ("reading", "writing", "listening", "speaking")
    }}.items():
        payload[field] = value
        for row in evidence:
            if row.get("field_key") == field:
                row["decision_status"] = "needs_review"
        evidence.append({"field_key": field, "value": value,
                         "method": "english.hsu_course_requirement", "confidence": 1.0,
                         "source_url": url, "snippet": " ".join(dict.fromkeys(snippets))})


def apply_course_authority(html: str, url: str, payload: dict, evidence: list) -> None:
    if not is_hsu_course(url):
        return
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("nav, header, footer, script, style"):
        element.decompose()
    _apply_english(soup, url, payload, evidence)
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
    for heading in soup.find_all(["h2", "h3", "h4"]):
        title = heading.get_text(" ", strip=True)
        if not re.search(r"\btuition fees?\b", title, re.I):
            continue
        year_match = re.search(r"\b(20\d{2})", title)
        year = int(year_match[1]) if year_match else 0
        international = False
        prices = set()
        snippets = []
        for node in heading.find_next_siblings():
            if node.name in {"h2", "h3"} or re.search(
                r"additional.*cost|further information|more information", node.get_text(" ", strip=True), re.I
            ) and node.name in {"h4", "h5"}:
                break
            text = node.get_text(" ", strip=True)
            if node.name in {"h4", "h5"}:
                international = bool(re.fullmatch(
                    r"(?:international|overseas)(?: students?)?(?: fees?)?", text, re.I
                ))
                continue
            # A single paragraph can contain domestic and international lines.
            # Match the explicit audience label before considering any amount.
            inline = re.search(r"\b(?:International|Overseas)(?: students?)?\s*[–—:-]\s*(£[^<\n]+)",
                               node.get_text("\n", strip=True).replace("\n£", " £"), re.I)
            if inline:
                text = inline[1]
            elif re.search(r"course is only open to international applicants",
                           soup.get_text(" ", strip=True), re.I) and re.match(r"^£", text):
                # International-only programme: use its headline tuition,
                # not the bursary or the discounted payable amount.
                text = re.split(r"\.\s+(?=[A-Z])", text, maxsplit=1)[0]
            elif not international:
                continue
            if re.search(
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
            prices.add((amount, term))
            snippets.append(f"{title}; International: {text}")
        sections.append((year, prices, snippets))
    # Two older HSU templates put the fee year in prose rather than in a
    # Tuition Fees heading. Restrict these to a labelled Course Fees section.
    for heading in soup.find_all("h2"):
        if not re.fullmatch(r"Course Fees", heading.get_text(" ", strip=True), re.I):
            continue
        section = heading.find_parent("section")
        if section is None:
            continue
        text = section.get_text(" ", strip=True)
        # WordPress wraps the whole course in one section; discard everything
        # before the labelled fee block. A later "tuition fees policy" link
        # must not suppress the actual price.
        text = re.split(r"\bCourse Fees\b", text, maxsplit=1, flags=re.I)[-1]
        phd = re.search(
            r"course (20\d{2})/\d{2} entry.*?per annum.*?"
            r"International\s+£[\d,\s.]+\s+Part-time\s+£([\d,\s.]+)\s+Full-time",
            text, re.I)
        total = re.search(
            r"International\s+(20\d{2})/\d{2}\s+£([\d,\s.]+)\s+"
            r"These are the fees for the full MSc", text, re.I)
        match = total or phd
        if match:
            year = int(match[1])
            amount = float(re.sub(r"[,\s]", "", match[2]))
            sections.append((year, {(amount, "Full Course" if total else "Annual")},
                             [match[0]]))
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

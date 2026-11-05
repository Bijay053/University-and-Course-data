"""Excelsia's course-owned Elementor fact panels, not navigation or related awards."""
import asyncio
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def owned(url):
    return (urlsplit(url).hostname or "").lower() in {"excelsia.edu.au", "www.excelsia.edu.au"}


def facts(html, url):
    if not owned(url):
        return {}
    soup = BeautifulSoup(html, "lxml")
    # Elementor often splits one labelled fact across inline spans.
    for node in soup.select("h1, h2, h3, p, .elementor-heading-title"):
        value = node.get_text(" ", strip=True)
        node.clear()
        node.append(value)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.split(r"\s+[|–-]\s+Excelsia", title, flags=re.I)[0].strip()
    heading = soup.find("h1")
    if heading and re.match(r"^(?:Bachelor|Master|Graduate|Associate|Doctor|Diploma|Certificate|PhD)\b", heading.get_text(" ", strip=True), re.I):
        title = heading.get_text(" ", strip=True)
    if not re.match(r"^(?:Bachelor|Master|Graduate|Associate|Doctor|Diploma|Certificate|PhD)\b", title, re.I):
        return {}
    panels = []
    for heading in soup.find_all(["h1", "h2", "h3"]):
        if heading.get_text(" ", strip=True).casefold() != title.casefold():
            continue
        for parent in heading.parents:
            text = parent.get_text("\n", strip=True)
            if all(label in text.lower() for label in ("duration:", "tuition", "ielts")):
                if len(text) < 22000:
                    panels.append(text)
                break
    if not panels:
        return {}
    text = min(panels, key=len)
    # The first labelled facts belong to the heading; a second award can follow.
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    for index, value in enumerate(lines):
        if value.casefold() == title.casefold():
            lines = lines[index:]
            break
    for index, value in enumerate(lines[1:], 1):
        if value.casefold() != title.casefold() and re.match(r"^(?:Bachelor|Master|Graduate|Associate|Doctor|Diploma|Certificate|PhD)\b", value, re.I):
            lines = lines[:index]
            break
    def line(label):
        for index, value in enumerate(lines):
            if not re.match(label, value, re.I):
                continue
            parts = [value]
            for following in lines[index + 1:]:
                if re.match(r"^(?:Duration|Delivery|Credit|CRICOS|Available|IELTS|English|AQF|Tuition|Introductory tuition|Intake|Course accreditation|Download|Call|Apply)\b", following, re.I):
                    break
                parts.append(following)
            return " ".join(parts)
        return ""
    availability = line(r"Available to:")
    result = {"course_name": title, "_snippet": "\n".join(lines)[:6000],
              "_domestic_only": bool(re.fullmatch(
                  r"Available to:\s*Domestic(?: students)? only[.!]?",
                  availability.strip(), re.I))}
    from app.services.scraper.extractors.degree_level import classify_degree_level, _academic_level_from_degree
    degree, _, _ = classify_degree_level(title)
    academic = _academic_level_from_degree(degree) if degree else None
    if degree:
        result["degree_level"] = degree
    if academic:
        result["academic_level"] = academic
    english = re.split(r"\*\*|\bapplicants\b|\bconditional\b", line(r"(?:English:\s*)?IELTS"), maxsplit=1, flags=re.I)[0].strip()
    match = re.search(r"IELTS\s*:?\s*([4-9](?:\.\d)?)\b.*?(?:no band (?:less|lower) than|minimum band|no band below)\s*([4-9](?:\.\d)?)\b", english, re.I)
    if match:
        result["ielts_overall"], result["ielts_band"] = map(float, match.groups())
        for component in ("listening", "reading", "writing", "speaking"):
            result["ielts_" + component] = result["ielts_band"]
        result["_english_snippet"] = english
    else:
        # Teaching pages publish distinct component floors. Conditional-offer
        # alternatives must not replace the standard admission requirements.
        standard = english.split("**", 1)[0]
        overall = re.search(r"IELTS\s*:?\s*(?:Overall\s*)?([4-9](?:\.\d)?)\b", standard, re.I)
        components = {}
        for pair in re.finditer(
            r"\b(listening|reading|writing|speaking)(?:\s*(?:and|&)\s*(listening|reading|writing|speaking))?"
            r"\s*(?:at|of|:)?\s*([4-9](?:\.\d)?)\b", standard, re.I,
        ):
            for component in pair.group(1, 2):
                if component:
                    components[component.lower()] = float(pair.group(3))
        if overall and len(components) == 4:
            result["ielts_overall"] = float(overall.group(1))
            result.update({"ielts_" + key: value for key, value in components.items()})
            result["_english_snippet"] = standard
    fee = line(r"(?:Introductory\s+)?Tuition\s+[Ff]ee")
    if not result["_domestic_only"]:
        audience = re.search(r"International\s+students?\s*:?\s*(.*)", fee, re.I)
        if audience:
            segment = re.split(r"\bDomestic\b", audience[1], flags=re.I)[0]
            if not re.search(r"\$", segment) and not re.search(r"\bDomestic\b", fee, re.I):
                # e.g. "$11,000 semester/$44,000 in total for International students"
                segment = re.sub(r"^(?:Introductory\s+)?Tuition\s+Fee\s*:", "", fee, flags=re.I)
            amounts = list(re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)", segment))
            chosen, period = None, None
            for amount in amounts:
                suffix = segment[amount.end():].split("$", 1)[0]
                if re.search(r"\btotal\b|\bfull.course\b", suffix, re.I):
                    chosen, period = amount, "Full Course"
                    break
            if chosen is None and len(amounts) == 1:
                chosen = amounts[0]
                if re.search(r"\bannual\b|per year", segment, re.I):
                    period = "Annual"
            if chosen:
                result.update(international_fee=float(chosen[1].replace(",", "")), currency="AUD", fee_term=period)
                result["_fee_snippet"] = fee
    return result


async def fetch_course(url):
    """Try the usable public static response before paying for rendered shells."""
    if not owned(url):
        return None
    from app.services.scrape_assistant_tools import public_request
    try:
        raw = await asyncio.wait_for(public_request(url), 10)
        html = raw.decode("utf-8", errors="replace")
        return html if facts(html, url) else None
    except Exception:
        return None


def apply_authority(html, url, payload, evidence):
    values = facts(html, url)
    if not values:
        return
    for key, value in values.items():
        if key.startswith("_") or key == "ielts_band":
            continue
        payload[key] = value
        payload.setdefault("extraction_method", {})[key] = "excelsia.course_facts"
        if value is None:
            continue
        snippet = (values.get("_english_snippet") if key.startswith("ielts") else
                   values.get("_fee_snippet") if key in {"international_fee", "fee_term", "currency"} else values["_snippet"])
        evidence.append({"field_key": key, "value": value, "confidence": 1.0,
                         "method": "excelsia.course_facts", "source_url": url, "snippet": snippet})
    if str(payload.get("course_location") or "").strip().lower() in {"on campus", "on-campus", "online", "blended"}:
        payload["course_location"] = None
    if values["_domestic_only"]:
        payload["domestic_only"] = True
        payload["international_fee"] = None
        payload["fee_term"] = None
        payload["international_eligible"] = False

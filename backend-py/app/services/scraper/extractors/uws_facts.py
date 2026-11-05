"""UWS course-owned annual international tuition and admission English profiles."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .base import ExtractionResult

FEE_METHOD = "fee.uws_international_year_card"
ENGLISH_METHOD = "english.uws_admission_profile"
_COURSE = re.compile(r"/study/(undergraduate|postgraduate)/\1-course-search/[^/]+/?$")


def is_course(url: str) -> bool:
    parsed = urlsplit(url or "")
    return parsed.hostname in {"uws.ac.uk", "www.uws.ac.uk"} and bool(_COURSE.fullmatch(parsed.path))


def course_fee(html: str, url: str) -> tuple[bool, ExtractionResult | None]:
    if not is_course(url):
        return False, None
    soup = BeautifulSoup(html or "", "html.parser")
    groups = []
    for heading in soup.select("h2,h3,h4"):
        label = heading.get_text(" ", strip=True)
        year = re.search(r"\b(20\d{2})\s*[/–-]\s*(?:20)?\d{2}\b", label)
        if not year or not re.search(r"tuition fees", label, re.I):
            continue
        if re.search(r"modul|credit|part.time", label, re.I):
            continue
        if re.search(r"this online course", label, re.I):
            continue
        cards = heading.parent.select(".fee-block")
        prices = []
        for card in cards:
            audience = card.select_one("h4")
            if not audience or not re.match(r"^International\b", audience.get_text(" ", strip=True), re.I):
                continue
            text = card.get_text(" ", strip=True)
            amount = re.search(r"£\s*(\d[\d,]*(?:\.\d{1,2})?)", text)
            term = (
                "Annual" if re.search(r"per year of study", text, re.I)
                else "Full Course" if re.search(r"total cost of tuition", text, re.I)
                else None
            )
            if amount and term:
                prices.append((float(amount.group(1).replace(",", "")), term, label + " — " + text))
        groups.append((int(year.group(1)), prices))
    if not groups:
        # Module-only prices are not an annual or whole-award tuition amount.
        fee_section = any(
            re.search(r"tuition fees", h.get_text(" ", strip=True), re.I)
            for h in soup.select("h2,h3")
        )
        return fee_section, None
    newest = max(year for year, _ in groups)
    prices = [p for year, values in groups if year == newest for p in values]
    if len({(p[0], p[1]) for p in prices}) != 1:
        return True, None
    amount, term, snippet = prices[0]
    return True, ExtractionResult(
        field_key="international_fee", value=amount,
        normalized={"international_fee": amount, "currency": "GBP",
                    "fee_term": term, "fee_year": newest},
        confidence=1.0, snippet=snippet, method=FEE_METHOD,
    )


def course_english(html: str, url: str) -> tuple[bool, list[ExtractionResult]]:
    parsed = urlsplit(url or "")
    central = (
        parsed.hostname in {"uws.ac.uk", "www.uws.ac.uk"}
        and parsed.path.rstrip("/") == "/international/english-language-requirements"
    )
    if not is_course(url) and not central:
        return False, []
    soup = BeautifulSoup(html or "", "html.parser")
    specific = []
    for node in soup.select("p,li"):
        text = node.get_text(" ", strip=True)
        if "ielts" not in text.lower():
            continue
        previous = node.find_previous(re.compile(r"^h[1-6]$"))
        heading = previous.get_text(" ", strip=True).lower() if previous else ""
        if (
            re.search(r"pre.sessional|alternative|equivalent qualifications", heading, re.I)
            or re.search(r"pre.sessional|English Language for University Study|programmes that require IELTS|PTE Pearson|Occupational English Test", text, re.I)
            or "postgraduate research degrees" in heading
            or ("undergraduate" in heading and re.search(r"taught(?:\s+postgraduate)?\s+degrees", heading))
        ):
            continue
        score = re.search(r"IELTS(?: Academic)?(?: score)?(?: of)?\s*[:(]?\s*([4-9](?:\.[05])?)\b", text, re.I)
        if not score:
            continue
        floor = re.search(r"no (?:individual )?(?:component|element|band)(?: score)? (?:below|lower than)\s+([4-9](?:\.[05])?)\b", text, re.I)
        overall = float(score.group(1))
        band = float(floor.group(1)) if floor else (
            overall if re.search(r"minimum across all aspects", text, re.I) else None
        )
        specific.append((overall, band, text))
    if specific:
        if len({(overall, band) for overall, band, _ in specific}) != 1:
            return True, []
        overall, band, text = specific[0]
        values = {"ielts_overall": overall}
        values.update({f"ielts_{skill}": band
                       for skill in ("listening", "speaking", "reading", "writing")})
        return True, [ExtractionResult(
            field_key="ielts_overall", value=overall, normalized=values,
            confidence=1.0, snippet=text, method=ENGLISH_METHOD,
        )]
    identity = url + " " + " ".join(
        node.get_text(" ", strip=True) for node in soup.select("h1,title")
    )
    research = bool(re.search(
        r"\b(?:mres|mphil|phd|dba)\b|\bmasters? (?:of|by) research\b",
        identity, re.I,
    ))
    for heading in soup.select("h4"):
        label = heading.get_text(" ", strip=True)
        if research:
            match = "postgraduate research degrees" in label.lower()
        else:
            match = "undergraduate" in label.lower() and bool(
                re.search(r"taught(?:\s+postgraduate)?\s+degrees", label, re.I)
            )
        if not match:
            continue
        paragraph = heading.find_next_sibling()
        if paragraph is None or paragraph.name not in {"ul", "p"}:
            return True, []
        text = paragraph.get_text(" ", strip=True)
        overall = re.search(r"overall IELTS score of\s+([4-9](?:\.[05])?)\b", text, re.I)
        floor = re.search(r"no (?:lower|less) than\s+([4-9](?:\.[05])?)\b", text, re.I)
        if not overall or not floor:
            return True, []
        values = {"ielts_overall": float(overall.group(1))}
        values.update({f"ielts_{skill}": float(floor.group(1))
                       for skill in ("listening", "speaking", "reading", "writing")})
        # Additional course-specific admission scores must not be downgraded
        # to the shared taught/research profile. Leave those for explicit review.
        for node in soup.select("p,li"):
            line = node.get_text(" ", strip=True)
            if "ielts" not in line.lower() or "pre-sessional" in line.lower():
                continue
            previous = node.find_previous(re.compile(r"^h[1-6]$"))
            previous_text = previous.get_text(" ", strip=True).lower() if previous else ""
            if (
                "postgraduate research degrees" in previous_text
                or ("undergraduate" in previous_text and re.search(r"taught(?:\s+postgraduate)?\s+degrees", previous_text))
            ):
                continue
            higher = re.search(r"IELTS(?: Academic)?(?:\s+score)?(?:\s+of)?\s*[:(]?\s*([4-9](?:\.[05])?)\b", line, re.I)
            if higher and float(higher.group(1)) > values["ielts_overall"]:
                return True, []
        return True, [ExtractionResult(
            field_key="ielts_overall", value=values["ielts_overall"],
            normalized=values, confidence=1.0, snippet=label + " — " + text,
            method=ENGLISH_METHOD,
        )]
    return False, []


def course_details(html: str, url: str) -> dict:
    """Course-owned banner facts; campus restrictions stay attached to intakes."""
    if not is_course(url):
        return {}
    soup = BeautifulSoup(html or "", "html.parser")
    facts = {}
    for block in soup.select(".detail-item"):
        label = block.select_one(".e-title")
        if label:
            value = label.find_next_sibling("p")
            if value:
                facts[label.get_text(" ", strip=True).casefold()] = value.get_text(" ", strip=True)
    result = {}
    heading = soup.select_one("h1")
    award = facts.get("award", "")
    result["award_choices"] = [s.strip() for s in award.split("/") if s.strip()]
    if heading and award:
        name = heading.get_text(" ", strip=True)
        if name:
            result["course_name"] = name if name.casefold().startswith(award.casefold()) else f"{award} {name}"
    duration = facts.get("duration & study mode", "")
    result["duration_source"] = duration
    standard = re.sub(r"\b\d+(?:\.\d+)?\s*(?:years?|months?)\s*,?\s*full[-\s]time\s*\([^)]*(?:sandwich|placement)[^)]*\)", "", duration, flags=re.I)
    matches = re.findall(r"\b(\d+(?:\.\d+)?)\s*(years?|months?)\s*,?\s*full[-\s]time\b", standard, re.I)
    values={(float(n),"Year" if unit.lower().startswith("year") else "Month") for n,unit in matches}
    if len(values)==1:
        amount,unit=next(iter(values))
        result.update(duration=amount,duration_term=unit)
    starts = []
    for item in soup.select(".starting-dates li"):
        campus = item.select_one("span")
        if not campus:
            continue
        text = item.get_text(" ", strip=True)
        label = campus.get_text(" ", strip=True)
        value = text.removeprefix(label).strip()
        months = re.findall(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", value, re.I)
        if months:
            starts.append({"campus":label.rstrip(":").strip(),"months":list(dict.fromkeys(m.title() for m in months)),
                           "restriction": " ".join(re.findall(r"\([^)]*\)",value)), "source_text":text})
    if starts:
        result["campus_intakes"] = starts
    return result


def apply_course_authority(html: str, url: str, payload: dict, evidence: list) -> None:
    details = course_details(html,url)
    if not details:
        return
    ambiguous_award = len(details.get("award_choices", [])) > 1
    if ambiguous_award:
        warnings = list(payload.get("scrape_warnings") or [])
        note = "UWS award requires selection: " + " / ".join(details["award_choices"])
        if note not in warnings:
            warnings.append(note)
        payload["scrape_warnings"] = warnings
    if details.get("duration_source") and (
        re.search(r"sandwich|placement|honours pathway", details["duration_source"],re.I)
        or ("duration" not in details and "full" in details["duration_source"].lower())
    ):
        warnings = list(payload.get("scrape_warnings") or [])
        note = "UWS duration and pathways: " + details["duration_source"]
        if note not in warnings:
            warnings.append(note)
        payload["scrape_warnings"] = warnings
    for key in ("course_name","duration","duration_term"):
        if key in details and not ambiguous_award:
            payload[key] = details[key]
            evidence.append({"field_key":key,"value":details[key],"method":"uws.course_banner",
                             "confidence":1.0,"source_url":url,
                             "snippet":details.get("duration_source","") if key.startswith("duration") else str(details[key])})
    if details.get("campus_intakes"):
        starts = details["campus_intakes"]
        location = str(payload.get("course_location") or "").strip().casefold()
        locations = {s.strip() for s in location.split(",") if s.strip()}
        matched = [s for s in starts if s["campus"].casefold() in locations]
        # Never present a different campus's start as universally available.
        candidates = matched if len(matched)==len(locations) and len({tuple(s["months"]) for s in matched})==1 else []
        candidates = candidates or (starts if len(starts)==1 and not location else [])
        if candidates:
            months = list(dict.fromkeys(m for s in candidates for m in s["months"]))
            if set(months)!=set(payload.get("intake_months") or []):
                payload["intake_months"] = months
        evidence.append({"field_key":"intake_months","value":payload.get("intake_months") if candidates else None,
                         "method":"uws.campus_start_dates","confidence":1.0 if candidates else 0.0,"source_url":url,
                         "snippet":"; ".join(s["source_text"] for s in starts)})
        warnings = list(payload.get("scrape_warnings") or [])
        note = "Campus-specific starts: " + "; ".join(s["source_text"] for s in starts)
        if (not candidates or any(s["restriction"] for s in starts)
                or re.search(r"\b20\d{2}\b",note)) and note not in warnings:
            warnings.append(note)
        payload["scrape_warnings"] = warnings
    known, fee = course_fee(html, url)
    if known:
        for item in evidence:
            if item.get("field_key") == "international_fee":
                item["decision_status"] = "needs_review"
        for key in ("international_fee", "currency", "fee_term", "fee_year"):
            payload[key] = fee.normalized.get(key) if fee else None
        if fee:
            evidence.append({"field_key": fee.field_key, "value": fee.value,
                             "method": fee.method, "confidence": 1.0,
                             "source_url": url, "snippet": fee.snippet})
    known, english = course_english(html, url)
    if known:
        for key in ("ielts_overall", "ielts_listening", "ielts_speaking", "ielts_reading", "ielts_writing"):
            payload[key] = english[0].normalized.get(key) if english else None
        if english:
            evidence.append({"field_key": "ielts_overall", "value": english[0].value,
                             "method": ENGLISH_METHOD, "confidence": 1.0,
                             "source_url": url, "snippet": english[0].snippet})

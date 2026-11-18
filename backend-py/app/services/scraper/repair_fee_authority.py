"""Independent, fail-closed audience/qualification checks for missing-fee rules.

This is not a fee extractor: a learned selector still proposes the amount.
The guard only permits that amount when a course-owned, applicable tuition
panel independently supports it. Shared widgets are not universal authority.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

FEE_EVIDENCE_GUARD = "course_owned_tuition_v1"
_MONEY = re.compile(
    r"\btuition(?:\s+fees?)?\s*[:–-]?\s*"
    r"(?:(?:CAD|AUD|NZD|USD|GBP|EUR)\s*)?[$£€]\s*"
    r"(\d[\d,]*(?:\.\d{1,2})?)", re.I,
)
_AWARD = re.compile(
    r"\b(bachelor|master|doctor)(?:'s)?(?:\s+of)?\s+(.+?)"
    r"(?=\s+(?:fees?|tuition|for\s+international|international)|$)", re.I,
)


def _amount(value):
    try:
        return Decimal(re.sub(r"[^\d.]", "", str(value))).quantize(Decimal(".01"))
    except (InvalidOperation, ValueError):
        return None


def missing_fee_authority(html: str, value) -> dict:
    """Return evidence for a proposed amount, never trusting model assertions."""
    failure = {"supported": False, "reason": "No applicable course-owned international tuition panel"}
    proposed = _amount(value)
    if proposed is None:
        return failure
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("main, [role=main], article")
    if main is None:
        return failure
    for node in main.select(
        "nav, aside, footer, script, style, [hidden], [aria-hidden=true], "
        ".related-courses, .related-programs, [data-related-courses]"
    ):
        node.decompose()
    # Some course templates keep the document H1 in outer chrome and use an
    # H2 for the actual programme overview inside main (including Acadia).
    heading = main.find(["h1", "h2"])
    if heading is None:
        return failure
    name = " ".join(heading.stripped_strings).casefold()
    if not name:
        return failure
    # Only the overview introduces the award; shared fee panels, navigation
    # and related-course cards must not establish the current qualification.
    overview = []
    for node in main.select("p"):
        text = node.get_text(" ", strip=True)
        if (node.find_parent(["nav", "aside", "footer"])
                or re.search(r"\b(?:tuition|fees?|related courses?|other programs?)\b", text, re.I)):
            continue
        overview.append(text)
        if len(overview) == 3:
            break
    identity = name + " " + " ".join(overview)[:1800].casefold()
    undergraduate = bool(re.search(r"\b(?:bachelor|undergraduate|bsc|bba|beng|bcom|bed|ba)\b", identity))
    postgraduate = bool(re.search(r"\b(?:master|msc|mba|ma|meng)\b", identity))
    research = bool(re.search(r"\b(?:phd|doctor|doctoral|research degree)\b", identity))
    candidates = []
    for node in main.select("p, tr, dl, section, div, li, span"):
        text = node.get_text(" ", strip=True)
        if len(text) > 1000 or not re.search(r"\binternational\b", text, re.I):
            continue
        if re.search(r"\b(?:domestic|scholarship|bursary|deposit|application fee)\b", text, re.I):
            continue
        matches = list(_MONEY.finditer(text))
        if len(matches) != 1:
            continue
        match = matches[0]
        label = text[:match.start()].strip().casefold()
        award = _AWARD.search(label)
        specificity = 0
        if award:
            subject = award[2].strip()
            # "Bachelor of Education" is an exception, not a generic UG fee.
            if subject not in {"arts", "science", "sciences", "degrees", "degree"}:
                if not re.search(r"\b" + re.escape(subject) + r"\b", name):
                    continue
                specificity = 2
            if award[1].casefold() == "bachelor" and not undergraduate:
                continue
            if award[1].casefold() == "master" and not postgraduate:
                continue
            if award[1].casefold() == "doctor" and not research:
                continue
        elif re.search(r"\bundergraduate\b", label):
            if not undergraduate or postgraduate or research:
                continue
            specificity = 1
        elif re.search(r"\b(?:postgraduate|graduate)\b", label):
            if not postgraduate or undergraduate or research:
                continue
            specificity = 1
        else:
            # A named panel (e.g. Nursing fees) must match the current course.
            remainder = re.sub(
                r"\b(?:international|students?|tuition|fees?|for|annual|full|time|course|load|"
                r"per|year|the|and|of)\b|20\d{2}(?:[/–-](?:20)?\d{2})?|[^\w\s]",
                " ", label,
            )
            remainder = " ".join(remainder.split())
            if remainder:
                if remainder not in name:
                    continue
                specificity = 2
        candidates.append((specificity, _amount(match[1]), text))
    if not candidates:
        return failure
    strongest = max(item[0] for item in candidates)
    applicable = [item for item in candidates if item[0] == strongest]
    amounts = {item[1] for item in applicable}
    if len(amounts) != 1 or proposed not in amounts:
        return {"supported": False, "reason": "Proposed tuition conflicts with the applicable course panel"}
    return {"supported": True, "reason": "Course-owned international tuition matched",
            "course": name, "amount": str(proposed), "excerpt": applicable[0][2][:1000]}

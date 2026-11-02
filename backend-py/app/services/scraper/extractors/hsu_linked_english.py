"""Verified HSU course/award routes; never a university-wide English default."""
import asyncio
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .hsu_facts import _apply_english, is_hsu_course

log = logging.getLogger(__name__)
CENTRAL_URL = (
    "https://www.hsu.ac.uk/study/apply/how-to-apply/"
    "international-applicants/english-language-requirements/"
)
# Exact current course identities inspected against official pages. Foundation,
# apprenticeship, unknown awards and the explicitly excepted management MSc are
# deliberately absent. Scores are read from the fetched guidance, not this map.
COURSES = {
    "bsc-hons-physiotherapy": ("BSc (Hons) Physiotherapy", "Undergraduate"),
    "bsc-hons-human-nutrition": ("BSc (Hons) Human Nutrition", "Undergraduate"),
    "bsc-occupational-therapy": ("BSc (Hons) Occupational Therapy", "Undergraduate"),
    "bsc-hons-speech-and-language-therapy": ("BSc (Hons) Speech and Language Therapy", "Undergraduate"),
    "bsc-hons-sport-rehabilitation": ("BSc (Hons) Sport Rehabilitation", "Undergraduate"),
    "master-of-osteopathy": ("Master of Osteopathy", "Undergraduate"),
    "msc-osteopathy-pre-registration": ("MSc Osteopathy (Pre-registration)", "Postgraduate"),
}
FIELDS = ("ielts_overall", "ielts_reading", "ielts_writing", "ielts_listening", "ielts_speaking")


def _normal(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _scope(html, url):
    if not is_hsu_course(url):
        return None
    rule = COURSES.get(urlparse(url).path.strip("/").split("/")[-1])
    if not rule:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("nav, header, footer, script, style"):
        node.decompose()
    title = soup.find("h1")
    if not title or _normal(title.get_text()) != _normal(rule[0]):
        return None
    sections = [h.parent for h in soup.find_all(["h4", "h5"])
                if re.search(r"international students|english language", h.get_text(), re.I)]
    sections += [d for d in soup.select("details") if d.find("summary") and
                 re.search(r"international|english|entry|required", d.find("summary").get_text(), re.I)]
    text = " ".join(s.get_text(" ", strip=True) for s in sections)
    # A menu link isn't a referral. Require admissions-owned guidance or IELTS.
    if not re.search(r"english language requirements|IELTS", text, re.I):
        return None
    return soup, rule, text


def _central_pair(html, level, overall):
    soup = BeautifulSoup(html, "html.parser")
    candidates = set()
    for section in soup.select("details"):
        summary = section.find("summary")
        if not summary or summary.get_text(" ", strip=True) != "IELTS (Academic)":
            continue
        for row in section.select("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) != 2 or cells[0].get_text(strip=True) != level:
                continue
            text = cells[1].get_text(" ", strip=True)
            pairs = re.findall(
                r"(\d(?:\.\d)?)\s+overall\s*(?:with no less than\s*|\(\s*(?:minimum\s*)?)"
                r"(\d(?:\.\d)?)\s*(?:in\s+)?each component", text, re.I,
            )
            for a, b in pairs:
                a, b = float(a), float(b)
                if 0 < b <= a <= 9 and (overall is None or a == overall):
                    candidates.add((a, b, text))
    return next(iter(candidates)) if len(candidates) == 1 else None


async def recover_linked_english(html, url, payload, evidence, fetch):
    scoped = _scope(html, url)
    if scoped is None:
        return
    soup, (award, level), referral = scoped
    owned, owned_evidence = {}, []
    _apply_english(soup, url, owned, owned_evidence)
    if re.search(r"IELTS|ILETS|Undergraduate standard", referral, re.I) and not owned:
        return  # Unparsed/conflicting course-owned requirements need review.
    # Remove generic/AI/institutional guesses on this exact recovery cohort.
    # Course-owned values are restored even when the central request fails.
    for field in FIELDS:
        payload[field] = owned.get(field)
        for row in evidence:
            if row.get("field_key") == field:
                row["decision_status"] = "superseded"
    for candidate in owned_evidence:
        # apply_course_authority already emitted this proof. Reuse it rather
        # than inserting a duplicate whose superseded first copy would win
        # the evidence table's ON CONFLICT DO NOTHING.
        existing = next((row for row in evidence if all(
            row.get(key) == candidate.get(key)
            for key in ("field_key", "value", "method", "source_url", "snippet")
        )), None)
        if existing is not None:
            existing["decision_status"] = "needs_review"
        else:
            evidence.append(candidate)
    if all(owned.get(field) is not None for field in FIELDS):
        return
    # Postgraduate guidance offers alternatives: only a course-owned overall
    # can select one. Never infer an MSc requirement from "majority of courses".
    if level == "Postgraduate" and owned.get("ielts_overall") is None:
        return
    try:
        central = await asyncio.wait_for(fetch(CENTRAL_URL), timeout=20)
    except Exception as exc:
        log.warning("HSU linked English guidance unavailable (%s)", type(exc).__name__)
        return
    pair = _central_pair(central or "", level, owned.get("ielts_overall"))
    if pair is None:
        return
    overall, band, source_text = pair
    for field in FIELDS:
        if owned.get(field) is not None:
            continue
        value = overall if field == "ielts_overall" else band
        payload[field] = value
        evidence.append({
            "field_key": field, "value": value, "confidence": 1.0,
            "method": "english.hsu_linked_requirement", "source_url": CENTRAL_URL,
            "snippet": f"IELTS (Academic) — {level}: {source_text}\n"
                       f"Applicable award: {award}; course: {url}; "
                       f"course-owned overall: {owned.get('ielts_overall')}.",
        })

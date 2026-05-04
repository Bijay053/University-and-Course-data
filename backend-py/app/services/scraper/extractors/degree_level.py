"""Degree-level extractor.

Mirrors the Node implementation that gates `auto_publish_status`: a course
without a degree level can never be auto-published, so the review table
shows "--" in the Level column and the row is permanently stuck in
`pending_review`.

Strategy:
1. Course-name regex (highest confidence — the title nearly always says it).
2. Page-text regex against an explicit "Degree level" / "Award" / "Qualification"
   line if the title was inconclusive.
3. AQF-level pattern (Australian Qualifications Framework) maps numeric AQF
   levels to their canonical degree names — common on AU university pages
   (e.g. asahe.edu.au shows "AQF Level 7" for a Bachelor's).

Output is written to ``payload['degree_level']`` so it lands directly in
the ``scraped_courses.degree_level`` column via stage_course's payload-merge.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.services.scraper.extractors.base import ExtractionResult

field_key = "degree_level"

# Order matters: more specific patterns must come first so e.g. "Graduate
# Certificate" is not matched by the looser "certificate" rule.
# Graduate Diploma and Graduate Certificate are DIFFERENT qualifications:
#   Graduate Diploma  ≈ 1 year (AQF 8 / NZQF 7)
#   Graduate Certificate ≈ 6 months (AQF 8 / NZQF 7)
# Both are AQF/NZQF level 8 so we cannot distinguish from a numeric level
# alone — rely on the name pattern instead.
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(doctor(ate)?|ph\.?d|d\.?phil)\b", re.IGNORECASE), "Doctorate"),
    (re.compile(r"\bgraduate\s+diploma\b", re.IGNORECASE), "Graduate Diploma"),
    (re.compile(r"\bgraduate\s+certificate\b", re.IGNORECASE), "Graduate Certificate"),
    (re.compile(r"\bpostgraduate\s+diploma\b", re.IGNORECASE), "Graduate Certificate"),
    (re.compile(r"\bpostgraduate\s+certificate\b", re.IGNORECASE), "Graduate Certificate"),
    (re.compile(r"\b(master('?s)?|mba|m\.?sc|m\.?eng|m\.?ed|m\.?phil)\b", re.IGNORECASE), "Master's"),
    (re.compile(r"\b(bachelor('?s)?|b\.?sc|b\.?eng|b\.?ed|b\.?a|b\.?bus)\b", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bassociate\s+degree\b", re.IGNORECASE), "Associate Degree"),
    (re.compile(r"\badvanced\s+diploma\b", re.IGNORECASE), "Advanced Diploma"),
    (re.compile(r"\bdiploma\b", re.IGNORECASE), "Diploma"),
    (re.compile(r"\bcertificate\b", re.IGNORECASE), "Certificate"),
)

# AQF (Australian Qualifications Framework) numeric level → degree name.
# Source: https://www.aqf.edu.au/aqf-levels — official AU mapping.
_AQF_LEVEL_TO_DEGREE: dict[str, str] = {
    "1": "Certificate",
    "2": "Certificate",
    "3": "Certificate",
    "4": "Certificate",
    "5": "Diploma",
    "6": "Advanced Diploma",
    "7": "Bachelor's",
    "8": "Graduate Certificate",
    "9": "Master's",
    "10": "Doctorate",
}

_AQF_RE = re.compile(r"\bAQF\s*Level\s*(\d{1,2})\b", re.IGNORECASE)

# Page-text rule: scan for an explicit qualification line. Limited to a
# narrow window of the page text so we don't accidentally pick up "bachelor"
# from prose about other programs.
_PAGE_LABELS = re.compile(
    r"(?:degree\s+level|award|qualification|level\s+of\s+study)\s*[:\-]\s*([^\n<]{3,80})",
    re.IGNORECASE,
)

# Strip HTML tags cheaply for body-text scanning. Real parser is overkill —
# we only need rough text proximity for the AQF / label rules.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(html: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or ""))


def _classify_text(text: str, patterns: Iterable[tuple[re.Pattern[str], str]]) -> str | None:
    for pattern, label in patterns:
        if pattern.search(text):
            return label
    return None


def classify_degree_level(course_name: str, page_text: str = "") -> tuple[str | None, str, str | None]:
    """Return (degree_level, method, snippet).

    ``method`` is one of ``name``, ``aqf``, ``label``, ``unknown`` so the
    caller can record provenance. Pure helper — no I/O — so unit tests can
    pin behavior without mocking.
    """
    name = (course_name or "").strip()
    if name:
        hit = _classify_text(name, _NAME_PATTERNS)
        if hit:
            return hit, "name", name[:200]

    plain = _strip_tags(page_text)

    aqf_match = _AQF_RE.search(plain)
    if aqf_match:
        level = aqf_match.group(1)
        degree = _AQF_LEVEL_TO_DEGREE.get(level)
        if degree:
            start = max(0, aqf_match.start() - 30)
            return degree, "aqf", plain[start : aqf_match.end() + 30].strip()

    label_match = _PAGE_LABELS.search(plain)
    if label_match:
        line = label_match.group(1)
        hit = _classify_text(line, _NAME_PATTERNS)
        if hit:
            return hit, "label", label_match.group(0)[:200]

    return None, "unknown", None


async def extract(html: str, url: str, course_name: str | None = None) -> list[ExtractionResult]:
    # The pipeline doesn't pass ``course_name`` directly — by the time this
    # extractor runs the course-name extractor has populated payload, but
    # extractors are independent so we re-derive a best-effort name from
    # the <title> tag if needed.
    name = course_name or _title_from_html(html) or ""
    degree, method, snippet = classify_degree_level(name, html)
    if not degree:
        return []
    confidence = {"name": 0.9, "aqf": 0.8, "label": 0.75}.get(method, 0.5)
    return [
        ExtractionResult(
            field_key=field_key,
            value=degree,
            normalized={"degree_level": degree},
            confidence=confidence,
            method=f"degree_level:{method}",
            snippet=snippet,
        )
    ]


_TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,300})</title>", re.IGNORECASE)


def _title_from_html(html: str) -> str | None:
    if not html:
        return None
    m = _TITLE_RE.search(html)
    return m.group(1).strip() if m else None

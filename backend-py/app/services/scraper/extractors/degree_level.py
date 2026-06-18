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

# Canonical degree level values accepted by the system.
# Any value NOT in this set is rejected at the staging chokepoint and
# re-inferred from the course name.  Keeping this in one place avoids the
# guard and the classifier drifting out of sync.
CANONICAL_DEGREE_LEVELS: frozenset[str] = frozenset({
    "Bachelor's",
    "Master's",
    "Doctorate",
    "Graduate Certificate",
    "Graduate Diploma",
    "Associate Degree",
    "Advanced Diploma",
    "Diploma",
    "Certificate",
    "Foundation",
})

# Order matters: more specific patterns must come first so e.g. "Graduate
# Certificate" is not matched by the looser "certificate" rule.
# Graduate Diploma and Graduate Certificate are DIFFERENT qualifications:
#   Graduate Diploma  ≈ 1 year (AQF 8 / NZQF 7)
#   Graduate Certificate ≈ 6 months (AQF 8 / NZQF 7)
# Both are AQF/NZQF level 8 so we cannot distinguish from a numeric level
# alone — rely on the name pattern instead.
_NAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ── Doctorate ────────────────────────────────────────────────────────────
    # EdD (Doctor of Education), DBA (Doctor of Business Administration) added.
    (re.compile(r"\b(doctor(ate)?|ph\.?d|d\.?phil|ed\.?d|dba)\b", re.IGNORECASE), "Doctorate"),
    # ── Graduate Diploma / Certificate ───────────────────────────────────────
    (re.compile(r"\bgraduate\s+diploma\b", re.IGNORECASE), "Graduate Diploma"),
    (re.compile(r"\bgraduate\s+certificate\b", re.IGNORECASE), "Graduate Certificate"),
    # A Postgraduate Diploma (AQF 8, 1 year) ≠ Graduate Certificate (6 months).
    (re.compile(r"\bpostgraduate\s+diploma\b", re.IGNORECASE), "Graduate Diploma"),
    (re.compile(r"\bpostgraduate\s+certificate\b", re.IGNORECASE), "Graduate Certificate"),
    # UK-specific abbreviations — must precede the bare "\bdiploma\b" and
    # "\bcertificate\b" catch-alls below so "PGDip Business" is not mis-classified
    # as a plain "Diploma" and "PGCE Primary" is not left unclassified.
    #   PGCE  = Postgraduate Certificate in Education (UK teacher training)
    #   PGDip = Postgraduate Diploma
    #   PGCert = Postgraduate Certificate
    (re.compile(r"\bPGCE\b", re.IGNORECASE), "Graduate Certificate"),
    (re.compile(r"\bPG\s*Dip\b|\bPGDip\b", re.IGNORECASE), "Graduate Diploma"),
    (re.compile(r"\bPG\s*Cert\b|\bPGCert\b", re.IGNORECASE), "Graduate Certificate"),
    # ── Master's ─────────────────────────────────────────────────────────────
    # MA (Master of Arts), MPH (Master of Public Health), LLM (Master of Laws)
    # added. MPharm MUST come before MPH/MA to avoid partial match on "MPh".
    # Integrated masters (e.g. "Integrated Master of Engineering") also mapped.
    (re.compile(
        r"\b(master('?s)?|mba|m\.?sc|m\.?eng|m\.?ed|m\.?phil|m\.?res|m\.?arch"
        r"|m\.?ph|ll\.?m|m\.?a)\b",
        re.IGNORECASE,
    ), "Master's"),
    (re.compile(r"\bintegrated\s+masters?\b", re.IGNORECASE), "Master's"),
    # ── Bachelor's ───────────────────────────────────────────────────────────
    # UK nursing/midwifery/law/pharmacy abbreviations added:
    #   BNurs = Bachelor of Nursing
    #   BMid  = Bachelor of Midwifery
    #   LLB   = Bachelor of Laws
    #   MPharm = Master of Pharmacy (UK 4-year integrated undergraduate degree)
    # Foundation Degrees (FdA / FdSc) and Higher Nationals (HNC / HND) also
    # mapped to Bachelor's level per institution convention.
    (re.compile(r"\b(b\.?nurs|b\.?mid)\b", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bll\.?b\b", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bm\.?pharm\b", re.IGNORECASE), "Bachelor's"),
    # Foundation Degrees — long forms used by Wolverhampton and other UK universities:
    #   "Fd (Arts) Early Years Services"  → Bachelor's
    #   "Fd (Science) Sports Coaching"    → Bachelor's
    #   "Foundation Degree in ..."        → Bachelor's
    # The short abbreviations FdA / FdSc are kept below as a fallback.
    (re.compile(r"\bfd\s*\(\s*arts?\s*\)", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bfd\s*\(\s*sci(?:ence)?\s*\)", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bfoundation\s+degree\b", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bfd(?:a|sc)\b", re.IGNORECASE), "Bachelor's"),  # FdA, FdSc
    (re.compile(r"\bhn[cd]\b", re.IGNORECASE), "Bachelor's"),   # HNC, HND
    # Existing broad bachelor patterns — BSc (Hons) style covered by b\.?sc;
    # BHons added explicitly for courses whose title leads with the honours tag.
    # BBA (Bachelor of Business Administration) and BSW (Bachelor of Social Work)
    # and BSN (Bachelor of Science in Nursing) added for US/UK university patterns.
    (re.compile(r"\b(bachelor('?s)?|b\.?sc|b\.?eng|b\.?ed|b\.?a|b\.?bus|bba|bsw|bsn)\b", re.IGNORECASE), "Bachelor's"),
    (re.compile(r"\bb\.?hons?\b", re.IGNORECASE), "Bachelor's"),
    # ── Sub-degree ───────────────────────────────────────────────────────────
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

    # Strategy 4: page-lead scan — degree abbreviation at the very start of the
    # stripped page text (first 150 chars, no label required).  Catches UK
    # universities like ARU where the qualification (e.g. "BEng (Hons)") is
    # the first content element before any explicit "Degree level:" label, e.g.:
    #   "BEng (Hons) With placement With foundation year 5 years part-time …"
    page_lead = plain.lstrip()[:150]
    hit = _classify_text(page_lead, _NAME_PATTERNS)
    if hit:
        return hit, "page_lead", page_lead.strip()[:150]

    # Strategy 5: body-qualification scan — search full page text for a
    # pattern "[DegreeQualifier] [course_name]".  Handles CMS / JS frameworks
    # (e.g. Roehampton) that dynamically strip the degree prefix from <title>
    # so the title becomes "AI and Digital Media" instead of
    # "MA AI and Digital Media", but the qualifier still appears in the page
    # body (e.g. "MA AI and Digital Media is a studio-based …").
    if name:
        _escaped = re.escape(name)
        _body_qual_re = re.compile(
            r"\b(MA|MSc|MBA|MArch|MRes|MPhil|MEng|MEd|MLaw|LLM|MFA|MDes"
            r"|MComp|MChem|MPhys|MMath|MSocSc"
            r"|BSc|BA|BEng|BEd|BArts|BNurs|BBus|BBA|BSW|BArch"
            r"|PGCert|PGDip|PGCE|Graduate\s+Certificate|Graduate\s+Diploma"
            r"|PhD|DPhil|DBA|EdD)\b"
            r"(?:\s+\([Hh]ons?\))?"
            # Allow optional preposition: "MA in AI…", "BSc of Computing…"
            r"\s+(?:(?:in|of|for|by|with|on)\s+)?"
            + _escaped,
            re.IGNORECASE,
        )
        _body_m = _body_qual_re.search(plain)
        if _body_m:
            _qual = _body_m.group(1)
            _hit = _classify_text(_qual, _NAME_PATTERNS)
            if _hit:
                return _hit, "body_qual", _body_m.group(0)[:120]

    return None, "unknown", None


def _from_bcu_panel(html: str) -> tuple[str | None, str, str | None]:
    """BCU-specific degree-level extractor: reads the 'Award' field from
    the structured course facts panel ``div.course__key-info__inner``.

    BCU course pages show two related fields in the panel:
        Award: BA (Hons) / BSc (Hons) / MSc / PhD / MBA / HND / …
        Level: Foundation / Undergraduate / Postgraduate Taught / Postgraduate Research

    Strategy: try 'Award' first (most specific), fall back to 'Level'
    with a BCU-specific Level→canonical mapping.  Returns (degree, method,
    snippet) matching the shape of ``classify_degree_level``.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None, "unknown", None

    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one("div.course__key-info__inner")
    if not panel:
        return None, "unknown", None

    award: str | None = None
    level: str | None = None
    for li in panel.select("li"):
        title_el = li.select_one("span.title")
        value_el = li.select_one("span.value")
        if not title_el or not value_el:
            continue
        key = title_el.get_text(strip=True).lower()
        val = value_el.get_text(strip=True)
        if key == "award":
            award = val
        elif key == "level":
            level = val

    # Primary: classify the Award field (BA (Hons), MSc, PhD, HND, etc.)
    if award:
        hit = _classify_text(award, _NAME_PATTERNS)
        if hit:
            return hit, "bcu_panel_award", award[:200]

    # Secondary: map BCU's Level descriptor to canonical degree_level.
    # BCU uses plain English descriptions, not abbreviations.
    _BCU_LEVEL_MAP: dict[str, str] = {
        "foundation": "Foundation",
        "undergraduate": "Bachelor's",
        "postgraduate taught": "Master's",
        "postgraduate research": "Doctorate",
        "professional": "Master's",
    }
    if level:
        mapped = _BCU_LEVEL_MAP.get(level.lower().strip())
        if mapped:
            return mapped, "bcu_panel_level", level[:200]

    return None, "unknown", None


async def extract(html: str, url: str, course_name: str | None = None) -> list[ExtractionResult]:
    # The pipeline doesn't pass ``course_name`` directly — by the time this
    # extractor runs the course-name extractor has populated payload, but
    # extractors are independent so we re-derive a best-effort name from
    # the <title> tag if needed.
    #
    # BCU structural pre-pass — reads 'Award' (BA (Hons)/MSc/PhD/…) directly
    # from the course facts panel div.course__key-info__inner, falling back
    # to the 'Level' descriptor (Undergraduate/Postgraduate Taught/…).
    # Runs before generic text classification to prevent degree-level noise
    # from other-course listings, nav text, or page prose.
    _bcu_degree, _bcu_method, _bcu_snippet = _from_bcu_panel(html)
    if _bcu_degree:
        return [
            ExtractionResult(
                field_key=field_key,
                value=_bcu_degree,
                normalized={"degree_level": _bcu_degree},
                confidence=0.97,
                method=f"degree_level:{_bcu_method}",
                snippet=_bcu_snippet,
            )
        ]

    name = course_name or _title_from_html(html) or ""
    degree, method, snippet = classify_degree_level(name, html)
    if not degree:
        return []
    confidence = {"name": 0.9, "aqf": 0.8, "label": 0.75, "page_lead": 0.65}.get(method, 0.5)
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

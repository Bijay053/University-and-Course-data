"""SearchStax Solr provider for University of Huddersfield.

The Huddersfield course catalogue is served from a SearchStax-hosted Solr
core — the same endpoint the live ``courses.hud.ac.uk`` React SPA queries
client-side. The old Scrapy spider hit ``courses.hud.ac.uk/json/...`` which
now returns the SPA HTML shell, not JSON, and ``www.hud.ac.uk`` is a
Cloudflare-protected SPA that HTML BFS cannot crawl.

This provider queries Solr directly: a paginated sweep returns all ~790
course documents, each with structured fields PLUS the full page-text
``content`` field (≈15 KB, carrying IELTS + entry requirements). No
per-course page fetch is needed.

Each Solr doc is mapped to a fully-formed staged-course result shaped
exactly like :func:`orchestrator._extract_only`'s output —
``{name, url, payload, evidence}`` — which the orchestrator embeds under
``link["searchstax_result"]`` and returns verbatim, so the prebuilt
records flow through the normal dedup + staging loop in ``run_scrape``.

Fee bands and the subject→band lookup are ported from the user's original
Scrapy spider (the spider never scraped fees from the page either — it
applied a central subject-area schedule). The fee is therefore a
band-derived estimate the operator reviews, not an on-page value; its
evidence row points at the central fee schedule page.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

import httpx

from app.services.scraper.config.schema import SearchStaxConfig

log = logging.getLogger("scraper.searchstax_hud")

# Solr fields we request (everything the mapper needs).
_FIELDS = (
    "h1,searchTitle,url,id,study_level_s,start_dates_s,"
    "duration_t,flags_list_ss,year_s,content"
)


# ── Fee bands (ported from the user's Scrapy spider) ────────────────────────
# International 2025-26 per-year tuition. The spider matched a subject
# substring inside the course title (case-insensitive). We check the most
# specific / most expensive band first so specialised health & science
# subjects are not shadowed by the broad £16,500 base list (e.g. PG
# "Public Health" → 18,700, not 16,500 via the "Health" substring).
_UG_BANDS: list[tuple[int, list[str]]] = [
    (18700, [
        "midwifery", "physiotherapy", "podiatry", "occupational therapy",
        "operating department practice", "perioperative",
        "nursing", "paramedic", "paramedic practice", "paramedic science",
        "radiography", "diagnostic radiography", "therapeutic radiography",
        "dental hygiene", "dental therapy", "dental",
        "orthoptics", "audiology",
        "speech and language therapy",
        "operating department",
    ]),
    (17600, [
        "computing", "engineering", "games development", "mathematics",
        "music", "social work", "geography", "science", "biological",
        "chemistry", "forensic", "pharmacy", "pharmacology", "optometry",
        "biochemistry", "biomedical", "biomedicine",
        "biology", "genetics", "microbiology", "immunology", "neuroscience",
        "analytical", "data science",
        "computer science", "cyber security", "information technology",
        "software", "electronic", "electrical", "mechanical", "civil",
        "chemical", "environmental", "architectural",
    ]),
    (16500, [
        "accountancy", "finance", "business", "economics", "events",
        "hospitality", "law", "logistics", "marketing", "tourism",
        "trade", "international trade", "international relations",
        "education", "health", "psychology", "social sciences", "sport",
        "youth and community", "architecture", "art", "design", "drama",
        "english", "fashion", "journalism", "media", "history", "music",
        "music technology", "counselling", "criminology", "criminal justice",
        "crime", "policing", "quantity surveying", "construction", "surveying",
        "photography", "animation", "film", "graphic", "illustration",
        "product design", "product innovation", "interior", "textile",
        "tesol", "teaching english", "teacher", "languages",
    ]),
]
_PG_BANDS: list[tuple[int, list[str]]] = [
    (18700, [
        "pharmaceutical science", "hbs master's with professional practice",
        "health studies", "public health", "biological", "chemistry",
        "forensic", "podiatry", "computing", "engineering", "mathematics",
        "music technology", "sound production msc", "science",
        "nursing", "clinical pharmacy", "pharmacy", "pharmacology", "paramedic",
        "radiography", "dental", "physiotherapy", "occupational therapy",
        "perioperative", "audiology", "speech and language", "biomedical",
        "biomedicine", "biology", "genetics", "microbiology", "biochemistry",
        "analytical", "data science", "computer science", "cyber security",
        "information technology", "software", "electronic", "electrical",
        "mechanical", "civil", "chemical", "automotive", "advanced project",
        "advanced practice", "clinical", "investigative psychology",
    ]),
    (17600, [
        "architecture", "art", "design", "drama", "english", "fashion",
        "history", "journalism", "media", "musicology", "music technology",
        "sound production", "education", "psychology", "social sciences",
        "social work", "sport", "youth and community",
        "health and human sciences", "counselling", "criminology",
        "criminal justice", "crime", "policing", "animation", "film",
        "creative music", "costume", "fine art",
        "illustration", "photography", "textile",
    ]),
    (16500, [
        "accountancy", "finance", "business", "economics", "events",
        "hospitality", "law", "logistics", "marketing", "tourism",
        "trade", "international", "quantity surveying", "construction",
        "housing", "leadership", "management", "information systems",
        "intelligence and analytics", "banking", "accounting",
        "strategic", "project management",
        "career development", "employability",
        "tesol", "teaching english", "teacher",
    ]),
]


def _fee_for(title: str, study_level: str) -> tuple[Optional[int], Optional[str]]:
    """Return (annual_fee, matched_subject) for a course title.

    Matches a subject substring in the title, most-specific band first.
    Returns (None, None) when no subject matches (rare — reviewer fills).
    """
    t = (title or "").lower()
    bands = _PG_BANDS if "postgrad" in (study_level or "").lower() else _UG_BANDS
    for fee, subjects in bands:
        for subj in subjects:
            if subj in t:
                return fee, subj
    return None, None


# ── Course-name reformatting ────────────────────────────────────────────────
# Huddersfield titles place the qualification at the END ("Building Surveying
# BSc(Hons)"). The staging degree-qualifier guard (guards._DEGREE_QUALIFIER_RE)
# is anchored at the START, so we reformat each title to LEAD with the full
# degree phrase ("Bachelor of Science Building Surveying"). The mapping below
# is ordered most-specific-first; the first qualification token found in the
# title wins.
_QUAL_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\bPgCert\b", re.I), "Postgraduate Certificate", "Postgraduate Certificate"),
    (re.compile(r"\bPgDip\b", re.I), "Postgraduate Diploma", "Postgraduate Diploma"),
    (re.compile(r"\bPGCE\b", re.I), "Postgraduate Certificate in Education", "Postgraduate Certificate"),
    (re.compile(r"\bMBA\b", re.I), "Master of Business Administration", "Master's"),
    (re.compile(r"\bMSci\b", re.I), "Master of Science (Integrated)", "Master's"),
    (re.compile(r"\bMEng\b", re.I), "Master of Engineering", "Master's"),
    (re.compile(r"\bMChem\b", re.I), "Master of Chemistry", "Master's"),
    (re.compile(r"\bMPharm\b", re.I), "Master of Pharmacy", "Master's"),
    (re.compile(r"\bMArch\b", re.I), "Master of Architecture", "Master's"),
    (re.compile(r"\bMMus\b", re.I), "Master of Music", "Master's"),
    (re.compile(r"\bMRes\b", re.I), "Master of Research", "Master's"),
    (re.compile(r"\bMPhil\b", re.I), "Master of Philosophy", "Master's"),
    (re.compile(r"\bMPH\b", re.I), "Master of Public Health", "Master's"),
    (re.compile(r"\bLLM\b", re.I), "Master of Laws", "Master's"),
    (re.compile(r"\bMSc\b", re.I), "Master of Science", "Master's"),
    (re.compile(r"\bMA\b", re.I), "Master of Arts", "Master's"),
    (re.compile(r"\bBSc\b", re.I), "Bachelor of Science", "Bachelor's"),
    (re.compile(r"\bBEng\b", re.I), "Bachelor of Engineering", "Bachelor's"),
    (re.compile(r"\bBMus\b", re.I), "Bachelor of Music", "Bachelor's"),
    (re.compile(r"\bLLB\b", re.I), "Bachelor of Laws", "Bachelor's"),
    (re.compile(r"\bBA\b", re.I), "Bachelor of Arts", "Bachelor's"),
    (re.compile(r"\bPhD\b", re.I), "Doctor of Philosophy", "Doctorate"),
    (re.compile(r"\bEdD\b", re.I), "Doctor of Education", "Doctorate"),
    (re.compile(r"\bDBA\b", re.I), "Doctor of Business Administration", "Doctorate"),
    (re.compile(r"\bFdSc\b", re.I), "Foundation Degree", "Foundation Degree"),
    (re.compile(r"\bFdA\b", re.I), "Foundation Degree", "Foundation Degree"),
]

# Titles that ALREADY lead with a guard-recognised degree word need no
# reformat; we just derive the degree_level from the leading phrase.
_LEADING_LEVEL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*postgraduate\s+certificate", re.I), "Postgraduate Certificate"),
    (re.compile(r"^\s*postgraduate\s+diploma", re.I), "Postgraduate Diploma"),
    (re.compile(r"^\s*graduate\s+certificate", re.I), "Graduate Certificate"),
    (re.compile(r"^\s*graduate\s+diploma", re.I), "Graduate Diploma"),
    (re.compile(r"^\s*foundation\s+degree", re.I), "Foundation Degree"),
    (re.compile(r"^\s*(?:master|m[a-z]{1,4}\b)", re.I), "Master's"),
    (re.compile(r"^\s*bachelor", re.I), "Bachelor's"),
    (re.compile(r"^\s*(?:doctor|phd)", re.I), "Doctorate"),
    (re.compile(r"^\s*diploma", re.I), "Diploma"),
    (re.compile(r"^\s*certificate", re.I), "Certificate"),
]


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s{2,}", " ", s).strip(" -–—,")


def _reformat_name(title: str, study_level: str) -> tuple[Optional[str], Optional[str]]:
    """Return (staged_name, degree_level) or (None, None) to skip the doc.

    1. Trailing qualification token → lead with the full degree phrase.
    2. Title already starts with a degree word → keep as-is.
    3. No qualification, Postgraduate level → badge as a Postgraduate
       Certificate (Huddersfield's CPD short courses are PG credit-bearing).
    4. Otherwise → skip (cannot classify; would be a category-page reject).
    """
    title = _clean_spaces(title or "")
    if not title:
        return None, None

    for pat, lead, level in _QUAL_RULES:
        m = pat.search(title)
        if m:
            subject = _clean_spaces(title[: m.start()] + " " + title[m.end():])
            if not subject:
                return _clean_spaces(lead), level
            return _clean_spaces(f"{lead} {subject}"), level

    for pat, level in _LEADING_LEVEL:
        if pat.search(title):
            return title, level

    if "postgrad" in (study_level or "").lower():
        return _clean_spaces(f"Postgraduate Certificate {title}"), "Postgraduate Certificate"

    return None, None


# ── Academic level (derived from degree_level) ──────────────────────────────
_UG_LEVELS = frozenset({"Bachelor's", "Foundation Degree", "Diploma", "Certificate"})
_PG_LEVELS = frozenset({
    "Master's", "Postgraduate Certificate", "Postgraduate Diploma",
    "Graduate Certificate", "Graduate Diploma",
})


def _academic_level(degree_level: Optional[str]) -> Optional[str]:
    if not degree_level:
        return None
    if degree_level in _UG_LEVELS:
        return "Undergraduate"
    if degree_level in _PG_LEVELS:
        return "Postgraduate"
    if "Doctorate" in (degree_level or ""):
        return "Doctorate"
    return None


# ── Other requirement (entry requirements from page content) ─────────────────
# Match the first complete sentence that mentions an academic entry requirement.
_ENTRY_REQ_RE = re.compile(
    r"""(?:(?:entry\s+requirements?[:\s]+)|(?:normally\s+)|(?:you.{0,20}need\s+))
        ([^.!?]{20,250}[.!?])""",
    re.I | re.X,
)
_DEGREE_REQ_RE = re.compile(
    r"""(
        (?:(?:first|second|third)[- ]class|2:1|2:2|honors?|honours?|undergraduate\s+degree
           |a\s+degree|bachelor|masters?|postgraduate|gpa\s+\d|grade\s+\w)
        [^.!?]{0,200}[.!?]
    )""",
    re.I | re.X,
)
# Reject _ENTRY_REQ_RE matches that are really English-language / IELTS sentences.
_LANG_REQ_RE = re.compile(
    r"english\s+language|language\s+qualif|ielts|pte|toefl|language\s+requirement",
    re.I,
)


def _extract_entry_requirement(content: str) -> Optional[str]:
    """Return a short academic-entry-requirement sentence from page content.

    Strategy:
    1. Try the explicit degree-classification pattern first (most precise).
    2. Fall back to the "entry requirements" / "normally" / "you need" anchor
       only when the captured snippet does NOT look like an English-language or
       IELTS requirement (which share similar sentence patterns).
    """
    if not content:
        return None
    # 1. Degree-classification anchor — highest precision
    m = _DEGREE_REQ_RE.search(content)
    if m:
        snippet = _clean_spaces(m.group(1))
        if len(snippet) > 15:
            return snippet[:300]
    # 2. Broad entry-requirements anchor — skip if it's an English/IELTS sentence
    m2 = _ENTRY_REQ_RE.search(content)
    if m2:
        snippet = _clean_spaces(m2.group(1))
        if len(snippet) > 15 and not _LANG_REQ_RE.search(snippet):
            return snippet[:300]
    return None


# ── Duration / study-mode parsing ───────────────────────────────────────────
_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(year|month|week)", re.I)


def _parse_duration(duration_t: str) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (duration_value, duration_term, study_mode) from duration_t.

    e.g. "2-3 years part-time" → (2.0, "Years", "Part-time")
         "3 years full-time"   → (3.0, "Years", "Full-time")
    """
    raw = duration_t or ""
    low = raw.lower()
    mode = "Part-time" if "part" in low else ("Full-time" if "full" in low else None)
    m = _DUR_RE.search(raw)
    if not m:
        return None, None, mode
    val = float(m.group(1))
    unit = m.group(2).lower()
    term = {"year": "Years", "month": "Months", "week": "Weeks"}[unit]
    return val, term, mode


# ── Intake months ───────────────────────────────────────────────────────────
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_MONTH_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\b", re.I)


def _parse_intakes(start_dates_s: str) -> Optional[list[str]]:
    """Extract unique month names from a start-dates string.

    "6 July 2026" → ["July"];  "Multiple start dates"/blank → None.
    """
    s = start_dates_s or ""
    if not s or "ultiple" in s:
        return None
    seen: list[str] = []
    for m in _MONTH_RE.finditer(s):
        name = m.group(1).capitalize()
        if name not in seen:
            seen.append(name)
    return seen or None


# ── IELTS extraction from page text ─────────────────────────────────────────
_IELTS_OVERALL_RE = re.compile(r"IELTS\D{0,40}?(\d(?:\.\d)?)\s*overall", re.I)
_IELTS_BAND_RE = re.compile(r"lower than\s*(\d(?:\.\d)?)", re.I)


def _extract_ielts(content: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (overall, per_band_min, snippet) parsed from page content."""
    if not content:
        return None, None, None
    m = _IELTS_OVERALL_RE.search(content)
    if not m:
        return None, None, None
    overall = float(m.group(1))
    # Snippet: window around the IELTS mention.
    idx = m.start()
    snippet = _clean_spaces(content[max(0, idx - 10): idx + 120])
    band_m = _IELTS_BAND_RE.search(content, m.end(), m.end() + 80)
    band = float(band_m.group(1)) if band_m else None
    return overall, band, snippet


def _first(v: Any) -> Any:
    """Solr returns some fields as single-element lists; unwrap them."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


# Anchors that mark the start of the real prose overview in the page text,
# past the "HomeSearch results<title>Apply Now...Start Dates...Duration..."
# navigation cruft. Tried in order; first hit wins.
_DESC_ANCHORS = (
    "Why choose Huddersfield for this course?",
    "About this course",
)


def _build_description(content: str, name: str) -> Optional[str]:
    """Best-effort short course description from the page text.

    The ``content`` field is the full rendered page text, prefixed with nav
    boilerplate. We skip to the first overview anchor, then take a
    sentence-bounded chunk so the review modal shows real prose.
    """
    if not content:
        return None
    start = 0
    for anchor in _DESC_ANCHORS:
        i = content.find(anchor)
        if i != -1:
            start = i + len(anchor)
            break
    # The source text often runs sentences together ("UK.Enables doctors");
    # re-insert a space after sentence punctuation followed by a capital.
    snippet = re.sub(r"([.!?])([A-Z])", r"\1 \2", content[start: start + 800])
    snippet = _clean_spaces(snippet)
    cut = max(snippet.rfind(". "), snippet.rfind("! "), snippet.rfind("? "))
    if cut > 150:
        snippet = snippet[: cut + 1]
    snippet = snippet.strip()
    return snippet or None


def _map_doc(doc: dict, cfg: SearchStaxConfig) -> Optional[dict]:
    """Map one Solr doc → a {name, url, searchstax_result} link dict.

    Returns None when the doc cannot be classified into a staged course.
    """
    url = _first(doc.get("url")) or _first(doc.get("id"))
    if not url:
        return None
    raw_title = _first(doc.get("h1")) or _first(doc.get("searchTitle")) or ""
    study_level = _first(doc.get("study_level_s")) or ""

    # ── Flags-based filters ──────────────────────────────────────────────────
    flags: list[str] = list(doc.get("flags_list_ss") or [])

    # Apprenticeship = UK employer-funded scheme; never open to international
    # visa students.
    if "Apprenticeship" in flags:
        log.info("[SEARCHSTAX] skip (apprenticeship): %r", raw_title)
        return None

    # Research Degree stubs (MRes / MPhil by-research entries) have no
    # structured fee / IELTS / intake data in Solr — they map to 31%
    # completeness and are not actionable for international admissions.
    if "Research Degree" in flags:
        log.info("[SEARCHSTAX] skip (research degree stub): %r", raw_title)
        return None

    name, degree_level = _reformat_name(raw_title, study_level)
    if not name:
        log.info("[SEARCHSTAX] skip (unclassifiable title): %r", raw_title)
        return None

    duration_t = _first(doc.get("duration_t")) or ""
    duration, duration_term, study_mode = _parse_duration(duration_t)

    # ── Part-time on-campus filter ────────────────────────────────────────────
    # UK Student Visa requires full-time study. On-campus part-time courses are
    # therefore domestic-only. Distance-learning part-time courses can be
    # studied from abroad and are kept.
    is_distance = "Distance Learning" in flags
    if study_mode == "Part-time" and not is_distance:
        log.info("[SEARCHSTAX] skip (part-time on-campus, domestic only): %r", raw_title)
        return None
    intakes = _parse_intakes(_first(doc.get("start_dates_s")) or "")
    content = _first(doc.get("content")) or ""
    ielts_overall, ielts_band, ielts_snippet = _extract_ielts(content)
    fee, fee_subject = _fee_for(raw_title, study_level)
    acad_level = _academic_level(degree_level)
    entry_req = _extract_entry_requirement(content)

    payload: dict[str, Any] = {
        "course_name": name,
        "degree_level": degree_level,
        "course_location": "Huddersfield",
        "course_website": url,
        "description": _build_description(content, name),
        "has_central_fee_page": True,
    }
    if study_mode:
        payload["study_mode"] = study_mode
    if duration is not None:
        payload["duration"] = duration
        payload["duration_term"] = duration_term
    if intakes:
        payload["intake_months"] = intakes
    if fee is not None:
        payload["international_fee"] = float(fee)
        payload["fee_term"] = "Year"
        payload["fee_year"] = cfg.fee_year
        payload["currency"] = cfg.currency
    if ielts_overall is not None:
        payload["ielts_overall"] = ielts_overall
        if ielts_band is not None:
            payload["ielts_listening"] = ielts_band
            payload["ielts_reading"] = ielts_band
            payload["ielts_writing"] = ielts_band
            payload["ielts_speaking"] = ielts_band
    if acad_level is not None:
        payload["academic_level"] = acad_level
    if entry_req is not None:
        payload["other_requirement"] = entry_req

    def _ev(field_key, value, method, source, page_type, snippet, confidence):
        return {
            "field_key": field_key,
            "value": value,
            "normalized": value,
            "source_url": source,
            "page_type": page_type,
            "method": method,
            "snippet": snippet,
            "confidence": confidence,
            "decision_status": "selected",
        }

    evidence: list[dict[str, Any]] = []

    # Course identity fields
    evidence.append(_ev(
        "course_name", name, "searchstax:h1", url, "course",
        f"Solr h1/searchTitle: {raw_title}", 0.95,
    ))
    evidence.append(_ev(
        "degree_level", degree_level, "searchstax:title_reformat", url, "course",
        f"Qualification token extracted from title: {raw_title}", 0.9,
    ))
    evidence.append(_ev(
        "course_location", "Huddersfield", "searchstax:hardcoded", url, "course",
        "All Huddersfield courses delivered at Huddersfield campus (or Distance Learning).", 0.99,
    ))

    # Duration / mode from Solr duration_t
    if duration_t:
        if duration is not None:
            evidence.append(_ev(
                "duration", duration, "searchstax:duration_t", url, "course",
                f"Solr duration_t: {duration_t}", 0.85,
            ))
        if study_mode:
            evidence.append(_ev(
                "study_mode", study_mode, "searchstax:duration_t", url, "course",
                f"Solr duration_t: {duration_t}", 0.8,
            ))

    # Intake months from Solr start_dates_s
    if intakes:
        start_dates_raw = _first(doc.get("start_dates_s")) or ""
        evidence.append(_ev(
            "intake_months", ", ".join(intakes), "searchstax:start_dates_s", url, "course",
            f"Solr start_dates_s: {start_dates_raw}", 0.85,
        ))

    # International fee from central band schedule
    if fee is not None:
        evidence.append(_ev(
            "international_fee", fee, "searchstax:fee_band",
            cfg.central_fee_page or url, "central_fee",
            (
                f"International tuition fee band for "
                f"{(fee_subject or 'this subject area').title()}: "
                f"£{fee:,}/year ({cfg.fee_year}-{(cfg.fee_year + 1) % 100:02d})"
            ),
            0.7,
        ))

    # IELTS from Solr content field
    if ielts_overall is not None:
        evidence.append(_ev(
            "ielts_overall", ielts_overall, "searchstax:content", url, "course",
            ielts_snippet or f"IELTS {ielts_overall} overall", 0.85,
        ))
        if ielts_band is not None:
            for sub in ("ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking"):
                evidence.append(_ev(
                    sub, ielts_band, "searchstax:content", url, "course",
                    f"No element lower than {ielts_band} (derived from IELTS band requirement)", 0.75,
                ))

    # Academic level derived from degree_level
    if acad_level is not None:
        evidence.append(_ev(
            "academic_level", acad_level, "searchstax:degree_level_derived", url, "course",
            f"Derived from degree level: {degree_level} → {acad_level}", 0.95,
        ))

    # Other/entry requirement extracted from page content
    if entry_req is not None:
        evidence.append(_ev(
            "other_requirement", entry_req, "searchstax:content_entry_req", url, "course",
            f"Entry requirement extracted from page content: {entry_req[:120]}", 0.7,
        ))

    result = {"name": name, "url": url, "payload": payload, "evidence": evidence}
    return {"name": name, "url": url, "searchstax_result": result}


def _first_str(doc: dict, *field_names: str) -> str:
    """Return the first non-empty string value found in ``doc`` for any of
    the given field names.  Handles both scalar and list-valued Solr fields
    (takes the first list element when the value is a list).
    """
    for field in field_names:
        v = doc.get(field)
        if v is None:
            continue
        if isinstance(v, list):
            v = v[0] if v else None
        if v is not None:
            s = str(v).strip()
            if s:
                return s
    return ""


def _resolve_token(cfg: SearchStaxConfig) -> Optional[str]:
    """Resolve the auth token using a 4-level priority chain.

    1. cfg.authorization_token  — new preferred field
    2. os.environ[cfg.token_env] — per-uni env var name
    3. cfg.token                 — legacy literal field
    4. os.environ["SEARCHSTAX_TOKEN"] — global fallback
    """
    if cfg.authorization_token:
        return cfg.authorization_token
    if cfg.token_env:
        env_val = os.environ.get(cfg.token_env)
        if env_val:
            return env_val
    if cfg.token:
        return cfg.token
    # Global fallback — set once in the environment, works for any uni
    return os.environ.get("SEARCHSTAX_TOKEN") or None


_MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}

_LEVEL_MAP = {
    "undergraduate": "Undergraduate",
    "postgraduate": "Postgraduate",
    "postgraduate taught": "Postgraduate",
    "postgraduate research": "Postgraduate",
    "doctorate": "Postgraduate",
    "phd": "Postgraduate",
}

_MODE_MAP = {
    "full time": "Full-time",
    "full-time": "Full-time",
    "part time": "Part-time",
    "part-time": "Part-time",
    "online": "Online",
    "distance learning": "Online",
    "blended": "Hybrid",
    "hybrid": "Hybrid",
}


def _parse_intake_months_from_dates(dates_raw: str) -> list[str]:
    """Extract unique month names from date strings in any word order.

    Handles both 'September 2026' (month-first) and '14 September 2026'
    (day-first, e.g. WLV Solr multi_course_start_date_ss format).
    Scans for a month name anywhere within each comma/semicolon-separated
    token rather than assuming the month is the first word.

    Returns a list of month names in order first seen, e.g. ['September', 'March'].
    intake_months is stored as JSONB (array); callers must NOT join to a string.
    """
    seen: list[str] = []
    for token in re.split(r"[,;/\n]+", dates_raw):
        token = token.strip()
        if not token:
            continue
        for word in token.split():
            if word.lower() in _MONTH_NAMES:
                month = word.capitalize()
                if month not in seen:
                    seen.append(month)
                break  # one month per date token
    return seen


def _normalize_study_mode(raw: str) -> str:
    """Normalise a Solr study-mode string to canonical values."""
    key = raw.strip().lower()
    return _MODE_MAP.get(key, raw.strip())


def _normalize_academic_level(raw: str) -> str:
    """Normalise a Solr degree-level string to Undergraduate/Postgraduate."""
    key = raw.strip().lower()
    return _LEVEL_MAP.get(key, raw.strip())


# Integrated masters awards — degree type starts with M + subject abbrev and
# confers a master's qualification even though entry is at undergraduate level.
# Universities (e.g. Durham) tag these as "Undergraduate" in their Solr index
# because students enrol via UCAS, but our academic_level field should reflect
# the award level (Postgraduate) for correct classification and fee tier.
_INTEGRATED_MASTERS_RE = re.compile(
    r"\bM(?:Chem|Eng|Math|Phys|Biol|Sci|Dent|Nurs|Arch|Vet|Opt|Earth)\b",
    re.I,
)


# ── Degree-level normalisation for field_map_as_payload universities ─────────
# Solr docs carry the specific award abbreviation (e.g. "BA (Hons)", "MSc").
# The system's `degree_level` field on scraped_courses / courses uses broad
# canonical categories.  Map every known abbreviation to the canonical value.
_FIELD_MAP_DEGREE_LEVEL: dict[str, str] = {
    # Undergraduate Bachelor's
    "ba": "Bachelor", "ba (hons)": "Bachelor",
    "bsc": "Bachelor", "bsc (hons)": "Bachelor",
    "beng": "Bachelor", "beng (hons)": "Bachelor",
    "llb": "Bachelor", "llb (hons)": "Bachelor",
    "bmus": "Bachelor", "bmus (hons)": "Bachelor",
    "bfa": "Bachelor", "bfa (hons)": "Bachelor",
    "bed": "Bachelor", "bed (hons)": "Bachelor",
    "bsw": "Bachelor", "bsw (hons)": "Bachelor",
    # Integrated masters (M-prefix with (Hons)) — system treats as Master
    "mchem (hons)": "Master", "meng (hons)": "Master",
    "mmath (hons)": "Master", "mphys (hons)": "Master",
    "mbiol (hons)": "Master", "msci (hons)": "Master",
    "march (hons)": "Master", "mvet sci (hons)": "Master",
    # Taught postgraduate masters
    "msc": "Master", "ma": "Master", "mba": "Master",
    "llm": "Master", "mres": "Master", "mphil": "Master",
    "mph": "Master", "mds": "Master", "med": "Master",
    "mpa": "Master", "mfa": "Master", "msw": "Master",
    "mmus": "Master",
    # Postgraduate certificates / diplomas
    "pgce": "Graduate Certificate & Diploma",
    "pcert": "Graduate Certificate & Diploma",
    "pdip": "Graduate Certificate & Diploma",
    "gdip": "Graduate Certificate & Diploma",
    "pgdip": "Graduate Certificate & Diploma",
    "pgcert": "Graduate Certificate & Diploma",
    "postgraduate certificate": "Graduate Certificate & Diploma",
    "postgraduate diploma": "Graduate Certificate & Diploma",
    # Doctorates
    "phd": "Doctor/Doctorate", "dphil": "Doctor/Doctorate",
    "edd": "Doctor/Doctorate", "dba": "Doctor/Doctorate",
    "md": "Doctor/Doctorate", "dsc": "Doctor/Doctorate",
}


def _normalise_field_map_degree_level(award: str) -> str:
    """Map a Solr degree abbreviation to the system's canonical degree_level.

    Falls back to the raw award string if not in the table so no data is lost.
    """
    return _FIELD_MAP_DEGREE_LEVEL.get(award.strip().lower(), award.strip())


def _slug_to_name(url: str) -> str:
    """Derive a human-readable fallback name from a course URL slug."""
    # e.g. .../accounting-with-study-abroad-n410/ → "Accounting with Study Abroad N410"
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-([a-z]{1,4}\d+)$", r" \1", slug)  # detach code suffix
    return slug.replace("-", " ").title()


def _map_doc_field_map(
    doc: dict,
    cfg: SearchStaxConfig,
    *,
    fee_defaults: dict | None = None,
    force_fee_stage: bool = False,
    ielts_defaults: dict | None = None,
    default_ielts: float | None = None,
) -> Optional[dict]:
    """Map a Solr doc → ``{name, url, searchstax_result}`` using cfg.field_map.

    Used when ``cfg.field_map_as_payload`` is True: builds a fully-formed
    staged-course payload from structured Solr fields without fetching the
    individual course page.  Intended for universities (e.g. Durham) whose
    Solr docs carry course metadata (name, level, duration, mode, intakes,
    department) but do NOT have fees or IELTS in the Solr index.

    ``fee_defaults``  — dict mapping tier → int fee (e.g. {"undergraduate": 26400})
    ``force_fee_stage`` — set has_central_fee_page=True even when no fee default matches
    ``ielts_defaults`` — dict mapping tier → {"ielts": 6.5, "pte": 59, ...}
    ``default_ielts``  — flat IELTS fallback when no tier matches

    Evidence rows use method ``searchstax:field_map`` with tier authority 1.5
    (above HTML heuristics, below PDF/AI).
    """
    _fm = cfg.field_map or {}
    _url_field   = _fm.get("url",          "url_t")
    _name_field  = _fm.get("name",         "title_t")
    _type_field  = _fm.get("degree_type",  "award_s")
    _level_field = _fm.get("degree_level", "study_level_s")
    _mode_field  = _fm.get("study_mode",   "mode_s")
    _dur_field   = _fm.get("duration",     "duration_t")
    _date_field  = _fm.get("intake_dates", "start_dates_s")
    _cat_field   = _fm.get("category",     "subject_s")
    _loc_field   = _fm.get("location",     None)

    url = _first_str(doc, _url_field, "id")
    if not url:
        return None

    # If the Solr url field contains a bare course code (e.g. "WR006J01UMU")
    # rather than a real HTTP URL, construct a full URL using url_base.
    # This avoids the domain-guard blocking all links and gives course_website
    # a browsable link.
    if "://" not in url and cfg.url_base:
        url = cfg.url_base.rstrip("/") + "/" + url.lower()

    raw_title = _first_str(doc, _name_field)
    award     = _first_str(doc, _type_field)

    if raw_title:
        if award and not raw_title.lower().startswith(award.lower()):
            name = f"{award} {raw_title}"
        else:
            name = raw_title
    elif award:
        name = award
    else:
        name = _slug_to_name(url)

    payload: dict[str, Any] = {"course_name": name, "course_website": url}
    evidence: list[dict] = []

    def _ev(field: str, value: Any, method: str) -> None:
        evidence.append({
            "field_key": field,
            "value": str(value),
            "snippet": f"[SearchStax:{method}] {field}={value}",
            "method": f"searchstax:{method}",
            "source_url": url,
            "entity_type": "course",
            "authority": 1.5,
            "confidence": 0.85,
            "decision_status": "selected",
            "selected": True,
        })

    _ev("course_name", name, "field_map")

    if award:
        # Normalise to system canonical values (Bachelor / Master /
        # Graduate Certificate & Diploma / Doctor/Doctorate) rather than
        # storing the raw Solr abbreviation (BA (Hons), MSc, etc.).
        normalised_degree_level = _normalise_field_map_degree_level(award)
        payload["degree_level"] = normalised_degree_level
        _ev("degree_level", normalised_degree_level, "field_map")

    raw_level = _first_str(doc, _level_field)
    if raw_level:
        acad = _normalize_academic_level(raw_level)
        # Integrated masters (MChem, MEng, MMath, MPhys, MBiol, MSci …) are
        # enrolled at undergraduate entry via UCAS — Durham's Solr tags them
        # "Undergraduate".  But they confer a master's award so academic_level
        # should be "Postgraduate" for correct classification AND fee tier
        # (PG fee default applies instead of UG fee default).
        if acad == "Undergraduate" and award and _INTEGRATED_MASTERS_RE.search(award):
            acad = "Postgraduate"
        payload["academic_level"] = acad
        _ev("academic_level", acad, "field_map")

    raw_mode_vals = doc.get(_mode_field, [])
    if isinstance(raw_mode_vals, list):
        raw_mode_vals = [v for v in raw_mode_vals if v]
    elif raw_mode_vals:
        raw_mode_vals = [raw_mode_vals]
    if raw_mode_vals:
        if cfg.exclude_part_time:
            # Normalise first so we compare canonical strings.
            norm_modes = [_normalize_study_mode(str(m)) for m in raw_mode_vals]
            ft_modes = [m for m in norm_modes if "part" not in m.lower()]
            if not ft_modes:
                # Pure Part-time course — skip entirely.
                return None
            # Mixed: keep only Full-time modes.
            raw_mode_vals = ft_modes
            modes = ", ".join(ft_modes)
        else:
            modes = ", ".join(_normalize_study_mode(str(m)) for m in raw_mode_vals)
        payload["study_mode"] = modes
        _ev("study_mode", modes, "field_map")

    # Collect ALL values from the (potentially multi-valued) duration field.
    # Multi-valued Solr fields like multi_duration_ss may carry both modes:
    #   ["Part-time (8 years)", "Full-time (4 years)"]
    # _first_str only returns index[0], so when exclude_part_time is set we
    # would pick the Part-time duration and show 8 years instead of 4 years.
    # Fix: read every entry and prefer Full-time when exclude_part_time: true.
    _all_durs: list[str] = []
    _raw_dur_vals = doc.get(_dur_field)
    if isinstance(_raw_dur_vals, list):
        _all_durs = [str(v).strip() for v in _raw_dur_vals if v]
    elif _raw_dur_vals:
        _all_durs = [str(_raw_dur_vals).strip()]

    raw_dur: str = ""
    if _all_durs:
        if cfg.exclude_part_time:
            # Prefer full-time entries; fall back to first value only when the
            # list is exclusively part-time (that course would have already been
            # skipped via the mode filter above, so this is a safety net).
            _ft_durs = [d for d in _all_durs if "part" not in d.lower()]
            raw_dur = _ft_durs[0] if _ft_durs else _all_durs[0]
        else:
            raw_dur = _all_durs[0]

    if raw_dur:
        # scraped_courses.duration is NUMERIC(6,2) — never write the raw text
        # (e.g. "3 years full-time") here or the INSERT flush fails with a
        # decimal ConversionSyntax error. Parse into (value, term, mode).
        _dur_val, _dur_term, _dur_mode = _parse_duration(raw_dur)
        if _dur_val is not None:
            payload["duration"] = _dur_val
            _ev("duration", _dur_val, "field_map")
            if _dur_term:
                payload["duration_term"] = _dur_term
                _ev("duration_term", _dur_term, "field_map")
        if _dur_mode and not payload.get("study_mode"):
            payload["study_mode"] = _dur_mode
            _ev("study_mode", _dur_mode, "field_map")

    raw_dates_vals = doc.get(_date_field, [])
    if isinstance(raw_dates_vals, list):
        dates_blob = ", ".join(str(v) for v in raw_dates_vals if v)
    else:
        dates_blob = str(raw_dates_vals) if raw_dates_vals else ""
    if dates_blob:
        months = _parse_intake_months_from_dates(dates_blob)
        if months:
            payload["intake_months"] = months
            _ev("intake_months", months, "field_map")

    raw_cat = _first_str(doc, _cat_field)
    if raw_cat:
        payload["category"] = raw_cat
        _ev("category", raw_cat, "field_map")

    if _loc_field:
        raw_loc_vals = doc.get(_loc_field, [])
        if isinstance(raw_loc_vals, list):
            raw_loc_vals = [v for v in raw_loc_vals if v]
        elif raw_loc_vals:
            raw_loc_vals = [raw_loc_vals]
        if raw_loc_vals:
            _strip_pfx = cfg.location_strip_prefixes or []
            cleaned: list[str] = []
            for _lv in raw_loc_vals:
                _lv = str(_lv)
                for _pfx in _strip_pfx:
                    if _lv.startswith(_pfx):
                        _lv = _lv[len(_pfx):]
                        break
                cleaned.append(_lv.strip())
            loc = ", ".join(cleaned)
            payload["course_location"] = loc
            _ev("course_location", loc, "field_map")

    if cfg.location_override:
        payload["course_location"] = cfg.location_override
        _ev("course_location", cfg.location_override, "location_override")

    # ── Fee degree_level_defaults fallback ──────────────────────────────────
    # fee_defaults and force_fee_stage are resolved by fetch_searchstax_links
    # before the doc loop (avoiding ContextVar timing issues) and passed in
    # as plain dicts so this sync function needs no async/ContextVar access.
    if payload.get("international_fee") in (None, "", 0):
        _acad_lvl = str(payload.get("academic_level", "")).lower()
        if "undergraduate" in _acad_lvl:
            _tier_key = "undergraduate"
        elif any(k in _acad_lvl for k in ("postgraduate", "doctorate", "phd")):
            _tier_key = "postgraduate"
        else:
            _tier_key = None
        _fdl = (fee_defaults or {}).get(_tier_key) if _tier_key else None
        if not _fdl:
            _fdl = (fee_defaults or {}).get("postgraduate") or (fee_defaults or {}).get("undergraduate")
        if _fdl:
            payload["international_fee"] = float(_fdl)
            payload["has_central_fee_page"] = True
            _ev("international_fee", _fdl, "degree_level_default")
        elif force_fee_stage:
            payload["has_central_fee_page"] = True

    # Always stamp currency + fee_term so the UI renders the correct symbol.
    # cfg.currency defaults to "GBP"; override per-uni in YAML (discovery.searchstax.currency).
    payload["currency"] = cfg.currency
    payload["fee_term"] = "year"  # degree_level_defaults are always annual fees

    # ── IELTS degree_level_defaults fallback ─────────────────────────────────
    # ielts_defaults is a dict of tier → {"ielts": float, "pte": int, ...}
    # default_ielts is the flat fallback when no tier matches.
    if payload.get("ielts_overall") is None and (ielts_defaults or default_ielts is not None):
        _acad_lvl2 = str(payload.get("academic_level", "")).lower()
        if "undergraduate" in _acad_lvl2:
            _ielts_tier = "undergraduate"
        elif any(k in _acad_lvl2 for k in ("postgraduate", "doctorate", "phd")):
            _ielts_tier = "postgraduate"
        else:
            _ielts_tier = None
        _eng_band = (ielts_defaults or {}).get(_ielts_tier) if _ielts_tier else None
        _ielts_val = float(_eng_band["ielts"]) if (_eng_band and _eng_band.get("ielts")) else (
            float(default_ielts) if default_ielts is not None else None
        )
        if _ielts_val is not None:
            payload["ielts_overall"] = _ielts_val
            _ev("ielts_overall", _ielts_val, "degree_level_default")
        if _eng_band:
            for _test, _fld in (("pte", "pte_overall"), ("toefl", "toefl_overall"), ("duolingo", "duolingo_overall")):
                _tval = _eng_band.get(_test)
                if _tval is not None and payload.get(_fld) is None:
                    payload[_fld] = int(_tval)
                    _ev(_fld, _tval, "degree_level_default")

    return {
        "name": name,
        "url": url,
        "searchstax_result": {
            "name": name,
            "url": url,
            "payload": payload,
            "evidence": evidence,
        },
    }


async def _fetch_links_only(cfg: SearchStaxConfig, emit=None) -> list[dict]:
    """SearchStax discovery-only mode (``cfg.links_only = True``).

    Queries the Solr core and returns plain ``{name, url}`` link dicts —
    **without** a ``searchstax_result`` key.  The orchestrator's normal
    per-course HTTP/browser extraction pipeline then fetches each URL to
    extract fees, IELTS, and all other fields.

    Use for universities (e.g. WLV) whose Solr docs do NOT contain fees or
    IELTS scores but whose individual course pages are reachable by the
    browser pool.  Solr is used purely as a complete URL catalogue (bypassing
    the browser BFS crawler which misses Cloudflare-paginated listing pages).

    WLV-specific field mapping:
      ``title_t``  — course name (e.g. "MSc Engineering Management")
      ``award_s``  — degree abbreviation (e.g. "MSc", "BA (Hons)")
      ``url_t``    — canonical course URL

    The name is reformatted as "{award_s} {title_t}" when award_s is not
    already the first token of title_t (avoids "MSc MSc Engineering…").
    """
    token = _resolve_token(cfg)
    headers: dict = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"

    _filter = cfg.filter_query or ""

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:  # noqa: BLE001
                pass

    links: list[dict] = []
    skipped = 0
    start = 0
    page_size = max(1, int(cfg.page_size or 100))
    total: Optional[int] = None
    _retried_unfiltered = False

    await _emit(
        f"[SEARCHSTAX links_only] Querying Solr "
        f"({'fq=' + _filter if _filter else 'unfiltered'}) ..."
    )

    # Resolve field names from field_map (YAML-configurable) with defaults.
    # Default keys match the original WLV field names for backward compatibility.
    _fm = cfg.field_map or {}
    _url_field    = _fm.get("url",          "url_t")
    _name_field   = _fm.get("name",         "title_t")
    _type_field   = _fm.get("degree_type",  "award_s")
    _level_field  = _fm.get("degree_level", "study_level_s")
    _mode_field   = _fm.get("study_mode",   "mode_s")
    _dur_field    = _fm.get("duration",     "duration_t")
    _date_field   = _fm.get("intake_dates", "start_dates_s")
    _cat_field    = _fm.get("category",     "subject_s")
    # Request only the fields we will actually use.
    _fl_fields = ",".join({
        _url_field, _name_field, _type_field, _level_field,
        _mode_field, _dur_field, _date_field, _cat_field,
    })

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params: dict = {
                "q": "*:*",
                "rows": str(page_size),
                "start": str(start),
                "fl": _fl_fields,
                "wt": "json",
            }
            if _filter:
                params["fq"] = _filter
            params.update(cfg.extra_params or {})

            try:
                resp = await client.get(cfg.endpoint, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[SEARCHSTAX links_only] fetch failed (start=%d): %s", start, exc)
                break

            response = data.get("response", {})
            docs = response.get("docs", [])

            # Auto-fallback: if the filter returned 0 on the first page, retry
            # without it — some cores use different sectionType values.
            if start == 0 and not docs and _filter and not _retried_unfiltered:
                _retried_unfiltered = True
                await _emit(f"[SEARCHSTAX links_only] {_filter!r} returned 0 — retrying unfiltered ...")
                log.info("[SEARCHSTAX links_only] fq=%r returned 0 — retrying unfiltered", _filter)
                _filter = ""
                continue

            if total is None:
                total = int(response.get("numFound", 0))
                await _emit(f"[SEARCHSTAX links_only] {total} course docs found.")

            if not docs:
                break

            for doc in docs:
                # URL: try mapped field first, then 'id' as universal fallback
                url = _first_str(doc, _url_field, "id")
                if not url:
                    skipped += 1
                    continue
                title = _first_str(doc, _name_field)
                award = _first_str(doc, _type_field)
                # Build name: prepend award/degree-type when not already the prefix
                if title:
                    if award and not title.lower().startswith(award.lower()):
                        name = f"{award} {title}"
                    else:
                        name = title
                elif award:
                    name = award
                else:
                    # Derive readable name from URL slug rather than using raw URL
                    name = _slug_to_name(url)
                link: dict = {"name": name, "url": url}
                # Carry pre-fetched structured metadata on the link dict so
                # the per-course extractor can use it as authoritative hints.
                prefetch: dict = {}
                lv = _first_str(doc, _level_field)
                if lv:
                    prefetch["degree_level_hint"] = lv
                mo = _first_str(doc, _mode_field)
                if mo:
                    prefetch["study_mode_hint"] = mo
                du = _first_str(doc, _dur_field)
                if du:
                    prefetch["duration_hint"] = du
                da = _first_str(doc, _date_field)
                if da:
                    prefetch["intake_dates_hint"] = da
                ca = _first_str(doc, _cat_field)
                if ca:
                    prefetch["category_hint"] = ca
                if prefetch:
                    link["_prefetch"] = prefetch
                links.append(link)

            start += len(docs)
            if total is not None and start >= total:
                break
            if cfg.max_courses and len(links) >= cfg.max_courses:
                links = links[: cfg.max_courses]
                break
            await asyncio.sleep(0)

    await _emit(
        f"[SEARCHSTAX links_only] Discovered {len(links)} course URL(s) "
        f"({skipped} skipped — no URL field)."
    )
    log.info(
        "[SEARCHSTAX links_only] total=%s links=%s skipped=%s token=%s url_field=%s",
        total, len(links), skipped, "yes" if token else "NO", _url_field,
    )
    return links


async def fetch_searchstax_links(
    cfg: SearchStaxConfig,
    emit=None,
    *,
    fee_defaults: dict | None = None,
    force_fee_stage: bool = False,
    ielts_defaults: dict | None = None,
    default_ielts: float | None = None,
) -> list[dict]:
    """Query the SearchStax Solr core (paginated) → list of link dicts.

    Each returned dict carries a prebuilt ``searchstax_result`` payload that
    ``orchestrator._extract_only`` returns verbatim.

    When ``cfg.links_only`` is True the provider operates in discovery-only
    mode: it returns bare ``{name, url}`` dicts (no ``searchstax_result``) and
    the orchestrator's normal per-course extraction runs for each URL.

    When ``cfg.use_generic_mapper`` is True the generic field mapper from
    ``generic_search_api`` is used instead of the Huddersfield-specific one
    (which applies HUD fee bands + name reformatting).  Set this to True for
    any non-HUD university.

    ``cfg.extra_params`` is merged into every Solr request, allowing
    university-specific flags (e.g. ``model=coursefinder-ug``) without code
    changes.

    ``fee_defaults`` / ``force_fee_stage`` / ``ielts_defaults`` / ``default_ielts``
    are extracted by the orchestrator from the UniConfig and passed directly
    so that ``_map_doc_field_map`` (a sync function) never needs to touch
    the ContextVar.
    """
    # Discovery-only mode: return bare links without searchstax_result so
    # normal per-course extraction runs (fees / IELTS fetched from live pages).
    if cfg.links_only:
        return await _fetch_links_only(cfg, emit=emit)
    token = _resolve_token(cfg)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"

    # Choose mapper: field_map-driven generic (for universities like Durham
    # whose Solr has structured metadata but no fees/IELTS content blob),
    # HUD-specific (default), or generic_search_api.
    if cfg.field_map_as_payload:
        def _mapper(doc: dict) -> Optional[dict]:  # type: ignore[misc]
            return _map_doc_field_map(
                doc, cfg,
                fee_defaults=fee_defaults,
                force_fee_stage=force_fee_stage,
                ielts_defaults=ielts_defaults,
                default_ielts=default_ielts,
            )
    elif cfg.use_generic_mapper:
        from app.services.scraper.generic_search_api import _map_searchstax_doc
        def _mapper(doc: dict) -> Optional[dict]:
            link = _map_searchstax_doc(doc)
            if not link:
                return None
            # Wrap in searchstax_result so orchestrator returns it verbatim
            result = {
                "name": link.get("name", ""),
                "url": link.get("url", ""),
                "payload": link.get("auto_extracted", {}),
                "evidence": [],
            }
            return {"name": result["name"], "url": result["url"], "searchstax_result": result}
    else:
        def _mapper(doc: dict) -> Optional[dict]:  # type: ignore[misc]
            return _map_doc(doc, cfg)

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:  # noqa: BLE001
                pass

    links: list[dict] = []
    skipped = 0
    start = 0
    page_size = max(1, int(cfg.page_size or 100))
    total: Optional[int] = None
    _filter = cfg.filter_query or ""

    await _emit(
        f"[SEARCHSTAX] Querying Solr core "
        f"({'fq=' + _filter if _filter else 'unfiltered'}) ..."
    )

    # When using field_map_as_payload, request the mapped Solr field names
    # (e.g. Durham's PascalCase fields like Degreename_t, Degreetype_ss, ...).
    # Also always include the three "identity" fields used by _map_doc_field_map
    # for course name, degree type, and URL — even if they aren't in field_map.
    # Without this, docs come back missing title_t / award_s / url_t and every
    # course gets a slug-derived name like "Sr037M01Uwu" instead of its real title.
    # For HUD/generic modes, fall back to the standard _FIELDS string.
    if cfg.field_map_as_payload and cfg.field_map:
        _fm_vals = cfg.field_map or {}
        _url_fl  = _fm_vals.get("url",         "url_t")
        _name_fl = _fm_vals.get("name",        "title_t")
        _type_fl = _fm_vals.get("degree_type", "award_s")
        _fl = ",".join({"id", _url_fl, _name_fl, _type_fl, *_fm_vals.values()})
    else:
        _fl = _FIELDS

    async with httpx.AsyncClient(timeout=30.0) as client:
        _retried_unfiltered = False
        while True:
            params: dict[str, Any] = {
                "q": "*:*",
                "rows": str(page_size),
                "start": str(start),
                "fl": _fl,
                "wt": "json",
            }
            if _filter:
                params["fq"] = _filter
            # Merge university-specific extra params (override builtins if same key)
            params.update(cfg.extra_params or {})

            try:
                resp = await client.get(cfg.endpoint, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except Exception as _fetch_exc:
                log.error("[SEARCHSTAX] fetch failed (start=%d): %s", start, _fetch_exc)
                break

            response = data.get("response", {})
            docs = response.get("docs", [])

            # Auto-fallback: if the filter returned 0 on the first page, retry
            # without it — some cores use different sectionType values.
            if start == 0 and not docs and _filter and not _retried_unfiltered:
                _retried_unfiltered = True
                await _emit(
                    f"[SEARCHSTAX] {_filter!r} returned 0 — retrying unfiltered ..."
                )
                log.info("[SEARCHSTAX] fq=%r returned 0 docs — retrying without filter", _filter)
                _filter = ""
                continue

            if total is None:
                total = int(response.get("numFound", 0))
                await _emit(f"[SEARCHSTAX] {total} course docs found.")
                if not token:
                    await _emit(
                        "[SEARCHSTAX] WARNING: no token configured — requests are "
                        "unauthenticated.  Set authorization_token in the YAML, "
                        "token_env, or the SEARCHSTAX_TOKEN environment variable."
                    )

            if not docs:
                break
            for doc in docs:
                mapped = _mapper(doc)
                if mapped is not None:
                    links.append(mapped)
                else:
                    skipped += 1
            start += len(docs)
            if total is not None and start >= total:
                break
            if cfg.max_courses and len(links) >= cfg.max_courses:
                links = links[: cfg.max_courses]
                break
            await asyncio.sleep(0)  # cooperative yield between pages

    await _emit(
        f"[SEARCHSTAX] Built {len(links)} course record(s) "
        f"({skipped} unclassifiable doc(s) skipped)."
    )
    log.info(
        "[SEARCHSTAX] fetched=%s mapped=%s skipped=%s token=%s",
        total, len(links), skipped, "yes" if token else "NO",
    )
    return links

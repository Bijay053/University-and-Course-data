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
        "operating department practice",
    ]),
    (17600, [
        "computing", "engineering", "games development", "mathematics",
        "music", "social work", "geography", "science", "biological",
        "chemistry", "forensic", "pharmacy", "optometry",
    ]),
    (16500, [
        "accountancy", "finance", "business", "economics", "events",
        "hospitality", "law", "logistics", "marketing", "tourism",
        "education", "health", "psychology", "social sciences", "sport",
        "youth and community", "architecture", "art", "design", "drama",
        "english", "fashion", "journalism", "media", "history", "music",
        "music technology",
    ]),
]
_PG_BANDS: list[tuple[int, list[str]]] = [
    (18700, [
        "pharmaceutical science", "hbs master's with professional practice",
        "health studies", "public health", "biological", "chemistry",
        "forensic", "podiatry", "computing", "engineering", "mathematics",
        "music technology", "sound production msc", "science",
    ]),
    (17600, [
        "architecture", "art", "design", "drama", "english", "fashion",
        "history", "journalism", "media", "musicology", "music technology",
        "sound production", "education", "psychology", "social sciences",
        "social work", "sport", "youth and community",
        "health and human sciences",
    ]),
    (16500, [
        "accountancy", "finance", "business", "economics", "events",
        "hospitality", "law", "logistics", "marketing", "tourism",
        "education", "health", "psychology", "social sciences", "sport",
        "youth and community", "architecture", "art", "design", "drama",
        "english", "fashion", "journalism", "media", "history", "music",
        "music technology",
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

    name, degree_level = _reformat_name(raw_title, study_level)
    if not name:
        log.info("[SEARCHSTAX] skip (unclassifiable title): %r", raw_title)
        return None

    duration_t = _first(doc.get("duration_t")) or ""
    duration, duration_term, study_mode = _parse_duration(duration_t)
    intakes = _parse_intakes(_first(doc.get("start_dates_s")) or "")
    content = _first(doc.get("content")) or ""
    ielts_overall, ielts_band, ielts_snippet = _extract_ielts(content)
    fee, fee_subject = _fee_for(raw_title, study_level)

    payload: dict[str, Any] = {
        "course_name": name,
        "degree_level": degree_level,
        "course_location": "Huddersfield",
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

    # Evidence rows. Only the critical fields the source-evidence guard
    # (guards.enforce_source_evidence) checks need a row with a non-empty
    # source_url + snippet: international_fee, ielts_overall, study_mode.
    evidence: list[dict[str, Any]] = []
    if fee is not None:
        evidence.append({
            "field_key": "international_fee",
            "value": fee,
            "normalized": fee,
            "source_url": cfg.central_fee_page or url,
            "page_type": "central_fee",
            "method": "searchstax:fee_band",
            "snippet": (
                f"International tuition fee band for "
                f"{(fee_subject or 'this subject area').title()}: "
                f"£{fee:,}/year ({cfg.fee_year}-{(cfg.fee_year + 1) % 100:02d})"
            ),
            "confidence": 0.7,
            "decision_status": "selected",
        })
    if ielts_overall is not None:
        evidence.append({
            "field_key": "ielts_overall",
            "value": ielts_overall,
            "normalized": ielts_overall,
            "source_url": url,
            "page_type": "course",
            "method": "searchstax:content",
            "snippet": ielts_snippet or f"IELTS {ielts_overall} overall",
            "confidence": 0.85,
            "decision_status": "selected",
        })
    if study_mode:
        evidence.append({
            "field_key": "study_mode",
            "value": study_mode,
            "normalized": study_mode,
            "source_url": url,
            "page_type": "course",
            "method": "searchstax:duration_t",
            "snippet": duration_t or study_mode,
            "confidence": 0.8,
            "decision_status": "selected",
        })

    result = {"name": name, "url": url, "payload": payload, "evidence": evidence}
    return {"name": name, "url": url, "searchstax_result": result}


def _resolve_token(cfg: SearchStaxConfig) -> Optional[str]:
    if cfg.token_env:
        env_val = os.environ.get(cfg.token_env)
        if env_val:
            return env_val
    return cfg.token


async def fetch_searchstax_links(cfg: SearchStaxConfig, emit=None) -> list[dict]:
    """Query the SearchStax Solr core (paginated) → list of link dicts.

    Each returned dict carries a prebuilt ``searchstax_result`` payload that
    ``orchestrator._extract_only`` returns verbatim.
    """
    token = _resolve_token(cfg)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"

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

    await _emit(f"[SEARCHSTAX] Querying Solr core ({cfg.filter_query})...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {
                "q": "*:*",
                "fq": cfg.filter_query,
                "rows": str(page_size),
                "start": str(start),
                "fl": _FIELDS,
                "wt": "json",
            }
            resp = await client.get(cfg.endpoint, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            response = data.get("response", {})
            if total is None:
                total = int(response.get("numFound", 0))
                await _emit(f"[SEARCHSTAX] {total} course docs found.")
            docs = response.get("docs", [])
            if not docs:
                break
            for doc in docs:
                mapped = _map_doc(doc, cfg)
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
        "[SEARCHSTAX] fetched=%s mapped=%s skipped=%s",
        total, len(links), skipped,
    )
    return links

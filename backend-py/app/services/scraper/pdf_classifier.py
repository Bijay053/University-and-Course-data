"""Phase 6: PDF classification.

Classifies a PDF URL + optional first-page text into one of eight categories:

    fee_schedule        — international tuition fee schedule
    entry_requirements  — academic / English entry requirements
    handbook            — subject / unit handbook
    prospectus          — undergraduate / postgraduate prospectus
    course_catalogue    — full course catalogue
    intake_calendar     — semester / trimester start date table
    scholarship         — scholarship guide
    other               — anything not matched above

Strategy
--------
1. **Keyword scoring** — score URL path + anchor text + first-page text
   against term sets for each category.  Fast and free.
2. **Gemini fallback** — triggered only when keyword confidence < 0.50.
   Sends the first 300 chars of the URL + up to 800 chars of page text
   to Gemini Flash-Lite as a low-cost classification call.

The Gemini fallback uses the existing ``gemini_client`` budget/circuit-breaker
so it cannot cause quota issues.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

# ── Category type ─────────────────────────────────────────────────────────────

PdfCategory = Literal[
    "fee_schedule",
    "entry_requirements",
    "handbook",
    "prospectus",
    "course_catalogue",
    "intake_calendar",
    "scholarship",
    "other",
]

_ALL_CATEGORIES: tuple[PdfCategory, ...] = (
    "fee_schedule",
    "entry_requirements",
    "handbook",
    "prospectus",
    "course_catalogue",
    "intake_calendar",
    "scholarship",
    "other",
)

# ── Keyword patterns per category ─────────────────────────────────────────────
# Each entry is a list of (regex_or_literal, weight) pairs.
# Weights are summed; result is normalised to [0,1].

_CAT_PATTERNS: dict[str, list[tuple[str, float]]] = {
    "other": [],          # catch-all; normed score stays 0.0, prior set manually
    "fee_schedule": [
        (r"\btuition\b",                   1.5),
        (r"\btuition fee",                  2.0),
        (r"\binternational fee",            2.0),
        (r"\bfee schedule\b",               2.5),
        (r"\bfee.{0,10}table\b",            2.0),
        (r"\bcost of study\b",              1.5),
        (r"\bcost\b",                       0.5),
        (r"\bfees\b",                       1.0),
        (r"\bcharges\b",                    0.8),
        (r"\bper year\b",                   0.8),
        (r"\bper semester\b",               0.8),
        (r"\$\d",                           0.6),
        (r"AUD|USD|GBP|NZD|CAD|EUR",       0.5),
        (r"fee.pdf|fees.pdf|tuition.pdf",   2.0),  # URL path signal
    ],
    "entry_requirements": [
        (r"\bentry requirement",            2.5),
        (r"\badmission requirement",        2.5),
        (r"\bacademic requirement",         2.0),
        (r"\beligibility\b",                1.5),
        (r"\bprerequisite\b",               1.5),
        (r"\bielts\b",                      1.5),
        (r"\benglish language requirement", 2.0),
        (r"\bminimum requirement",          1.5),
        (r"\bhow to apply\b",               1.0),
        (r"\bATAR\b",                       1.5),
        (r"\bGPA\b",                        1.0),
        (r"\bprior degree\b",               1.0),
        (r"entry.req|admission|eligib",     1.5),  # URL signal
    ],
    "handbook": [
        (r"\bhandbook\b",                   3.0),
        (r"\bcourse guide\b",               2.0),
        (r"\bprogramme guide\b",            2.0),
        (r"\bunit outline\b",               2.0),
        (r"\bsubject outline\b",            2.0),
        (r"\bcurriculum\b",                 0.8),
        (r"handbook\.pdf",                  2.5),
    ],
    "prospectus": [
        (r"\bprospectus\b",                 3.0),
        (r"\bviewbook\b",                   3.0),
        (r"\bbrochure\b",                   1.5),
        (r"\bundergraduate guide\b",        2.0),
        (r"\bpostgraduate guide\b",         2.0),
        (r"\bfuture student\b",             1.0),
        (r"prospectus\.pdf|viewbook\.pdf",  2.5),
    ],
    "course_catalogue": [
        (r"\bcourse catalogue\b",           3.0),
        (r"\bcourse catalog\b",             3.0),
        (r"\bprogram.{0,5}catalogue\b",     2.5),
        (r"\bfull course list\b",           2.0),
        (r"\ball course\b",                 1.5),
        (r"catalogue\.pdf|catalog\.pdf",    2.5),
    ],
    "intake_calendar": [
        (r"\bintake\b",                     1.5),
        (r"\bacademic calendar\b",          2.5),
        (r"\bkey date\b",                   2.0),
        (r"\bsemester date\b",              2.5),
        (r"\btrimester date\b",             2.5),
        (r"\bstart date\b",                 1.5),
        (r"\bcritical date\b",              1.5),
        (r"calendar\.pdf|dates\.pdf",       2.5),
    ],
    "scholarship": [
        (r"\bscholarship\b",                3.0),
        (r"\bbursary\b",                    2.5),
        (r"\bfinancial aid\b",              2.0),
        (r"\bgrant\b",                      1.0),
        (r"\baward\b",                      0.8),
        (r"scholarship\.pdf|bursary\.pdf",  2.5),
    ],
}

# Pre-compile patterns
_COMPILED: dict[str, list[tuple[re.Pattern, float]]] = {
    cat: [(re.compile(pat, re.I), w) for pat, w in entries]
    for cat, entries in _CAT_PATTERNS.items()
}

_MIN_KEYWORD_CONFIDENCE = 0.50
_GEMINI_MAX_TEXT_CHARS = 800
_GEMINI_PROMPT = (
    "You are classifying a PDF from a university website. "
    "Based on the URL and page text below, output ONLY the single best "
    "category (one of: fee_schedule, entry_requirements, handbook, prospectus, "
    "course_catalogue, intake_calendar, scholarship, other). "
    "No explanation.\n\nURL: {url}\n\nText excerpt:\n{text}"
)


_NON_TUITION_FEE_URL_RE = re.compile(
    r"(?:incidental(?:s)?|ancillary|non[-_\s]?tuition)[-_\s]*(?:fees?|costs?|charges?)"
    r"|student[-_\s]*services[-_\s]*(?:and|&)[-_\s]*amenities[-_\s]*fees?"
    r"|\bssaf\b"
    r"|application[-_\s]*fees?"
    r"|(?:enrolment|enrollment)[-_\s]*deposits?"
    r"|(?:materials?|equipment)[-_\s]*(?:fees?|costs?)",
    re.I,
)
# UniSC's "full fee paying" PDFs are schedules for individual units of study,
# not annual degree-program tuition.  Their amounts (for example A$3,566) can
# fuzzy-match a programme name and must never fill the programme fee field.
# Keep this host/file rule exact so genuine international tuition schedules at
# UniSC and similarly named documents at other universities are unaffected.
_UNISC_UNIT_FEE_SCHEDULE_URL_RE = re.compile(
    r"https?://(?:www\.)?unisc\.edu\.au/"
    r".*/20\d{2}-(?:"
    r"full-fee-paying-(?:1st|2nd)-half"
    r"|units-of-study-[^/?#]*fees?(?:-v\d+)?"
    r")\.pdf(?:[?#].*)?$",
    re.I,
)
_NON_TUITION_FEE_TITLE_RE = re.compile(
    r"\b(?:incidental(?:s)?|ancillary|non[-\s]?tuition)\s+(?:fees?|costs?|charges?)\b"
    r"|\bstudent\s+services\s+(?:and|&)\s+amenities\s+fees?\b"
    r"|\bssaf\b"
    r"|\bapplication\s+fees?\b"
    r"|\b(?:enrolment|enrollment)\s+deposits?\b"
    r"|\b(?:materials?|equipment)\s+(?:fees?|costs?)\b",
    re.I,
)
_POSITIVE_TUITION_TITLE_RE = re.compile(
    r"\b(?:international\s+)?tuition\s+(?:fee\s+)?schedule\b"
    r"|\binternational\s+tuition\s+fees?\b",
    re.I,
)
_BEYOND_TUITION_RE = re.compile(
    r"\b(?:additional|extra)\s+costs?.{0,100}\bbeyond\s+(?:their\s+)?tuition\b",
    re.I | re.S,
)


def is_non_tuition_fee_pdf(url: str, first_page_text: str = "") -> bool:
    """Return True for documents explicitly dedicated to non-tuition charges.

    This is intentionally narrower than the general low-value PDF filter.
    Universities publish incidental, ancillary, SSAF, application, deposit,
    materials, and equipment fee documents whose dollar amounts must never be
    used as international tuition.
    """
    if (
        _NON_TUITION_FEE_URL_RE.search(url or "")
        or _UNISC_UNIT_FEE_SCHEDULE_URL_RE.search(url or "")
    ):
        return True
    sample = (first_page_text or "")[:600]
    title = next((line.strip() for line in sample.splitlines() if line.strip()), "")
    if not _NON_TUITION_FEE_TITLE_RE.search(title):
        return bool(_BEYOND_TUITION_RE.search(sample))
    # A combined international tuition schedule may mention incidental costs
    # in its title; keep it unless the introduction explicitly says the costs
    # are beyond tuition.
    if _POSITIVE_TUITION_TITLE_RE.search(title) and not _BEYOND_TUITION_RE.search(sample):
        return False
    return True


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class ClassifiedPdf:
    """Result of classifying one PDF."""

    url: str
    category: PdfCategory
    confidence: float                          # 0.0–1.0
    classification_method: str                 # "keyword" | "gemini" | "default"
    raw_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "category": self.category,
            "confidence": round(self.confidence, 3),
            "method": self.classification_method,
        }


# ── Keyword scorer ────────────────────────────────────────────────────────────

def _keyword_scores(text: str) -> dict[str, float]:
    """Return a normalised score per category for *text*."""
    if not text:
        return {cat: 0.0 for cat in _ALL_CATEGORIES}

    raw: dict[str, float] = {}
    for cat, patterns in _COMPILED.items():
        score = sum(w for pat, w in patterns if pat.search(text))
        raw[cat] = score

    # Normalise by max possible (sum of all weights in that category)
    max_possible = {
        cat: sum(w for _, w in entries)
        for cat, entries in _CAT_PATTERNS.items()
    }
    normed = {
        cat: min(1.0, raw[cat] / max_possible[cat])
        if max_possible[cat] > 0 else 0.0
        for cat in _ALL_CATEGORIES
    }
    normed["other"] = 0.05  # tiny prior so "other" only wins when all else scores 0
    return normed


def classify_by_keywords(url: str, first_page_text: str = "") -> ClassifiedPdf:
    """Classify using keyword scoring only.  Fast and synchronous."""
    if is_non_tuition_fee_pdf(url, first_page_text):
        return ClassifiedPdf(
            url=url,
            category="other",
            confidence=0.99,
            classification_method="keyword",
            raw_scores={},
        )
    combined = f"{url} {first_page_text}"
    scores = _keyword_scores(combined)
    best_cat = max(scores, key=scores.__getitem__)
    best_score = scores[best_cat]

    # Require the winner to be clearly ahead of second place
    sorted_scores = sorted(scores.values(), reverse=True)
    margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
    confidence = min(1.0, best_score * 0.7 + margin * 0.3)

    return ClassifiedPdf(
        url=url,
        category=best_cat,  # type: ignore[arg-type]
        confidence=confidence,
        classification_method="keyword",
        raw_scores={k: round(v, 3) for k, v in scores.items()},
    )


# ── Gemini fallback ───────────────────────────────────────────────────────────

async def _classify_via_gemini(url: str, first_page_text: str) -> PdfCategory:
    """Ask Gemini to classify the PDF.  Returns 'other' on any failure."""
    try:
        from app.services.ai.gemini_client import call_gemini
        prompt = _GEMINI_PROMPT.format(
            url=url[:200],
            text=first_page_text[:_GEMINI_MAX_TEXT_CHARS],
        )
        response = await call_gemini(
            prompt=prompt,
            call_type="pdf_classification",
            model="gemini-2.5-flash-lite",
        )
        raw = (response or "").strip().lower().replace("-", "_")
        for cat in _ALL_CATEGORIES:
            if cat in raw:
                return cat  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        log.debug("[PDF_CLS] Gemini fallback failed: %s", exc)
    return "other"


# ── Low-value PDF filter ──────────────────────────────────────────────────────
# Mirrors the blocklist in pdf_link_discoverer._LOW_VALUE_URL_RE.
# Kept separate so classify_pdf() can be called stand-alone without importing
# the discoverer (which would pull in aiohttp).
_LOW_VALUE_PDF_RE = re.compile(
    r"privacy.?polic"
    r"|terms.?(?:of.?service|and.?conditions?)"
    r"|complaint.?(?:procedure|polic|handling|process)"
    r"|annual.?report"
    r"|feedback.?form"
    r"|enquiry.?form"
    r"|code.?of.?conduct"
    r"|safety.?polic"
    r"|whistleblow"
    r"|governance.?report"
    r"|board.?minutes"
    r"|council.?minutes"
    r"|newsletter"
    r"|media.?release"
    r"|press.?release"
    r"|accessibilit.?statement"
    r"|refund.?polic"
    r"|financial.?statement"
    r"|financial.?report"
    r"|strategic.?plan"
    r"|sustainability.?report",
    re.I,
)
# High-value signals that override the low-value blocklist.
_HV_OVERRIDE_RE = re.compile(
    r"fee|tuition|cost|requirement|admission|entry|english|ielts|handbook|course|programme|catalog",
    re.I,
)


def is_low_value_pdf(url: str, first_page_text: str = "") -> bool:
    """Return True for PDFs that contain no useful course data.

    Checks URL path and the first 300 chars of page text against a blocklist
    of administrative document types (privacy policies, annual reports, forms,
    etc.).  A strong high-value signal in the same text overrides the blocklist
    so that edge cases like "international-students-fee-refund-policy.pdf" are
    not wrongly suppressed.
    """
    if is_non_tuition_fee_pdf(url, first_page_text):
        return True
    sample = f"{url} {first_page_text[:300]}"
    if not _LOW_VALUE_PDF_RE.search(sample):
        return False
    return not bool(_HV_OVERRIDE_RE.search(sample))


# ── Public entry point ────────────────────────────────────────────────────────

async def classify_pdf(
    url: str,
    first_page_text: str = "",
) -> ClassifiedPdf:
    """Classify a PDF, using keyword scoring + optional Gemini fallback.

    Parameters
    ----------
    url:
        Full URL of the PDF.
    first_page_text:
        Optional text of the first page(s) of the PDF, used to strengthen
        keyword scoring and provide context for the Gemini fallback.

    Returns
    -------
    ClassifiedPdf
        category + confidence + method used.
    """
    # Fast low-value gate — return "other" immediately for administrative PDFs
    # (privacy policies, annual reports, forms, etc.) so they never enter the
    # Gemini fallback path.
    if is_low_value_pdf(url, first_page_text):
        log.debug("[PDF_CLS] low-value PDF suppressed: %s", url[:80])
        return ClassifiedPdf(
            url=url,
            category="other",
            confidence=0.95,
            classification_method="keyword",
            raw_scores={},
        )

    result = classify_by_keywords(url, first_page_text)

    if result.confidence < _MIN_KEYWORD_CONFIDENCE:
        log.debug(
            "[PDF_CLS] low keyword confidence (%.2f) for %s — trying Gemini",
            result.confidence, url[:60],
        )
        gemini_cat = await _classify_via_gemini(url, first_page_text)
        return ClassifiedPdf(
            url=url,
            category=gemini_cat,
            confidence=0.65,            # fixed moderate confidence for Gemini
            classification_method="gemini",
            raw_scores=result.raw_scores,
        )

    return result


def classify_pdf_url(url: str) -> ClassifiedPdf:
    """Synchronous URL-only classification (no I/O, no Gemini).

    Useful for quick triage inside the PDF discoverer before any PDF
    content is fetched.
    """
    return classify_by_keywords(url, "")

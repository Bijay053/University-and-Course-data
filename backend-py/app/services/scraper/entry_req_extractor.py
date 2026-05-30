"""Phase 6: Academic entry requirement extraction from PDF text.

Extracts structured academic entry requirements from plain text (typically
obtained from ``pdf_fetcher.download_pdf_text``):

- ATAR range (Australian Tertiary Admission Rank)
- GPA (Grade Point Average) on 4.0, 7.0, or percentage scales
- Prior degree requirement (bachelor, honours, master, diploma)
- Work experience requirements (years + description)
- Prerequisite subjects
- Portfolio / interview requirements
- Country-specific academic equivalency statements

Returns an ``EntryRequirement`` dataclass whose ``to_summary_text()`` method
produces a concise plain-text summary suitable for the ``other_requirement``
field on the ``courses`` table.

The extractor is entirely rule-based (no Gemini) so it is free and fast.
Gemini extraction of entry requirements from PDFs is already available via
``pdf_vision.py`` for scanned documents — this module handles text-native PDFs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ── ATAR ──────────────────────────────────────────────────────────────────────
# ATAR is an Australian rank 0–99.95.
# "minimum ATAR of 75", "ATAR: 80.00", "an ATAR of at least 65"
# Simplified ATAR regex: allow up to 35 non-digit, non-newline chars between
# "ATAR" and the score to handle "of at least", "score of", "rank of", etc.
_ATAR_RE = re.compile(
    r"\bATAR\b[^0-9\n]{0,35}(\d{2,3}(?:\.\d{1,2})?)",
    re.I,
)

# ── GPA ───────────────────────────────────────────────────────────────────────
# "GPA of 5.0 out of 7.0", "minimum GPA of 3.0 on a 4.0 scale",
# "GPA: 3.5/4.0", "grade point average of 5.0 (7.0 scale)"
_GPA_WITH_SCALE_RE = re.compile(
    r"(?:minimum\s+)?(?:GPA|grade\s+point\s+average)(?:\s+of|\s*:\s*|\s+score\s+of)?\s*"
    r"(\d(?:\.\d{1,2})?)\s*"
    r"(?:out\s+of|on\s+(?:a\s+)?|/|,\s+out\s+of\s+|on\s+a\s+)\s*"
    r"(\d(?:\.\d)?)\s*(?:scale|point\s+scale)?",
    re.I,
)
# Bare GPA with no explicit scale
_GPA_BARE_RE = re.compile(
    r"(?:minimum\s+)?(?:GPA|grade\s+point\s+average)(?:\s+of|\s*:\s*|\s+score\s+of)?\s*"
    r"(\d(?:\.\d{1,2})?)",
    re.I,
)

# ── Percentage / WAM ──────────────────────────────────────────────────────────
# "Weighted Average Mark of 65%", "minimum WAM of 70", "65% or above"
_WAM_RE = re.compile(
    r"(?:minimum\s+)?(?:WAM|weighted\s+average\s+mark|average\s+grade)\s*(?:of|:|\s)?\s*"
    r"(\d{2,3}(?:\.\d)?)\s*%?",
    re.I,
)

# ── Prior degree ──────────────────────────────────────────────────────────────
# Matches "bachelor's degree", "honours degree", "master's degree",
# "graduate diploma", "undergraduate degree" in any form
_PRIOR_DEGREE_PATS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdoctorate\b|\bPhD\b", re.I), "doctorate"),
    (re.compile(r"\bhonours\s+degree\b|\bfirst[\s-]class\s+honours\b", re.I), "honours"),
    (re.compile(r"\bmaster'?s?\s+degree\b|\bmaster\s+of\b", re.I), "master"),
    (re.compile(r"\bgraduate\s+diploma\b", re.I), "graduate_diploma"),
    (re.compile(r"\bgraduate\s+certificate\b", re.I), "graduate_certificate"),
    (re.compile(r"\bbachelor'?s?\s+degree\b|\bbachelor\s+of\b|\bundergraduate\s+degree\b", re.I), "bachelor"),
    (re.compile(r"\bdiploma\b", re.I), "diploma"),
    (re.compile(r"\bcertificate\s+IV\b|\bcertificate\s+4\b", re.I), "cert_iv"),
]

# ── Work experience ───────────────────────────────────────────────────────────
# "2 years of work experience", "minimum 3 years' professional experience",
# "5+ years industry experience"
_WORK_EXP_RE = re.compile(
    r"(\d+)\s*\+?\s*years?'?\s*(?:of\s+)?(?:relevant\s+)?(?:full[\s-]time\s+)?"
    r"(?:work|professional|industry|management|clinical|teaching|nursing|relevant)\s+experience",
    re.I,
)
_WORK_EXP_GENERAL_RE = re.compile(
    r"(\d+)\s*\+?\s*years?'?\s+experience\s+in",
    re.I,
)

# ── Portfolio / interview ─────────────────────────────────────────────────────
_PORTFOLIO_RE = re.compile(
    r"\bportfolio\s+(?:is\s+)?(?:required|of\s+work|must\s+be\s+submitted)\b"
    r"|\bsubmit\s+a\s+portfolio\b"
    r"|\bportfolio\s+submission\s+(?:is\s+)?(?:required|mandatory|compulsory)\b",
    re.I,
)
_INTERVIEW_RE = re.compile(
    r"\binterview\s+(?:is\s+)?(?:required|may\s+be\s+required|is\s+part\s+of|forms\s+part)\b"
    r"|\bmay\s+be\s+required\s+to\s+attend\s+an?\s+interview\b",
    re.I,
)

# ── Prerequisite subjects ─────────────────────────────────────────────────────
# "prerequisite: Mathematics B" or "Mathematics Methods (or equivalent)"
_PREREQ_SUBJECT_RE = re.compile(
    r"(?:prerequisite[s]?\s*:?\s*|you\s+must\s+have\s+completed\s+)"
    r"([A-Z][A-Za-z0-9\s,/&()\-]{4,60}?)(?:\.|;|\n|$)",
    re.I,
)

# ── Country equivalency ───────────────────────────────────────────────────────
# "equivalent to an Australian bachelor's degree",
# "International Baccalaureate: 28 points"
_COUNTRY_EQ_RE = re.compile(
    r"(?:India|China|UK|United\s+Kingdom|USA|United\s+States|International\s+Baccalaureate"
    r"|IB\s+Diploma|A[\s-]Levels?|Indian\s+HSC|GCSE)[^.]{0,200}?(?:\.|$)",
    re.I,
)

# ── Cleanup helpers ───────────────────────────────────────────────────────────
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class EntryRequirement:
    """Structured academic entry requirements extracted from PDF text."""

    # Scores / ranks
    atar_min: float | None = None
    gpa_min: float | None = None
    gpa_scale: float | None = None          # 4.0, 7.0, or 100 (percentage/WAM)
    wam_min: float | None = None            # Weighted Average Mark (percentage)

    # Qualifications
    prior_degree: str | None = None         # "bachelor", "honours", "master", etc.

    # Work experience
    work_experience_years: float | None = None
    work_experience_text: str | None = None

    # Other requirements
    prerequisite_subjects: list[str] = field(default_factory=list)
    portfolio_required: bool = False
    interview_required: bool = False
    country_equivalencies: list[str] = field(default_factory=list)

    # Provenance
    raw_excerpt: str = ""                   # up to 500 chars of matching context
    confidence: float = 0.0                 # 0.0–1.0 based on fields extracted
    fields_found: int = 0

    def to_summary_text(self) -> str:
        """Return a concise plain-text summary for the ``other_requirement`` field.

        Produces at most ~300 characters of human-readable requirements.
        Returns "" if nothing was extracted.
        """
        parts: list[str] = []
        if self.prior_degree:
            degree_map = {
                "bachelor": "Bachelor's degree",
                "honours": "Bachelor's (Honours) degree",
                "master": "Master's degree",
                "graduate_diploma": "Graduate Diploma",
                "graduate_certificate": "Graduate Certificate",
                "diploma": "Diploma",
                "doctorate": "Doctorate (PhD)",
                "cert_iv": "Certificate IV",
            }
            parts.append(degree_map.get(self.prior_degree, self.prior_degree.replace("_", " ").title()))
        if self.atar_min is not None:
            parts.append(f"ATAR {self.atar_min:.0f}+")
        if self.gpa_min is not None and self.gpa_scale is not None:
            parts.append(f"GPA {self.gpa_min:.1f}/{self.gpa_scale:.0f}")
        elif self.gpa_min is not None:
            parts.append(f"GPA {self.gpa_min:.1f}")
        if self.wam_min is not None:
            parts.append(f"WAM {self.wam_min:.0f}%")
        if self.work_experience_years is not None:
            parts.append(f"{self.work_experience_years:.0f}+ years work experience")
        elif self.work_experience_text:
            parts.append(self.work_experience_text[:80])
        if self.prerequisite_subjects:
            subj = ", ".join(self.prerequisite_subjects[:3])
            parts.append(f"Prerequisites: {subj}")
        if self.portfolio_required:
            parts.append("Portfolio required")
        if self.interview_required:
            parts.append("Interview may be required")
        if self.country_equivalencies:
            parts.append(self.country_equivalencies[0][:80])
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "atar_min": self.atar_min,
            "gpa_min": self.gpa_min,
            "gpa_scale": self.gpa_scale,
            "wam_min": self.wam_min,
            "prior_degree": self.prior_degree,
            "work_experience_years": self.work_experience_years,
            "work_experience_text": self.work_experience_text,
            "prerequisite_subjects": self.prerequisite_subjects,
            "portfolio_required": self.portfolio_required,
            "interview_required": self.interview_required,
            "country_equivalencies": self.country_equivalencies,
            "confidence": round(self.confidence, 3),
            "fields_found": self.fields_found,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EntryRequirement":
        obj = cls()
        for k, v in d.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        return obj


# ── Extractor ─────────────────────────────────────────────────────────────────

def extract_entry_requirements(text: str) -> EntryRequirement:
    """Extract academic entry requirements from *text*.

    Parameters
    ----------
    text:
        Plain text of a requirements PDF (from ``pdf_fetcher.download_pdf_text``
        or ``pdf_vision.extract_via_vision``).  May be many pages.

    Returns
    -------
    EntryRequirement
        Populated fields + ``confidence`` based on number of fields found.
        Returns an empty ``EntryRequirement`` (confidence=0.0) when nothing
        is matched — the caller should treat this as "no data extracted".
    """
    req = EntryRequirement()
    if not text or len(text) < 5:
        return req

    fields_found = 0
    excerpt_parts: list[str] = []

    # ── ATAR ──────────────────────────────────────────────────────────────────
    atar_match = _ATAR_RE.search(text)
    if atar_match:
        try:
            val = float(atar_match.group(1))
            if 0 < val <= 99.95:
                req.atar_min = val
                fields_found += 1
                excerpt_parts.append(atar_match.group(0)[:60])
        except ValueError:
            pass

    # ── GPA ───────────────────────────────────────────────────────────────────
    gpa_match = _GPA_WITH_SCALE_RE.search(text)
    if gpa_match:
        try:
            req.gpa_min = float(gpa_match.group(1))
            req.gpa_scale = float(gpa_match.group(2))
            fields_found += 1
            excerpt_parts.append(gpa_match.group(0)[:80])
        except ValueError:
            pass
    elif not gpa_match:
        bare_match = _GPA_BARE_RE.search(text)
        if bare_match:
            try:
                val = float(bare_match.group(1))
                if 0 < val <= 4.0:
                    req.gpa_min = val
                    req.gpa_scale = 4.0
                elif val <= 7.0:
                    req.gpa_min = val
                    req.gpa_scale = 7.0
                elif val <= 100.0:
                    req.gpa_min = val
                    req.gpa_scale = 100.0
                if req.gpa_min is not None:
                    fields_found += 1
                    excerpt_parts.append(bare_match.group(0)[:60])
            except ValueError:
                pass

    # ── WAM ───────────────────────────────────────────────────────────────────
    wam_match = _WAM_RE.search(text)
    if wam_match:
        try:
            val = float(wam_match.group(1))
            if 40 <= val <= 100:
                req.wam_min = val
                fields_found += 1
                excerpt_parts.append(wam_match.group(0)[:60])
        except ValueError:
            pass

    # ── Prior degree ──────────────────────────────────────────────────────────
    for pat, degree_name in _PRIOR_DEGREE_PATS:
        if pat.search(text):
            req.prior_degree = degree_name
            fields_found += 1
            excerpt_parts.append(degree_name)
            break   # take highest-ranked match

    # ── Work experience ───────────────────────────────────────────────────────
    work_match = _WORK_EXP_RE.search(text) or _WORK_EXP_GENERAL_RE.search(text)
    if work_match:
        try:
            yrs = float(work_match.group(1))
            if 0 < yrs <= 30:
                req.work_experience_years = yrs
                req.work_experience_text = _clean(work_match.group(0))
                fields_found += 1
                excerpt_parts.append(work_match.group(0)[:80])
        except ValueError:
            pass

    # ── Prerequisite subjects ─────────────────────────────────────────────────
    for m in _PREREQ_SUBJECT_RE.finditer(text):
        subj = _clean(m.group(1))
        if len(subj) >= 5 and subj not in req.prerequisite_subjects:
            req.prerequisite_subjects.append(subj)
            if len(req.prerequisite_subjects) >= 5:
                break
    if req.prerequisite_subjects:
        fields_found += 1

    # ── Portfolio / interview ─────────────────────────────────────────────────
    if _PORTFOLIO_RE.search(text):
        req.portfolio_required = True
        fields_found += 1
    if _INTERVIEW_RE.search(text):
        req.interview_required = True
        fields_found += 1

    # ── Country equivalencies ─────────────────────────────────────────────────
    for m in _COUNTRY_EQ_RE.finditer(text):
        eq = _clean(m.group(0))
        if eq and eq not in req.country_equivalencies:
            req.country_equivalencies.append(eq[:150])
            if len(req.country_equivalencies) >= 3:
                break
    if req.country_equivalencies:
        fields_found += 1

    # ── Confidence ────────────────────────────────────────────────────────────
    # 1 field = 0.30, 2 = 0.55, 3 = 0.75, 4+ = 0.90
    req.fields_found = fields_found
    req.confidence = min(0.95, fields_found * 0.25 + 0.05 if fields_found else 0.0)
    req.raw_excerpt = "; ".join(excerpt_parts)[:500]

    if fields_found > 0:
        log.info(
            "[ENTRY_REQ] extracted %d field(s) (conf=%.2f): %s",
            fields_found, req.confidence,
            req.to_summary_text()[:100],
        )

    return req

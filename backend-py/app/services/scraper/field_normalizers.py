"""Phase 9B — T002: Field-Specific Normalizers.

Each normalizer converts a raw string to a canonical comparison key so that
"apparent conflicts" caused by formatting differences are resolved before
(or instead of) being flagged as genuine source disagreements.

Canonical forms
---------------
- Duration     → integer string representing months  ("24", "18")
- Fee          → float string rounded to nearest 100  ("45000.0", "35000.0")
- IELTS/score  → decimal string                       ("6.5", "7.0")
- Intake month → integer string 1–12                  ("2", "7", "3")

All functions return None for values they cannot parse so the caller can
fall back to the generic text normalizer.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------

_DUR_YEAR = re.compile(r"(\d+(?:\.\d+)?)\s*(?:year|yr)s?", re.I)
_DUR_MONTH = re.compile(r"(\d+(?:\.\d+)?)[\s-]*months?", re.I)
_DUR_WEEK = re.compile(r"(\d+(?:\.\d+)?)\s*weeks?", re.I)
_DUR_SEM = re.compile(r"(\d+)\s*semesters?", re.I)

# Half-year phrases ("one year", "two years") — rare but present
_DUR_WRITTEN = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def normalize_duration(raw: Any) -> str | None:
    """Return duration in whole months as a string, e.g. "24".

    Handles: "2 years", "24 months", "1.5 years", "18-month", "2 semesters",
             "6 weeks", "one year".
    """
    if raw is None:
        return None
    val = str(raw).strip().lower()

    # Written-out numbers: "two years" etc.
    for word, n in _DUR_WRITTEN.items():
        if word in val:
            if "year" in val or "yr" in val:
                return str(round(n * 12))
            if "month" in val:
                return str(n)
            if "week" in val:
                return str(round(n * 52 / 12))

    m = _DUR_YEAR.search(val)
    if m:
        return str(round(float(m.group(1)) * 12))

    m = _DUR_MONTH.search(val)
    if m:
        return str(round(float(m.group(1))))

    m = _DUR_SEM.search(val)
    if m:
        return str(round(int(m.group(1)) * 6))

    m = _DUR_WEEK.search(val)
    if m:
        return str(round(float(m.group(1)) / 4.33))

    return None


# ---------------------------------------------------------------------------
# Fee
# ---------------------------------------------------------------------------

_FEE_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)
_FEE_RANGE_RE = re.compile(r"(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)")


def normalize_fee(raw: Any) -> str | None:
    """Return fee as a float string rounded to the nearest 100.

    Handles: "AUD 45,000", "45000 AUD", "$45k", "45K", "35,000-45,000" (→ mid-point).
    """
    if raw is None:
        return None
    val = str(raw).strip()

    # "$45k" / "45K"
    m = _FEE_K_RE.search(val)
    if m:
        f = float(m.group(1)) * 1000
        return f"{round(f / 100) * 100:.1f}"

    # "35,000–45,000" — use mid-point
    m = _FEE_RANGE_RE.search(val)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        f = (lo + hi) / 2
        return f"{round(f / 100) * 100:.1f}"

    # Generic: strip all non-numeric, parse float, round to 100
    cleaned = re.sub(r"[^\d.]", "", val)
    if cleaned:
        try:
            f = float(cleaned)
            if f < 100:
                # Looks like it was already in thousands notation (e.g. "45" for 45,000)
                # — leave it so generic normalizer handles it
                return None
            return f"{round(f / 100) * 100:.1f}"
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# English test score (IELTS, PTE, TOEFL, etc.)
# ---------------------------------------------------------------------------

_SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def normalize_score(raw: Any) -> str | None:
    """Return first numeric token as a 1-decimal float string.

    Works for IELTS (6.5), PTE (58), TOEFL (79), etc.
    """
    if raw is None:
        return None
    m = _SCORE_RE.search(str(raw))
    if m:
        return f"{float(m.group(1)):.1f}"
    return None


# ---------------------------------------------------------------------------
# Intake month
# ---------------------------------------------------------------------------

_INTAKE_MAP: dict[str, str] = {
    # Full month names
    "january": "1", "february": "2", "march": "3", "april": "4",
    "may": "5", "june": "6", "july": "7", "august": "8",
    "september": "9", "october": "10", "november": "11", "december": "12",
    # 3-letter abbreviations
    "jan": "1", "feb": "2", "mar": "3", "apr": "4",
    "jun": "6", "jul": "7", "aug": "8",
    "sep": "9", "sept": "9", "oct": "10", "nov": "11", "dec": "12",
    # AU trimesters (approximate)
    "trimester 1": "3", "t1": "3",
    "trimester 2": "7", "t2": "7",
    "trimester 3": "11", "t3": "11",
    # Semesters
    "semester 1": "3", "sem 1": "3", "s1": "3",
    "semester 2": "7", "sem 2": "7", "s2": "7",
    # Sessions (Australian naming)
    "autumn session": "3", "autumn": "3",
    "spring session": "7", "spring": "7",
    "summer session": "11", "summer": "11",
    "session 1": "3", "session 2": "7", "session 3": "11",
    # Quarters
    "q1": "1", "q2": "4", "q3": "7", "q4": "10",
}


def normalize_intake(raw: Any) -> str | None:
    """Return intake as a month number string ("1"–"12").

    Handles month names, abbreviations, trimester/semester/session labels,
    and plain integer month numbers.
    """
    if raw is None:
        return None
    val = str(raw).strip().lower()
    # Plain numeric month ("2", "02")
    try:
        n = int(val)
        if 1 <= n <= 12:
            return str(n)
    except ValueError:
        pass
    return _INTAKE_MAP.get(val)


# ---------------------------------------------------------------------------
# Dispatcher — called from verification_engine and conflict_repair
# ---------------------------------------------------------------------------

_DURATION_FIELDS: frozenset[str] = frozenset({"duration", "course_duration"})
_FEE_FIELDS: frozenset[str] = frozenset({
    "international_fee", "domestic_fee", "fee_year", "annual_fee",
})
_SCORE_FIELDS: frozenset[str] = frozenset({
    "ielts_overall", "ielts_listening", "ielts_speaking",
    "ielts_writing", "ielts_reading",
    "pte_overall", "pte_listening", "pte_speaking",
    "pte_writing", "pte_reading",
    "toefl_overall", "cambridge_overall", "duolingo_overall",
    "academic_score",
})
_INTAKE_FIELDS: frozenset[str] = frozenset({"intake_months", "intake_month"})


def normalize_for_conflict(field_name: str, raw: Any) -> str | None:
    """Return a canonical comparison key for ``field_name`` / ``raw`` pair.

    Returns None if the value cannot be parsed (caller falls back to generic
    text normalization).
    """
    fn = (field_name or "").lower()

    if fn in _DURATION_FIELDS or fn == "duration":
        return normalize_duration(raw)

    if fn in _FEE_FIELDS or "fee" in fn:
        return normalize_fee(raw)

    if fn in _SCORE_FIELDS:
        return normalize_score(raw)

    if fn in _INTAKE_FIELDS or "intake" in fn:
        return normalize_intake(raw)

    return None

"""Course-level data confidence scoring.

Computes a 0-100 score per staged course payload based on the presence
of the five critical fields identified in the architecture-fix brief:

    Fee present               → 25 pts
    English test present      → 25 pts  (any of IELTS / PTE / TOEFL / CAE)
    Duration present          → 20 pts
    Intake months present     → 20 pts
    Study mode present        → 10 pts
    ─────────────────────────────────
    Total possible            → 100 pts

Thresholds
----------
CONFIDENCE_PASS   ≥ 80   — all critical fields present; auto-publish candidate
CONFIDENCE_WARN   60-79  — one field missing; emit warning to live log
CONFIDENCE_LOW    < 60   — two or more fields missing; flag for review

Usage
-----
    from app.services.scraper.confidence import score_payload, CONFIDENCE_WARN

    result = score_payload(payload)
    if result["score"] < CONFIDENCE_WARN:
        payload.setdefault("scrape_warnings", [])
        payload["scrape_warnings"].append("confidence_low")
"""
from __future__ import annotations

from typing import Any

Payload = dict[str, Any]

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

CONFIDENCE_PASS = 80
CONFIDENCE_WARN = 60

# ---------------------------------------------------------------------------
# Field weights — must sum to 100
# ---------------------------------------------------------------------------

_WEIGHTS: tuple[tuple[str, int, str], ...] = (
    # (label, points, description)
    ("fee",     25, "International fee"),
    ("english", 25, "English test score (IELTS / PTE / TOEFL / CAE)"),
    ("duration", 20, "Course duration"),
    ("intake",  20, "Intake months"),
    ("mode",    10, "Study mode"),
)

assert sum(w for _, w, _ in _WEIGHTS) == 100, "weights must sum to 100"


# ---------------------------------------------------------------------------
# Field presence checkers
# ---------------------------------------------------------------------------

_ENGLISH_FIELDS = (
    "ielts_overall", "pte_overall", "toefl_overall",
    "cambridge_overall", "duolingo_overall",
)


def _has_fee(payload: Payload) -> bool:
    """True when the payload carries a numeric international fee OR has the
    central-fee-page flag set (ECU / Bond style — fee lives off-page)."""
    if payload.get("has_central_fee_page"):
        return True
    fee = payload.get("international_fee")
    if fee is None:
        return False
    try:
        return float(fee) > 0
    except (TypeError, ValueError):
        return False


def _has_english(payload: Payload) -> bool:
    return any(
        payload.get(f) is not None and payload[f] != ""
        for f in _ENGLISH_FIELDS
    )


def _has_duration(payload: Payload) -> bool:
    dur = payload.get("duration")
    if dur is None:
        return False
    try:
        return float(dur) > 0
    except (TypeError, ValueError):
        return False


def _has_intake(payload: Payload) -> bool:
    months = payload.get("intake_months")
    if not months:
        return False
    return bool(months) if isinstance(months, list) else bool(str(months).strip())


def _has_mode(payload: Payload) -> bool:
    mode = payload.get("study_mode")
    return bool(mode and str(mode).strip())


_CHECKERS: dict[str, Any] = {
    "fee":      _has_fee,
    "english":  _has_english,
    "duration": _has_duration,
    "intake":   _has_intake,
    "mode":     _has_mode,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_payload(payload: Payload) -> dict[str, Any]:
    """Compute confidence score for a single course payload.

    Returns a dict with:
        score       int   0-100
        breakdown   dict  {label: {"present": bool, "points": int, "max": int}}
        level       str   "pass" | "warn" | "low"
        missing     list  labels of absent fields
    """
    total = 0
    breakdown: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    for label, pts, description in _WEIGHTS:
        checker = _CHECKERS[label]
        present = bool(checker(payload))
        earned = pts if present else 0
        total += earned
        breakdown[label] = {
            "description": description,
            "present": present,
            "points_earned": earned,
            "points_max": pts,
        }
        if not present:
            missing.append(label)

    level = (
        "pass" if total >= CONFIDENCE_PASS
        else ("warn" if total >= CONFIDENCE_WARN else "low")
    )

    return {
        "score": total,
        "level": level,
        "breakdown": breakdown,
        "missing": missing,
    }


def format_confidence_log_line(
    course_name: str,
    result: dict[str, Any],
    url: str = "",
) -> str:
    """Return a single-line human-readable log string for the live scrape log."""
    score = result["score"]
    level = result["level"].upper()
    missing = result["missing"]
    icon = "✅" if level == "PASS" else ("⚠️" if level == "WARN" else "❌")
    name_short = (course_name or "?")[:50]
    parts = [f"{icon} [{level} {score}/100] {name_short}"]
    if missing:
        parts.append(f"— missing: {', '.join(missing)}")
    if url:
        parts.append(f"| {url.split('/')[-1][:40]}")
    return " ".join(parts)

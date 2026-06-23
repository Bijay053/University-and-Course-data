"""Auto-publish gate logic — Bug #6 fixes baked in.

The Node implementation required ``international_fee`` to be present and only
accepted IELTS as the English-test signal. That blocked >40% of legitimate
auto-publishes. The fix:

* International fee is OPTIONAL. (Some unis publish fee on a separate page that
  scrapers can't read; should not gate publication.)
* English requirement: any one of IELTS overall, PTE overall, TOEFL overall,
  Cambridge overall, or Duolingo overall counts.
* Decision threshold (completeness %) lives in settings.

Hard-required field gates (in addition to the completeness floor):
* course_name  — must be present and ≥ 3 chars
* degree_level — must be present
* English test — at least one of IELTS/PTE/TOEFL/Cambridge/Duolingo overall
* duration     — must be non-null (every legitimate course has a duration)
* intake_months — must be non-empty, UNLESS the course is online-only
  (online-only courses sometimes don't publish intake windows)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.models import ScrapedCourse


@dataclass
class AutoPublishDecision:
    auto_publish: bool
    reason: str
    score: float


def _has_english(sc: ScrapedCourse) -> bool:
    return any(
        getattr(sc, attr) is not None and getattr(sc, attr) > 0
        for attr in (
            "ielts_overall",
            "pte_overall",
            "toefl_overall",
            "cambridge_overall",
            "duolingo_overall",
        )
    )


def _has_intake(sc: ScrapedCourse) -> bool:
    """Return True if intake_months is a non-empty list."""
    raw: Any = sc.intake_months
    if raw is None:
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return isinstance(parsed, list) and len(parsed) > 0
        except (ValueError, TypeError):
            return bool(raw.strip())
    return bool(raw)


def _is_online_only(sc: ScrapedCourse) -> bool:
    """Return True when the course is explicitly online-only."""
    mode = (sc.study_mode or "").lower()
    return "online" in mode and "campus" not in mode and "on-campus" not in mode


# Phase A — hard floor for auto-publish.  The configurable
# ``min_completeness_for_auto_publish`` setting can be lowered for
# debugging, but Phase A enforces an absolute lower bound: nothing with
# completeness below this number ever auto-publishes, regardless of
# settings.  Matches the "confidence ≥ 85" rule in
# SCRAPING_ACCURACY_PLAN.md (Phase A step 2).
_PHASE_A_MIN_COMPLETENESS = 85


def should_auto_publish(sc: ScrapedCourse) -> AutoPublishDecision:
    completeness = sc.completeness or 0
    score = float(sc.decision_score or 0)

    # ── Hard-required field checks ────────────────────────────────────────
    if not sc.course_name or len(sc.course_name.strip()) < 3:
        return AutoPublishDecision(False, "Missing or invalid course name", score)
    if not sc.degree_level:
        return AutoPublishDecision(False, "Missing degree level", score)
    if not _has_english(sc):
        return AutoPublishDecision(False, "No English-test score (IELTS/PTE/TOEFL/etc.)", score)
    if sc.duration is None:
        return AutoPublishDecision(False, "Missing duration", score)
    if not _has_intake(sc) and not _is_online_only(sc):
        return AutoPublishDecision(False, "Missing intake months (required for non-online courses)", score)

    # Phase A: take the higher of the configured threshold and the hard floor.
    # The hard floor wins when settings.min_completeness_for_auto_publish < 85,
    # so a misconfiguration cannot accidentally publish low-confidence rows.
    threshold = max(_PHASE_A_MIN_COMPLETENESS, settings.min_completeness_for_auto_publish)
    if completeness < threshold:
        return AutoPublishDecision(
            False,
            f"Completeness {completeness}% < {threshold}% (Phase A floor)",
            score,
        )

    # Phase A: also gate on per-row eligibility_confidence when the
    # extractors provided one.  ``eligibility_confidence`` is populated
    # by completeness scoring downstream; treat None as "unknown" and
    # fall through (i.e. don't block when we have no signal — completeness
    # already covers that case above).
    conf = sc.eligibility_confidence
    if conf is not None and conf < _PHASE_A_MIN_COMPLETENESS:
        return AutoPublishDecision(
            False,
            f"Eligibility confidence {conf:.0f} < {_PHASE_A_MIN_COMPLETENESS} (Phase A floor)",
            score,
        )

    # Phase 9: cross-source verification confidence gate.
    # avg_verification_confidence is set by the verification engine after staging.
    # Only gate when it has been computed (None = not yet run, allow through).
    avg_vc = getattr(sc, "avg_verification_confidence", None)
    if avg_vc is not None and avg_vc < _PHASE_A_MIN_COMPLETENESS:
        return AutoPublishDecision(
            False,
            f"Avg verification confidence {avg_vc:.0f}% < {_PHASE_A_MIN_COMPLETENESS}% (Phase 9)",
            score,
        )

    return AutoPublishDecision(True, "ok", score)

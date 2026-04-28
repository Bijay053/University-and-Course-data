"""Auto-publish gate logic — Bug #6 fixes baked in.

The Node implementation required ``international_fee`` to be present and only
accepted IELTS as the English-test signal. That blocked >40% of legitimate
auto-publishes. The fix:

* International fee is OPTIONAL. (Some unis publish fee on a separate page that
  scrapers can't read; should not gate publication.)
* English requirement: any one of IELTS overall, PTE overall, TOEFL overall,
  Cambridge overall, or Duolingo overall counts.
* Decision threshold (completeness %) lives in settings.
"""
from __future__ import annotations

from dataclasses import dataclass

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

    if not sc.course_name or len(sc.course_name.strip()) < 3:
        return AutoPublishDecision(False, "Missing or invalid course name", score)
    if not sc.degree_level:
        return AutoPublishDecision(False, "Missing degree level", score)
    if not _has_english(sc):
        return AutoPublishDecision(False, "No English-test score (IELTS/PTE/TOEFL/etc.)", score)

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
    return AutoPublishDecision(True, "ok", score)

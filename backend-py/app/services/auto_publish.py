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


def should_auto_publish(sc: ScrapedCourse) -> AutoPublishDecision:
    completeness = sc.completeness or 0
    score = float(sc.decision_score or 0)

    if not sc.course_name or len(sc.course_name.strip()) < 3:
        return AutoPublishDecision(False, "Missing or invalid course name", score)
    if not sc.degree_level:
        return AutoPublishDecision(False, "Missing degree level", score)
    if not _has_english(sc):
        return AutoPublishDecision(False, "No English-test score (IELTS/PTE/TOEFL/etc.)", score)
    if completeness < settings.min_completeness_for_auto_publish:
        return AutoPublishDecision(
            False, f"Completeness {completeness}% < {settings.min_completeness_for_auto_publish}%", score
        )
    return AutoPublishDecision(True, "ok", score)

"""Completeness scoring + eligibility decision for staged courses.

Mirrors Node's `computeCompleteness` + `assessPublishReadiness`. The Review
table's "Score" column reads ``scraped_courses.completeness``; without this
module every row shows "--" and the operator has no triage signal.

Scoring rule: 13 canonical review fields, weighted equally. A field counts
as "filled" when it has a non-empty value. English-test fields collapse to
a single slot (any one of IELTS/PTE/TOEFL/CAE/Duolingo overall) — that
matches the auto-publish rule in ``app.services.auto_publish`` so a course
isn't penalised for offering only PTE instead of IELTS.

Eligibility decision: separate from completeness. It captures the
publish-readiness reasoning that the UI surfaces verbatim
(e.g. "Publish blocked: Needs review: duolingoOverall | Warnings: missing
academic requirement"). Three buckets:
  * ``ready`` — completeness ≥ threshold, no blockers
  * ``review`` — has blockers (missing degree level, missing English test,
    missing course name, etc.)
  * ``blocked`` — not used yet; reserved for hard rejections (kept so the
    UI label set stays open).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.models import ScrapedCourse


# (field_attr, human_label_for_warnings)
REVIEW_FIELDS: tuple[tuple[str, str], ...] = (
    ("course_name", "courseName"),
    ("degree_level", "degreeLevel"),
    ("category", "category"),
    ("study_mode", "studyMode"),
    ("course_location", "courseLocation"),
    ("duration", "duration"),
    ("intake_months", "intakeMonths"),
    ("international_fee", "internationalFee"),
    ("description", "description"),
    ("academic_level", "academicLevel"),
    ("academic_score", "academicScore"),
    # English-test slot: any one of the five overall scores satisfies it.
    # ScrapedCourse won't have this attribute literally; handled below.
    ("__english__", "englishTest"),
    ("other_requirement", "otherRequirement"),
)


def _has_value(sc: ScrapedCourse, attr: str) -> bool:
    if attr == "__english__":
        return any(
            getattr(sc, k, None) is not None and (getattr(sc, k) or 0) > 0
            for k in (
                "ielts_overall",
                "pte_overall",
                "toefl_overall",
                "cambridge_overall",
                "duolingo_overall",
            )
        )
    val = getattr(sc, attr, None)
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return bool(val)
    if isinstance(val, (int, float)):
        return val != 0
    return True


@dataclass
class CompletenessResult:
    score: int                       # 0..100
    missing: list[str]               # human-readable labels
    filled: list[str]


def compute_completeness(sc: ScrapedCourse) -> CompletenessResult:
    filled: list[str] = []
    missing: list[str] = []
    for attr, label in REVIEW_FIELDS:
        if _has_value(sc, attr):
            filled.append(label)
        else:
            missing.append(label)
    total = len(REVIEW_FIELDS)
    score = round(len(filled) / total * 100) if total else 0
    return CompletenessResult(score=score, missing=missing, filled=filled)


@dataclass
class EligibilityDecision:
    status: str          # "ready" | "review" | "blocked"
    reason: str          # human-readable, surfaced verbatim by the UI
    blockers: list[str]
    warnings: list[str]


_HARD_BLOCKERS: tuple[tuple[str, str], ...] = (
    ("course_name", "courseName"),
    ("degree_level", "degreeLevel"),
    ("__english__", "englishTest"),
)


def decide_eligibility(sc: ScrapedCourse, completeness: CompletenessResult) -> EligibilityDecision:
    """Compute the publish-readiness verdict the review modal renders.

    The blocker list is what prevents auto-publish; warnings are advisory
    (low-confidence fields the operator should double-check).
    """
    blockers: list[str] = []
    for attr, label in _HARD_BLOCKERS:
        if not _has_value(sc, attr):
            blockers.append(label)

    # Course-name length sanity check — same as stage_course's own guard
    # but expressed as a blocker so the UI explains why a row that staged
    # OK can still fail to publish.
    if sc.course_name and len(sc.course_name.strip()) < 3:
        blockers.append("courseName")

    warnings: list[str] = []
    # Field-level warnings (non-blocking but worth flagging).
    if not _has_value(sc, "academic_level") and not _has_value(sc, "academic_score"):
        warnings.append("missing academic requirement")
    if not _has_value(sc, "category"):
        warnings.append("missing category")
    if not _has_value(sc, "study_mode"):
        warnings.append("missing studyMode")
    if completeness.score < settings.min_completeness_for_auto_publish:
        warnings.append(
            f"completeness {completeness.score}% < {settings.min_completeness_for_auto_publish}%"
        )

    # T205: build the human-readable reason string the UI surfaces verbatim.
    # Mirrors Node's ``buildReviewNotes`` (routes/scrape.ts: buildReviewNotes)
    # so prod and the dev API write the same shape into
    # ``scraped_courses.eligibility_reason``:
    #   "Publish blocked: <blockers> | Validation: <val> | Missing: <missing>
    #    | Warnings: <warnings>"
    # The "Publish blocked: " prefix is part of the stored value (not a UI
    # boilerplate) so the same string is useful in API responses, log
    # exports, and the React modal without per-surface prefixing. The
    # ``Validation`` slot is reserved for a future per-field validation pass
    # — currently empty so the section is omitted when no items qualify.
    blocker_labels = list(blockers)
    # Missing = completeness.missing minus the items already surfaced as
    # blockers (so we don't double-print "courseName" in two places).
    missing_extra = [
        m for m in completeness.missing if m not in set(blocker_labels)
    ]
    validation: list[str] = []  # placeholder for future per-field validators

    def _build_reason() -> str | None:
        parts: list[str] = []
        if blocker_labels:
            parts.append(f"Publish blocked: {', '.join(blocker_labels)}")
        if validation:
            parts.append(f"Validation: {'; '.join(validation)}")
        if missing_extra:
            parts.append(f"Missing: {', '.join(missing_extra)}")
        if warnings:
            parts.append(f"Warnings: {', '.join(warnings)}")
        return " | ".join(parts) if parts else None

    if blockers:
        return EligibilityDecision(
            status="review",
            reason=_build_reason() or "Publish blocked",
            blockers=blockers,
            warnings=warnings,
        )

    if warnings and completeness.score < settings.min_completeness_for_auto_publish:
        return EligibilityDecision(
            status="review",
            reason=_build_reason() or "review",
            blockers=[],
            warnings=warnings,
        )

    return EligibilityDecision(
        status="ready",
        reason="ok",
        blockers=[],
        warnings=warnings,
    )

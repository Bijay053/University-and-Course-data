"""Phase 9 — Conflict Repair Loop.

When the verification engine flags a field as "conflict", this module attempts to
resolve it automatically before the course is sent for human review.

Design constraints
------------------
* Evidence-only — no live HTTP re-fetches (too slow, out of scope for repair loop).
* Idempotent — the ``conflict_repair_log`` table has a UNIQUE (sc_id, field_name)
  constraint; a second attempt for the same (course, field) is an upsert-noop.
* Soft-fail everywhere — a repair error never aborts the job or modifies the course.
* Never overwrites a higher-authority value with a lower-authority one.

Repair strategy (priority-based, no re-fetch)
----------------------------------------------
A conflict exists when ≥2 source types extracted different normalised values.

Step 1 — Diagnosis: classify by which sources disagree.
Step 2 — Resolution attempt:
    * If ONLY low-authority sources (ai, pattern) disagree with the high-authority
      consensus (html, pdf, api), drop the low-authority sources and recompute
      confidence using only the high-authority evidence  →  ``resolved``.
    * If high-authority sources disagree with each other (html vs pdf, api vs html,
      api vs pdf) or no high-authority source exists  →  ``unresolved``.
Step 3 — Update ``field_verification_results`` with the new confidence + status.
Step 4 — Recompute ``scraped_courses.avg_verification_confidence``.

Source authority
----------------
HIGH: api, html, pdf
LOW:  ai, pattern
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.scraper.verification_engine import (
    _normalize_value,
    classify_source_type,
    compute_field_confidence,
)

log = logging.getLogger(__name__)

HIGH_AUTHORITY: frozenset[str] = frozenset({"api", "html", "pdf"})
LOW_AUTHORITY: frozenset[str] = frozenset({"ai", "pattern"})

# Conflict rate threshold (%) above which the orchestrator queues repair
REPAIR_CONFLICT_RATE_THRESHOLD = 0.0  # queue repair whenever any conflict exists


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ConflictDiagnosis:
    diagnosis_type: str        # see _DIAGNOSIS_* constants below
    high_auth_agree: set[str]  # high-authority sources that agree with consensus
    low_auth_conflict: set[str]  # low-authority sources that conflict
    high_auth_conflict: set[str]  # high-authority sources that conflict (bad)
    can_auto_resolve: bool


@dataclass
class FieldRepairResult:
    field_name: str
    diagnosis_type: str
    action_taken: str          # "drop_low_authority" | "unresolved" | "skipped"
    resolved: bool
    confidence_before: int
    confidence_after: int | None
    resolved_value: str | None
    conflicting_sources: list[str]


@dataclass
class CourseRepairSummary:
    sc_id: int
    fields_attempted: int = 0
    fields_resolved: int = 0
    fields_unresolved: int = 0
    field_results: list[FieldRepairResult] = field(default_factory=list)


@dataclass
class JobRepairSummary:
    job_id: str
    courses_attempted: int = 0
    fields_attempted: int = 0
    fields_resolved: int = 0
    fields_unresolved: int = 0
    avg_confidence_before: float = 0.0
    avg_confidence_after: float = 0.0


# ---------------------------------------------------------------------------
# Diagnosis constants
# ---------------------------------------------------------------------------

DIAG_AI_VS_DIRECT = "ai_vs_direct"
DIAG_PATTERN_VS_DIRECT = "pattern_vs_direct"
DIAG_LOW_AUTHORITY_VS_DIRECT = "low_authority_vs_direct"
DIAG_HTML_VS_PDF = "html_vs_pdf"
DIAG_API_VS_HTML = "api_vs_html"
DIAG_API_VS_PDF = "api_vs_pdf"
DIAG_MULTI_HIGH_AUTHORITY = "multi_high_authority"
DIAG_NO_HIGH_AUTHORITY = "no_high_authority"


def diagnose_conflict(
    conflict_sources: list[str],
    all_source_keys: list[str],
) -> ConflictDiagnosis:
    """Classify why the conflict happened and whether it is auto-resolvable."""
    cs = set(conflict_sources)
    hs = set(all_source_keys)
    agreeing = hs - cs

    high_auth_conflict = cs & HIGH_AUTHORITY
    low_auth_conflict = cs & LOW_AUTHORITY
    high_auth_agree = agreeing & HIGH_AUTHORITY

    # Determine diagnosis type
    if not high_auth_conflict:
        # Only low-authority sources are conflicting
        if low_auth_conflict == {"ai"}:
            dtype = DIAG_AI_VS_DIRECT
        elif low_auth_conflict == {"pattern"}:
            dtype = DIAG_PATTERN_VS_DIRECT
        else:
            dtype = DIAG_LOW_AUTHORITY_VS_DIRECT
        can_auto = bool(high_auth_agree)
    elif len(high_auth_conflict) == 1:
        # Exactly one high-auth source is conflicting; classify by the pair involved.
        # Checks the AGREEING set — one source conflicts, the other(s) agree.
        ch = next(iter(high_auth_conflict))
        if ch in ("html", "pdf") and (high_auth_agree & {"html", "pdf"}):
            # html conflicts while pdf agrees (or vice-versa)
            dtype = DIAG_HTML_VS_PDF
        elif ch == "api" and "html" in high_auth_agree:
            dtype = DIAG_API_VS_HTML
        elif ch == "api" and "pdf" in high_auth_agree:
            dtype = DIAG_API_VS_PDF
        elif not hs & HIGH_AUTHORITY:
            dtype = DIAG_NO_HIGH_AUTHORITY
        else:
            dtype = DIAG_MULTI_HIGH_AUTHORITY
        can_auto = False
    else:
        # Multiple high-auth sources all conflicting with each other
        dtype = DIAG_MULTI_HIGH_AUTHORITY
        can_auto = False

    return ConflictDiagnosis(
        diagnosis_type=dtype,
        high_auth_agree=high_auth_agree,
        low_auth_conflict=low_auth_conflict,
        high_auth_conflict=high_auth_conflict,
        can_auto_resolve=can_auto,
    )


# ---------------------------------------------------------------------------
# Repair attempt (evidence-only)
# ---------------------------------------------------------------------------

def attempt_repair(
    source_values: dict[str, set[str]],
    diagnosis: ConflictDiagnosis,
) -> tuple[str | None, str, dict[str, Any]]:
    """Try to resolve the conflict using source priority rules.

    Returns:
        (resolved_value, action_taken, new_outcome_dict)

    resolved_value is None when action_taken == "unresolved".
    """
    if not diagnosis.can_auto_resolve:
        return None, "unresolved", {}

    # Drop low-authority conflicting sources; recompute from high-auth only
    high_auth_sources = {
        src: vals for src, vals in source_values.items()
        if src in HIGH_AUTHORITY
    }
    if not high_auth_sources:
        return None, "unresolved", {}

    new_outcome = compute_field_confidence(high_auth_sources)

    if new_outcome["status"] == "conflict":
        # Even after dropping low-auth, high-auth sources still disagree
        return None, "unresolved", {}

    return new_outcome["verified_value"], "drop_low_authority", new_outcome


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _load_conflict_fields_for_course(
    db: AsyncSession,
    sc_id: int,
) -> list[dict[str, Any]]:
    """Return all field_verification_results rows with status='conflict'."""
    from app.models.field_verification import FieldVerificationResult

    q = await db.execute(
        select(
            FieldVerificationResult.id,
            FieldVerificationResult.field_name,
            FieldVerificationResult.confidence,
            FieldVerificationResult.conflict_sources,
            FieldVerificationResult.sources,
        ).where(
            FieldVerificationResult.scraped_course_id == sc_id,
            FieldVerificationResult.status == "conflict",
        )
    )
    return [
        {
            "fvr_id": r.id,
            "field_name": r.field_name,
            "confidence_before": r.confidence,
            "conflict_sources": r.conflict_sources or [],
            "sources": r.sources or [],
        }
        for r in q.fetchall()
    ]


async def _load_evidence_for_field(
    db: AsyncSession,
    sc_id: int,
    field_name: str,
) -> dict[str, set[str]]:
    """Return {source_type → set(normalised_value)} for one field."""
    from app.models.evidence import ScrapedFieldEvidence

    q = await db.execute(
        select(
            ScrapedFieldEvidence.candidate_value,
            ScrapedFieldEvidence.normalized_value,
            ScrapedFieldEvidence.extraction_method,
        ).where(
            ScrapedFieldEvidence.scraped_course_id == sc_id,
            ScrapedFieldEvidence.field_key == field_name,
        )
    )
    source_values: dict[str, set[str]] = {}
    for row in q.fetchall():
        raw = row.normalized_value or row.candidate_value
        norm = _normalize_value(field_name, raw)
        if norm is None:
            continue
        src = classify_source_type(row.extraction_method)
        source_values.setdefault(src, set()).add(norm)
    return source_values


async def _already_repaired(db: AsyncSession, sc_id: int, field_name: str) -> bool:
    """Return True if a repair attempt was already logged for this (course, field)."""
    q = await db.execute(
        text(
            "SELECT 1 FROM conflict_repair_log "
            "WHERE scraped_course_id = :sc_id AND field_name = :fn LIMIT 1"
        ),
        {"sc_id": sc_id, "fn": field_name},
    )
    return q.first() is not None


async def _persist_repair_log(
    db: AsyncSession,
    sc_id: int,
    field_name: str,
    diagnosis_type: str,
    action_taken: str,
    resolved: bool,
    confidence_before: int,
    confidence_after: int | None,
    conflicting_sources: list[str],
    resolved_value: str | None,
) -> None:
    """Upsert one row into conflict_repair_log (idempotent via ON CONFLICT DO UPDATE)."""
    import json
    await db.execute(
        text("""
            INSERT INTO conflict_repair_log
                (scraped_course_id, field_name, attempted_at, diagnosis,
                 action_taken, resolved, confidence_before, confidence_after,
                 conflicting_sources, resolved_value)
            VALUES
                (:sc_id, :fn, NOW(), :diag,
                 :action, :resolved, :cb, :ca,
                 cast(:cs as jsonb), :rv)
            ON CONFLICT (scraped_course_id, field_name)
            DO UPDATE SET
                attempted_at       = EXCLUDED.attempted_at,
                diagnosis          = EXCLUDED.diagnosis,
                action_taken       = EXCLUDED.action_taken,
                resolved           = EXCLUDED.resolved,
                confidence_before  = EXCLUDED.confidence_before,
                confidence_after   = EXCLUDED.confidence_after,
                conflicting_sources= EXCLUDED.conflicting_sources,
                resolved_value     = EXCLUDED.resolved_value
        """),
        {
            "sc_id": sc_id,
            "fn": field_name,
            "diag": diagnosis_type,
            "action": action_taken,
            "resolved": resolved,
            "cb": confidence_before,
            "ca": confidence_after,
            "cs": json.dumps(conflicting_sources),
            "rv": resolved_value,
        },
    )


async def _update_fvr(
    db: AsyncSession,
    sc_id: int,
    field_name: str,
    new_outcome: dict[str, Any],
) -> None:
    """Update field_verification_results for one (course, field) after repair."""
    import json
    await db.execute(
        text("""
            UPDATE field_verification_results
               SET confidence         = :conf,
                   status             = :status,
                   verified_value     = :vv,
                   source_count       = :sc_count,
                   sources            = cast(:sources as jsonb),
                   conflict_sources   = NULL,
                   verification_time  = NOW()
             WHERE scraped_course_id  = :sc_id
               AND field_name         = :fn
        """),
        {
            "conf": new_outcome.get("confidence", 0),
            "status": new_outcome.get("status", "needs_review"),
            "vv": (new_outcome.get("verified_value") or "")[:500] or None,
            "sc_count": new_outcome.get("source_count", 1),
            "sources": json.dumps(new_outcome.get("sources", [])),
            "sc_id": sc_id,
            "fn": field_name,
        },
    )


async def _recompute_avg_confidence(db: AsyncSession, sc_id: int) -> float | None:
    """Recalculate and persist avg_verification_confidence for one course."""
    from app.models import ScrapedCourse
    from sqlalchemy import func as _f
    from app.models.field_verification import FieldVerificationResult

    q = await db.execute(
        select(_f.avg(FieldVerificationResult.confidence))
        .where(FieldVerificationResult.scraped_course_id == sc_id)
    )
    avg = q.scalar_one_or_none()
    if avg is None:
        return None
    avg_val = round(float(avg), 2)

    sc = await db.get(ScrapedCourse, sc_id)
    if sc is not None:
        sc.avg_verification_confidence = avg_val
    return avg_val


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def repair_course_conflicts(
    db: AsyncSession,
    sc_id: int,
) -> CourseRepairSummary:
    """Attempt to resolve all conflict fields for one staged course.

    Safe to call multiple times — already-repaired fields are skipped.
    """
    summary = CourseRepairSummary(sc_id=sc_id)
    conflict_fields = await _load_conflict_fields_for_course(db, sc_id)

    if not conflict_fields:
        return summary

    any_resolved = False

    for cf in conflict_fields:
        fn = cf["field_name"]

        # Safety: skip if already repaired (idempotent guard)
        if await _already_repaired(db, sc_id, fn):
            log.debug("[REPAIR] sc=%s field=%s already repaired — skip", sc_id, fn)
            continue

        summary.fields_attempted += 1

        # Load evidence and diagnose
        source_values = await _load_evidence_for_field(db, sc_id, fn)
        if not source_values:
            continue

        conflict_sources = cf["conflict_sources"]
        all_sources = list(source_values.keys())
        diagnosis = diagnose_conflict(conflict_sources, all_sources)

        # Attempt repair
        resolved_value, action_taken, new_outcome = attempt_repair(
            source_values, diagnosis
        )
        resolved = action_taken != "unresolved"

        # Persist repair log (idempotent)
        try:
            await _persist_repair_log(
                db=db,
                sc_id=sc_id,
                field_name=fn,
                diagnosis_type=diagnosis.diagnosis_type,
                action_taken=action_taken,
                resolved=resolved,
                confidence_before=cf["confidence_before"],
                confidence_after=new_outcome.get("confidence") if resolved else None,
                conflicting_sources=conflict_sources,
                resolved_value=resolved_value,
            )
        except Exception as _log_exc:  # noqa: BLE001
            log.warning("[REPAIR] log persist failed sc=%s field=%s: %s", sc_id, fn, _log_exc)

        # Update field_verification_results if resolved
        if resolved and new_outcome:
            try:
                await _update_fvr(db, sc_id, fn, new_outcome)
                any_resolved = True
            except Exception as _fvr_exc:  # noqa: BLE001
                log.warning("[REPAIR] FVR update failed sc=%s field=%s: %s", sc_id, fn, _fvr_exc)

        if resolved:
            summary.fields_resolved += 1
        else:
            summary.fields_unresolved += 1

        summary.field_results.append(
            FieldRepairResult(
                field_name=fn,
                diagnosis_type=diagnosis.diagnosis_type,
                action_taken=action_taken,
                resolved=resolved,
                confidence_before=cf["confidence_before"],
                confidence_after=new_outcome.get("confidence") if resolved else None,
                resolved_value=resolved_value,
                conflicting_sources=conflict_sources,
            )
        )

    # Recompute avg confidence if anything changed
    if any_resolved:
        try:
            await _recompute_avg_confidence(db, sc_id)
        except Exception as _avg_exc:  # noqa: BLE001
            log.warning("[REPAIR] avg recompute failed sc=%s: %s", sc_id, _avg_exc)

    try:
        await db.commit()
    except Exception as _commit_exc:  # noqa: BLE001
        log.warning("[REPAIR] commit failed sc=%s: %s", sc_id, _commit_exc)
        await db.rollback()

    return summary


async def repair_conflicts_for_job(
    db: AsyncSession,
    job_id: str,
) -> JobRepairSummary:
    """Repair all conflict fields across all courses staged in one scrape job.

    Returns a summary suitable for Celery task result / API response.
    """
    from app.models import ScrapedCourse
    from app.models.field_verification import FieldVerificationResult

    job_summary = JobRepairSummary(job_id=job_id)

    # Find all courses in this job that have at least one conflict field
    q = await db.execute(
        select(ScrapedCourse.id, ScrapedCourse.avg_verification_confidence)
        .where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.id.in_(
                select(FieldVerificationResult.scraped_course_id)
                .where(FieldVerificationResult.status == "conflict")
                .distinct()
            ),
        )
    )
    courses = q.fetchall()

    if not courses:
        log.info("[REPAIR] job=%s — no courses with conflicts", job_id)
        return job_summary

    confidences_before = [float(r[1]) for r in courses if r[1] is not None]
    job_summary.avg_confidence_before = (
        round(sum(confidences_before) / len(confidences_before), 1)
        if confidences_before else 0.0
    )

    for (sc_id, _) in courses:
        job_summary.courses_attempted += 1
        try:
            course_result = await repair_course_conflicts(db, sc_id)
            job_summary.fields_attempted += course_result.fields_attempted
            job_summary.fields_resolved += course_result.fields_resolved
            job_summary.fields_unresolved += course_result.fields_unresolved
        except Exception as _exc:  # noqa: BLE001
            log.warning("[REPAIR] course sc=%s failed: %s", sc_id, _exc)

    # Compute after-confidence across same courses
    q2 = await db.execute(
        select(ScrapedCourse.avg_verification_confidence)
        .where(
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.avg_verification_confidence.is_not(None),
        )
    )
    confs_after = [float(r[0]) for r in q2.fetchall()]
    job_summary.avg_confidence_after = (
        round(sum(confs_after) / len(confs_after), 1)
        if confs_after else 0.0
    )

    log.info(
        "[REPAIR] job=%s courses=%d fields=%d resolved=%d unresolved=%d "
        "avg_conf %.1f→%.1f",
        job_id,
        job_summary.courses_attempted,
        job_summary.fields_attempted,
        job_summary.fields_resolved,
        job_summary.fields_unresolved,
        job_summary.avg_confidence_before,
        job_summary.avg_confidence_after,
    )
    return job_summary

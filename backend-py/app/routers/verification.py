"""Phase 9 — Verification & Confidence Engine API endpoints.

GET /api/verification/course/{sc_id}
    Per-field verification results for one staged course.

GET /api/verification/university/{uni_id}/summary
    University-level verification intelligence summary.

GET /api/verification/dashboard
    Fleet-wide verification metrics for the dashboard.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapedCourse
from app.models.evidence import ScrapedFieldEvidence
from app.models.field_verification import FieldVerificationResult

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Per-course verification detail
# ---------------------------------------------------------------------------

@router.get("/verification/course/{sc_id}")
async def get_course_verification(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return all field verification results for a staged course."""
    rows_q = await db.execute(
        select(FieldVerificationResult)
        .where(FieldVerificationResult.scraped_course_id == sc_id)
        .order_by(FieldVerificationResult.confidence.desc())
    )
    rows = rows_q.scalars().all()

    if not rows:
        # Try to run verification on demand if not yet computed
        try:
            from app.services.scraper.verification_engine import run_field_verification
            summary = await run_field_verification(db, sc_id)
            await db.commit()
            # Re-fetch
            rows_q2 = await db.execute(
                select(FieldVerificationResult)
                .where(FieldVerificationResult.scraped_course_id == sc_id)
                .order_by(FieldVerificationResult.confidence.desc())
            )
            rows = rows_q2.scalars().all()
        except Exception as exc:  # noqa: BLE001
            log.warning("on-demand verification failed for sc %s: %s", sc_id, exc)

    fields = [
        {
            "field_name": r.field_name,
            "verified_value": r.verified_value,
            "confidence": r.confidence,
            "status": r.status,
            "source_count": r.source_count,
            "sources": r.sources or [],
            "conflict_sources": r.conflict_sources or [],
            "verification_time": r.verification_time.isoformat() if r.verification_time else None,
        }
        for r in rows
    ]

    confidences = [r.confidence for r in rows]
    avg_conf = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

    return {
        "scraped_course_id": sc_id,
        "avg_confidence": avg_conf,
        "field_count": len(fields),
        "verified_count": sum(1 for r in rows if r.status == "verified"),
        "likely_correct_count": sum(1 for r in rows if r.status == "likely_correct"),
        "needs_review_count": sum(1 for r in rows if r.status == "needs_review"),
        "conflict_count": sum(1 for r in rows if r.status == "conflict"),
        "fields": fields,
    }


# ---------------------------------------------------------------------------
# University-level summary
# ---------------------------------------------------------------------------

@router.get("/verification/university/{uni_id}/summary")
async def get_university_verification_summary(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Verification intelligence summary for all staged courses of a university."""

    # All scraped_course ids for this university (recent status = pending/review/approved)
    sc_ids_q = await db.execute(
        select(ScrapedCourse.id, ScrapedCourse.avg_verification_confidence)
        .where(
            ScrapedCourse.university_id == uni_id,
            ScrapedCourse.status.not_in(["rejected"]),
        )
        .order_by(ScrapedCourse.created_at.desc())
        .limit(500)
    )
    sc_rows = sc_ids_q.fetchall()
    sc_ids = [r[0] for r in sc_rows]
    stored_confs = [r[1] for r in sc_rows if r[1] is not None]

    if not sc_ids:
        return _empty_summary(uni_id)

    # Aggregate from field_verification_results
    agg_q = await db.execute(
        select(
            FieldVerificationResult.status,
            func.count(FieldVerificationResult.id).label("cnt"),
            func.avg(FieldVerificationResult.confidence).label("avg_conf"),
        )
        .where(FieldVerificationResult.scraped_course_id.in_(sc_ids))
        .group_by(FieldVerificationResult.status)
    )
    agg_rows = agg_q.fetchall()

    status_counts: dict[str, int] = {}
    status_avg_conf: dict[str, float] = {}
    total_fields = 0
    for row in agg_rows:
        status_counts[row.status] = row.cnt
        status_avg_conf[row.status] = float(row.avg_conf or 0)
        total_fields += row.cnt

    verified = status_counts.get("verified", 0)
    likely = status_counts.get("likely_correct", 0)
    review = status_counts.get("needs_review", 0)
    conflict = status_counts.get("conflict", 0)

    verified_rate = round(verified / total_fields * 100, 1) if total_fields else 0.0
    conflict_rate = round(conflict / total_fields * 100, 1) if total_fields else 0.0
    low_conf_rate = round(review / total_fields * 100, 1) if total_fields else 0.0

    # Avg confidence across all fields
    all_conf_q = await db.execute(
        select(func.avg(FieldVerificationResult.confidence))
        .where(FieldVerificationResult.scraped_course_id.in_(sc_ids))
    )
    avg_conf_all = float(all_conf_q.scalar_one_or_none() or 0)

    # Auto-publish safe rate: courses where avg_verification_confidence >= 85
    safe_courses = sum(1 for c in stored_confs if c >= 85)
    auto_publish_safe_rate = round(safe_courses / len(sc_ids) * 100, 1) if sc_ids else 0.0

    # Per-field breakdown (top 15 fields by conflict count)
    field_q = await db.execute(
        select(
            FieldVerificationResult.field_name,
            func.count(FieldVerificationResult.id).label("total"),
            func.avg(FieldVerificationResult.confidence).label("avg_conf"),
            func.sum(
                case((FieldVerificationResult.status == "verified", 1), else_=0)
            ).label("verified_cnt"),
            func.sum(
                case((FieldVerificationResult.status == "conflict", 1), else_=0)
            ).label("conflict_cnt"),
        )
        .where(FieldVerificationResult.scraped_course_id.in_(sc_ids))
        .group_by(FieldVerificationResult.field_name)
        .order_by(
            func.sum(
                case((FieldVerificationResult.status == "conflict", 1), else_=0)
            ).desc(),
            FieldVerificationResult.field_name,
        )
        .limit(20)
    )
    field_rows = field_q.fetchall()

    field_breakdown = [
        {
            "field": r.field_name,
            "avg_confidence": round(float(r.avg_conf or 0), 1),
            "verified_count": int(r.verified_cnt or 0),
            "conflict_count": int(r.conflict_cnt or 0),
            "total_count": int(r.total or 0),
        }
        for r in field_rows
    ]

    return {
        "university_id": uni_id,
        "course_count": len(sc_ids),
        "total_fields_verified": total_fields,
        "avg_confidence": round(avg_conf_all, 1),
        "verified_rate": verified_rate,
        "conflict_rate": conflict_rate,
        "low_confidence_rate": low_conf_rate,
        "auto_publish_safe_rate": auto_publish_safe_rate,
        "status_breakdown": {
            "verified": verified,
            "likely_correct": likely,
            "needs_review": review,
            "conflict": conflict,
        },
        "field_breakdown": field_breakdown,
    }


# ---------------------------------------------------------------------------
# Fleet-wide dashboard metrics
# ---------------------------------------------------------------------------

@router.get("/verification/dashboard")
async def get_verification_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Fleet-wide Phase 9 verification metrics (spec §T008)."""

    # Status distribution across all verification results
    agg_q = await db.execute(
        select(
            FieldVerificationResult.status,
            func.count(FieldVerificationResult.id).label("cnt"),
        )
        .group_by(FieldVerificationResult.status)
    )
    agg_rows = agg_q.fetchall()

    status_counts: dict[str, int] = {}
    total = 0
    for row in agg_rows:
        status_counts[row.status] = row.cnt
        total += row.cnt

    verified = status_counts.get("verified", 0)
    conflict = status_counts.get("conflict", 0)
    review = status_counts.get("needs_review", 0)

    verified_rate = round(verified / total * 100, 1) if total else 0.0
    conflict_rate = round(conflict / total * 100, 1) if total else 0.0
    low_conf_rate = round(review / total * 100, 1) if total else 0.0

    # Overall avg confidence
    avg_q = await db.execute(
        select(func.avg(FieldVerificationResult.confidence))
    )
    avg_conf = float(avg_q.scalar_one_or_none() or 0)

    # Auto-publish safe rate: scraped_courses where avg_verification_confidence >= 85
    total_sc_q = await db.execute(
        select(func.count(ScrapedCourse.id))
        .where(ScrapedCourse.avg_verification_confidence.isnot(None))
    )
    total_sc = total_sc_q.scalar_one()

    safe_sc_q = await db.execute(
        select(func.count(ScrapedCourse.id))
        .where(ScrapedCourse.avg_verification_confidence >= 85)
    )
    safe_sc = safe_sc_q.scalar_one()

    auto_publish_safe_rate = round(safe_sc / total_sc * 100, 1) if total_sc else 0.0

    return {
        "total_field_verifications": total,
        "avg_confidence": round(avg_conf, 1),
        "verified_rate": verified_rate,
        "conflict_rate": conflict_rate,
        "low_confidence_rate": low_conf_rate,
        "auto_publish_safe_rate": auto_publish_safe_rate,
        "status_breakdown": {
            "verified": verified,
            "likely_correct": status_counts.get("likely_correct", 0),
            "needs_review": review,
            "conflict": conflict,
        },
    }


def _empty_summary(uni_id: int) -> dict[str, Any]:
    return {
        "university_id": uni_id,
        "course_count": 0,
        "total_fields_verified": 0,
        "avg_confidence": 0.0,
        "verified_rate": 0.0,
        "conflict_rate": 0.0,
        "low_confidence_rate": 0.0,
        "auto_publish_safe_rate": 0.0,
        "status_breakdown": {
            "verified": 0,
            "likely_correct": 0,
            "needs_review": 0,
            "conflict": 0,
        },
        "field_breakdown": [],
    }

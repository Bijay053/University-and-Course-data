"""Agent Recovery API endpoints.

GET  /api/scrape/recovery/{scraped_course_id}
    Returns all agent_recovery_results rows for a course.

PATCH /api/scrape/recovery/{result_id}
    Body: { "action": "apply" | "reject" }
    - apply: writes recovered value into scraped_courses, inserts evidence row,
             sets status = 'applied'.
    - reject: sets status = 'rejected' only.

POST /api/scrape/recovery/trigger
    Body: { "scraped_course_id": 123 }
    Runs a fresh single-course recovery pass and returns new results.
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db

log = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/scrape/recovery/summary/{runtime_job_id}
# ---------------------------------------------------------------------------

@router.get("/recovery/summary/{runtime_job_id}")
async def get_recovery_summary(
    runtime_job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return aggregate recovery stats for a scrape job.

    Only counts actionable rows (pending / applied / rejected).
    Diagnostic trace rows (no_source, no_value, level_mismatch,
    browser_failed, pdf_failed) are excluded so the counts reflect
    real recovery attempts only.
    """
    _TRACE_STATUSES = (
        "no_source", "no_value", "level_mismatch",
        "browser_failed", "pdf_failed",
    )
    trace_list = ", ".join(f"'{s}'" for s in _TRACE_STATUSES)

    row = (await db.execute(
        text(f"""
            SELECT
                COUNT(DISTINCT scraped_course_id)                              AS courses_with_recovery,
                COUNT(*) FILTER (WHERE status = 'pending')                     AS pending,
                COUNT(*) FILTER (WHERE status = 'applied')                     AS applied,
                COUNT(*) FILTER (WHERE status = 'rejected')                    AS rejected,
                COUNT(*) FILTER (WHERE status = 'pending'
                                   AND confidence >= 0.80)                     AS high_confidence_pending,
                COUNT(DISTINCT source_url)
                    FILTER (WHERE source_type IN ('pdf', 'pdf_broad'))          AS pdf_sources,
                COUNT(DISTINCT source_url)
                    FILTER (WHERE source_type = 'pdf_broad')                   AS pdf_broad_sources
            FROM agent_recovery_results
            WHERE scrape_run_id = :run_id
              AND status NOT IN ({trace_list})
        """),
        {"run_id": runtime_job_id},
    )).first()

    return {
        "coursesWithRecovery": int(row.courses_with_recovery or 0) if row else 0,
        "pending": int(row.pending or 0) if row else 0,
        "applied": int(row.applied or 0) if row else 0,
        "rejected": int(row.rejected or 0) if row else 0,
        "highConfidencePending": int(row.high_confidence_pending or 0) if row else 0,
        # PDF source counts: total PDFs used + how many needed the broad-keyword fallback
        "pdfSources": int(row.pdf_sources or 0) if row else 0,
        "pdfBroadSources": int(row.pdf_broad_sources or 0) if row else 0,
    }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELD_TO_COLUMN: dict[str, str] = {
    "international_fee": "international_fee",
    "ielts_overall": "ielts_overall",
    "intake_months": "intake_months",
    "course_location": "course_location",
    "other_requirement": "other_requirement",
}


def _parse_recovered_value(field: str, raw: str | None) -> object:
    """Coerce the stored string back to the correct Python type."""
    if raw is None:
        return None
    if field == "international_fee":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
    if field == "ielts_overall":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
    if field == "intake_months":
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return val
        except (ValueError, TypeError):
            pass
        return [raw] if raw else None
    # string fields
    return raw


async def _fetch_result(db: AsyncSession, result_id: int) -> dict:
    row = (await db.execute(
        text(
            "SELECT id, scraped_course_id, scrape_run_id, field, recovered_value, "
            "source_url, source_type, evidence_text, confidence, mapping_reason, "
            "status, created_at "
            "FROM agent_recovery_results WHERE id = :id"
        ),
        {"id": result_id},
    )).first()
    if not row:
        raise HTTPException(status_code=404, detail="Recovery result not found")
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# GET /api/scrape/recovery/{scraped_course_id}
# ---------------------------------------------------------------------------

@router.get("/recovery/{scraped_course_id}")
async def get_recovery_results(
    scraped_course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return all agent_recovery_results for a staged course."""
    rows = (await db.execute(
        text(
            "SELECT id, scraped_course_id, scrape_run_id, field, recovered_value, "
            "source_url, source_type, evidence_text, confidence, mapping_reason, "
            "status, created_at "
            "FROM agent_recovery_results "
            "WHERE scraped_course_id = :sc_id "
            "ORDER BY status, confidence DESC NULLS LAST, id"
        ),
        {"sc_id": scraped_course_id},
    )).all()

    results = [
        {
            "id": r.id,
            "scrapedCourseId": r.scraped_course_id,
            "scrapeRunId": r.scrape_run_id,
            "field": r.field,
            "recoveredValue": r.recovered_value,
            "sourceUrl": r.source_url,
            "sourceType": r.source_type,
            "evidenceText": r.evidence_text,
            "confidence": r.confidence,
            "mappingReason": r.mapping_reason,
            "status": r.status,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"results": results, "total": len(results)}


# ---------------------------------------------------------------------------
# PATCH /api/scrape/recovery/{result_id}
# ---------------------------------------------------------------------------

class RecoveryActionBody(BaseModel):
    action: str  # "apply" | "reject"


@router.patch("/recovery/{result_id}")
async def act_on_recovery_result(
    result_id: int,
    body: RecoveryActionBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Apply or reject a recovery result."""
    if body.action not in ("apply", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'apply' or 'reject'")

    row = await _fetch_result(db, result_id)

    if row["status"] != "pending":
        # Trace rows (no_source, no_value, level_mismatch, browser_failed, pdf_failed)
        # are diagnostic only and cannot be applied or rejected.
        # Applied/rejected rows represent final operator decisions.
        status = row["status"]
        if status in ("applied", "rejected"):
            raise HTTPException(status_code=409, detail=f"Result already {status}")
        raise HTTPException(
            status_code=409,
            detail=f"Row is a diagnostic trace ({status}) — only 'pending' results can be applied or rejected",
        )

    sc_id = row["scraped_course_id"]
    field = row["field"]

    if body.action == "reject":
        await db.execute(
            text("UPDATE agent_recovery_results SET status='rejected' WHERE id=:id"),
            {"id": result_id},
        )
        await db.commit()
        log.info("[RECOVERY:api] result=%d rejected for course=%s field=%r", result_id, sc_id, field)
        return {"ok": True, "action": "rejected", "resultId": result_id}

    # === APPLY ===
    if field not in _FIELD_TO_COLUMN:
        raise HTTPException(
            status_code=422,
            detail=f"Field {field!r} is not yet supported for apply",
        )

    raw_value = row["recovered_value"]
    value = _parse_recovered_value(field, raw_value)

    if value is None:
        raise HTTPException(status_code=422, detail="Recovered value could not be parsed")

    col = _FIELD_TO_COLUMN[field]

    # Write value into scraped_courses
    if field == "intake_months":
        # JSONB column
        await db.execute(
            text(f"UPDATE scraped_courses SET {col} = CAST(:v AS jsonb) WHERE id=:id"),
            {"v": json.dumps(value), "id": sc_id},
        )
    elif field in ("international_fee", "ielts_overall"):
        await db.execute(
            text(f"UPDATE scraped_courses SET {col} = :v WHERE id=:id"),
            {"v": float(value), "id": sc_id},
        )
    else:
        await db.execute(
            text(f"UPDATE scraped_courses SET {col} = :v WHERE id=:id"),
            {"v": str(value), "id": sc_id},
        )

    # Insert evidence row
    source_url = row.get("source_url")
    snippet = row.get("evidence_text")
    confidence = row.get("confidence")

    await db.execute(
        text(
            """
            INSERT INTO scraped_field_evidence
                (scraped_course_id, field_key, candidate_value, normalized_value,
                 source_url, extraction_method, snippet, confidence,
                 decision_status, selected)
            VALUES
                (:sc_id, :field, :cval, :nval,
                 :source_url, 'agent_recovery', :snippet, :conf,
                 'selected', true)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "sc_id": sc_id,
            "field": field,
            "cval": raw_value,
            "nval": raw_value,
            "source_url": source_url,
            "snippet": str(snippet)[:1000] if snippet else None,
            "conf": confidence,
        },
    )

    # Mark recovery result as applied
    await db.execute(
        text("UPDATE agent_recovery_results SET status='applied' WHERE id=:id"),
        {"id": result_id},
    )

    await db.commit()
    log.info(
        "[RECOVERY:api] result=%d APPLIED for course=%s field=%r value=%r",
        result_id, sc_id, field, value,
    )

    # Recompute completeness for the course
    try:
        from app.models import ScrapedCourse
        from app.services.scraper.completeness import compute_completeness, decide_eligibility
        sc = await db.get(ScrapedCourse, sc_id)
        if sc:
            comp = compute_completeness(sc)
            dec = decide_eligibility(sc, comp)
            await db.execute(
                text(
                    "UPDATE scraped_courses SET completeness=:c, "
                    "eligibility_status=:es, eligibility_reason=:er WHERE id=:id"
                ),
                {
                    "c": comp.score,
                    "es": dec.status,
                    "er": dec.reason,
                    "id": sc_id,
                },
            )
            await db.commit()
    except Exception as exc:
        log.warning("[RECOVERY:api] completeness recompute failed for course=%s: %s", sc_id, exc)

    return {
        "ok": True,
        "action": "applied",
        "resultId": result_id,
        "field": field,
        "value": raw_value,
    }


# ---------------------------------------------------------------------------
# POST /api/scrape/recovery/trigger
# ---------------------------------------------------------------------------

class RecoveryTriggerBody(BaseModel):
    scraped_course_id: int


@router.post("/recovery/trigger")
async def trigger_recovery(
    body: RecoveryTriggerBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Run a fresh single-course recovery pass."""
    from app.services.scraper.recovery.run_recovery import run_single_course_recovery

    log.info("[RECOVERY:api] trigger requested for course=%s", body.scraped_course_id)
    try:
        results = await run_single_course_recovery(body.scraped_course_id, db)
    except Exception as exc:
        log.exception("[RECOVERY:api] trigger failed for course=%s: %s", body.scraped_course_id, exc)
        raise HTTPException(status_code=500, detail=f"Recovery pass failed: {exc}")

    return {
        "ok": True,
        "scrapedCourseId": body.scraped_course_id,
        "results": results,
        "total": len(results),
    }

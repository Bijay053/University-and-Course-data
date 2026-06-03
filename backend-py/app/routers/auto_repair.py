"""Auto-repair suggestions router.

Routes:
  GET  /api/settings/auto-repair?university_ids=1,2,3[&status=pending,ready,developer_required]
  POST /api/settings/auto-repair/{id}/apply
  POST /api/settings/auto-repair/{id}/dismiss
  POST /api/settings/auto-repair/trigger   body: {university_id, regression_alert_id?}
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.permissions import require_permission

log = logging.getLogger(__name__)
router = APIRouter()

_DEFAULT_STATUSES = "pending,ready,developer_required"


class _ApplyBody(BaseModel):
    applied_by: str | None = None


class _TriggerBody(BaseModel):
    university_id: int
    regression_alert_id: int | None = None


def _row_to_dict(row: dict) -> dict:
    vr = row.get("validation_result") or {}
    return {
        "id":                  row["id"],
        "university_id":       row["university_id"],
        "regression_alert_id": row["regression_alert_id"],
        "issue_summary":       row["issue_summary"],
        "root_cause_category": row["root_cause_category"],
        "fix_recommendation":  row["fix_recommendation"],
        "fix_yaml_snippet":    row["fix_yaml_snippet"],
        "safe_fix":            row["safe_fix"],
        "risk_label":          row["risk_label"],
        "developer_note":      row["developer_note"],
        "fail_reason":         row.get("fail_reason"),
        "evidence":            row["evidence"] if isinstance(row["evidence"], list) else [],
        "validation_result":   vr,
        "confidence":          row["confidence"],
        "status":              row["status"],
        "created_at":          row["created_at"].isoformat() if row["created_at"] else None,
        "applied_at":          row["applied_at"].isoformat()   if row["applied_at"]   else None,
        "dismissed_at":        row["dismissed_at"].isoformat() if row["dismissed_at"] else None,
        "applied_by":          row.get("applied_by"),
        "old_config":          row.get("old_config"),
        "new_config":          row.get("new_config"),
    }


@router.get("/auto-repair")
async def list_auto_repair(
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    university_ids: str = Query(..., description="Comma-separated university IDs"),
    status: str = Query(_DEFAULT_STATUSES, description="Comma-separated statuses"),
) -> JSONResponse:
    """List auto-repair suggestions for the given universities."""
    ids = [
        int(i) for i in university_ids.split(",")
        if i.strip().lstrip("-").isdigit() and int(i.strip()) > 0
    ]
    if not ids:
        return JSONResponse(content={"suggestions": {}})

    valid = {"pending", "ready", "developer_required", "applied", "dismissed", "failed"}
    statuses = [s.strip() for s in status.split(",") if s.strip() in valid]
    if not statuses:
        statuses = ["pending", "ready", "developer_required"]

    res = await db.execute(text("""
        SELECT
            id, university_id, regression_alert_id,
            issue_summary, root_cause_category, fix_recommendation,
            fix_yaml_snippet, safe_fix, risk_label, developer_note,
            fail_reason, evidence, validation_result, confidence,
            status, created_at, applied_at, dismissed_at,
            applied_by, old_config, new_config
        FROM auto_repair_suggestions
        WHERE university_id = ANY(:ids)
          AND status        = ANY(:statuses)
        ORDER BY university_id, created_at DESC
    """), {"ids": ids, "statuses": statuses})

    suggestions: dict[str, list[dict]] = {}
    for row in res.mappings():
        uid = str(row["university_id"])
        if uid not in suggestions:
            suggestions[uid] = []
        suggestions[uid].append(_row_to_dict(row))

    return JSONResponse(content={"suggestions": suggestions})


@router.post("/auto-repair/{suggestion_id}/apply")
async def apply_repair(
    suggestion_id: int,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: _ApplyBody = _ApplyBody(),
) -> JSONResponse:
    """Apply the proposed fix to the university's config.

    Pass ``{"applied_by": "..."}`` in the JSON body to record who triggered the apply.
    Falls back to the session user email if omitted.
    """
    from app.services.auto_repair import apply_fix_to_university
    actor = body.applied_by or (_user.get("email") if isinstance(_user, dict) else None) or "admin"
    try:
        result = await apply_fix_to_university(suggestion_id, db, applied_by=actor)
        return JSONResponse(content=result)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/auto-repair/{suggestion_id}/dismiss")
async def dismiss_repair(
    suggestion_id: int,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    res = await db.execute(text("""
        UPDATE auto_repair_suggestions
        SET status = 'dismissed', dismissed_at = NOW()
        WHERE id = :sid AND status NOT IN ('applied', 'dismissed')
        RETURNING id
    """), {"sid": suggestion_id})
    row = res.scalar()
    if not row:
        raise HTTPException(404, "Suggestion not found or already dismissed/applied")
    await db.commit()
    return JSONResponse(content={"id": suggestion_id, "status": "dismissed"})


@router.post("/auto-repair/trigger")
async def trigger_repair(
    body: _TriggerBody,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Manually trigger the auto-repair pipeline for a university (runs async)."""
    from app.tasks.auto_repair_task import generate_repair_suggestion

    task = generate_repair_suggestion.delay(body.university_id, body.regression_alert_id)
    return JSONResponse(content={"task_id": task.id, "university_id": body.university_id, "status": "queued"})

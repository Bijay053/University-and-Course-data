"""Regression alerts router — CRUD for university_regression_alerts.

Routes:
  GET  /api/settings/regression-alerts?university_ids=1,2,3[&status=open]
  POST /api/settings/regression-alerts/{alert_id}/acknowledge
  POST /api/settings/regression-alerts/{alert_id}/resolve
  POST /api/settings/regression-alerts/detect          (manual trigger)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.permissions import require_permission

log = logging.getLogger(__name__)
router = APIRouter()


def _row_to_dict(row: dict) -> dict:
    return {
        "id":             row["id"],
        "university_id":  row["university_id"],
        "job_id":         row["job_id"],
        "alert_type":     row["alert_type"],
        "severity":       row["severity"],
        "previous_value": float(row["previous_value"]) if row["previous_value"] is not None else None,
        "current_value":  float(row["current_value"])  if row["current_value"]  is not None else None,
        "delta":          float(row["delta"])           if row["delta"]          is not None else None,
        "probable_causes": row["probable_causes"] if isinstance(row["probable_causes"], list) else [],
        "status":         row["status"],
        "snapshot_date":  str(row["snapshot_date"]) if row["snapshot_date"] else None,
        "created_at":     row["created_at"].isoformat() if row["created_at"] else None,
        "acknowledged_at":row["acknowledged_at"].isoformat() if row["acknowledged_at"] else None,
        "resolved_at":    row["resolved_at"].isoformat()     if row["resolved_at"]     else None,
    }


@router.get("/regression-alerts")
async def list_regression_alerts(
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    university_ids: str = Query(..., description="Comma-separated university IDs"),
    status: str = Query("open,acknowledged", description="Comma-separated statuses to include"),
) -> JSONResponse:
    """Return open/acknowledged regression alerts for the given universities."""
    ids: list[int] = [
        int(i) for i in university_ids.split(",")
        if i.strip().lstrip("-").isdigit() and int(i.strip()) > 0
    ]
    if not ids:
        return JSONResponse(content={"alerts": {}})

    statuses = [s.strip() for s in status.split(",") if s.strip()]
    valid = {"open", "acknowledged", "resolved"}
    statuses = [s for s in statuses if s in valid] or ["open", "acknowledged"]

    res = await db.execute(text("""
        SELECT
            id, university_id, job_id, alert_type, severity,
            previous_value, current_value, delta, probable_causes,
            status, snapshot_date, created_at, acknowledged_at, resolved_at
        FROM university_regression_alerts
        WHERE university_id = ANY(:ids)
          AND status        = ANY(:statuses)
        ORDER BY university_id, severity DESC, created_at DESC
    """), {"ids": ids, "statuses": statuses})

    alerts: dict[str, list[dict]] = {}
    for row in res.mappings():
        uid = str(row["university_id"])
        if uid not in alerts:
            alerts[uid] = []
        alerts[uid].append(_row_to_dict(row))

    return JSONResponse(content={"alerts": alerts})


@router.post("/regression-alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: int,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    res = await db.execute(text("""
        UPDATE university_regression_alerts
        SET status = 'acknowledged', acknowledged_at = NOW()
        WHERE id = :aid AND status = 'open'
        RETURNING id
    """), {"aid": alert_id})
    row = res.scalar()
    if not row:
        raise HTTPException(404, "Alert not found or not in 'open' state")
    await db.commit()
    return JSONResponse(content={"id": alert_id, "status": "acknowledged"})


@router.post("/regression-alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    res = await db.execute(text("""
        UPDATE university_regression_alerts
        SET status = 'resolved', resolved_at = NOW()
        WHERE id = :aid AND status != 'resolved'
        RETURNING id
    """), {"aid": alert_id})
    row = res.scalar()
    if not row:
        raise HTTPException(404, "Alert not found or already resolved")
    await db.commit()
    return JSONResponse(content={"id": alert_id, "status": "resolved"})


@router.post("/regression-alerts/detect")
async def trigger_detection(
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    university_ids: str | None = Query(None, description="Optional: only check these uni IDs"),
) -> JSONResponse:
    """Run regression detection on-demand (useful after a manual health snapshot)."""
    from app.services.regression_detector import run_regression_detection
    result = await run_regression_detection(db)
    return JSONResponse(content=result)

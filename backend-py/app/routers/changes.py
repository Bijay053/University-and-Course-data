"""Phase 10 — Change Detection API router.

Endpoints:
  GET /api/universities/{uni_id}/changes
  GET /api/scrape/jobs/{job_id}/changes
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func as sa_func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.course_change_event import CourseChangeEvent

log = logging.getLogger(__name__)
router = APIRouter()

_VALID_SEVERITIES = {"critical", "major", "minor", "info"}
_VALID_CHANGE_TYPES = {"new_course", "removed_course", "field_change"}
_VALID_STATUSES = {"new", "acknowledged", "resolved"}


def _event_dict(e: CourseChangeEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "university_id": e.university_id,
        "course_id": e.course_id,
        "course_name": e.course_name,
        "scrape_job_id": e.scrape_job_id,
        "field_name": e.field_name,
        "old_value": e.old_value,
        "new_value": e.new_value,
        "change_type": e.change_type,
        "severity": e.severity,
        "confidence_before": e.confidence_before,
        "confidence_after": e.confidence_after,
        "detected_at": e.detected_at.isoformat() if e.detected_at else None,
        "status": e.status,
    }


def _summary(events: list[CourseChangeEvent]) -> dict[str, Any]:
    """Aggregate counts for a list of events."""
    by_severity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for e in events:
        by_severity[e.severity] = by_severity.get(e.severity, 0) + 1
        by_type[e.change_type] = by_type.get(e.change_type, 0) + 1
    return {
        "total": len(events),
        "critical": by_severity.get("critical", 0),
        "major": by_severity.get("major", 0),
        "minor": by_severity.get("minor", 0),
        "info": by_severity.get("info", 0),
        "new_courses": by_type.get("new_course", 0),
        "removed_courses": by_type.get("removed_course", 0),
        "field_changes": by_type.get("field_change", 0),
    }


@router.get("/universities/{uni_id}/changes")
async def get_university_changes(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
    severity: str | None = Query(None),
    change_type: str | None = Query(None, alias="type"),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Return change events for a university with optional filters.

    Query params:
      severity   — critical | major | minor | info
      type       — new_course | removed_course | field_change
      status     — new | acknowledged | resolved
      limit      — default 100, max 500
      offset     — pagination offset
    """
    q = select(CourseChangeEvent).where(
        CourseChangeEvent.university_id == uni_id
    )
    if severity and severity in _VALID_SEVERITIES:
        q = q.where(CourseChangeEvent.severity == severity)
    if change_type and change_type in _VALID_CHANGE_TYPES:
        q = q.where(CourseChangeEvent.change_type == change_type)
    if status and status in _VALID_STATUSES:
        q = q.where(CourseChangeEvent.status == status)

    q = q.order_by(desc(CourseChangeEvent.detected_at)).limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    # Also return summary counts (unfiltered by pagination) for this uni
    count_q = select(
        CourseChangeEvent.severity,
        CourseChangeEvent.change_type,
        sa_func.count(CourseChangeEvent.id).label("n"),
    ).where(CourseChangeEvent.university_id == uni_id)
    if status and status in _VALID_STATUSES:
        count_q = count_q.where(CourseChangeEvent.status == status)
    count_q = count_q.group_by(
        CourseChangeEvent.severity, CourseChangeEvent.change_type
    )
    count_rows = (await db.execute(count_q)).all()

    summary: dict[str, int] = {
        "total": 0, "critical": 0, "major": 0, "minor": 0, "info": 0,
        "new_courses": 0, "removed_courses": 0, "field_changes": 0,
    }
    _type_key = {"new_course": "new_courses", "removed_course": "removed_courses", "field_change": "field_changes"}
    for sev, ctype, n in count_rows:
        summary["total"] += n
        if sev in summary:
            summary[sev] += n
        if ctype in _type_key:
            summary[_type_key[ctype]] += n

    return {"summary": summary, "events": [_event_dict(e) for e in rows]}


@router.get("/scrape/jobs/{job_id}/changes")
async def get_job_changes(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
    severity: str | None = Query(None),
) -> dict[str, Any]:
    """Return all change events detected in a specific scrape job."""
    q = select(CourseChangeEvent).where(
        CourseChangeEvent.scrape_job_id == job_id
    )
    if severity and severity in _VALID_SEVERITIES:
        q = q.where(CourseChangeEvent.severity == severity)
    q = q.order_by(desc(CourseChangeEvent.detected_at))
    rows = (await db.execute(q)).scalars().all()
    return {
        "job_id": job_id,
        "summary": _summary(rows),
        "events": [_event_dict(e) for e in rows],
    }


@router.patch("/changes/{event_id}/status")
async def update_change_status(
    event_id: int,
    body: dict[str, str],
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[Any, Depends(get_current_user)],
) -> dict[str, Any]:
    """Acknowledge or resolve a change event. Body: {status: 'acknowledged'|'resolved'}"""
    new_status = body.get("status", "")
    if new_status not in _VALID_STATUSES:
        return {"ok": False, "error": f"Invalid status: {new_status!r}"}
    event = await db.get(CourseChangeEvent, event_id)
    if event is None:
        return {"ok": False, "error": "Not found"}
    event.status = new_status
    await db.commit()
    return {"ok": True, "id": event_id, "status": new_status}

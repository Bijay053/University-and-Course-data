"""Scraped-course review queue endpoints (admin-only)."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapedCourse, University

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/scraped-courses")
async def list_scraped_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    status_filter: str | None = Query(default=None, alias="status"),
    university_id: int | None = None,
    auto_publish_status: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    stmt = select(ScrapedCourse, University.name.label("university_name")).join(
        University, ScrapedCourse.university_id == University.id
    )
    if status_filter:
        stmt = stmt.where(ScrapedCourse.status == status_filter)
    if auto_publish_status:
        stmt = stmt.where(ScrapedCourse.auto_publish_status == auto_publish_status)
    if university_id:
        stmt = stmt.where(ScrapedCourse.university_id == university_id)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapedCourse.created_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).all()

    return {
        "data": [
            {
                **{c.name: getattr(sc, c.name) for c in sc.__table__.columns},
                "university_name": uname,
            }
            for sc, uname in rows
        ],
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@router.post("/scraped-courses/{sc_id}/approve")
async def approve_scraped_course(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Promote a staged scraped_course into the live ``courses`` table.
    Delegates to ``app.services.scraper.approve_course`` which contains the
    Bug #1 case-insensitive dedup logic."""
    from app.services.scraper.approve_course import approve_scraped_course as _approve

    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    result = await _approve(db, sc, actor=user.get("email", "admin"))
    return result


@router.post("/scraped-courses/{sc_id}/reject")
async def reject_scraped_course(
    sc_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default_factory=dict),
) -> dict:
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    sc.status = "rejected"
    sc.auto_publish_status = "rejected"
    if "reason" in body:
        sc.notes = (sc.notes or "") + f"\nREJECTED: {body['reason']}"
    await db.commit()
    return {"ok": True, "id": sc_id, "status": "rejected"}


@router.post("/scraped-courses/bulk-approve")
async def bulk_approve_scraped_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[dict, Depends(get_current_user)],
    university_id: int = Query(..., description="University to promote courses for"),
    sc_status: str = Query(
        default="pending",
        alias="status",
        description="scraped_course.status filter (pending | approved)",
    ),
    ap_status: str = Query(
        default="ready",
        alias="auto_publish_status",
        description="auto_publish_status filter — use 'ready' for new rows, "
                    "'pending_review' for pre-feature legacy rows",
    ),
    dry_run: bool = Query(
        default=False,
        description="When true, count matching rows and return a preview without committing",
    ),
    limit: int = Query(
        default=500,
        ge=1,
        le=2000,
        description="Safety cap: max rows to promote in one call",
    ),
) -> dict:
    """Bulk-promote scraped courses into the live ``courses`` table.

    Covers three known gap scenarios:

    * ``status=pending  & auto_publish_status=ready``          – AUT (73 rows)
    * ``status=approved & auto_publish_status=ready``          – KBS legacy (30 rows)
    * ``status=approved & auto_publish_status=pending_review`` – CSU legacy (84 rows)

    Always run with ``dry_run=true`` first to preview the count, then repeat
    without it to commit.  Each row is promoted via the same
    ``approve_scraped_course`` service used by the single-row endpoint, so
    dedup, satellite rows (fees, intakes, English requirements) and status
    updates are identical.
    """
    from app.services.scraper.approve_course import approve_scraped_course as _approve

    stmt = (
        select(ScrapedCourse)
        .where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.status == sc_status,
            ScrapedCourse.auto_publish_status == ap_status,
        )
        .order_by(ScrapedCourse.id)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()

    if dry_run:
        return {
            "dry_run": True,
            "university_id": university_id,
            "status_filter": sc_status,
            "auto_publish_status_filter": ap_status,
            "would_approve": len(rows),
            "sample_names": [r.course_name for r in rows[:10]],
        }

    actor = user.get("email", "admin")
    approved: list[dict] = []
    failed: list[dict] = []

    for sc in rows:
        try:
            result = await _approve(db, sc, actor=actor)
            approved.append({"scraped_course_id": sc.id, "course_id": result.get("course_id")})
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "bulk_approve: failed sc_id=%s uni=%s: %s",
                sc.id, university_id, exc,
            )
            failed.append({"scraped_course_id": sc.id, "error": str(exc)})

    return {
        "ok": True,
        "university_id": university_id,
        "status_filter": sc_status,
        "auto_publish_status_filter": ap_status,
        "approved": len(approved),
        "failed": len(failed),
        "failures": failed,
    }

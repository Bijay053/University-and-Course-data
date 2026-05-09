"""Admin dashboard endpoints.

Mirrors the Node API surface (artifacts/api-server/src/routes/dashboard.ts):
- /summary  — legacy snake_case payload (kept for backwards compat)
- /stats    — camelCase payload the React Dashboard page reads via the
             generated client (`useGetDashboardStats`)
- /recent-changes
- /courses-by-level
- /upcoming-intakes
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models import (
    Course,
    Intake,
    Scholarship,
    ScrapedCourse,
    ScrapeRuntimeJob,
    ScrapingChange,
    University,
)

router = APIRouter()


@router.get("/summary")
async def summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    total_unis = (await db.execute(select(func.count(University.id)))).scalar_one()
    total_courses = (await db.execute(select(func.count(Course.id)))).scalar_one()
    pending_review = (
        await db.execute(
            select(func.count(ScrapedCourse.id)).where(ScrapedCourse.status == "pending")
        )
    ).scalar_one()
    auto_published = (
        await db.execute(
            select(func.count(ScrapedCourse.id)).where(
                ScrapedCourse.auto_publish_status == "auto_published"
            )
        )
    ).scalar_one()
    running_jobs = (
        await db.execute(
            select(func.count(ScrapeRuntimeJob.runtime_job_id)).where(
                ScrapeRuntimeJob.status.in_(["queued", "running"])
            )
        )
    ).scalar_one()
    return {
        "total_universities": int(total_unis or 0),
        "total_courses": int(total_courses or 0),
        "pending_review": int(pending_review or 0),
        "auto_published": int(auto_published or 0),
        "running_jobs": int(running_jobs or 0),
    }


@router.get("/stats")
async def stats(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Bug K: ported from Node dashboard.ts. The React Dashboard page binds
    to these exact camelCase keys via the generated client; missing the
    endpoint flooded the browser console with 404s on every page load.
    """
    try:
        uni_count = (await db.execute(select(func.count(University.id)))).scalar_one() or 0
        course_count = (await db.execute(select(func.count(Course.id)))).scalar_one() or 0
        scholarship_count = (
            await db.execute(select(func.count(Scholarship.id)))
        ).scalar_one() or 0
        pending_count = (
            await db.execute(
                select(func.count(ScrapingChange.id)).where(ScrapingChange.status == "pending")
            )
        ).scalar_one() or 0
        active_job_count = (
            await db.execute(
                select(func.count(ScrapeRuntimeJob.runtime_job_id)).where(
                    ScrapeRuntimeJob.status.in_(["queued", "running"])
                )
            )
        ).scalar_one() or 0
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        courses_this_month = (
            await db.execute(
                select(func.count(Course.id)).where(Course.created_at >= month_start)
            )
        ).scalar_one() or 0
        return {
            "totalUniversities": int(uni_count),
            "totalCourses": int(course_count),
            "totalScholarships": int(scholarship_count),
            "pendingChanges": int(pending_count),
            "activeScrapingJobs": int(active_job_count),
            "coursesThisMonth": int(courses_this_month),
        }
    except Exception as exc:
        tb = traceback.format_exc()
        _log.error("dashboard/stats failed: %s", tb)
        try:
            with open("/tmp/dashboard_stats_error.log", "w") as fh:
                fh.write(tb)
        except Exception:
            pass
        raise


@router.get("/recent-changes")
async def recent_changes(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    rows = (
        await db.execute(
            select(ScrapingChange).order_by(desc(ScrapingChange.detected_at)).limit(10)
        )
    ).scalars().all()
    out: list[dict] = []
    for r in rows:
        d = {c.name: getattr(r, c.name) for c in r.__table__.columns}
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        out.append(d)
    return out


@router.get("/courses-by-level")
async def courses_by_level(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    rows = (
        await db.execute(
            select(Course.degree_level.label("label"), func.count().label("count"))
            .group_by(Course.degree_level)
        )
    ).all()
    return [{"label": (r.label or "Unknown"), "count": int(r.count)} for r in rows]


@router.get("/upcoming-intakes")
async def upcoming_intakes(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    stmt = (
        select(
            Intake.course_id.label("courseId"),
            Course.name.label("courseName"),
            University.name.label("universityName"),
            Intake.intake_month,
            Intake.intake_year,
            Intake.is_open,
        )
        .select_from(Intake)
        .outerjoin(Course, Course.id == Intake.course_id)
        .outerjoin(University, University.id == Course.university_id)
        .where(Intake.is_open == True)  # noqa: E712 — SQLAlchemy needs ==True
        .limit(10)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "courseId": r.courseId,
            "courseName": r.courseName or "Unknown",
            "universityName": r.universityName or "Unknown",
            "intakeMonth": r.intake_month,
            "intakeYear": r.intake_year,
            "isOpen": r.is_open,
        }
        for r in rows
    ]

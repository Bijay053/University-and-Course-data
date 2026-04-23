"""Admin dashboard summary endpoint."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, ScrapedCourse, ScrapeRuntimeJob, University

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

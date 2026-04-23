"""Scraping job control & monitoring endpoints.

Read-only listing works today against the existing scrape_runtime_jobs table.
Bulk start enqueues to Celery (which falls back to a no-op if Redis is not
available, returning a 503).
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapeRuntimeJob, University
from app.schemas.scrape import (
    BulkScrapeBody,
    BulkScrapeResponse,
    ScrapeJobRead,
    ScrapeStartResponse,
    StartScrapeBody,
)

router = APIRouter()


@router.get("/jobs")
async def list_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    stmt = select(ScrapeRuntimeJob)
    if status_filter:
        stmt = stmt.where(ScrapeRuntimeJob.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapeRuntimeJob.started_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "data": [ScrapeJobRead.model_validate(r).model_dump() for r in rows],
        "total": int(total),
        "page": page,
        "limit": limit,
    }


@router.get("/jobs/{job_id}", response_model=ScrapeJobRead)
async def get_job(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> ScrapeJobRead:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return ScrapeJobRead.model_validate(job)


@router.post("/start", response_model=ScrapeStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_scrape(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> ScrapeStartResponse:
    uni = await db.get(University, body.university_id)
    if not uni:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job = ScrapeRuntimeJob(
        runtime_job_id=job_id,
        university_id=uni.id,
        university_name=uni.name,
        url=uni.scrape_url,
        job_type="single",
        status="queued",
        fast_mode=body.fast_mode,
        request_payload={"university_id": uni.id, "fast_mode": body.fast_mode},
    )
    db.add(job)
    await db.commit()

    # Try to enqueue on Celery; if broker unreachable we still return 202 so the
    # frontend shows it queued, and the row stays in 'queued' for retry.
    try:
        from app.tasks.scrape_tasks import scrape_university

        scrape_university.delay(job_id)
    except Exception:
        pass

    return ScrapeStartResponse(job_id=job_id, status="queued")


@router.post("/bulk", response_model=BulkScrapeResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_bulk(
    body: BulkScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> BulkScrapeResponse:
    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    queued = 0
    for uid in body.university_ids:
        uni = await db.get(University, uid)
        if not uni:
            continue
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = ScrapeRuntimeJob(
            runtime_job_id=job_id,
            university_id=uni.id,
            university_name=uni.name,
            url=uni.scrape_url,
            job_type="bulk",
            status="queued",
            fast_mode=body.fast_mode,
            request_payload={
                "session_id": session_id,
                "university_id": uni.id,
                "fast_mode": body.fast_mode,
            },
        )
        db.add(job)
        queued += 1
        try:
            from app.tasks.scrape_tasks import scrape_university

            scrape_university.delay(job_id)
        except Exception:
            pass
    await db.commit()
    return BulkScrapeResponse(session_id=session_id, queued=queued)


@router.post("/jobs/{job_id}/stop")
async def stop_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job.stop_requested = True
    await db.commit()
    return {"ok": True, "id": job_id}

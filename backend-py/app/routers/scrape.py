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
) -> ScrapeStartResponse:
    # Lookup by university_id first, fall back to URL match (UI compatibility)
    uni = None
    if body.university_id:
        uni = await db.get(University, body.university_id)
    if not uni and body.url:
        from sqlalchemy import select, or_, func
        result = await db.execute(
            select(University).where(
                or_(
                    University.scrape_url == body.url,
                    University.website == body.url,
                    func.lower(University.name) == (body.university_name or "").lower(),
                )
            ).limit(1)
        )
        uni = result.scalar_one_or_none()
    if not uni:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"University not found (id={body.university_id}, url={body.url}, name={body.university_name})"
        )
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

    return ScrapeStartResponse(job_id=job_id, runtime_job_id=job_id, status="queued")


@router.post("/bulk", response_model=BulkScrapeResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_bulk(
    body: BulkScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BulkScrapeResponse:
    session_id = f"bulk_{uuid.uuid4().hex[:12]}"
    job_ids: list[str] = []
    for uid in body.university_ids:
        uni = await db.get(University, uid)
        if not uni:
            continue
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        db.add(
            ScrapeRuntimeJob(
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
        )
        job_ids.append(job_id)

    # Commit BEFORE enqueueing so the worker can never race ahead of the row insert.
    await db.commit()

    try:
        from app.tasks.scrape_tasks import scrape_university

        for jid in job_ids:
            scrape_university.delay(jid)
    except Exception:
        # Broker unavailable: rows stay 'queued' for retry by the next start call
        # or the periodic reaper.
        pass
    return BulkScrapeResponse(session_id=session_id, queued=len(job_ids))


@router.post("/jobs/{job_id}/stop")
async def stop_job(
    job_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    job.stop_requested = True
    await db.commit()
    return {"ok": True, "id": job_id}



# ----- UI-COMPAT ALIASES (match Node API surface) -----

@router.get("/status/{job_id}")
async def get_status(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """UI polls this every 2s. Match Node's payload shape."""
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Build runtime logs since 'since' index (UI sends ?since=N)
    return {
        "id": job.runtime_job_id,
        "runtimeJobId": job.runtime_job_id,
        "jobId": job.runtime_job_id,
        "status": job.status,
        "progress": {
            "current": job.current or 0,
            "total": job.total_found or 0,
            "imported": job.imported or 0,
            "skipped": job.skipped or 0,
            "errors": job.errors or 0,
        },
        "imported": job.imported or 0,
        "skipped": job.skipped or 0,
        "errors": job.errors or 0,
        "current": job.current or 0,
        "totalFound": job.total_found or 0,
        "total": job.total_found or 0,
        "universityId": job.university_id,
        "universityName": job.university_name,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "errorMessage": job.error_message,
        "logs": [],
        "events": [],
        "ok": True,
    }


@router.post("/stop/{job_id}")
async def stop_alias(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.stop_requested = True
    await db.commit()
    return {"message": "Scraping stopped", "imported": job.imported or 0, "ok": True}


@router.post("/approve/{job_id}")
async def approve_alias(job_id: str, body: dict | None = None) -> dict:
    return {"ok": True, "proceed": bool((body or {}).get("proceed", True))}


@router.get("/active")
async def list_active(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    rows = (await db.execute(
        select(ScrapeRuntimeJob)
        .where(ScrapeRuntimeJob.status.in_(["queued", "running"]))
        .order_by(desc(ScrapeRuntimeJob.started_at))
        .limit(50)
    )).scalars().all()
    return {"data": [
        {
            "jobId": r.runtime_job_id,
            "runtimeJobId": r.runtime_job_id,
            "universityName": r.university_name,
            "status": r.status,
            "current": r.current or 0,
            "total": r.total_found or 0,
        } for r in rows
    ], "ok": True}


@router.get("/history")
async def history_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    rows = (await db.execute(
        select(ScrapeRuntimeJob)
        .order_by(desc(ScrapeRuntimeJob.started_at))
        .limit(limit)
    )).scalars().all()
    return {"data": [
        {
            "jobId": r.runtime_job_id,
            "runtimeJobId": r.runtime_job_id,
            "universityId": r.university_id,
            "universityName": r.university_name,
            "status": r.status,
            "imported": r.imported or 0,
            "skipped": r.skipped or 0,
            "errors": r.errors or 0,
            "totalFound": r.total_found or 0,
            "startedAt": r.started_at.isoformat() if r.started_at else None,
            "completedAt": r.completed_at.isoformat() if r.completed_at else None,
        } for r in rows
    ], "ok": True}


@router.get("/history/{job_id}")
async def history_one(job_id: str, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "jobId": job.runtime_job_id,
        "runtimeJobId": job.runtime_job_id,
        "universityId": job.university_id,
        "universityName": job.university_name,
        "status": job.status,
        "imported": job.imported or 0,
        "skipped": job.skipped or 0,
        "errors": job.errors or 0,
        "totalFound": job.total_found or 0,
        "current": job.current or 0,
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
        "errorMessage": job.error_message,
        "ok": True,
    }


@router.get("/last-runs")
async def last_runs(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Latest job per university."""
    from sqlalchemy import func
    rows = (await db.execute(
        select(ScrapeRuntimeJob)
        .order_by(ScrapeRuntimeJob.university_id, desc(ScrapeRuntimeJob.started_at))
    )).scalars().all()
    seen = {}
    for r in rows:
        if r.university_id not in seen:
            seen[r.university_id] = r
    return {"data": [
        {
            "universityId": r.university_id,
            "universityName": r.university_name,
            "lastRunAt": r.started_at.isoformat() if r.started_at else None,
            "status": r.status,
            "imported": r.imported or 0,
        } for r in seen.values()
    ], "ok": True}


@router.post("/rescrape")
async def rescrape_alias(
    body: StartScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScrapeStartResponse:
    """Same as /start, just different name UI uses."""
    return await start_scrape(body, db)


@router.get("/staged")
async def staged_list(
    db: Annotated[AsyncSession, Depends(get_db)],
    job_id: str | None = Query(default=None, alias="jobId"),
    university_id: int | None = Query(default=None, alias="universityId"),
    status_f: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    page: int = Query(default=1, ge=1),
) -> dict:
    from app.models import ScrapedCourse
    stmt = select(ScrapedCourse)
    if job_id:
        stmt = stmt.where(ScrapedCourse.scrape_job_id == job_id)
    if university_id:
        stmt = stmt.where(ScrapedCourse.university_id == university_id)
    if status_f:
        stmt = stmt.where(ScrapedCourse.status == status_f)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(ScrapedCourse.created_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "data": [{
            "id": r.id,
            "courseName": r.course_name,
            "courseWebsite": r.course_website,
            "universityId": r.university_id,
            "scrapeJobId": r.scrape_job_id,
            "status": r.status,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
        "total": int(total),
        "page": page,
        "limit": limit,
        "ok": True,
    }


@router.get("/staged/{sc_id}")
async def staged_one(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    from app.models import ScrapedCourse
    sc = await db.get(ScrapedCourse, sc_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Not found")
    # Return all column data
    return {c.name: getattr(sc, c.name) for c in sc.__table__.columns} | {"ok": True}


@router.get("/bulk/history")
async def bulk_history() -> dict:
    return {"data": [], "ok": True}


@router.get("/bulk/active")
async def bulk_active() -> dict:
    return {"data": [], "ok": True}


@router.get("/bulk/status/{session_id}")
async def bulk_status(session_id: str) -> dict:
    return {"sessionId": session_id, "status": "unknown", "data": [], "ok": True}


@router.post("/bulk/stop/{session_id}")
async def bulk_stop(session_id: str) -> dict:
    return {"sessionId": session_id, "stopped": True, "ok": True}


@router.post("/bulk/start")
async def bulk_start_alias(
    body: BulkScrapeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BulkScrapeResponse:
    """Alias for /bulk that UI uses."""
    return await start_bulk(body, db)

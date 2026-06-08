"""Snapshot and replay API endpoints.

GET  /api/scrape/snapshots/{job_id}          — list snapshots for a job
POST /api/scrape/replay/{job_id}             — trigger replay extraction (diff only)
POST /api/scrape/replay/{job_id}/commit      — replay and commit changes to DB
GET  /api/scrape/snapshot/download/{job_id}  — presign URL for a specific snapshot key
POST /api/scrape/snapshot/setup-lifecycle    — apply S3 lifecycle rules (admin)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.page_snapshot import PageSnapshot
from app.services.scraper.replay_extraction import replay_job
from app.services.snapshot_store import (
    is_enabled,
    list_snapshots_for_job,
    presign_url,
    setup_lifecycle_rules,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ── Response models ──────────────────────────────────────────────────────────

class SnapshotMeta(BaseModel):
    id: int
    university_id: int
    scrape_job_id: str
    course_url: str
    url_hash: str
    snapshot_type: str
    storage_path: str | None
    status_code: int | None
    content_length: int | None
    fetch_method: str | None
    fetched_at: str | None

    class Config:
        from_attributes = True


class SnapshotListResponse(BaseModel):
    job_id: str
    total: int
    s3_enabled: bool
    s3_objects: list[dict]
    db_records: list[SnapshotMeta]


class ReplayResponse(BaseModel):
    job_id: str
    replayed: int
    changed: int
    unchanged: int
    errors: int
    commit: bool
    message: str
    diffs: list[dict]


class ReplayRequest(BaseModel):
    use_ai_fallback: bool = False
    max_courses: int = 500


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/snapshots/{job_id}", response_model=SnapshotListResponse)
async def list_snapshots(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """List all snapshots for a scrape job — DB metadata + S3 object list."""
    # DB records
    result = await db.execute(
        select(PageSnapshot)
        .where(PageSnapshot.scrape_job_id == job_id)
        .order_by(PageSnapshot.fetched_at.desc())
        .limit(1000)
    )
    records = list(result.scalars().all())

    db_metas: list[SnapshotMeta] = []
    for r in records:
        db_metas.append(SnapshotMeta(
            id=r.id,
            university_id=r.university_id,
            scrape_job_id=r.scrape_job_id,
            course_url=r.course_url,
            url_hash=r.url_hash,
            snapshot_type=r.snapshot_type,
            storage_path=r.storage_path,
            status_code=r.status_code,
            content_length=r.content_length,
            fetch_method=r.fetch_method,
            fetched_at=r.fetched_at.isoformat() if r.fetched_at else None,
        ))

    # S3 object listing
    s3_objects: list[dict] = []
    if records and is_enabled():
        university_id = records[0].university_id
        s3_objects = await list_snapshots_for_job(university_id, job_id)

    return SnapshotListResponse(
        job_id=job_id,
        total=len(records),
        s3_enabled=is_enabled(),
        s3_objects=s3_objects,
        db_records=db_metas,
    )


@router.post("/replay/{job_id}", response_model=ReplayResponse)
async def replay_scrape_job(
    job_id: str,
    body: ReplayRequest = ReplayRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Replay extraction from S3 snapshots — returns diff without committing."""
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 snapshot storage is not configured.")
    try:
        result = await replay_job(
            job_id,
            commit=False,
            max_courses=body.max_courses,
            db=db,
        )
        return ReplayResponse(**result)
    except Exception as exc:
        log.exception("replay failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/replay/{job_id}/commit", response_model=ReplayResponse)
async def replay_and_commit(
    job_id: str,
    body: ReplayRequest = ReplayRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Replay extraction from S3 snapshots AND commit changes to scraped_courses."""
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 snapshot storage is not configured.")
    try:
        result = await replay_job(
            job_id,
            commit=True,
            max_courses=body.max_courses,
            db=db,
        )
        return ReplayResponse(**result)
    except Exception as exc:
        log.exception("replay+commit failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/snapshot/download/{job_id}")
async def get_snapshot_presigned_url(
    job_id: str,
    key: str = Query(..., description="S3 object key"),
    expires: int = Query(3600, ge=60, le=86400),
):
    """Generate a pre-signed S3 URL to download a specific snapshot."""
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 not configured.")
    url = await presign_url(key, expires_in=expires)
    if not url:
        raise HTTPException(status_code=404, detail="Could not generate presigned URL.")
    return {"url": url, "expires_in": expires}


@router.post("/snapshot/setup-lifecycle")
async def apply_s3_lifecycle():
    """Apply S3 lifecycle rules for automatic snapshot expiry. Run once on setup."""
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 not configured.")
    ok = setup_lifecycle_rules()
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to apply lifecycle rules — check logs.")
    return {"ok": True, "message": "S3 lifecycle rules applied successfully."}

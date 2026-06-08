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
from sqlalchemy import func, select
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

@router.get("/snapshots/{job_id}/summary")
async def snapshot_summary(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Lightweight summary for the UI badge — count + latest date, no S3 listing."""
    row = (await db.execute(
        select(func.count(), func.max(PageSnapshot.fetched_at))
        .where(PageSnapshot.scrape_job_id == job_id)
    )).one()
    count, latest_at = int(row[0]), row[1]
    has = count > 0
    return {
        "job_id": job_id,
        "snapshot_count": count,
        "has_snapshots": has,
        "replay_available": has and is_enabled(),
        "latest_snapshot_at": latest_at.isoformat() if latest_at else None,
        "s3_enabled": is_enabled(),
    }


@router.get("/snapshots/storage-stats")
async def storage_stats(
    db: AsyncSession = Depends(get_db),
):
    """S3 storage monitor — aggregate counts from page_snapshots DB table.

    Returns per-university and per-type breakdowns so operators can spot
    storage growth after the first 10-20 university runs without needing
    direct AWS console access.
    """
    from app.models import University

    # Total counts + size estimate by snapshot_type
    type_rows = (await db.execute(
        select(
            PageSnapshot.snapshot_type,
            func.count().label("snap_count"),
            func.sum(PageSnapshot.content_length).label("raw_bytes"),
        )
        .group_by(PageSnapshot.snapshot_type)
        .order_by(func.count().desc())
    )).all()

    # Per-university breakdown (top 50 by count)
    uni_rows = (await db.execute(
        select(
            PageSnapshot.university_id,
            func.count().label("snap_count"),
            func.sum(PageSnapshot.content_length).label("raw_bytes"),
            func.min(PageSnapshot.fetched_at).label("first_snap"),
            func.max(PageSnapshot.fetched_at).label("latest_snap"),
        )
        .group_by(PageSnapshot.university_id)
        .order_by(func.count().desc())
        .limit(50)
    )).all()

    # Resolve university names
    uni_ids = [r[0] for r in uni_rows]
    name_map: dict[int, str] = {}
    if uni_ids:
        name_rows = (await db.execute(
            select(University.id, University.name).where(University.id.in_(uni_ids))
        )).all()
        name_map = {r[0]: r[1] for r in name_rows}

    # Total summary
    total_row = (await db.execute(
        select(func.count(), func.sum(PageSnapshot.content_length))
    )).one()
    total_count = int(total_row[0] or 0)
    total_raw_bytes = int(total_row[1] or 0)
    # Gzip saves ~70%; multiply by 0.30 for estimated S3 size
    estimated_s3_bytes = int(total_raw_bytes * 0.30)

    # Jobs with snapshots (distinct scrape_job_id count)
    job_count_row = (await db.execute(
        select(func.count(func.distinct(PageSnapshot.scrape_job_id)))
    )).scalar_one()

    return {
        "s3_enabled": is_enabled(),
        "total_snapshots": total_count,
        "distinct_jobs_with_snapshots": int(job_count_row or 0),
        "total_raw_bytes": total_raw_bytes,
        "estimated_s3_bytes": estimated_s3_bytes,
        "estimated_s3_mb": round(estimated_s3_bytes / 1_048_576, 2),
        "by_type": [
            {
                "snapshot_type": r[0],
                "count": int(r[1]),
                "raw_bytes": int(r[2] or 0),
            }
            for r in type_rows
        ],
        "by_university": [
            {
                "university_id": r[0],
                "university_name": name_map.get(r[0], f"uni_{r[0]}"),
                "count": int(r[1]),
                "raw_bytes": int(r[2] or 0),
                "first_snapshot_at": r[3].isoformat() if r[3] else None,
                "latest_snapshot_at": r[4].isoformat() if r[4] else None,
            }
            for r in uni_rows
        ],
        "note": "raw_bytes is pre-gzip size; estimated_s3_bytes applies 0.30 ratio (70% gzip reduction).",
    }


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

"""Snapshot and replay API endpoints.

GET  /api/scrape/snapshots/{job_id}          — list snapshots for a job
POST /api/scrape/replay/{job_id}             — trigger replay extraction (diff only)
GET  /api/scrape/replay/{job_id}/stream      — SSE stream of replay progress (diff only)
POST /api/scrape/replay/{job_id}/commit      — replay and commit changes to DB
GET  /api/scrape/snapshot/download/{job_id}  — presign URL for a specific snapshot key
POST /api/scrape/snapshot/setup-lifecycle    — apply S3 lifecycle rules (admin)
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.page_snapshot import PageSnapshot
from app.permissions import require_permission
from app.services.scraper.replay_extraction import replay_job, restore_review_rows
from app.services.snapshot_store import (
    get_snapshot_bytes,
    is_enabled,
    list_snapshots_for_job,
    presign_url,
    setup_lifecycle_rules,
    url_hash as _url_hash,
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


class RestoreReviewRequest(BaseModel):
    commit: bool = False


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
    course_url: str | None = Query(None, description="Filter to a single course URL (for per-course replay)"),
    body: ReplayRequest = ReplayRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Replay extraction from S3 snapshots — returns diff without committing.

    Pass course_url to replay a single course instead of the whole job.
    """
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 snapshot storage is not configured.")
    try:
        result = await replay_job(
            job_id,
            commit=False,
            max_courses=body.max_courses,
            course_url=course_url,
            db=db,
        )
        return ReplayResponse(**result)
    except Exception as exc:
        log.exception("replay failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/restore-review/{job_id}")
async def restore_scrape_review_rows(
    job_id: str,
    _user: Annotated[dict, Depends(require_permission("staged.approve"))],
    body: RestoreReviewRequest = RestoreReviewRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Preview or restore deleted review rows without fetching course pages."""
    try:
        result = await restore_review_rows(job_id, commit=body.commit, db=db)
        if not result["chain_job_ids"]:
            raise HTTPException(status_code=404, detail=result["message"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("review-row restore failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/replay/{job_id}/stream")
async def replay_stream(
    job_id: str,
    course_url: str | None = Query(None),
    max_courses: int = Query(500, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream of replay-extraction progress (diff-only, no commit).

    Streams server-sent events while re-extracting snapshots.
    Each event is ``data: <json>\\n\\n`` where json has keys:
      event   — "status" | "progress" | "warn" | "done" | "error"
      message — human-readable text
      (done also carries a ``result`` key with the full ReplayResponse dict)
    """
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 snapshot storage is not configured.")

    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def _emit(event: str, message: str, **kwargs: object) -> None:
        await queue.put({"event": event, "message": message, **kwargs})

    async def _run() -> None:
        try:
            result = await replay_job(
                job_id,
                commit=False,
                max_courses=max_courses,
                course_url=course_url,
                db=db,
                emit=_emit,
            )
            await queue.put({"event": "done", "message": result["message"], "result": result})
        except Exception as exc:
            log.exception("replay stream failed for job %s", job_id)
            await queue.put({"event": "error", "message": str(exc)})
        finally:
            await queue.put(None)  # sentinel

    async def _generate():
        task = asyncio.create_task(_run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield f"data: {_json.dumps(item)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/replay/{job_id}/commit", response_model=ReplayResponse)
async def replay_and_commit(
    job_id: str,
    course_url: str | None = Query(None, description="Filter to a single course URL (for per-course replay)"),
    body: ReplayRequest = ReplayRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Replay extraction from S3 snapshots AND commit changes to scraped_courses.

    Pass course_url to replay and commit a single course only.
    """
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 snapshot storage is not configured.")
    try:
        result = await replay_job(
            job_id,
            commit=True,
            max_courses=body.max_courses,
            course_url=course_url,
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


@router.get("/snapshot/for-course")
async def get_snapshots_for_course(
    job_id: str = Query(..., description="Scrape runtime job ID"),
    course_url: str = Query(..., description="Course page URL"),
    db: AsyncSession = Depends(get_db),
):
    """List all snapshots captured for a specific course URL within a scrape job.

    Returns DB metadata + presigned download URLs for each snapshot.
    Powers the 'View raw source' button in the staged-course review panel.
    """
    h = _url_hash(course_url)
    result = await db.execute(
        select(PageSnapshot)
        .where(
            PageSnapshot.scrape_job_id == job_id,
            PageSnapshot.url_hash == h,
        )
        .order_by(PageSnapshot.snapshot_type, PageSnapshot.fetched_at.desc())
    )
    records = list(result.scalars().all())

    snaps = []
    for r in records:
        download_url = None
        if r.storage_path and is_enabled():
            download_url = await presign_url(r.storage_path)
        snaps.append({
            "id": r.id,
            "snapshot_type": r.snapshot_type,
            "fetch_method": r.fetch_method,
            "content_length": r.content_length,
            "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
            "scraper_commit": r.scraper_commit,
            "yaml_version": r.yaml_version,
            "original_extraction": r.original_extraction or {},
            "download_url": download_url,
            "storage_path": r.storage_path,
            "has_text": r.snapshot_type in ("ai_prompt", "html", "repair"),
        })

    return {
        "job_id": job_id,
        "course_url": course_url,
        "url_hash": h,
        "snapshots": snaps,
        "s3_enabled": is_enabled(),
        "total": len(snaps),
    }


@router.get("/snapshot/text/{snapshot_id}")
async def get_snapshot_text(
    snapshot_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return decompressed UTF-8 text content of an ai_prompt, html, or repair snapshot.

    Fetches the gzip from S3 and decompresses server-side so the browser
    can display the raw prompt or HTML inline without S3 CORS concerns.
    """
    import gzip as _gzip
    from fastapi.responses import PlainTextResponse

    r = await db.get(PageSnapshot, snapshot_id)
    if not r:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    if r.snapshot_type not in ("ai_prompt", "html", "repair"):
        raise HTTPException(
            status_code=400,
            detail=f"Text view not supported for snapshot type '{r.snapshot_type}'.",
        )
    if not r.storage_path:
        raise HTTPException(status_code=404, detail="No storage path recorded for this snapshot.")
    if not is_enabled():
        raise HTTPException(status_code=503, detail="S3 storage not configured.")

    raw = await get_snapshot_bytes(r.storage_path)
    if raw is None:
        raise HTTPException(status_code=502, detail="Failed to fetch snapshot bytes from S3.")

    try:
        text = _gzip.decompress(raw).decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    return PlainTextResponse(content=text)

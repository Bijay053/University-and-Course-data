"""Phase 13 — Autonomous Monitoring Engine — API router.

Endpoints:
  GET  /api/monitoring/stats          — dashboard summary stats
  GET  /api/monitoring                — list all watchers
  GET  /api/monitoring/{uni_id}       — single watcher detail
  POST /api/monitoring/{uni_id}/enable
  POST /api/monitoring/{uni_id}/disable
  PUT  /api/monitoring/{uni_id}       — update strategy / probe_url
  POST /api/monitoring/{uni_id}/probe — manual probe (non-blocking result)
  POST /api/monitoring/bulk-enable    — enable all universities that have a URL
  DELETE /api/monitoring/{uni_id}     — remove watcher (re-created on next enable)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.university import University
from app.models.university_watcher import UniversityWatcher
from app.services.monitoring_engine import (
    _watcher_to_dict,
    apply_probe_result,
    compute_next_check_at,
    detect_change,
    get_monitoring_stats,
    get_or_create_watcher,
    list_watchers,
    run_probe,
    trigger_scrape,
)

router = APIRouter()


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/monitoring/stats")
async def get_stats(db: Annotated[AsyncSession, Depends(get_db)]):
    return await get_monitoring_stats(db)


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/monitoring")
async def get_all_watchers(db: Annotated[AsyncSession, Depends(get_db)]):
    return await list_watchers(db)


# ── Single watcher ────────────────────────────────────────────────────────────

@router.get("/monitoring/{uni_id}")
async def get_watcher(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(UniversityWatcher, University.name.label("uni_name"), University.country.label("uni_country"))
        .join(University, University.id == UniversityWatcher.university_id)
        .where(UniversityWatcher.university_id == uni_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watcher not found")
    w, uni_name, uni_country = row
    return _watcher_to_dict(w, uni_name, uni_country)


# ── Enable / disable ──────────────────────────────────────────────────────────

@router.post("/monitoring/{uni_id}/enable")
async def enable_watcher(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    watcher = await get_or_create_watcher(uni_id, db)
    watcher.enabled = True
    if not watcher.next_check_at or watcher.next_check_at < datetime.now(timezone.utc):
        watcher.next_check_at = compute_next_check_at(watcher.change_frequency_days)
    await db.commit()
    return {"ok": True, "enabled": True, "university_id": uni_id}


@router.post("/monitoring/{uni_id}/disable")
async def disable_watcher(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(UniversityWatcher).where(UniversityWatcher.university_id == uni_id)
    )
    watcher = result.scalar_one_or_none()
    if watcher:
        watcher.enabled = False
        await db.commit()
    return {"ok": True, "enabled": False, "university_id": uni_id}


# ── Update ────────────────────────────────────────────────────────────────────

class WatcherUpdate(BaseModel):
    monitoring_strategy: str | None = None
    probe_url: str | None = None
    enabled: bool | None = None


@router.put("/monitoring/{uni_id}")
async def update_watcher(
    uni_id: int,
    body: WatcherUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    watcher = await get_or_create_watcher(uni_id, db)
    if body.monitoring_strategy is not None:
        if body.monitoring_strategy not in ("passive", "active", "deep"):
            raise HTTPException(status_code=400, detail="strategy must be passive, active, or deep")
        watcher.monitoring_strategy = body.monitoring_strategy
    if body.probe_url is not None:
        watcher.probe_url = body.probe_url.strip() or None
    if body.enabled is not None:
        watcher.enabled = body.enabled
    await db.commit()
    return {"ok": True, "university_id": uni_id}


# ── Manual probe ──────────────────────────────────────────────────────────────

@router.post("/monitoring/{uni_id}/probe")
async def manual_probe(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    watcher = await get_or_create_watcher(uni_id, db)
    if not watcher.probe_url:
        raise HTTPException(status_code=422, detail="No probe URL configured for this university")

    probe = await run_probe(watcher)
    changed = detect_change(watcher, probe)
    await apply_probe_result(watcher, probe, changed, db)

    job_id = None
    if changed:
        job_id = await trigger_scrape(watcher, db)
    else:
        await db.commit()

    return {
        "university_id": uni_id,
        "changed": changed,
        "probe": probe,
        "scrape_triggered": job_id is not None,
        "job_id": job_id,
    }


# ── Bulk enable ───────────────────────────────────────────────────────────────

@router.post("/monitoring/bulk-enable")
async def bulk_enable(db: Annotated[AsyncSession, Depends(get_db)]):
    """Enable monitoring for every university that has a scrape_url or website set."""
    unis_result = await db.execute(
        select(University).where(
            (University.scrape_url.isnot(None) & (University.scrape_url != ""))
            | (University.website.isnot(None) & (University.website != ""))
        )
    )
    unis = list(unis_result.scalars())
    enabled_count = 0
    for uni in unis:
        watcher = await get_or_create_watcher(uni.id, db)
        if not watcher.enabled:
            watcher.enabled = True
        if not watcher.probe_url:
            watcher.probe_url = (uni.scrape_url or uni.website or "").strip() or None
        enabled_count += 1
    await db.commit()
    return {"ok": True, "enabled_count": enabled_count}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/monitoring/{uni_id}")
async def delete_watcher(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(UniversityWatcher).where(UniversityWatcher.university_id == uni_id)
    )
    watcher = result.scalar_one_or_none()
    if not watcher:
        raise HTTPException(status_code=404, detail="Watcher not found")
    await db.delete(watcher)
    await db.commit()
    return {"ok": True, "deleted": True, "university_id": uni_id}

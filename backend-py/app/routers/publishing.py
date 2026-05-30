"""Phase 14 — Autonomous Publishing & Review Engine — API router."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.publishing_engine import (
    get_ledger,
    get_publishing_stats,
    get_review_queue,
    manually_approve,
    manually_hold,
    manually_reject,
    run_publishing_pass,
    score_course,
)
from app.models.scraped_course import ScrapedCourse
from sqlalchemy import select

router = APIRouter()


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/publishing/stats")
async def publishing_stats(db: Annotated[AsyncSession, Depends(get_db)]):
    return await get_publishing_stats(db)


# ── Review queue ───────────────────────────────────────────────────────────────

@router.get("/publishing/review-queue")
async def review_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    university_id: int | None = Query(None),
    decision: str | None = Query(None),
    limit: int = Query(50, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    return await get_review_queue(db, university_id, decision, limit, offset)


# ── Run publishing pass ────────────────────────────────────────────────────────

class RunPassBody(BaseModel):
    university_id: int | None = None


@router.post("/publishing/run")
async def run_pass(body: RunPassBody, db: Annotated[AsyncSession, Depends(get_db)]):
    counts = await run_publishing_pass(db, university_id=body.university_id)
    return {"ok": True, **counts}


# ── Score a single staged course ───────────────────────────────────────────────

@router.post("/publishing/score/{sc_id}")
async def score_one(sc_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(ScrapedCourse).where(ScrapedCourse.id == sc_id))
    sc = result.scalar_one_or_none()
    if not sc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staged course not found")
    scored = await score_course(sc, db)
    await db.commit()
    return {"ok": True, "scraped_course_id": sc_id, **scored}


# ── Manual review actions ──────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    reason: str = ""


@router.post("/publishing/review/{sc_id}/approve")
async def approve(sc_id: int, body: ReviewAction, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        return await manually_approve(sc_id, body.reason, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/publishing/review/{sc_id}/reject")
async def reject(sc_id: int, body: ReviewAction, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        return await manually_reject(sc_id, body.reason, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/publishing/review/{sc_id}/hold")
async def hold(sc_id: int, body: ReviewAction, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        return await manually_hold(sc_id, body.reason, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Ledger ─────────────────────────────────────────────────────────────────────

@router.get("/publishing/ledger")
async def ledger(
    db: Annotated[AsyncSession, Depends(get_db)],
    university_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    return await get_ledger(db, university_id, limit, offset)

"""
Auto API Discovery router
=========================
POST /api/scrape/discover-api          — start a discovery job
GET  /api/scrape/discover-api/{job_id} — poll status / results
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.permissions import require_permission

log = logging.getLogger(__name__)
router = APIRouter()

_REDIS_TTL_S = 3600  # keep results for 1 hour


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def _redis_set(key: str, value: dict, ttl: int = _REDIS_TTL_S) -> None:
    try:
        import redis.asyncio as aioredis  # type: ignore
        r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
        await r.set(key, json.dumps(value), ex=ttl)
        await r.aclose()
    except Exception as exc:
        log.warning("api_discovery redis_set failed: %s", exc)


async def _redis_get(key: str) -> dict | None:
    try:
        import redis.asyncio as aioredis  # type: ignore
        r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
        raw = await r.get(key)
        await r.aclose()
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("api_discovery redis_get failed: %s", exc)
        return None


def _redis_key(job_id: str) -> str:
    return f"api_disc:{job_id}"


# ── Fetch sample URLs from DB ─────────────────────────────────────────────────

async def _get_sample_urls(uni_id: int, db: AsyncSession) -> list[str]:
    """Return up to 3 recently-scraped course URLs for the university."""
    try:
        row = await db.execute(
            text("SELECT scrape_url FROM universities WHERE id = :uid LIMIT 1"),
            {"uid": uni_id},
        )
        uni_row = row.fetchone()
        scrape_url: str = uni_row[0] if uni_row and uni_row[0] else ""

        # Try scraped_courses for real per-course page URLs first
        sc_rows = await db.execute(
            text("""
                SELECT DISTINCT source_url
                FROM scraped_courses
                WHERE university_id = :uid
                  AND source_url IS NOT NULL
                  AND source_url != ''
                ORDER BY created_at DESC
                LIMIT 6
            """),
            {"uid": uni_id},
        )
        urls = [r[0] for r in sc_rows.fetchall() if r[0]]
        if urls:
            return urls[:3]

        # Fall back to the university scrape_url itself
        if scrape_url:
            return [scrape_url]
    except Exception as exc:
        log.warning("api_discovery: could not fetch sample URLs for uni %s: %s", uni_id, exc)
    return []


async def _get_uni_hostname(uni_id: int, db: AsyncSession) -> str:
    try:
        row = await db.execute(
            text("SELECT scrape_url FROM universities WHERE id = :uid LIMIT 1"),
            {"uid": uni_id},
        )
        r = row.fetchone()
        if r and r[0]:
            return urlparse(r[0]).hostname or ""
    except Exception:
        pass
    return ""


async def _get_course_hints(uni_id: int, db: AsyncSession) -> list[str]:
    try:
        rows = await db.execute(
            text("""
                SELECT name FROM courses
                WHERE university_id = :uid
                  AND name IS NOT NULL AND name != ''
                ORDER BY id DESC LIMIT 5
            """),
            {"uid": uni_id},
        )
        return [r[0] for r in rows.fetchall()]
    except Exception:
        return []


# ── Background task ───────────────────────────────────────────────────────────

async def _run_discovery(
    job_id: str,
    uni_id: int,
    sample_urls: list[str],
    uni_hostname: str,
    course_hints: list[str],
) -> None:
    key = _redis_key(job_id)
    try:
        from app.services.scraper.api_discovery import discover_api_endpoints
        result = await discover_api_endpoints(
            sample_urls=sample_urls,
            uni_hostname=uni_hostname,
            course_hints=course_hints,
        )
        await _redis_set(key, {
            "status": "done",
            "uni_id": uni_id,
            "candidates": result["candidates"],
            "sample_urls": result["sample_urls"],
            "uni_hostname": result["uni_hostname"],
        })
        log.info("api_discovery job %s done — %d candidates", job_id, len(result["candidates"]))
    except Exception as exc:
        log.exception("api_discovery job %s failed", job_id)
        await _redis_set(key, {
            "status": "error",
            "uni_id": uni_id,
            "candidates": [],
            "sample_urls": sample_urls,
            "uni_hostname": uni_hostname,
            "error": str(exc),
        })


# ── Request / response schemas ────────────────────────────────────────────────

class DiscoverApiRequest(BaseModel):
    uni_id: int
    sample_urls: list[str] = []


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/discover-api")
async def start_discovery(
    body: DiscoverApiRequest,
    background_tasks: BackgroundTasks,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """
    Start an API discovery job.  Returns ``{"job_id": "..."}`` immediately;
    poll ``GET /api/scrape/discover-api/{job_id}`` for results.
    """
    job_id = str(uuid.uuid4())
    key = _redis_key(job_id)

    # Resolve sample URLs and university context
    sample_urls = body.sample_urls[:3] if body.sample_urls else await _get_sample_urls(body.uni_id, db)
    if not sample_urls:
        raise HTTPException(
            status_code=422,
            detail="No sample course URLs found for this university. Please provide sample_urls.",
        )

    uni_hostname = await _get_uni_hostname(body.uni_id, db)
    course_hints = await _get_course_hints(body.uni_id, db)

    # Store initial "running" state so polling works immediately
    await _redis_set(key, {
        "status": "running",
        "uni_id": body.uni_id,
        "candidates": [],
        "sample_urls": sample_urls,
        "uni_hostname": uni_hostname,
    })

    log.info(
        "api_discovery: starting job %s for uni_id=%s, %d sample URLs",
        job_id, body.uni_id, len(sample_urls),
    )

    background_tasks.add_task(
        _run_discovery, job_id, body.uni_id, sample_urls, uni_hostname, course_hints
    )

    return {"ok": True, "job_id": job_id, "sample_urls": sample_urls}


@router.get("/discover-api/{job_id}")
async def poll_discovery(
    job_id: str,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
) -> dict[str, Any]:
    """Poll the status and results of a discovery job."""
    data = await _redis_get(_redis_key(job_id))
    if data is None:
        raise HTTPException(status_code=404, detail="Discovery job not found or expired.")
    return data

"""Celery tasks. Each task opens its own async session because Celery workers
run sync; we use ``asyncio.run`` to bridge.

IMPORTANT: Each asyncio.run() creates a fresh event loop. Any asyncpg
connection held in the SQLAlchemy pool from a previous task is bound to
a now-closed loop. We dispose the engine at task start so the pool is
empty and new connections bind to the current loop.
"""
from __future__ import annotations

import asyncio
import logging

from celery.exceptions import SoftTimeLimitExceeded

from app.database import AsyncSessionLocal, engine
from app.services.scraper.orchestrator import run_scrape
from app.services.scraper.repair import run_repair
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


async def _async_scrape(runtime_job_id: str) -> None:
    # Dispose any stale connections bound to previous event loops.
    await engine.dispose()
    async with AsyncSessionLocal() as db:
        await run_scrape(db, runtime_job_id)


async def _async_repair(runtime_job_id: str) -> None:
    # Same engine.dispose() dance as the scrape task — Celery worker is
    # sync, asyncio.run() spins a fresh loop per task and any pooled
    # asyncpg connection from a previous task is bound to a now-closed
    # loop ("Future attached to a different loop").
    await engine.dispose()
    async with AsyncSessionLocal() as db:
        await run_repair(db, runtime_job_id)


@celery_app.task(name="scrape.university", bind=True, max_retries=0)
def scrape_university(self, runtime_job_id: str) -> dict:  # noqa: ANN001
    log.info("Celery task scrape_university start id=%s", runtime_job_id)
    try:
        asyncio.run(_async_scrape(runtime_job_id))
        return {"ok": True, "id": runtime_job_id}
    except SoftTimeLimitExceeded:
        # 2-hour ceiling hit. Mark the job failed so the UI shows a real
        # error instead of spinning forever, then let Celery clean up.
        log.error(
            "scrape_university soft time limit exceeded for job %s — marking failed",
            runtime_job_id,
        )
        try:
            asyncio.run(_mark_failed(runtime_job_id, "Scrape exceeded 2-hour time limit"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": "soft_time_limit_exceeded"}
    except Exception as exc:
        log.exception("Task failed id=%s: %s", runtime_job_id, exc)
        # Mark job failed in DB so UI sees real status. No retry — the loop
        # issue won't fix itself on retry.
        try:
            asyncio.run(_mark_failed(runtime_job_id, str(exc)))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": str(exc)}


@celery_app.task(name="scrape.repair", bind=True, max_retries=0)
def repair_university(self, runtime_job_id: str) -> dict:  # noqa: ANN001
    """Re-extract a known list of course URLs and back-fill missing
    ``courses`` / ``english_requirements`` data. Mirrors
    ``scrape_university`` exactly so the worker boot path, asyncpg
    pool dispose, failure-mark fallback and Celery retry semantics
    are identical for both job types."""
    log.info("Celery task repair_university start id=%s", runtime_job_id)
    try:
        asyncio.run(_async_repair(runtime_job_id))
        return {"ok": True, "id": runtime_job_id}
    except Exception as exc:
        log.exception("Repair task failed id=%s: %s", runtime_job_id, exc)
        try:
            asyncio.run(_mark_failed(runtime_job_id, str(exc)))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": str(exc)}


async def _mark_failed(runtime_job_id: str, err: str) -> None:
    await engine.dispose()
    from app.models import ScrapeRuntimeJob
    async with AsyncSessionLocal() as db:
        job = await db.get(ScrapeRuntimeJob, runtime_job_id)
        if job:
            job.status = "failed"
            job.error_message = f"Scraping failed: {err[:200]}"
            await db.commit()

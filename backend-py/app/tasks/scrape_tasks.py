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
from datetime import datetime, timedelta, timezone

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.services.scraper.orchestrator import run_scrape
from app.services.scraper.repair import run_repair
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# How old (in minutes) a queued job must be before the reaper re-dispatches it.
_STALE_QUEUED_MINUTES = 5

# Redis lock TTL (seconds) set per-job after dispatch to prevent duplicate
# Celery messages while a task is already queued in the broker backlog.
# Must be >= _STALE_QUEUED_MINUTES * 60 so a single dispatch cannot re-fire
# before the lock expires.
_REQUEUE_LOCK_TTL_S = _STALE_QUEUED_MINUTES * 60


def _requeue_lock_key(runtime_job_id: str) -> str:
    return f"scrape:requeue_lock:{runtime_job_id}"


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


async def _async_find_stale() -> list[tuple[str, str]]:
    """Return (runtime_job_id, job_type) for every job that is stuck in
    ``queued`` status with no DB activity for longer than
    ``_STALE_QUEUED_MINUTES``.

    The ``updated_at`` timestamp is bumped to *now* inside the DB transaction
    for each candidate so that the next beat iteration skips the row while
    the freshly enqueued Celery task has time to claim it.  This is the
    first line of defence against rapid re-dispatch.  A Redis lock (set
    by the caller after dispatch) is the second line of defence against
    duplicate messages while the task sits in a broker backlog.
    """
    from app.models import ScrapeRuntimeJob

    await engine.dispose()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=_STALE_QUEUED_MINUTES)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScrapeRuntimeJob).where(
                ScrapeRuntimeJob.status == "queued",
                ScrapeRuntimeJob.updated_at < cutoff,
            )
        )
        stale_jobs = result.scalars().all()

        if not stale_jobs:
            return []

        now = datetime.now(tz=timezone.utc)
        for job in stale_jobs:
            job.updated_at = now

        await db.commit()

    return [(j.runtime_job_id, j.job_type) for j in stale_jobs]


@celery_app.task(name="scrape.requeue_stale", bind=True, max_retries=0)
def requeue_stale_queued(self) -> dict:  # noqa: ANN001
    """Celery beat task: re-dispatch any scrape/repair jobs that have been
    stuck in ``queued`` status for longer than ``_STALE_QUEUED_MINUTES``
    minutes with no worker activity.

    This closes the gap where the stale-running-job reaper resets a job back
    to ``queued`` but no Celery task is enqueued to actually run it, leaving
    the job permanently stuck unless the user manually re-triggers the scrape.

    Double-dispatch prevention uses two layers:
    1. DB layer: ``updated_at`` is bumped before dispatch so the next beat
       tick skips the row while the task sits in the worker's queue.
    2. Redis lock: a per-job key with TTL = ``_REQUEUE_LOCK_TTL_S`` is set
       via NX (set-if-not-exists) immediately before ``.delay()``. If the
       key already exists the job is skipped — it was already dispatched and
       is still in the broker backlog or being processed.  The lock expires
       automatically, allowing re-dispatch if the worker never picks it up.
    """
    import redis as redis_lib

    log.info("requeue_stale_queued: checking for stuck queued jobs")
    try:
        stale = asyncio.run(_async_find_stale())
    except Exception as exc:
        log.exception("requeue_stale_queued DB query failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not stale:
        return {"ok": True, "requeued": []}

    try:
        r = redis_lib.from_url(celery_app.conf.broker_url, decode_responses=True)
    except Exception as exc:
        log.exception("requeue_stale_queued Redis connect failed: %s", exc)
        return {"ok": False, "error": f"redis connect: {exc}"}

    dispatched: list[str] = []
    for jid, jtype in stale:
        lock_key = _requeue_lock_key(jid)
        acquired = r.set(lock_key, "1", nx=True, ex=_REQUEUE_LOCK_TTL_S)
        if not acquired:
            log.info(
                "requeue_stale_queued: job %s already locked (dispatch in-flight), skipping",
                jid,
            )
            continue
        try:
            if jtype == "repair":
                repair_university.delay(jid)
            else:
                scrape_university.delay(jid)
        except Exception as exc:
            # Release the lock so the next beat tick can try again.
            r.delete(lock_key)
            log.error("requeue_stale_queued: dispatch failed for %s: %s", jid, exc)
            continue
        log.warning(
            "requeue_stale_queued: re-dispatched stale %s job %s "
            "(queued for >%d min with no worker activity)",
            jtype,
            jid,
            _STALE_QUEUED_MINUTES,
        )
        dispatched.append(jid)

    return {"ok": True, "requeued": dispatched}


async def _mark_failed(runtime_job_id: str, err: str) -> None:
    await engine.dispose()
    from app.models import ScrapeRuntimeJob
    async with AsyncSessionLocal() as db:
        job = await db.get(ScrapeRuntimeJob, runtime_job_id)
        if job:
            job.status = "failed"
            job.error_message = f"Scraping failed: {err[:200]}"
            await db.commit()

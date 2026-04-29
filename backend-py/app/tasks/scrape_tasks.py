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

from app.config import STALE_QUEUED_MINUTES
from app.database import AsyncSessionLocal, engine
from app.services.scraper.orchestrator import run_scrape
from app.services.scraper.repair import run_repair
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

# Alias for internal use within this module.
_STALE_QUEUED_MINUTES = STALE_QUEUED_MINUTES

# Redis lock TTL (seconds) set per-job after dispatch to prevent duplicate
# Celery messages while a task is already queued in the broker backlog.
# Must be >= _STALE_QUEUED_MINUTES * 60 so a single dispatch cannot re-fire
# before the lock expires.
_REQUEUE_LOCK_TTL_S = _STALE_QUEUED_MINUTES * 60

# Maximum number of automatic re-dispatches before a job is declared failed.
# Prevents infinite requeue loops when a worker crashes before claiming the job.
_MAX_REQUEUES = 5


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
    except BaseException as exc:
        # asyncio.CancelledError is BaseException (not Exception) in Python
        # 3.8+.  Without this block it escapes silently and the Celery slot
        # appears stuck until the 2-hour soft-time-limit fires.  Reraise
        # SystemExit / KeyboardInterrupt so Celery can still shut down cleanly.
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        log.error(
            "scrape_university BaseException id=%s: %s",
            runtime_job_id, exc,
        )
        try:
            asyncio.run(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": f"BaseException: {exc}"}


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
    except BaseException as exc:
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        log.error("repair_university BaseException id=%s: %s", runtime_job_id, exc)
        try:
            asyncio.run(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": f"BaseException: {exc}"}


async def _async_find_stale() -> list[tuple[str, str, int]]:
    """Return (runtime_job_id, job_type, requeue_count) for every job that is
    stuck in ``queued`` status with no DB activity for longer than
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

    return [(j.runtime_job_id, j.job_type, j.requeue_count) for j in stale_jobs]


async def _async_increment_requeue(runtime_job_id: str) -> None:
    """Atomically increment ``requeue_count`` and append a timestamped
    requeue event to ``requeue_events`` for a job after it has been
    successfully re-dispatched.

    A single ``UPDATE`` statement handles both fields so there is no
    read-modify-write race even if two beat ticks overlap on the same job.

    Disposes the engine first — each asyncio.run() in the Celery task body
    creates a fresh event loop and any pooled asyncpg connection from a prior
    run would be bound to the old, closed loop.
    """
    from sqlalchemy import text

    await engine.dispose()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE scrape_runtime_jobs "
                "SET requeue_count = requeue_count + 1, "
                "    requeue_events = COALESCE(requeue_events, '[]'::jsonb) || "
                "        jsonb_build_array(jsonb_build_object( "
                "            'number', requeue_count + 1, "
                "            'stale_minutes', :stale_min, "
                "            'timestamp', to_char("
                "                NOW() AT TIME ZONE 'UTC', "
                "                'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'"
                "            ) "
                "        )) "
                "WHERE runtime_job_id = :jid"
            ),
            {"jid": runtime_job_id, "stale_min": _STALE_QUEUED_MINUTES},
        )
        await db.commit()


async def _async_mark_failed_max_requeue(runtime_job_id: str) -> None:
    """Mark a job ``failed`` because it has exceeded the maximum number of
    automatic requeue attempts, indicating a pathological loop.

    Disposes the engine first for the same reason as ``_async_increment_requeue``.
    """
    from app.models import ScrapeRuntimeJob

    await engine.dispose()
    async with AsyncSessionLocal() as db:
        job = await db.get(ScrapeRuntimeJob, runtime_job_id)
        if job:
            job.status = "failed"
            job.error_message = (
                f"Auto-recovery abandoned after {job.requeue_count} requeue attempts "
                f"(limit: {_MAX_REQUEUES}). Worker may be crashing before claiming the job."
            )
            from datetime import datetime, timezone as _tz
            exhausted_ts = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            current_events = list(job.requeue_events or [])
            current_events.append(
                {
                    "number": job.requeue_count,
                    "timestamp": exhausted_ts,
                    "exhausted": True,
                }
            )
            job.requeue_events = current_events
            await db.commit()


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
    exhausted: list[str] = []
    for jid, jtype, requeue_count in stale:
        # ── Max-requeue guard ─────────────────────────────────────────────
        if requeue_count >= _MAX_REQUEUES:
            log.error(
                "requeue_stale_queued: job %s has been requeued %d times (limit %d) "
                "without a worker claiming it — marking failed",
                jid,
                requeue_count,
                _MAX_REQUEUES,
            )
            try:
                asyncio.run(_async_mark_failed_max_requeue(jid))
            except Exception as exc:
                log.exception(
                    "requeue_stale_queued: could not mark job %s failed: %s", jid, exc
                )
            exhausted.append(jid)
            continue

        # ── Normal re-dispatch path ───────────────────────────────────────
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

        # Increment the persistent counter so operators can track bouncing jobs.
        try:
            asyncio.run(_async_increment_requeue(jid))
        except Exception as exc:
            log.warning(
                "requeue_stale_queued: could not increment requeue_count for %s: %s",
                jid,
                exc,
            )

        log.warning(
            "requeue_stale_queued: re-dispatched stale %s job %s "
            "(queued for >%d min with no worker activity, requeue #%d)",
            jtype,
            jid,
            _STALE_QUEUED_MINUTES,
            requeue_count + 1,
        )
        dispatched.append(jid)

    return {"ok": True, "requeued": dispatched, "exhausted": exhausted}


async def _mark_failed(runtime_job_id: str, err: str) -> None:
    await engine.dispose()
    from app.models import ScrapeRuntimeJob
    async with AsyncSessionLocal() as db:
        job = await db.get(ScrapeRuntimeJob, runtime_job_id)
        if job:
            job.status = "failed"
            job.error_message = f"Scraping failed: {err[:200]}"
            await db.commit()


@celery_app.task(name="scrape.refresh_baselines", bind=True, max_retries=0)
def refresh_baselines_weekly(self) -> dict:  # type: ignore[override]
    """Celery beat task — recompute fill-rate baselines from the trailing 30 days.

    Runs weekly (Sunday 04:00 UTC via beat_schedule in celery_app.py).
    Idempotent: uses INSERT ... ON CONFLICT DO UPDATE so re-running is safe.
    """
    async def _run() -> dict:
        await engine.dispose()
        async with AsyncSessionLocal() as db:
            from app.scripts.seed_baselines import seed_baselines
            count = await seed_baselines(db)
            return {"ok": True, "baselines_upserted": count}

    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception("refresh_baselines_weekly failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

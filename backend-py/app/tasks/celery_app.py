"""Celery entry point. Run worker with:

    celery -A app.tasks.celery_app worker --concurrency=4 --loglevel=info

Run beat (daily snapshot scheduler + stale-job reaper) with:

    celery -A app.tasks.celery_app beat --loglevel=info

If Redis isn't reachable, the FastAPI process still boots — only the
``.delay()`` call from the API will quietly fail (and the job stays in
``queued`` state for manual retry).
"""
from __future__ import annotations

import asyncio
import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from app.config import settings

log = logging.getLogger(__name__)

celery_app = Celery(
    "uniportal",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Both the per-job scrape tasks and the daily snapshot live under
    # tasks/ — keep them in one ``include`` list so a single worker
    # process can serve both queues.
    include=[
        "app.tasks.scrape_tasks",
        "app.tasks.snapshot_tasks",
    ],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="scrape",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Hard ceiling so a single hung scrape can never block the worker
    # indefinitely (prod incident: ASA job sat for 660+ minutes).
    # soft_time_limit raises SoftTimeLimitExceeded inside the task so the
    # orchestrator can mark the job failed cleanly; time_limit sends SIGKILL
    # after an extra 10 minutes if the soft signal is not handled.
    task_soft_time_limit=7200,   # 2 hours → raises SoftTimeLimitExceeded
    task_time_limit=7800,        # 2 h 10 m → SIGKILL fallback
    # Diff item L (MIGRATION_AUDIT.md §6): daily snapshot at 03:00 UTC.
    # The Node ``daily-backup.ts`` ran hourly and short-circuited when
    # today's row already existed (catch-up safety net for missed
    # windows). Beat gives us a precise once-per-day fire instead. We
    # accept the trade-off: if the worker is down at 03:00, the daily
    # row is skipped that day — operationally simpler than re-deriving
    # the catch-up logic, and the snapshot tables only need to reflect
    # *some* daily-ish history, not strict every-day coverage. A
    # missed-day catch-up can be added later by reusing the existing
    # ``triggered_by="manual"`` code path.
    #
    # Note: every call to ``snapshot_editable_tables`` inserts a fresh
    # snapshot row regardless of whether one already exists for today
    # — manual + scheduled runs on the same date will produce two
    # rows. That's fine (the snapshot history is keyed on
    # ``backed_up_at``, not on the day), but it's not idempotent at
    # the day grain.
    beat_schedule={
        "snapshot-editable-tables-daily": {
            "task": "tasks.snapshot.editable",
            "schedule": crontab(hour=3, minute=0),
            "args": (),
            "options": {"queue": "scrape"},
        },
        # Re-dispatch any scrape/repair jobs that are stuck in ``queued``
        # status with no Celery task in-flight (e.g. after a worker restart
        # that left running→queued rows but never enqueued a new task).
        # Fires every minute; the task only re-dispatches jobs whose
        # ``updated_at`` is older than 5 minutes, so rapid re-fires within
        # the cooldown window are prevented by the updated_at bump the task
        # performs before calling ``.delay()``.
        "requeue-stale-queued-jobs": {
            "task": "scrape.requeue_stale",
            "schedule": 60.0,
            "args": (),
            "options": {"queue": "scrape"},
        },
    },
)


# ---------------------------------------------------------------------------
# Worker startup: free any ghost slots left by a previous SIGKILL
# ---------------------------------------------------------------------------
# When the worker process is killed with SIGKILL (e.g. during a deployment
# restart), Python's exception handlers never run, so scraping_jobs rows
# remain in status='running' forever.  The heartbeat reaper in /active takes
# up to 5 minutes to notice.  This hook fires the moment the new worker is
# fully ready and immediately resets those ghost jobs to 'failed', freeing
# all 4 Celery slots right away — no manual "Cancel All" needed.

async def _reset_ghost_running_jobs() -> int:
    """Mark all scraping_jobs rows stuck in status='running' as failed.

    Returns the number of rows reset.
    """
    from datetime import datetime, timezone as _tz

    from sqlalchemy import text

    from app.database import AsyncSessionLocal, engine

    await engine.dispose()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "UPDATE scraping_jobs "
                "SET status = 'failed', "
                "    completed_at = :now, "
                "    error_message = 'Worker restarted — slot freed on startup' "
                "WHERE status = 'running'"
            ),
            {"now": datetime.now(_tz.utc)},
        )
        await db.commit()
        return result.rowcount  # type: ignore[return-value]


@worker_ready.connect
def on_worker_ready(**kwargs) -> None:  # noqa: ANN003
    """Reset ghost 'running' scraping_jobs when the Celery worker comes online.

    Runs once per worker process start — harmless if there are no stuck rows.
    """
    try:
        reset = asyncio.run(_reset_ghost_running_jobs())
        if reset:
            log.warning(
                "worker_ready: reset %d ghost running job(s) → failed "
                "(left over from previous worker process)",
                reset,
            )
        else:
            log.info("worker_ready: no ghost running jobs found — all slots clean")
    except Exception as exc:
        log.error("worker_ready: ghost-job reset failed: %s", exc)

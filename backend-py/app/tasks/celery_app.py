"""Celery entry point. Run worker with:

    celery -A app.tasks.celery_app worker --concurrency=4 --loglevel=info -Q scrape,beat

Run beat (daily snapshot scheduler + stale-job reaper) with:

    celery -A app.tasks.celery_app beat --loglevel=info

Queue strategy
--------------
``scrape``  — user-triggered scrape/repair jobs (scrape.university,
              scrape.repair, scrape.probe_configure, etc.).
              High-priority; never blocked by maintenance work.

``beat``    — periodic maintenance tasks (requeue_stale, monitoring,
              nightly_sweep, refresh_baselines, daily snapshot).
              Lower-priority; Beat injects here so maintenance work
              cannot bury user-triggered scrapes in the same queue.

The worker must subscribe to BOTH queues: ``-Q scrape,beat``.
With Redis as broker and ``worker_prefetch_multiplier=1``, the worker
checks ``scrape`` first on each poll cycle, so user-triggered tasks
get a free worker before any pending beat work.

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
        "app.tasks.monitoring_tasks",
        "app.tasks.health_snapshot",
        "app.tasks.auto_repair_task",
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
    # indefinitely (prod incident: ASA job sat for 660+ minutes; Replit
    # incident: UEL browser discovery hung for 45+ min blocking all 4 workers).
    # 45 min soft limit covers all known real scrapes (longest: Bond ~30 min).
    # soft_time_limit raises SoftTimeLimitExceeded inside the task so the
    # orchestrator can mark the job failed cleanly; time_limit sends SIGKILL
    # after an extra 10 minutes if the soft signal is not handled.
    task_soft_time_limit=2700,   # 45 min → raises SoftTimeLimitExceeded
    task_time_limit=3000,        # 50 min → SIGKILL fallback
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
        # ── Maintenance tasks → ``beat`` queue ───────────────────────────
        # All periodic/maintenance tasks go to the ``beat`` queue so they
        # can never bury user-triggered scrape.university tasks that land
        # in the higher-priority ``scrape`` queue.  The worker subscribes
        # to both: ``-Q scrape,beat``.
        "snapshot-editable-tables-daily": {
            "task": "tasks.snapshot.editable",
            "schedule": crontab(hour=3, minute=0),
            "args": (),
            "options": {"queue": "beat"},
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
            "options": {"queue": "beat"},
        },
        # Recompute fill-rate baselines from the trailing 30 days of clean runs.
        # Runs once a week (Sunday 04:00 UTC) — baselines drift slowly so a
        # weekly refresh keeps them fresh without incurring unnecessary DB load.
        "refresh-baselines-weekly": {
            "task": "scrape.refresh_baselines",
            "schedule": crontab(hour=4, minute=0, day_of_week=0),
            "args": (),
            "options": {"queue": "beat"},
        },
        # Nightly regression sweep: capture a fresh baseline snapshot for all
        # universities, compare against the previous night's snapshot, and
        # push a drift alert (Slack/email) if unexpected field changes are
        # detected.  Runs at 02:00 UTC (after AEST business hours, before the
        # 03:00 snapshot and 04:00 baseline-refresh tasks).
        "nightly-sweep-and-drift-alert": {
            "task": "scrape.nightly_sweep",
            "schedule": crontab(hour=2, minute=0),
            "args": (),
            "options": {"queue": "beat"},
        },
        # Phase 13 — Autonomous Monitoring Engine.
        # Probes all enabled watchers whose next_check_at <= now().
        # Runs every 30 minutes; smart scheduling inside the task means
        # most universities are skipped (next_check_at in the future).
        "monitoring-check-watchers": {
            "task": "monitoring.check_watchers",
            "schedule": 1800.0,  # every 30 minutes
            "args": (),
            "options": {"queue": "beat"},
        },
        # Daily health snapshot: upserts v_university_health into
        # university_health_snapshots at 01:30 UTC — after overnight
        # scrapes finish, before the 02:00 regression sweep reads them.
        "snapshot-university-health-daily": {
            "task": "health.snapshot_daily",
            "schedule": crontab(hour=1, minute=30),
            "args": (),
            "options": {"queue": "beat"},
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

_RESET_SQL = (
    "UPDATE scrape_runtime_jobs "
    "SET status = 'failed', "
    "    completed_at = now(), "
    "    error_message = 'Worker restarted — slot freed on startup' "
    "WHERE status = 'running'"
)


async def _reset_via_asyncpg(url: str) -> int:
    """Run the ghost-job reset using a given asyncpg URL."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    _engine = create_async_engine(url, pool_size=1, max_overflow=0, future=True)
    try:
        _Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        async with _Session() as db:
            result = await db.execute(text(_RESET_SQL))
            await db.commit()
            return result.rowcount  # type: ignore[return-value]
    finally:
        await _engine.dispose()


async def _reset_ghost_running_jobs() -> int:
    """Mark all scrape_runtime_jobs rows stuck in status='running' as failed.

    Tries the configured DATABASE_URL first; if DNS resolution fails (common
    when the .env has a cloud DB URL that is unreachable from the server),
    falls back to the local 127.0.0.1 credentials baked into config.py.

    Returns the number of rows reset.
    """
    from app.config import settings

    primary_url = settings.database_url

    # Attempt 1: use the configured URL
    try:
        return await _reset_via_asyncpg(primary_url)
    except OSError as dns_exc:
        # DNS / network unreachable — fall through to local fallback
        log.warning("worker_ready: primary DB unreachable (%s) — trying 127.0.0.1 fallback", dns_exc)
    except Exception as exc:
        log.warning("worker_ready: primary DB attempt failed (%s) — trying 127.0.0.1 fallback", exc)

    # Attempt 2: local PostgreSQL via 127.0.0.1 (works on the DigitalOcean host
    # when the .env DATABASE_URL is a cloud endpoint that doesn't resolve locally).
    # Credentials match the server_default in config.py.
    fallback_url = (
        "postgresql+asyncpg://uniportal:Bij%40y12345@127.0.0.1:5432/university_portal"
    )
    return await _reset_via_asyncpg(fallback_url)


async def _check_job_status_single(job_id: str) -> str | None:
    """Return the DB status of a single scrape_runtime_jobs row, or None."""
    from sqlalchemy import text as _text2
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    _url = settings.database_url
    _engine = create_async_engine(_url, pool_size=1, max_overflow=0, future=True)
    try:
        _Sess = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        async with _Sess() as _db:
            row = await _db.execute(
                _text2("SELECT status FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
                {"jid": job_id},
            )
            return row.scalar()
    finally:
        await _engine.dispose()


@worker_ready.connect
def on_worker_ready(**kwargs) -> None:  # noqa: ANN003
    """Reset ghost 'running' scrape_runtime_jobs when the Celery worker comes online,
    then immediately re-dispatch any jobs stuck in 'queued' state with no Celery task
    in the broker (e.g. after Redis was restarted and cleared the task queue).

    Runs once per worker process start — harmless if there are no stuck rows.
    The Redis NX lock in _immediate_requeue_hook makes this race-safe across all
    4 worker processes that each fire this signal on startup.
    """
    # ── Shadow-mode startup log ───────────────────────────────────────────────
    # Emitted at boot so operators can confirm SHADOW_MODE_UNI_IDS /
    # SHADOW_CUTOVER_UNI_IDS are being read by the worker process.
    # If this line is absent from the worker log, the worker is not loading
    # the env var (e.g. .env not sourced at startup) — fix before triggering
    # a shadow scrape or the shadow report will never be written.
    try:
        import os
        _shadow_ids = os.environ.get("SHADOW_MODE_UNI_IDS", "").strip()
        _cutover_ids = os.environ.get("SHADOW_CUTOVER_UNI_IDS", "").strip()
        if _shadow_ids:
            log.info("[SHADOW] shadow mode ENABLED for uni_ids: %s", _shadow_ids)
        else:
            log.info("[SHADOW] shadow mode OFF (SHADOW_MODE_UNI_IDS not set)")
        if _cutover_ids:
            log.info("[SHADOW] cutover ACTIVE for uni_ids: %s", _cutover_ids)
    except Exception as _shadow_exc:
        log.warning("[SHADOW] startup config log failed: %s", _shadow_exc)
    # ── Ghost-job reset ───────────────────────────────────────────────────────
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
    # ── Stale uni_lock cleanup ────────────────────────────────────────────────
    # When a Celery worker is SIGKILL'd (e.g. OOM or deployment restart), the
    # orchestrator finally-block that releases scrape:uni_lock:<uni_id> never
    # runs, leaving a lock with a 4-hour TTL in Redis.  Future scrapes of that
    # university see the lock, check the DB, find the holder job is no longer
    # "running"/"queued", and steal it — BUT only after a Celery task actually
    # executes.  When the queue is saturated (workers stuck on long scrapes),
    # new tasks pile up and steal-logic never fires, so the university appears
    # permanently locked until the 4-hour TTL expires.
    #
    # Fix: on every worker restart, delete all uni_lock keys whose holder job
    # is no longer "running" or "queued" in the DB.  This is idempotent and
    # race-safe: the orchestrator re-acquires the lock at the start of each
    # run_scrape call so a miss here only matters for the window between the
    # delete and the next claim.
    try:
        import redis as _redis_lib
        _r = _redis_lib.from_url(settings.redis_url, decode_responses=True)
        _stale_keys: list[str] = []
        for _key in _r.keys("scrape:uni_lock:*"):
            _holder_jid = _r.get(_key)
            if not _holder_jid:
                _stale_keys.append(_key)
                continue
            # Use a short-lived DB check via asyncpg
            try:
                from sqlalchemy import text as _text2
                _holder_status = asyncio.run(
                    _check_job_status_single(_holder_jid)
                )
                if _holder_status not in (None, "running", "queued"):
                    _stale_keys.append(_key)
            except Exception:
                pass  # leave the lock if we can't check — safe default
        if _stale_keys:
            _r.delete(*_stale_keys)
            log.warning(
                "worker_ready: deleted %d stale uni_lock key(s): %s",
                len(_stale_keys), _stale_keys,
            )
        else:
            log.info("worker_ready: no stale uni_lock keys found")
    except Exception as exc:
        log.warning("worker_ready: stale uni_lock cleanup failed (non-fatal): %s", exc)

    # ── Orphaned queued-job recovery ──────────────────────────────────────────
    # After a Redis restart the broker queue is cleared, but DB rows remain in
    # status='queued' with no Celery task to claim them.  Re-dispatch them NOW
    # so they start within seconds of the worker coming online rather than
    # waiting up to STALE_QUEUED_MINUTES (5 min) for the next beat tick.
    # _immediate_requeue_hook uses a Redis NX lock per job so only one of the
    # 4 concurrent worker processes will dispatch each job — no duplicates.
    try:
        from app.tasks.scrape_tasks import _immediate_requeue_hook
        _immediate_requeue_hook()
        log.info("worker_ready: orphaned queued-job recovery sweep complete")
    except Exception as exc:
        log.error("worker_ready: orphaned queued-job recovery failed: %s", exc)

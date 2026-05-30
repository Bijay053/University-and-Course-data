"""Celery tasks. Each task opens its own async session because Celery workers
run sync; we use ``asyncio.run`` to bridge.

IMPORTANT: Each ``asyncio.run()`` creates a fresh event loop.  Any asyncpg
connection held in the SQLAlchemy pool from a *previous* task is bound to a
now-closed loop.  We call ``_sync_dispose()`` **before** every
``asyncio.run()`` (not inside the coroutine) so the pool is invalidated
synchronously — no asyncio involvement, no "Future attached to a different
loop" error.  See ``_sync_dispose`` for the full explanation.
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


def _sync_dispose() -> None:
    """Synchronously invalidate the SQLAlchemy connection pool before
    starting a fresh ``asyncio.run()`` event loop inside a Celery task.

    Each ``asyncio.run()`` creates a new event loop.  Any asyncpg connection
    that the pool holds from a *previous* ``asyncio.run()`` is bound to the
    old (now-closed) loop.  If we try to dispose those connections *inside*
    the new coroutine via ``await engine.dispose()``, asyncpg tries to call
    ``loop.call_soon()`` on the old loop and raises:

        RuntimeError: Task ... got Future attached to a different loop
        RuntimeError: Event loop is closed

    The fix: call ``engine.sync_engine.dispose(close=False)`` *synchronously*
    before entering ``asyncio.run()``.  ``close=False`` marks all pooled
    connections invalid (so new ones are created in the new loop) without
    trying to close/await the old asyncpg connections — no asyncio required.
    """
    try:
        engine.sync_engine.dispose(close=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("_sync_dispose: could not invalidate engine pool: %s", exc)

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


def _get_redis():
    """Return a synchronous Redis client using the Celery broker URL."""
    import redis as redis_lib
    return redis_lib.from_url(celery_app.conf.broker_url, decode_responses=True)


def set_initial_dispatch_lock(job_id: str) -> None:
    """Mark a job as 'has a Celery task in the broker' using a Redis NX lock.

    Called by the API router (start_scrape, start_bulk) after a successful
    ``.delay()`` call so that the post-completion ``_immediate_requeue_hook``
    does not try to re-dispatch the job while it is still waiting to be picked
    up by a worker.

    The TTL is slightly longer than _REQUEUE_LOCK_TTL_S to give the worker
    time to claim the job before the lock expires.
    """
    try:
        r = _get_redis()
        ttl = _REQUEUE_LOCK_TTL_S + 30
        r.set(_requeue_lock_key(job_id), "1", nx=True, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        log.debug("set_initial_dispatch_lock: Redis unavailable for %s: %s", job_id, exc)


async def _async_scrape(runtime_job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await run_scrape(db, runtime_job_id)


async def _async_repair(runtime_job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await run_repair(db, runtime_job_id)


def _immediate_requeue_hook() -> None:
    """Post-completion hook: immediately re-dispatch any queued jobs that have
    no Celery task in the broker.

    Called at the end of every ``scrape_university`` and ``repair_university``
    Celery task so that when a worker slot frees up, orphaned queued jobs start
    immediately instead of waiting up to ``_STALE_QUEUED_MINUTES`` minutes for
    the beat task.

    Uses the same Redis NX lock as the periodic ``requeue_stale_queued`` beat
    task to avoid double-dispatch:
    • Jobs dispatched via the API (start_scrape / start_bulk) have a lock set
      by ``set_initial_dispatch_lock`` and are skipped here — they are already
      in the Celery broker and will be picked up when a slot is free.
    • Jobs whose initial ``.delay()`` call failed silently have no lock and
      are re-dispatched immediately by this hook.
    """
    _sync_dispose()
    try:
        stale = asyncio.run(_async_find_all_queued())
    except Exception as exc:  # noqa: BLE001
        log.warning("immediate_requeue_hook: DB query failed: %s", exc)
        return

    if not stale:
        return

    try:
        r = _get_redis()
    except Exception as exc:  # noqa: BLE001
        log.warning("immediate_requeue_hook: Redis connect failed: %s", exc)
        return

    for jid, jtype, requeue_count in stale:
        if requeue_count >= _MAX_REQUEUES:
            log.warning(
                "immediate_requeue_hook: job %s hit max requeues (%d), skipping",
                jid,
                _MAX_REQUEUES,
            )
            continue

        lock_key = _requeue_lock_key(jid)
        acquired = r.set(lock_key, "1", nx=True, ex=_REQUEUE_LOCK_TTL_S)
        if not acquired:
            # Job already has a Celery task in the broker (set by initial
            # dispatch or a previous requeue) — skip to avoid duplicates.
            log.debug("immediate_requeue_hook: job %s already locked (in broker), skipping", jid)
            continue

        try:
            if jtype == "repair":
                repair_university.delay(jid)
            else:
                scrape_university.delay(jid)
            log.warning(
                "immediate_requeue_hook: re-dispatched orphaned %s job %s "
                "(no broker lock found — initial .delay() likely failed silently)",
                jtype,
                jid,
            )
        except Exception as exc:  # noqa: BLE001
            r.delete(lock_key)
            log.warning(
                "immediate_requeue_hook: dispatch failed for %s: %s", jid, exc
            )


@celery_app.task(name="scrape.university", bind=True, max_retries=0)
def scrape_university(self, runtime_job_id: str) -> dict:  # noqa: ANN001
    log.info("Celery task scrape_university start id=%s", runtime_job_id)
    _sync_dispose()
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
            _sync_dispose()
            asyncio.run(_mark_failed(runtime_job_id, "Scrape exceeded 2-hour time limit"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": "soft_time_limit_exceeded"}
    except Exception as exc:
        log.exception("Task failed id=%s: %s", runtime_job_id, exc)
        # Mark job failed in DB so UI sees real status. No retry — the loop
        # issue won't fix itself on retry.
        try:
            _sync_dispose()
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
            _sync_dispose()
            asyncio.run(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": f"BaseException: {exc}"}
    finally:
        # Always attempt to pick up any queued jobs whose initial .delay()
        # call failed silently — this is the key auto-start mechanism.
        _immediate_requeue_hook()


@celery_app.task(name="scrape.repair", bind=True, max_retries=0)
def repair_university(self, runtime_job_id: str) -> dict:  # noqa: ANN001
    """Re-extract a known list of course URLs and back-fill missing
    ``courses`` / ``english_requirements`` data. Mirrors
    ``scrape_university`` exactly so the worker boot path, asyncpg
    pool dispose, failure-mark fallback and Celery retry semantics
    are identical for both job types."""
    log.info("Celery task repair_university start id=%s", runtime_job_id)
    _sync_dispose()
    try:
        asyncio.run(_async_repair(runtime_job_id))
        return {"ok": True, "id": runtime_job_id}
    except Exception as exc:
        log.exception("Repair task failed id=%s: %s", runtime_job_id, exc)
        try:
            _sync_dispose()
            asyncio.run(_mark_failed(runtime_job_id, str(exc)))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": str(exc)}
    except BaseException as exc:
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        log.error("repair_university BaseException id=%s: %s", runtime_job_id, exc)
        try:
            _sync_dispose()
            asyncio.run(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": f"BaseException: {exc}"}
    finally:
        _immediate_requeue_hook()


async def _async_find_all_queued() -> list[tuple[str, str, int]]:
    """Return (runtime_job_id, job_type, requeue_count) for every job currently
    in ``queued`` status, with no time cutoff.

    Used by the post-completion ``_immediate_requeue_hook`` so orphaned jobs
    (whose initial ``.delay()`` call failed silently) are picked up immediately
    when any worker slot frees up.
    """
    from app.models import ScrapeRuntimeJob

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ScrapeRuntimeJob).where(ScrapeRuntimeJob.status == "queued")
        )
        jobs = result.scalars().all()
    return [(j.runtime_job_id, j.job_type, j.requeue_count) for j in jobs]


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

    The caller must call ``_sync_dispose()`` before ``asyncio.run()`` so the
    pool is fresh when this coroutine creates new asyncpg connections.
    """
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE scrape_runtime_jobs "
                "SET requeue_count = requeue_count + 1, "
                "    requeue_events = COALESCE(requeue_events, '[]'::jsonb) || "
                "        jsonb_build_array(jsonb_build_object( "
                "            'number', requeue_count + 1, "
                "            'stale_minutes', CAST(:stale_min AS INTEGER), "
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

    The caller must call ``_sync_dispose()`` before ``asyncio.run()`` so the
    pool is fresh when this coroutine creates new asyncpg connections.
    """
    from app.models import ScrapeRuntimeJob

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
    log.info("requeue_stale_queued: checking for stuck queued jobs")
    _sync_dispose()
    try:
        stale = asyncio.run(_async_find_stale())
    except Exception as exc:
        log.exception("requeue_stale_queued DB query failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not stale:
        return {"ok": True, "requeued": []}

    try:
        r = _get_redis()
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
                _sync_dispose()
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
            _sync_dispose()
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
    from app.models import ScrapeRuntimeJob
    async with AsyncSessionLocal() as db:
        job = await db.get(ScrapeRuntimeJob, runtime_job_id)
        if job:
            job.status = "failed"
            job.error_message = f"Scraping failed: {err[:200]}"
            await db.commit()


@celery_app.task(name="scrape.nightly_sweep", bind=True, max_retries=0)
def nightly_sweep_and_alert(self) -> dict:  # type: ignore[override]
    """Celery beat task — nightly regression sweep at 02:00 UTC.

    Workflow
    --------
    1. Capture a fresh baseline snapshot for all universities into
       ``baselines/nightly/<YYYYMMDD>/`` using ``capture_baseline.py``.
    2. Find the most recent *previous* snapshot directory.
    3. Run ``regression_sweep.py`` to compare before vs after.
    4. Call ``deliver_drift_alert()`` if unexpected diffs are found
       (sweep exit code 1).

    The task is intentionally fire-and-forget: a sweep failure only
    logs an error, it never marks itself as retriable (max_retries=0).
    """
    import os
    import pathlib
    import re
    import subprocess

    _backend_py = pathlib.Path(__file__).resolve().parent.parent.parent
    # Use sys.executable so the correct interpreter is found in any environment
    # (Replit uses .pythonlibs, production uses venv — neither is at a fixed path).
    _python = sys.executable
    _env = {**os.environ, "PYTHONPATH": "."}

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    today_dir = _backend_py / "baselines" / "nightly" / today
    today_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: capture today's snapshot ──────────────────────────────────
    log.info("[NIGHTLY SWEEP] capturing baseline snapshot → %s", today_dir)
    capture_result = subprocess.run(
        [str(_python), "scripts/capture_baseline.py", "--out-dir", str(today_dir)],
        cwd=str(_backend_py),
        env=_env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if capture_result.returncode != 0:
        log.error(
            "[NIGHTLY SWEEP] capture_baseline.py failed (rc=%d):\n%s",
            capture_result.returncode,
            capture_result.stderr[:2000],
        )
        return {"ok": False, "reason": "capture_baseline failed", "rc": capture_result.returncode}

    log.info("[NIGHTLY SWEEP] baseline captured OK:\n%s", capture_result.stdout[:1000])

    # ── Step 2: find the most recent previous snapshot directory ──────────
    nightly_root = _backend_py / "baselines" / "nightly"
    prev_dirs = sorted(
        [d for d in nightly_root.iterdir() if d.is_dir() and d.name != today],
        reverse=True,
    )
    if not prev_dirs:
        log.info("[NIGHTLY SWEEP] no previous snapshot to compare against — skipping sweep")
        return {"ok": True, "today": today, "sweep": "skipped_no_baseline"}

    before_dir = prev_dirs[0]
    before_date = before_dir.name
    log.info("[NIGHTLY SWEEP] comparing %s → %s", before_date, today)

    # ── Step 3: run regression sweep ──────────────────────────────────────
    sweep_result = subprocess.run(
        [
            str(_python),
            "scripts/regression_sweep.py",
            "--before", str(before_dir),
            "--after", str(today_dir),
        ],
        cwd=str(_backend_py),
        env=_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    sweep_stdout = sweep_result.stdout
    sweep_rc = sweep_result.returncode

    log.info(
        "[NIGHTLY SWEEP] sweep finished rc=%d:\n%s",
        sweep_rc, sweep_stdout[:2000],
    )
    if sweep_result.stderr:
        log.warning("[NIGHTLY SWEEP] sweep stderr:\n%s", sweep_result.stderr[:500])

    if sweep_rc == 0:
        log.info("[NIGHTLY SWEEP] clean — no unexpected diffs")
        return {"ok": True, "before": before_date, "after": today, "sweep": "clean"}

    # ── Step 4: parse diffs and warnings from stdout and deliver alert ────
    diffs: list[dict] = []
    warnings: list[dict] = []
    _current_slug = ""
    # Capture lines like:  "      fen: 32000 → 33000"  or  "    (method-only) ielts: ..."
    _diff_re = re.compile(r"^\s+((?:\(method-only\)\s+)?)([\w_]+):\s+(.+?)\s+→\s+(.+)$")
    _unexpected_re = re.compile(r"\[UNEXPECTED\]\s+(\w+)")

    for line in sweep_stdout.splitlines():
        m_slug = _unexpected_re.search(line)
        if m_slug:
            _current_slug = m_slug.group(1)
        m_diff = _diff_re.match(line)
        if m_diff and _current_slug:
            method_only = bool(m_diff.group(1).strip())
            entry = {
                "slug": _current_slug,
                "field": m_diff.group(2),
                "before": m_diff.group(3),
                "after": m_diff.group(4),
            }
            if method_only:
                warnings.append(entry)
            else:
                diffs.append(entry)

    from app.services.scraper.alert_delivery import deliver_drift_alert
    deliver_drift_alert(
        before_date=before_date,
        after_date=today,
        diffs=diffs,
        warnings=warnings,
        summary=sweep_stdout[:3000],
    )

    return {
        "ok": True,
        "before": before_date,
        "after": today,
        "sweep": "diffs_found",
        "errors": len(diffs),
        "warnings": len(warnings),
    }


@celery_app.task(name="scrape.probe_configure", bind=True, max_retries=0)
def probe_and_configure(  # noqa: ANN001
    self,
    university_id: int,
    triggered_by: str = "manual",
) -> dict:
    """Probe a university website and auto-generate a scraper config.

    1. Fetch the university's scrape_url from the DB.
    2. Run site_probe.probe_site() — detects Cloudflare, JS-SPA, search APIs,
       sitemap, Wayback — and picks the optimal strategy.
    3. Fetch one sample course page to give Gemini richer context.
    4. Run auto_config_generator.generate_config() — uses probe + Gemini to
       produce a UniConfig-compatible dict.
    5. Persist: probe_result, probe_status='configured', and
       scrape_config['auto_config'] = generated dict in the university row.
    6. When triggered_by='cascade' (automatic self-healing after poor scrape),
       create and dispatch a NEW scrape job so the auto_config is used
       immediately — operators don't need to manually re-trigger.

    This task is dispatched by:
      - POST /api/universities/{id}/probe  (manual operator trigger)
      - Orchestrator CASCADE hook          (triggered_by='cascade')
    """
    async def _run() -> dict:
        import uuid
        from datetime import datetime, timezone

        from sqlalchemy import update

        from app.models import ScrapeRuntimeJob, University
        from app.services.scraper.auto_config_generator import (
            fetch_sample_course_html,
            generate_config,
        )
        from app.services.scraper.site_probe import probe_site

        async with AsyncSessionLocal() as db:
            uni = await db.get(University, university_id)
            if uni is None:
                log.error("probe_and_configure: university %d not found", university_id)
                return {"ok": False, "reason": "not_found"}

            probe_url = uni.scrape_url or uni.website
            if not probe_url:
                log.error(
                    "probe_and_configure: university %d has no scrape_url or website",
                    university_id,
                )
                await db.execute(
                    update(University)
                    .where(University.id == university_id)
                    .values(probe_status="failed", probe_updated_at=datetime.now(timezone.utc))
                )
                await db.commit()
                return {"ok": False, "reason": "no_url"}

            # ── Stage 1: Probe the site ────────────────────────────────────────
            log.info("[PROBE] Starting probe for uni_id=%d url=%s", university_id, probe_url)
            try:
                profile = await probe_site(str(probe_url), timeout=20.0)
            except Exception as exc:
                log.error("[PROBE] probe_site failed for uni_id=%d: %s", university_id, exc)
                await db.execute(
                    update(University)
                    .where(University.id == university_id)
                    .values(probe_status="failed", probe_updated_at=datetime.now(timezone.utc))
                )
                await db.commit()
                return {"ok": False, "reason": f"probe_failed: {exc!s:.120}"}

            # ── Stage 2: Fetch a sample course page for Gemini context ─────────
            sample_urls = (
                profile.sample_course_urls[:3]
                or profile.wayback_sample_urls[:3]
            )
            sample_html: str | None = None
            if sample_urls:
                try:
                    sample_html = await fetch_sample_course_html(sample_urls[0])
                except Exception as _fetch_exc:
                    log.debug("[PROBE] sample fetch failed: %s", _fetch_exc)

            # ── Stage 3: Generate the config ───────────────────────────────────
            try:
                auto_cfg = await generate_config(
                    profile,
                    sample_html=sample_html,
                    sample_urls=sample_urls,
                )
            except Exception as exc:
                log.error("[PROBE] generate_config failed for uni_id=%d: %s", university_id, exc)
                auto_cfg = {}

            # ── Stage 4: Persist to DB ─────────────────────────────────────────
            import json

            existing_cfg: dict = uni.scrape_config or {}
            updated_cfg = {**existing_cfg, "auto_config": auto_cfg}

            await db.execute(
                update(University)
                .where(University.id == university_id)
                .values(
                    probe_result=profile.to_dict(),
                    probe_status="configured",
                    probe_updated_at=datetime.now(timezone.utc),
                    scrape_config=updated_cfg,
                )
            )
            await db.commit()

            log.info(
                "[PROBE] Done uni_id=%d strategy=%s confidence=%.2f blocked=%s apis=%d",
                university_id,
                profile.recommended_strategy,
                profile.strategy_confidence,
                profile.is_cloudflare_blocked,
                len(profile.detected_apis),
            )

            result = {
                "ok": True,
                "university_id": university_id,
                "strategy": profile.recommended_strategy,
                "confidence": profile.strategy_confidence,
                "cloudflare_blocked": profile.is_cloudflare_blocked,
                "js_spa": profile.is_js_spa,
                "detected_apis": [a.provider for a in profile.detected_apis],
                "has_sitemap": profile.has_sitemap,
                "wayback_count": profile.wayback_course_count,
                "triggered_by": triggered_by,
            }

            # ── Stage 5: CASCADE self-heal — auto-queue a retry scrape ─────────
            # When triggered by the orchestrator's poor-quality cascade, the
            # auto_config we just wrote is live.  Queue a new scrape job so the
            # operator doesn't have to manually re-trigger — the next run
            # will use the newly-generated config automatically.
            # Skip if the site is Cloudflare-blocked (retrying won't help yet).
            if triggered_by == "cascade" and not profile.is_cloudflare_blocked:
                try:
                    new_job_id = f"job_{uuid.uuid4().hex[:12]}"
                    retry_job = ScrapeRuntimeJob(
                        runtime_job_id=new_job_id,
                        university_id=university_id,
                        university_name=uni.name,
                        url=str(probe_url),
                        job_type="single",
                        status="queued",
                        fast_mode=False,
                        request_payload={
                            "url": str(probe_url),
                            "universityId": university_id,
                            "universityName": uni.name,
                            "triggeredBy": "cascade_self_heal",
                            "autoConfig": True,
                        },
                    )
                    async with AsyncSessionLocal() as db2:
                        db2.add(retry_job)
                        await db2.commit()
                    # Dispatch AFTER commit so the row is visible to the worker.
                    celery_app.send_task("scrape.university", args=[new_job_id])
                    log.info(
                        "[PROBE] CASCADE self-heal: queued retry scrape %s for "
                        "uni_id=%d (strategy=%s)",
                        new_job_id, university_id, profile.recommended_strategy,
                    )
                    result["retry_job_id"] = new_job_id
                except Exception as _retry_exc:
                    log.warning(
                        "[PROBE] CASCADE self-heal: failed to queue retry scrape "
                        "for uni_id=%d: %s",
                        university_id, _retry_exc,
                    )

            return result

    _sync_dispose()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception("probe_and_configure failed uni_id=%d: %s", university_id, exc)
        return {"ok": False, "university_id": university_id, "error": str(exc)}


@celery_app.task(name="scrape.refresh_baselines", bind=True, max_retries=0)
def refresh_baselines_weekly(self) -> dict:  # type: ignore[override]
    """Celery beat task — recompute fill-rate baselines from the trailing 30 days.

    Runs weekly (Sunday 04:00 UTC via beat_schedule in celery_app.py).
    Idempotent: uses INSERT ... ON CONFLICT DO UPDATE so re-running is safe.
    """
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            from app.scripts.seed_baselines import seed_baselines
            count = await seed_baselines(db)
            return {"ok": True, "baselines_upserted": count}

    _sync_dispose()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception("refresh_baselines_weekly failed: %s", exc)
        return {"ok": False, "reason": str(exc)}

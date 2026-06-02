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


def _run_in_fresh_loop(coro) -> None:  # noqa: ANN001
    """Run *coro* in a brand-new event loop and then close that loop.

    Used in exception-handler paths (SoftTimeLimitExceeded, BaseException)
    where ``asyncio.run()`` can fail because the interrupted event loop left
    Python's current-loop thread-local in a dirty state.  Creating an
    explicit loop bypasses ``asyncio.run()``'s "is there already a running
    loop?" guard and guarantees a clean execution context for the failure-mark
    DB write.
    """
    asyncio.set_event_loop(None)
    _loop = asyncio.new_event_loop()
    try:
        _loop.run_until_complete(coro)
    finally:
        try:
            _loop.close()
        except Exception:  # noqa: BLE001
            pass
        asyncio.set_event_loop(None)


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
    from app.services.scraper.browser_pool import pool as _browser_pool
    try:
        async with AsyncSessionLocal() as db:
            await run_scrape(db, runtime_job_id)
    finally:
        # Always close the Playwright browser pool before the event loop
        # tears down.  Without this, when SoftTimeLimitExceeded (or any
        # other exception) propagates, Playwright callbacks try to call
        # call_soon() on a closing/closed loop and raise
        # RuntimeError: Event loop is closed — which chains over the real
        # exception and can prevent _mark_failed from being called correctly.
        try:
            await _browser_pool.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


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
        # 45-min ceiling hit. Mark the job failed so the UI shows a real
        # error instead of spinning forever, then let Celery clean up.
        log.error(
            "scrape_university soft time limit exceeded for job %s — marking failed",
            runtime_job_id,
        )
        try:
            _sync_dispose()
            # Do NOT use asyncio.run() here — SoftTimeLimitExceeded interrupts
            # the event loop mid-flight and can leave the current-loop thread-
            # local in a dirty state, causing the next asyncio.run() to raise
            # RuntimeError: Event loop is closed (chained over the real error).
            # _run_in_fresh_loop() creates an explicit new loop that bypasses
            # these guards and guarantees _mark_failed actually writes to DB.
            _run_in_fresh_loop(_mark_failed(runtime_job_id, "Scrape exceeded 45-min time limit"))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": "soft_time_limit_exceeded"}
    except Exception as exc:
        log.exception("Task failed id=%s: %s", runtime_job_id, exc)
        # Mark job failed in DB so UI sees real status. No retry — the loop
        # issue won't fix itself on retry.
        try:
            _sync_dispose()
            _run_in_fresh_loop(_mark_failed(runtime_job_id, str(exc)))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": str(exc)}
    except BaseException as exc:
        # asyncio.CancelledError is BaseException (not Exception) in Python
        # 3.8+.  Without this block it escapes silently and the Celery slot
        # appears stuck until the soft-time-limit fires.  Reraise
        # SystemExit / KeyboardInterrupt so Celery can still shut down cleanly.
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        log.error(
            "scrape_university BaseException id=%s: %s",
            runtime_job_id, exc,
        )
        try:
            _sync_dispose()
            _run_in_fresh_loop(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
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
            _run_in_fresh_loop(_mark_failed(runtime_job_id, str(exc)))
        except Exception:
            pass
        return {"ok": False, "id": runtime_job_id, "error": str(exc)}
    except BaseException as exc:
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        log.error("repair_university BaseException id=%s: %s", runtime_job_id, exc)
        try:
            _sync_dispose()
            _run_in_fresh_loop(_mark_failed(runtime_job_id, f"BaseException: {exc}"))
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
    import sys

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
    exclude_strategies: list | None = None,
) -> dict:
    """Probe a university website and auto-generate a scraper config.

    1. Fetch the university's scrape_url from the DB.
    2. Run site_probe.probe_site() — detects Cloudflare, JS-SPA, search APIs,
       sitemap, Wayback — and picks the optimal strategy.
    3. If the recommended strategy is in ``exclude_strategies`` (strategies
       already tried and found to produce poor results), advance to the next
       rung in the escalation ladder so each cascade picks a different approach.
    4. Fetch one sample course page to give Gemini richer context.
    5. Run auto_config_generator.generate_config() — uses probe + Gemini to
       produce a UniConfig-compatible dict.
    6. Persist: probe_result, probe_status='configured', and
       scrape_config['auto_config'] = generated dict in the university row.
    7. When triggered_by='cascade' (automatic self-healing after poor scrape),
       create and dispatch a NEW scrape job so the auto_config is used
       immediately — operators don't need to manually re-trigger.

    This task is dispatched by:
      - POST /api/universities/{id}/probe  (manual operator trigger)
      - Orchestrator CASCADE hook          (triggered_by='cascade', exclude_strategies=[...])
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

            # ── Stage 1b: Strategy-ladder advancement (cascade self-heal) ─────
            # When triggered by the orchestrator's CASCADE hook the caller
            # passes ``exclude_strategies`` — the strategy that already produced
            # poor results.  If the probe picked the same strategy again, advance
            # to the next rung so each cascade genuinely tries a different path.
            _excluded = set(exclude_strategies or [])
            if _excluded and profile.recommended_strategy in _excluded:
                from app.services.scraper.site_probe import next_strategy
                _next = next_strategy(profile.recommended_strategy, profile.strategy_ladder)
                if _next:
                    log.info(
                        "[PROBE] CASCADE: strategy %r excluded — advancing to %r",
                        profile.recommended_strategy, _next,
                    )
                    profile.recommended_strategy = _next
                    profile.notes.append(
                        f"CASCADE: skipped {', '.join(_excluded)} (already tried); "
                        f"using {_next} instead"
                    )
                else:
                    log.warning(
                        "[PROBE] CASCADE: strategy %r excluded but no next rung found "
                        "in ladder %s — keeping original recommendation",
                        profile.recommended_strategy, profile.strategy_ladder,
                    )

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

            # ── Stage 2b: Phase 3 — load learned patterns for this platform ──
            # Compute platform type from the probe (mirrors _derive_platform_type
            # in auto_config_generator so we don't import it here).
            _platform_type: str = ""
            if getattr(profile, "detected_apis", None):
                _platform_type = (profile.detected_apis[0].provider or "").lower().strip()
            elif getattr(profile, "library_stack", None) and getattr(
                profile.library_stack, "situation", None
            ):
                _platform_type = profile.library_stack.situation.lower().strip()
            else:
                _platform_type = (getattr(profile, "recommended_strategy", None) or "").lower().strip()

            _learned_patterns: dict = {}
            if _platform_type:
                try:
                    from app.services.scraper.pattern_store import lookup_patterns
                    _learned_patterns = await lookup_patterns(_platform_type, db)
                    if _learned_patterns:
                        log.info(
                            "[PROBE] Phase 3: loaded %d learned patterns for platform=%r uni_id=%d",
                            len(_learned_patterns), _platform_type, university_id,
                        )
                except Exception as _pex:
                    log.debug("[PROBE] pattern lookup non-fatal: %s", _pex)
                    # lookup_patterns runs a DB query via the same session.
                    # If it raises (e.g. missing table), the transaction is
                    # left in a failed state. Roll back so the UPDATE below
                    # does not crash with InFailedSQLTransactionError.
                    try:
                        await db.rollback()
                    except Exception:
                        pass

            # ── Stage 3: Generate the config ───────────────────────────────────
            try:
                auto_cfg = await generate_config(
                    profile,
                    sample_html=sample_html,
                    sample_urls=sample_urls,
                    learned_patterns=_learned_patterns or None,
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


@celery_app.task(name="scrape.repair_extractor", bind=True, max_retries=1)
def repair_extractor(
    self,
    university_id: int,
    *,
    scrape_run_id: str | None = None,
    triggered_by: str = "manual",
) -> dict:  # type: ignore[override]
    """Celery task — Phase 2 autonomous extraction repair.

    Called by CASCADE when staged ≥ 5 but avg completeness < 70 %.
    Computes per-field fill rates from the failed scrape run, identifies
    the worst-performing fields, uses Gemini to regenerate CSS/XPath/regex
    rules for those fields, persists repaired rules into ``auto_config``,
    and re-queues a fresh scrape for the university.

    Idempotent: re-running after a successful repair just sees ≥ 70 % fill
    rates and returns ``{"ok": True, "fields_repaired": 0, "rescraped": False}``.
    """
    async def _run() -> dict:
        from app.services.scraper.ai_extractor_repair import (
            compute_field_fill_rates,
            identify_failing_fields,
            fetch_repair_samples,
            repair_extraction_rules,
            apply_repaired_rules_to_db,
        )
        from app.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            log.info(
                "[repair_extractor] starting for uni_id=%s run=%s triggered_by=%r",
                university_id, scrape_run_id, triggered_by,
            )

            # 1. Compute per-field fill rates for this run
            # Signature: compute_field_fill_rates(scrape_run_id, db)
            fill_rates = await compute_field_fill_rates(
                scrape_run_id, db
            )

            # 2. Find fields < 50 % fill
            failing = identify_failing_fields(fill_rates, threshold=0.50)
            log.info(
                "[repair_extractor] uni_id=%s failing fields (%d): %s",
                university_id, len(failing), failing,
            )
            if not failing:
                log.info(
                    "[repair_extractor] no failing fields — skipping repair for uni_id=%s",
                    university_id,
                )
                return {"ok": True, "fields_repaired": 0, "rescraped": False}

            # 3. Fetch sample HTML pages from recent scraped_courses rows
            # Signature: fetch_repair_samples(scrape_run_id, db, n=3)
            samples = await fetch_repair_samples(
                scrape_run_id, db, n=5
            )
            if not samples:
                log.warning(
                    "[repair_extractor] no sample pages available for uni_id=%s — cannot repair",
                    university_id,
                )
                return {"ok": False, "reason": "no_samples", "fields_repaired": 0}

            # 4. Ask Gemini to regenerate rules for each failing field
            new_rules = await repair_extraction_rules(
                failing_fields=failing, sample_pages=samples
            )

            # 5. Persist repaired rules into auto_config in the DB
            # NOTE: signature is (university_id, repaired_rules, db) — run_id first
            applied = await apply_repaired_rules_to_db(
                university_id, new_rules, db
            )
            log.info(
                "[repair_extractor] uni_id=%s applied %d repaired rules: %s",
                university_id, len(new_rules) if applied else 0, list(new_rules.keys()),
            )

            # Phase 3: promote successful repairs into the learning store so
            # future universities on the same platform start with these rules.
            if applied and new_rules:
                try:
                    from sqlalchemy import text as _t2
                    from app.services.scraper.pattern_store import (
                        promote_patterns,
                        REPAIR_ESTIMATED_FILL_RATE,
                    )
                    _pt_row = await db.execute(
                        _t2("SELECT scrape_config FROM universities WHERE id = :id"),
                        {"id": university_id},
                    )
                    _pt_first = _pt_row.first()
                    _pt_sc: dict = (_pt_first[0] if _pt_first else {}) or {}
                    _repair_platform = _pt_sc.get("auto_config", {}).get("_platform_type", "")
                    if _repair_platform:
                        # Optimistic estimated fill rate — corrected by the rescrape
                        # cascade which will call promote_patterns again with real rates.
                        _est_rates = {fk: REPAIR_ESTIMATED_FILL_RATE for fk in new_rules}
                        _n_promoted = await promote_patterns(
                            _repair_platform, new_rules, _est_rates, db
                        )
                        log.info(
                            "[repair_extractor] Phase 3: promoted %d rules for platform=%r",
                            _n_promoted, _repair_platform,
                        )
                except Exception as _prom_exc:
                    log.warning("[repair_extractor] promote_patterns non-fatal: %s", _prom_exc)

            # 6. Queue a fresh scrape so the repaired rules are exercised
            try:
                from app.tasks.scrape_tasks import run_scrape as _scrape_task
                _scrape_task.delay(university_id, triggered_by="repair_extractor")
                rescraped = True
            except Exception as _qs_exc:
                log.warning(
                    "[repair_extractor] could not queue rescrape for uni_id=%s: %s",
                    university_id, _qs_exc,
                )
                rescraped = False

            return {
                "ok": True,
                "fields_repaired": applied,
                "fields": list(new_rules.keys()),
                "rescraped": rescraped,
            }

    _sync_dispose()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception("repair_extractor failed for uni_id=%s: %s", university_id, exc)
        raise self.retry(exc=exc, countdown=120)


@celery_app.task(name="scrape.run_quality_actions", bind=True, max_retries=1)
def run_quality_actions(
    self,
    university_id: int,
    *,
    job_id: str,
    triggered_by: str = "orchestrator",
    cascade_repair_fired: bool = False,
) -> dict:  # type: ignore[override]
    """Phase 7: Autonomous Quality Action Dispatcher.

    Called by the orchestrator after a scrape completes with average
    completeness in the 70–84 % gap (above CASCADE's repair floor but
    below the 85 % auto-publish gate).

    Actions taken, in priority order:
    1. PDF extraction — backfills international_fee, other_requirement,
       english_test (ielts_overall), academic_score from discovered PDFs.
    2. repair_extractor — dispatches AI rule-regeneration for structural
       fields (degree_level, study_mode, duration, etc.).
    3. browser_retry — queues a new scrape with Playwright forced when
       the site is identified as a JS SPA.

    Idempotent: each ActionType dispatched at most once per run.  Celery
    task budget capped at 2 downstream tasks.  repair_extractor skipped
    when cascade_repair_fired=True to prevent duplicates.

    Result stored in ``universities.scrape_config['_p7_last_run']`` for
    audit / frontend display.
    """
    async def _run() -> dict:
        from app.database import AsyncSessionLocal
        from app.services.scraper.quality_action_dispatcher import dispatch_quality_actions
        from app.services.scraper.ai_extractor_repair import compute_field_fill_rates
        from sqlalchemy import text as _sql
        from datetime import datetime, timezone as _tz
        import json as _json

        async with AsyncSessionLocal() as db:
            log.info(
                "[run_quality_actions] start uni_id=%s job=%s triggered_by=%r "
                "cascade_repair=%s",
                university_id, job_id, triggered_by, cascade_repair_fired,
            )

            async def _persist_last_run(payload: dict) -> None:
                """Write payload to universities.scrape_config['_p7_last_run'].

                Called on EVERY exit path so the frontend polling can always
                detect task completion via the freshly-written timestamp.
                """
                try:
                    _p = _json.dumps({
                        "timestamp": datetime.now(_tz.utc).isoformat(),
                        "job_id": job_id,
                        **payload,
                    })
                    await db.execute(
                        _sql("""
                            UPDATE universities
                               SET scrape_config = jsonb_set(
                                     COALESCE(scrape_config, '{}'::jsonb),
                                     '{_p7_last_run}',
                                     cast(:payload as jsonb)
                                   )
                             WHERE id = :uni_id
                        """),
                        {"payload": _p, "uni_id": university_id},
                    )
                    await db.commit()
                except Exception as _pe:  # noqa: BLE001
                    log.warning("[run_quality_actions] _persist_last_run failed: %s", _pe)

            # 1. Fill rates for this job (may be empty if evidence rows lack
            #    selected=TRUE — dispatcher still runs via get_avg_completeness).
            fill_rates = await compute_field_fill_rates(job_id, db)
            if not fill_rates:
                log.info(
                    "[run_quality_actions] no fill rate data for job %s"
                    " — proceeding without field-level rates", job_id,
                )

            # 2. University config + metadata
            uni_row = (await db.execute(
                _sql("SELECT scrape_url, scrape_config, country"
                     " FROM universities WHERE id = :id"),
                {"id": university_id},
            )).mappings().first()

            if uni_row is None:
                await _persist_last_run({"ok": False, "reason": "university_not_found"})
                return {"ok": False, "reason": "university_not_found"}

            scrape_url      = (uni_row.get("scrape_url") or "").strip()
            uni_scrape_cfg  = uni_row.get("scrape_config") or {}
            uni_country     = (uni_row.get("country") or "").upper()

            # 3. Dispatch quality actions
            result = await dispatch_quality_actions(
                university_id=university_id,
                job_id=job_id,
                fill_rates=fill_rates,
                scrape_url=scrape_url,
                uni_country=uni_country,
                uni_scrape_config=uni_scrape_cfg,
                db=db,
                emit=None,
                cascade_repair_fired=cascade_repair_fired,
            )

            # 4. Persist action log — always written on every exit path so
            #    the frontend polling can detect completion via the timestamp.
            await _persist_last_run({"ok": True, **result.to_dict()})

            return {
                "ok": True,
                "university_id": university_id,
                "job_id": job_id,
                **result.to_dict(),
            }

    _sync_dispose()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception(
            "run_quality_actions failed for uni_id=%s job=%s: %s",
            university_id, job_id, exc,
        )
        raise self.retry(exc=exc, countdown=60)


# ── Phase 9: Conflict Repair Loop ─────────────────────────────────────────

@celery_app.task(name="scrape.repair_conflicts", bind=True, max_retries=1)
def repair_conflicts(
    self,
    *,
    job_id: str,
    triggered_by: str = "orchestrator",
) -> dict:  # type: ignore[override]
    """Phase 9: Conflict Repair Loop.

    Attempts to automatically resolve field-level verification conflicts before
    sending courses for human review.  Evidence-only — no live HTTP re-fetches.

    Strategy:
      If only low-authority sources (ai, pattern) disagree while high-authority
      sources (api, html, pdf) agree → resolve to the high-auth consensus.
      If high-authority sources disagree with each other → mark unresolved.

    Safety:
      At most one attempt per (course, field): ``conflict_repair_log`` has a
      UNIQUE (scraped_course_id, field_name) constraint — re-running is a no-op.
    """
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            from app.services.scraper.conflict_repair import repair_conflicts_for_job
            result = await repair_conflicts_for_job(db, job_id)
            return {
                "ok": True,
                "job_id": job_id,
                "triggered_by": triggered_by,
                "courses_attempted": result.courses_attempted,
                "fields_attempted": result.fields_attempted,
                "fields_resolved": result.fields_resolved,
                "fields_unresolved": result.fields_unresolved,
                "avg_confidence_before": result.avg_confidence_before,
                "avg_confidence_after": result.avg_confidence_after,
            }

    _sync_dispose()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        log.exception("[repair_conflicts] job=%s failed: %s", job_id, exc)
        raise self.retry(exc=exc, countdown=60)


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


# ── Phase 8: Performance Intelligence ─────────────────────────────────────────

@celery_app.task(name="scrape.record_job_performance", bind=True, max_retries=2,
                 default_retry_delay=120)
def record_job_performance(  # type: ignore[override]
    self,
    *,
    university_id: int,
    job_id: str,
) -> dict:
    """Record per-job performance metrics into scrape_performance_ledger.

    Dispatched automatically by the orchestrator ~30 s after job completion
    so all P7 inline updates are committed before aggregation.
    """
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            from app.services.performance_intelligence import compute_job_performance
            return await compute_job_performance(job_id, db)

    _sync_dispose()
    try:
        result = asyncio.run(_run())
        if not result.get("ok"):
            log.warning(
                "[P8] record_job_performance non-ok uni_id=%s job=%s: %s",
                university_id, job_id, result.get("reason"),
            )
        return result
    except Exception as exc:
        log.exception(
            "[P8] record_job_performance failed uni_id=%s job=%s: %s",
            university_id, job_id, exc,
        )
        raise self.retry(exc=exc)

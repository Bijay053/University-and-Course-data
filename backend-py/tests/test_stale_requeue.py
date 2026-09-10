"""Tests for the stale-job requeue logic in ``app/tasks/scrape_tasks.py``.

Seven behaviours exercised:

1. Jobs younger than 5 minutes are NOT returned by ``_async_find_stale``
   (i.e. they are NOT re-dispatched).
2. Jobs older than 5 minutes with ``status=queued`` ARE returned / re-dispatched.
3. ``updated_at`` is bumped inside ``_async_find_stale`` before control
   returns to the caller — the double-dispatch prevention heartbeat.
4. Job-type routing: ``repair`` jobs dispatch to ``repair_university``
   (``scrape.repair``); all other types dispatch to ``scrape_university``
   (``scrape.university``).
5. Redis NX lock collision: if the lock is already held (``set(nx=True)``
   returns falsy), the job must be skipped — not dispatched, not in
   ``result["requeued"]``.
6. **Concurrent beat ticks against real Redis**: two invocations fired
   simultaneously must produce exactly one dispatch — the NX lock is the
   sole serialisation point and must survive a genuine thread race.
7. Every database coroutine gets a fresh pool immediately before its new event
   loop, including consecutive checks in one long-lived worker process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.scraper.job_claim import (
    RuntimeJobClaimError,
    fail_queued_runtime_job_direct,
)
from app.tasks.scrape_tasks import (
    _STALE_QUEUED_MINUTES,
    _async_find_stale,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_engine_pool():
    """Dispose the connection pool around every test so asyncpg connections
    bound to a prior event loop are never reused."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _pick_any_university_id() -> int:
    """Return the id of any university that already exists in the test DB."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("SELECT id FROM universities LIMIT 1"))).one_or_none()
    if row is None:
        pytest.skip("need at least one university in the test DB")
    return row[0]


async def _seed_job(
    *,
    job_type: str = "full",
    age_minutes: int,
    requeue_count: int = 0,
) -> str:
    """Insert a ``queued`` scrape_runtime_jobs row backdated by *age_minutes*.

    Uses the ORM model so column defaults are respected regardless of DB
    migration state.  Returns the ``runtime_job_id`` for later lookups.
    """
    uni_id = await _pick_any_university_id()
    job_id = f"test_stale_{uuid.uuid4().hex[:12]}"
    backdated = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)

    async with AsyncSessionLocal() as db:
        job = ScrapeRuntimeJob(
            runtime_job_id=job_id,
            university_id=uni_id,
            job_type=job_type,
            status="queued",
            requeue_count=requeue_count,
        )
        db.add(job)
        await db.flush()
        # Override the server_default timestamps after flush.
        job.updated_at = backdated
        job.started_at = backdated
        await db.commit()
    return job_id


async def _get_updated_at(job_id: str) -> datetime:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                text("SELECT updated_at FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
                {"j": job_id},
            )
        ).one()
    dt = row[0]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _delete_job(job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
            {"j": job_id},
        )
        await db.commit()


def _make_sync_redis(*, nx_result: Any = True) -> MagicMock:
    """Return a synchronous Redis mock (scrape_tasks uses the sync redis client)."""
    r = MagicMock()
    r.set = MagicMock(return_value=nx_result)
    r.delete = MagicMock(return_value=1)
    return r


def test_scrape_claim_failure_uses_direct_queued_only_fallback():
    """A failure at the ownership boundary must bypass the normal async pool."""
    from app.tasks import scrape_tasks as st

    job_id = f"test_preclaim_{uuid.uuid4().hex[:8]}"
    claim_error = RuntimeJobClaimError("pool unavailable")

    def fake_fresh_loop(coro: Any) -> None:
        assert _coro_name(coro) == "fail_queued_runtime_job_direct"
        coro.close()

    def fail_scrape(coro: Any) -> None:
        coro.close()
        raise claim_error

    with (
        patch.object(st, "_sync_dispose"),
        patch.object(st.asyncio, "run", side_effect=fail_scrape),
        patch.object(st, "_run_in_fresh_loop", side_effect=fake_fresh_loop) as fallback,
        patch.object(st, "_immediate_requeue_hook"),
        patch.object(st, "_mark_failed") as normal_mark,
    ):
        result = st.scrape_university.run(job_id)

    assert result == {"ok": False, "id": job_id, "error": "pool unavailable"}
    fallback.assert_called_once()
    normal_mark.assert_not_called()


@pytest.mark.parametrize(
    ("task_name", "async_coro_name"),
    [
        ("repair_university", "_async_repair"),
        ("bulk_fix_staged_courses", "_async_bulk_fix"),
    ],
)
def test_secondary_worker_claim_failure_uses_direct_queued_only_fallback(
    task_name: str,
    async_coro_name: str,
):
    """Repair and bulk Fix preserve their payload and completion-hook contract."""
    from app.tasks import scrape_tasks as st

    job_id = f"test_preclaim_{uuid.uuid4().hex[:8]}"
    claim_error = RuntimeJobClaimError("pool unavailable")

    def fake_fresh_loop(coro: Any) -> None:
        assert _coro_name(coro) == "fail_queued_runtime_job_direct"
        coro.close()

    def fail_claim(coro: Any) -> None:
        assert _coro_name(coro) == async_coro_name
        coro.close()
        raise claim_error

    task = getattr(st, task_name)
    with (
        patch.object(st, "_sync_dispose"),
        patch.object(st.asyncio, "run", side_effect=fail_claim),
        patch.object(st, "_run_in_fresh_loop", side_effect=fake_fresh_loop) as fallback,
        patch.object(st, "_immediate_requeue_hook") as completion_hook,
        patch.object(st, "_mark_failed") as normal_mark,
    ):
        result = task.run(job_id)

    assert result == {"ok": False, "id": job_id, "error": "pool unavailable"}
    fallback.assert_called_once()
    normal_mark.assert_not_called()
    completion_hook.assert_called_once_with()


@pytest.mark.asyncio
async def test_direct_preclaim_failure_transitions_queued_job_to_failed():
    """The pool-independent fallback durably terminates an unclaimed job."""
    job_id = await _seed_job(age_minutes=0)
    try:
        changed = await fail_queued_runtime_job_direct(job_id, "startup failed")

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT status, error_message, completed_at "
                        "FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"
                    ),
                    {"jid": job_id},
                )
            ).one()
        assert changed is True
        assert row.status == "failed"
        assert "startup failed" in row.error_message
        assert row.completed_at is not None
    finally:
        await _delete_job(job_id)


@pytest.mark.asyncio
async def test_direct_preclaim_failure_does_not_overwrite_racing_claim():
    """A worker that claimed first keeps ownership when the fallback arrives."""
    job_id = await _seed_job(age_minutes=0)
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE scrape_runtime_jobs SET status = 'running' "
                    "WHERE runtime_job_id = :jid AND status = 'queued'"
                ),
                {"jid": job_id},
            )
            await db.commit()

        changed = await fail_queued_runtime_job_direct(job_id, "startup failed")

        async with AsyncSessionLocal() as db:
            status = await db.scalar(
                text(
                    "SELECT status FROM scrape_runtime_jobs "
                    "WHERE runtime_job_id = :jid"
                ),
                {"jid": job_id},
            )
        assert changed is False
        assert status == "running"
    finally:
        await _delete_job(job_id)


def _call_requeue_stale(task_module: Any) -> dict:
    """Invoke ``requeue_stale_queued`` directly, bypassing Celery's proxy.

    For ``bind=True`` tasks, ``task.run()`` (no extra args) calls the
    underlying function with the task instance bound as ``self`` — exactly
    what Celery does when it dispatches via ``.delay()`` or ``.apply()``.
    """
    return task_module.requeue_stale_queued.run()


def test_repeated_stale_checks_invalidate_pool_before_every_database_loop():
    """A successful lease-recovery query must not donate its pooled asyncpg
    connection to the stale-query loop that immediately follows it.

    Invoke the complete task twice, as a long-lived Celery worker does. The
    regression is the exact ordering: every ``asyncio.run`` must be preceded by
    a synchronous pool invalidation, including the second run inside each task.
    """
    from app.tasks import scrape_tasks as st

    events: list[str] = []

    def fake_dispose() -> None:
        events.append("dispose")

    def fake_run(coro: Any) -> list:
        name = _coro_name(coro)
        try:
            coro.close()
        except AttributeError:
            pass
        events.append(f"run:{name}")
        return []

    with (
        patch.object(st, "_sync_dispose", side_effect=fake_dispose),
        patch.object(st.asyncio, "run", side_effect=fake_run),
    ):
        first = _call_requeue_stale(st)
        second = _call_requeue_stale(st)

    assert first == {"ok": True, "requeued": []}
    assert second == {"ok": True, "requeued": []}
    one_check = [
        "dispose",
        "run:_async_requeue_abandoned_bulk_fixes",
        "dispose",
        "run:_async_find_stale",
    ]
    assert events == one_check + one_check


# ─── Case 1: young jobs are NOT re-dispatched ─────────────────────────────────


@pytest.mark.asyncio
async def test_find_stale_skips_fresh_jobs():
    """A job updated less than 5 minutes ago must NOT appear in the stale list."""
    fresh_id = await _seed_job(age_minutes=2)
    try:
        stale = await _async_find_stale()
        stale_ids = [jid for jid, _, _ in stale]
        assert fresh_id not in stale_ids, (
            f"Fresh job {fresh_id!r} should not be flagged as stale"
        )
    finally:
        await _delete_job(fresh_id)


# ─── Case 2: old jobs ARE re-dispatched ───────────────────────────────────────


@pytest.mark.asyncio
async def test_find_stale_returns_old_queued_jobs():
    """A job stuck in ``queued`` for longer than 5 minutes must appear in the
    stale list."""
    old_id = await _seed_job(age_minutes=_STALE_QUEUED_MINUTES + 2)
    try:
        stale = await _async_find_stale()
        stale_ids = [jid for jid, _, _ in stale]
        assert old_id in stale_ids, (
            f"Old queued job {old_id!r} should be returned by _async_find_stale"
        )
    finally:
        await _delete_job(old_id)


# ─── Case 3: updated_at is bumped before dispatch ─────────────────────────────


@pytest.mark.asyncio
async def test_find_stale_bumps_updated_at():
    """``_async_find_stale`` must advance ``updated_at`` to *now* for every
    stale job it returns. This prevents the next beat tick from picking the same
    job while the freshly dispatched Celery task is still in the broker backlog."""
    old_id = await _seed_job(age_minutes=_STALE_QUEUED_MINUTES + 2)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_QUEUED_MINUTES)

    before_call = await _get_updated_at(old_id)
    assert before_call < cutoff, "Precondition: updated_at should be older than the cutoff"

    try:
        stale = await _async_find_stale()
        stale_ids = [jid for jid, _, _ in stale]
        assert old_id in stale_ids, "Old job should appear in stale list"

        after_call = await _get_updated_at(old_id)
        assert after_call > cutoff, (
            f"updated_at ({after_call}) should have been bumped past the stale cutoff "
            f"({cutoff}) to prevent double-dispatch"
        )
    finally:
        await _delete_job(old_id)


# ─── Case 4a: non-repair jobs dispatch to scrape_university ──────────────────


def test_requeue_dispatches_full_job_to_scrape_university():
    """A stale job with ``job_type='full'`` must call ``scrape_university.delay``
    (queue name ``scrape.university``), not ``repair_university.delay``."""
    from app.tasks import scrape_tasks as st

    fake_job_id = f"test_full_{uuid.uuid4().hex[:8]}"
    stale_result = [(fake_job_id, "full", 0)]

    mock_redis = _make_sync_redis(nx_result=True)

    with (
        patch.object(st, "asyncio") as mock_asyncio,
        patch.object(st, "scrape_university") as mock_scrape,
        patch.object(st, "repair_university") as mock_repair,
        patch("redis.from_url", return_value=mock_redis),
    ):
        mock_asyncio.run.side_effect = _make_asyncio_run_interceptor(stale_result)

        result = _call_requeue_stale(st)

    assert result["ok"] is True
    assert fake_job_id in result["requeued"]
    mock_scrape.delay.assert_called_once_with(fake_job_id)
    mock_repair.delay.assert_not_called()


# ─── Case 4b: repair jobs dispatch to repair_university ──────────────────────


def test_requeue_dispatches_repair_job_to_repair_university():
    """A stale job with ``job_type='repair'`` must call ``repair_university.delay``
    (queue name ``scrape.repair``), not ``scrape_university.delay``."""
    from app.tasks import scrape_tasks as st

    fake_job_id = f"test_repair_{uuid.uuid4().hex[:8]}"
    stale_result = [(fake_job_id, "repair", 0)]

    mock_redis = _make_sync_redis(nx_result=True)

    with (
        patch.object(st, "asyncio") as mock_asyncio,
        patch.object(st, "scrape_university") as mock_scrape,
        patch.object(st, "repair_university") as mock_repair,
        patch("redis.from_url", return_value=mock_redis),
    ):
        mock_asyncio.run.side_effect = _make_asyncio_run_interceptor(stale_result)

        result = _call_requeue_stale(st)

    assert result["ok"] is True
    assert fake_job_id in result["requeued"]
    mock_repair.delay.assert_called_once_with(fake_job_id)
    mock_scrape.delay.assert_not_called()


# ─── Bonus: exhausted jobs (max requeues) are not dispatched ─────────────────


def test_requeue_exhausted_job_goes_to_exhausted_list():
    """A job that has already been requeued ``_MAX_REQUEUES`` times must NOT
    be dispatched again — it must appear in the ``exhausted`` list and
    ``_async_mark_failed_max_requeue`` must be called for it."""
    from app.tasks import scrape_tasks as st

    fake_job_id = f"test_exhaust_{uuid.uuid4().hex[:8]}"
    stale_result = [(fake_job_id, "full", st._MAX_REQUEUES)]

    mock_redis = _make_sync_redis(nx_result=True)

    with (
        patch.object(st, "asyncio") as mock_asyncio,
        patch.object(st, "scrape_university") as mock_scrape,
        patch.object(st, "repair_university") as mock_repair,
        patch("redis.from_url", return_value=mock_redis),
    ):
        mock_asyncio.run.side_effect = _make_asyncio_run_interceptor(stale_result)

        result = _call_requeue_stale(st)

    assert result["ok"] is True
    assert fake_job_id in result["exhausted"]
    assert fake_job_id not in result.get("requeued", [])
    mock_scrape.delay.assert_not_called()
    mock_repair.delay.assert_not_called()
    mark_calls = [
        c for c in mock_asyncio.run.call_args_list
        if _coro_name(c.args[0]) == "_async_mark_failed_max_requeue"
    ]
    assert len(mark_calls) == 1, "mark_failed should be called exactly once for exhausted job"


# ─── Case 5: Redis NX lock already held → job is silently skipped ────────────


def test_requeue_skips_job_when_nx_lock_is_held():
    """If the Redis NX lock for a job is already held (``r.set(nx=True)``
    returns falsy), ``requeue_stale_queued`` must skip that job entirely:
    - the job must NOT appear in ``result["requeued"]``
    - ``scrape_university.delay`` and ``repair_university.delay`` must NOT
      be called
    This prevents double-dispatch when two beat ticks overlap."""
    from app.tasks import scrape_tasks as st

    fake_job_id = f"test_nx_{uuid.uuid4().hex[:8]}"
    stale_result = [(fake_job_id, "full", 0)]

    # nx_result=None simulates SET NX returning falsy (lock already held).
    mock_redis = _make_sync_redis(nx_result=None)

    with (
        patch.object(st, "asyncio") as mock_asyncio,
        patch.object(st, "scrape_university") as mock_scrape,
        patch.object(st, "repair_university") as mock_repair,
        patch("redis.from_url", return_value=mock_redis),
    ):
        mock_asyncio.run.side_effect = _make_asyncio_run_interceptor(stale_result)

        result = _call_requeue_stale(st)

    assert result["ok"] is True
    assert fake_job_id not in result.get("requeued", []), (
        f"Locked job {fake_job_id!r} must NOT be in requeued list; got {result}"
    )
    mock_scrape.delay.assert_not_called()
    mock_repair.delay.assert_not_called()


# ─── Case 6: concurrent beat ticks against real Redis ────────────────────────


@pytest.mark.asyncio
async def test_requeue_concurrent_two_ticks_dispatch_exactly_once(
    require_real_redis,
):
    """Two simultaneous invocations of ``requeue_stale_queued`` against a *real*
    Redis instance must result in exactly one ``.delay()`` call.

    Strategy
    --------
    - A *real* stale ``scrape_runtime_jobs`` row is seeded (async, same event
      loop as the test) so the job actually exists in the database.
    - Both invocations are submitted to a ``ThreadPoolExecutor`` via
      ``loop.run_in_executor`` so they race concurrently while the async test
      event loop stays live for DB cleanup — solving the cross-loop asyncpg
      issue that arises from nesting ``asyncio.run()`` calls in a sync test.
    - ``asyncio.run`` inside ``scrape_tasks`` is intercepted to return the
      real stale-job tuple from both threads, keeping the race deterministic.
    - The winner acquires the NX lock and dispatches; the loser skips silently.
    - Cleanup runs in the same event loop: delete the Redis key and DB row.
    """
    import redis as redis_lib
    from app.tasks import scrape_tasks as st
    from app.tasks.celery_app import celery_app as _celery

    real_job_id = await _seed_job(age_minutes=_STALE_QUEUED_MINUTES + 2)
    stale_result = [(real_job_id, "full", 0)]
    lock_key = st._requeue_lock_key(real_job_id)
    r_cleanup = redis_lib.from_url(_celery.conf.broker_url, decode_responses=True)

    loop = asyncio.get_running_loop()
    try:
        with (
            patch.object(st, "asyncio") as mock_asyncio,
            patch.object(st, "scrape_university") as mock_scrape,
            patch.object(st, "repair_university") as mock_repair,
        ):
            mock_asyncio.run.side_effect = _make_asyncio_run_interceptor(stale_result)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = loop.run_in_executor(pool, _call_requeue_stale, st)
                fut_b = loop.run_in_executor(pool, _call_requeue_stale, st)
                result_a, result_b = await asyncio.gather(fut_a, fut_b)

        all_requeued = result_a.get("requeued", []) + result_b.get("requeued", [])
        dispatch_count = all_requeued.count(real_job_id)
        assert dispatch_count == 1, (
            f"Expected exactly 1 dispatch for job {real_job_id!r}, "
            f"got {dispatch_count}: result_a={result_a}, result_b={result_b}"
        )
        assert mock_scrape.delay.call_count == 1, (
            f"scrape_university.delay should be called exactly once, "
            f"got {mock_scrape.delay.call_count}"
        )
        mock_repair.delay.assert_not_called()
    finally:
        r_cleanup.delete(lock_key)
        r_cleanup.close()
        await _delete_job(real_job_id)


# ─── Utility ──────────────────────────────────────────────────────────────────


def _make_asyncio_run_interceptor(stale_result: list) -> Any:
    """Return a ``side_effect`` callable for the mocked ``asyncio.run``.

    When the task calls ``asyncio.run(_async_find_stale())``, this interceptor
    closes the unawaited coroutine (preventing the "coroutine was never awaited"
    RuntimeWarning) and returns the pre-built *stale_result* list.  All other
    coroutines (``_async_increment_requeue``, ``_async_mark_failed_max_requeue``)
    are closed and return ``None``.
    """
    def _intercept(coro: Any) -> Any:
        name = _coro_name(coro)
        try:
            coro.close()
        except AttributeError:
            pass
        if name == "_async_find_stale":
            return stale_result
        return None

    return _intercept


def _coro_name(coro: Any) -> str:
    """Extract the coroutine function name from a coroutine object or mock."""
    try:
        return coro.__name__
    except AttributeError:
        pass
    try:
        return coro.cr_code.co_name
    except AttributeError:
        return ""

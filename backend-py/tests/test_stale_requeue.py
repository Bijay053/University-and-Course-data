"""Tests for the stale-job requeue logic in ``app/tasks/scrape_tasks.py``.

Four behaviours exercised:

1. Jobs younger than 5 minutes are NOT returned by ``_async_find_stale``
   (i.e. they are NOT re-dispatched).
2. Jobs older than 5 minutes with ``status=queued`` ARE returned / re-dispatched.
3. ``updated_at`` is bumped inside ``_async_find_stale`` before control
   returns to the caller — the double-dispatch prevention heartbeat.
4. Job-type routing: ``repair`` jobs dispatch to ``repair_university``
   (``scrape.repair``); all other types dispatch to ``scrape_university``
   (``scrape.university``).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.models.scrape_runtime import ScrapeRuntimeJob
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


def _call_requeue_stale(task_module: Any) -> dict:
    """Invoke ``requeue_stale_queued`` directly, bypassing Celery's proxy.

    For ``bind=True`` tasks, ``task.run()`` (no extra args) calls the
    underlying function with the task instance bound as ``self`` — exactly
    what Celery does when it dispatches via ``.delay()`` or ``.apply()``.
    """
    return task_module.requeue_stale_queued.run()


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

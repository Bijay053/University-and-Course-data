"""Tests for the per-university Redis distributed lock.

Four scenarios exercised for run_repair (same lock pattern lives in
run_scrape / orchestrator.py):

1. **Blocked duplicate** — SET NX returns falsy (lock already held by
   another job) → run_repair returns
   ``{"ok": False, "reason": "concurrent_university_scrape"}`` and
   the DB row is updated to ``status='stopped'``.

2. **Fail-open on Redis outage** — ``redis.asyncio.from_url`` raises
   ConnectionError → repair proceeds normally (lock_acquired=True),
   so a Redis outage never blocks all repairs.

3. **Owner-check protects against TTL expiry** — lock is acquired
   (SET NX=True) but by the time the finally block calls GET, the
   key holds a *different* job ID (simulating TTL expiry + re-acquire
   by another worker) → DELETE is *not* called, protecting the new
   holder's lock.

4. **True concurrent gather** — two jobs for the same university run
   via ``asyncio.gather`` against a *real* Redis instance. Exactly one
   must succeed (ok=True) and the other must be stopped with reason=
   ``concurrent_university_scrape``.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.services.scraper import repair as repair_mod


# ─── shared fixtures ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _reset_engine_pool():
    """Dispose the SQLAlchemy pool before each test so async-pg
    connections opened on a previous event loop are not reused
    (mirrors the pattern in test_repair_scrape.py)."""
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed(job_id_override: str | None = None) -> dict[str, Any]:
    """Insert one university, one course, and one repair job (queued)."""
    job_id = job_id_override or f"repair_{uuid.uuid4().hex[:12]}"
    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities (name, country, city) "
                    "VALUES ('Lock Test Uni', 'Australia', 'Sydney') RETURNING id"
                )
            )
        ).scalar_one()
        course_id = (
            await db.execute(
                text(
                    "INSERT INTO courses "
                    "(university_id, name, status, course_website) "
                    "VALUES (:u, 'Bachelor of Locking', 'active', "
                    "        'https://lock.test/course') RETURNING id"
                ),
                {"u": uni_id},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO scrape_runtime_jobs "
                "(runtime_job_id, university_id, university_name, url, "
                " job_type, status, request_payload) "
                "VALUES (:j, :u, 'Lock Test Uni', "
                "        'https://lock.test/', 'repair', 'queued', "
                "        CAST(:pl AS jsonb))"
            ),
            {
                "j": job_id,
                "u": uni_id,
                "pl": (
                    '{"repair_targets": [{"course_id": '
                    + str(course_id)
                    + ', "url": "https://lock.test/course"}]}'
                ),
            },
        )
        await db.commit()
    return {"uni_id": uni_id, "course_id": course_id, "job_id": job_id}


async def _cleanup(ids: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "DELETE FROM scrape_runtime_jobs WHERE university_id = :u"
            ),
            {"u": ids["uni_id"]},
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :u"),
            {"u": ids["uni_id"]},
        )
        await db.commit()


def _make_mock_redis(
    *,
    set_return: bool | None,
    get_return: str = "other_job",
) -> AsyncMock:
    """Build a minimal async-Redis mock.

    set_return=True  → SET NX succeeds (lock acquired by caller).
    set_return=None  → SET NX fails (lock already held by get_return).
    """
    mock = AsyncMock()
    mock.set = AsyncMock(return_value=set_return)
    mock.get = AsyncMock(return_value=get_return)
    mock.delete = AsyncMock(return_value=1)
    mock.aclose = AsyncMock()
    return mock


def _noop_extract(
    link: dict, country: Any, uni_pdf_data: Any, emit: Any = None
) -> dict:
    return {
        "name": link["name"],
        "url": link["url"],
        "payload": {"duration": 1},
        "evidence": [],
    }


# ─── Test 1: duplicate repair is blocked ─────────────────────────────────


@pytest.mark.asyncio
async def test_repair_lock_blocks_duplicate() -> None:
    """SET NX returns falsy → run_repair must abort with
    reason='concurrent_university_scrape' and set status='stopped'."""
    ids = await _seed()
    mock_redis = _make_mock_redis(set_return=None, get_return="repair_first_holder")

    try:
        import redis.asyncio as _real_aioredis

        with patch.object(_real_aioredis, "from_url", return_value=mock_redis):
            async with AsyncSessionLocal() as db:
                result = await repair_mod.run_repair(db, ids["job_id"])

        assert result == {
            "ok": False,
            "reason": "concurrent_university_scrape",
        }, f"Unexpected result: {result}"

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT status, error_message "
                        "FROM scrape_runtime_jobs "
                        "WHERE runtime_job_id = :j"
                    ),
                    {"j": ids["job_id"]},
                )
            ).mappings().one()

        assert row["status"] == "stopped", (
            f"Expected status='stopped', got '{row['status']}'"
        )
        assert "already running" in (row["error_message"] or ""), (
            f"error_message missing expected text: {row['error_message']!r}"
        )
    finally:
        await _cleanup(ids)


# ─── Test 2: Redis unavailable → fail open ───────────────────────────────


@pytest.mark.asyncio
async def test_repair_lock_redis_down_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If redis.asyncio.from_url raises (Redis down) run_repair must
    proceed normally (fail-open) — a Redis outage never blocks repairs."""
    ids = await _seed()

    async def _fake_extract(
        link: dict, country: Any, uni_pdf_data: Any, emit: Any = None
    ) -> dict:
        return _noop_extract(link, country, uni_pdf_data, emit)

    monkeypatch.setattr(repair_mod, "_extract_only", _fake_extract)

    try:
        import redis.asyncio as _real_aioredis

        with patch.object(
            _real_aioredis, "from_url", side_effect=ConnectionError("Redis unreachable")
        ):
            async with AsyncSessionLocal() as db:
                result = await repair_mod.run_repair(db, ids["job_id"])

        assert result.get("reason") != "concurrent_university_scrape", (
            "Repair should NOT be blocked when Redis is unavailable; "
            f"got {result}"
        )
        assert result.get("ok") is True, (
            f"Fail-open path should complete successfully; got {result}"
        )
    finally:
        await _cleanup(ids)


# ─── Test 3: TTL expiry before finally — owner check guards delete ────────


@pytest.mark.asyncio
async def test_repair_lock_ttl_expiry_skips_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If our lock TTL expires and a new job re-acquires it before our
    finally block runs, the owner check (GET then compare) must prevent
    us from deleting the interloper's lock key."""
    ids = await _seed()

    async def _fake_extract(
        link: dict, country: Any, uni_pdf_data: Any, emit: Any = None
    ) -> dict:
        return _noop_extract(link, country, uni_pdf_data, emit)

    monkeypatch.setattr(repair_mod, "_extract_only", _fake_extract)

    mock_redis = _make_mock_redis(
        set_return=True,
        get_return="repair_interloper_job",
    )

    try:
        import redis.asyncio as _real_aioredis

        with patch.object(_real_aioredis, "from_url", return_value=mock_redis):
            async with AsyncSessionLocal() as db:
                await repair_mod.run_repair(db, ids["job_id"])

        mock_redis.delete.assert_not_called()
    finally:
        await _cleanup(ids)


# ─── Test 4: true concurrent gather against real Redis ────────────────────


@pytest.mark.asyncio
async def test_repair_lock_concurrent_gather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two repairs for the same university run concurrently via
    asyncio.gather against a *real* Redis instance.  Exactly one must
    succeed (ok=True) and the other must be stopped with
    reason='concurrent_university_scrape'."""
    ids_a = await _seed()
    uni_id = ids_a["uni_id"]

    job_b = f"repair_{uuid.uuid4().hex[:12]}"
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO scrape_runtime_jobs "
                "(runtime_job_id, university_id, university_name, url, "
                " job_type, status, request_payload) "
                "VALUES (:j, :u, 'Lock Test Uni', "
                "        'https://lock.test/', 'repair', 'queued', "
                "        CAST(:pl AS jsonb))"
            ),
            {
                "j": job_b,
                "u": uni_id,
                "pl": (
                    '{"repair_targets": [{"course_id": '
                    + str(ids_a["course_id"])
                    + ', "url": "https://lock.test/course"}]}'
                ),
            },
        )
        await db.commit()
    ids_b = {"uni_id": uni_id, "course_id": ids_a["course_id"], "job_id": job_b}

    async def _fake_extract(
        link: dict, country: Any, uni_pdf_data: Any, emit: Any = None
    ) -> dict:
        return _noop_extract(link, country, uni_pdf_data, emit)

    monkeypatch.setattr(repair_mod, "_extract_only", _fake_extract)

    import redis.asyncio as _real_aioredis

    lock_key = f"scrape:uni_lock:{uni_id}"
    r_cleanup = _real_aioredis.from_url("redis://localhost:6379")

    try:
        async def _run_a() -> dict:
            async with AsyncSessionLocal() as db:
                return await repair_mod.run_repair(db, ids_a["job_id"])

        async def _run_b() -> dict:
            async with AsyncSessionLocal() as db:
                return await repair_mod.run_repair(db, ids_b["job_id"])

        result_a, result_b = await asyncio.gather(_run_a(), _run_b())

        results = [result_a, result_b]
        winners = [res for res in results if res.get("ok") is True]
        losers = [
            res
            for res in results
            if res.get("reason") == "concurrent_university_scrape"
        ]

        assert len(winners) == 1, (
            f"Expected exactly 1 winner, got: {results}"
        )
        assert len(losers) == 1, (
            f"Expected exactly 1 loser, got: {results}"
        )
    finally:
        await r_cleanup.delete(lock_key)
        await r_cleanup.aclose()
        await _cleanup(ids_a)

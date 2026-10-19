"""Overlapping HTTP starts with real transactions and best-effort Redis failures.

Run only on a development/test PostgreSQL database:
ALLOW_ISOLATED_DISPATCH_TESTS=1 python -m pytest \
    tests/test_initial_dispatch_concurrency.py -q

No app lifespan, shared schema, worker, or broker is used. PostgreSQL itself
must confirm one request is blocked by the other's university advisory lock;
merely scheduling two coroutines is not accepted as concurrency coverage.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, call
import uuid

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio
import redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import ScrapeRuntimeJob, University
from app.routers.scrape import router
from tests.helpers.disposable_dispatch_database import disposable_dispatch_database
from tests.test_initial_dispatch_lock_cleanup import isolated_redis  # noqa: F401


@pytest_asyncio.fixture(loop_scope="function")
async def dispatch_database():
    if os.environ.get("ALLOW_ISOLATED_DISPATCH_TESTS") != "1":
        pytest.skip("Enable only against a development/test PostgreSQL database")
    async with disposable_dispatch_database() as engine:
        # Advisory locks are database-wide, NOT schema-scoped. Use negative,
        # random IDs so test locks cannot collide with real university IDs.
        first = -(uuid.uuid4().int % 1_000_000_000) - 100
        ids = [first, first + 1, first + 2]
        async with AsyncSession(engine) as session:
            session.add_all([
                University(
                    id=uid, name=f"Disposable university {uid}",
                    country="Test", city="Test",
                    scrape_url=f"https://university-{abs(uid)}.example/courses",
                    probe_status="completed",
                )
                for uid in ids
            ])
            await session.commit()
        yield engine, ids


@pytest.fixture
def dispatch_redis_failures(monkeypatch, isolated_redis):
    """Keep the real initial-lock helper; replace its client factory, not it."""
    from app.tasks import scrape_tasks as st
    from celery.app.task import Task
    from kombu import Connection
    from redis.asyncio.connection import Connection as AsyncRedisConnection

    socket_path, observer = isolated_redis
    state = SimpleNamespace(
        mode=None, delivered=[], factory_jobs=[], clients=[], disconnects=[],
        written_expiries={}, existing_expiries={}, set_results=[], prohibited=[],
        observer=observer, baseline=observer.info("clients")["connected_clients"],
    )

    def prohibited(*args, **kwargs):
        state.prohibited.append(True)
        pytest.fail("Shared broker / non-disposable Redis access is prohibited")

    monkeypatch.setattr(redis.Connection, "connect", prohibited)
    monkeypatch.setattr(AsyncRedisConnection, "connect", prohibited)
    monkeypatch.setattr(Connection, "connect", prohibited)
    monkeypatch.setattr(Task, "apply_async", prohibited)

    def deliver(job_id):
        state.delivered.append(job_id)
        if state.mode == "nx_collision_close_failure":
            key = st._requeue_lock_key(job_id)
            observer.set(key, "other-owner", ex=600)
            state.existing_expiries[key] = observer.pexpiretime(key)

    state.delay = MagicMock(side_effect=deliver)
    # Replacing a method on a registered Celery task can miss a reloaded task.
    monkeypatch.setattr(st, "scrape_university", SimpleNamespace(delay=state.delay))

    def factory():
        state.factory_jobs.append(state.delivered[-1] if state.delivered else None)
        if state.mode == "creation_failure":
            raise redis.ConnectionError("injected client creation failure")
        if state.mode == "borrowed_close_failure":
            pool = redis.ConnectionPool(
                connection_class=redis.UnixDomainSocketConnection,
                path=socket_path, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
            client = redis.Redis(connection_pool=pool)
        else:
            client = redis.Redis(
                unix_socket_path=socket_path, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
        state.clients.append(client)  # Retain references: GC cannot hide leaks.
        state.disconnects.append(client.connection_pool.disconnect)
        client.connection_pool.disconnect = MagicMock(wraps=client.connection_pool.disconnect)
        real_set = client.set

        def set_lock(key, value, **kwargs):
            result = real_set(key, value, **kwargs)
            state.set_results.append((key, result))
            state.written_expiries[key] = observer.pexpiretime(key)
            return result

        client.set = MagicMock(side_effect=set_lock)
        client.delete = MagicMock(wraps=client.delete)
        # Fail BEFORE closing the socket; owned-pool fallback must do the work.
        client.close = MagicMock(side_effect=redis.ConnectionError("injected close failure"))
        return client

    # Handles task module reloads without changing the real helper implementation.
    monkeypatch.setitem(st.set_initial_dispatch_lock.__globals__, "_get_redis", factory)
    try:
        yield state
    finally:
        for disconnect in state.disconnects:
            disconnect()
        assert state.prohibited == []


async def wait_for_contention(observer, owner_pid, waiter_pid, uid):
    """Wait for a real ungranted advisory lock, not a guessed scheduling delay."""
    async with asyncio.timeout(5):
        while True:
            locks = (await observer.execute(text("""
                SELECT pid, granted FROM pg_locks
                WHERE locktype = 'advisory'
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                  AND classid::bigint = 4294967295
                  AND objid::bigint = :low_bits AND objsubid = 1
                  AND pid IN (:owner, :waiter)
            """), {
                "low_bits": uid & 0xFFFFFFFF, "owner": owner_pid, "waiter": waiter_pid,
            })).all()
            if set(locks) == {(owner_pid, True), (waiter_pid, False)}:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid},
                )).scalar_one()
                assert owner_pid in blockers
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(
    "first_endpoint,second_endpoint",
    [("start", "start"), ("start", "bulk"), ("bulk", "start"), ("bulk", "bulk")],
    ids=["single-single", "single-bulk", "bulk-single", "bulk-bulk-reversed"],
)
@pytest.mark.parametrize(
    "redis_failure",
    ["creation_failure", "close_failure", "nx_collision_close_failure", "borrowed_close_failure"],
)
async def test_overlapping_requests_deduplicate_despite_redis_failure(
    dispatch_database, dispatch_redis_failures, first_endpoint, second_endpoint, redis_failure,
):
    from app.tasks import scrape_tasks as st

    engine, ids = dispatch_database
    redis_state = dispatch_redis_failures
    redis_state.mode = redis_failure
    owner_locked = asyncio.Event()
    release_owner = asyncio.Event()
    release_connections = asyncio.Event()
    sessions = []

    class RequestSession(AsyncSession):
        """Real session; pause only after PostgreSQL grants the first lock."""

        async def execute(self, statement, params=None, **kwargs):
            is_lock = str(statement) == "SELECT pg_advisory_xact_lock(:uid)"
            if is_lock:
                self.lock_attempts.append(params["uid"])
                self.attempted.set()
            result = await super().execute(statement, params, **kwargs)
            if is_lock and self is sessions[0] and len(self.lock_attempts) == 1:
                owner_locked.set()
                await release_owner.wait()
            return result

    async def owned_db():
        # Pin a distinct physical connection per request, but DO NOT wrap it
        # in an outer transaction or replace commit with a savepoint.
        async with engine.connect() as connection:
            async with RequestSession(
                bind=connection, expire_on_commit=False, autoflush=False,
            ) as session:
                session.lock_attempts = []
                session.attempted = asyncio.Event()
                session.finished = asyncio.Event()
                sessions.append(session)
                session.pid = await session.scalar(text("SELECT pg_backend_pid()"))
                try:
                    yield session
                finally:
                    session.finished.set()
                    # Keep connections alive so lock-release assertions cannot
                    # accidentally pass because connection close released locks.
                    await release_connections.wait()

    app = FastAPI()  # No production lifespan, shared overrides, or workers.
    app.include_router(router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = owned_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "isolated-test", "permissions": ["scraping.trigger"],
    }

    def body(endpoint, reverse=False):
        if endpoint == "start":
            return {"university_id": ids[0], "fast_mode": False}
        # Opposite order and repeated IDs also exercise stable lock ordering.
        return {"university_ids": (list(reversed(ids)) if reverse else ids) + [ids[0]],
                "fast_mode": False}

    requests = []
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://isolated.test",
            ) as client, engine.connect() as observer:
                requests.append(asyncio.create_task(client.post(
                    f"/api/scrape/{first_endpoint}", json=body(first_endpoint, reverse=True),
                )))
                await owner_locked.wait()
                requests.append(asyncio.create_task(client.post(
                    f"/api/scrape/{second_endpoint}", json=body(second_endpoint),
                )))
                async with asyncio.timeout(5):
                    while len(sessions) < 2:
                        await asyncio.sleep(0.01)
                    await sessions[1].attempted.wait()
                assert sessions[0].pid != sessions[1].pid
                assert sessions[0].lock_attempts == sessions[1].lock_attempts == [ids[0]]
                await wait_for_contention(observer, sessions[0].pid, sessions[1].pid, ids[0])
                assert not any(request.done() for request in requests)
                assert redis_state.delivered == []
                release_owner.set()
                await asyncio.gather(*(session.finished.wait() for session in sessions))

                # These are still-open request connections. Transaction locks
                # must be gone BEFORE dependency teardown closes either one.
                assert (await observer.execute(text("""
                    SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'
                    AND pid IN (:first, :second)
                """), {"first": sessions[0].pid, "second": sessions[1].pid})).scalar_one() == 0
                for session in sessions:
                    assert not session.in_transaction()
                release_connections.set()
                responses = await asyncio.gather(*requests)
    finally:
        release_owner.set()
        release_connections.set()
        for request in requests:
            if not request.done():
                request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)

    expected_ids = ids if "bulk" in (first_endpoint, second_endpoint) else ids[:1]
    # Independent connection and committed query: no request identity map or
    # fake commit ledger can conceal extra rows, failed commits, or delivery.
    async with AsyncSession(engine) as observer:
        rows = (await observer.scalars(select(ScrapeRuntimeJob))).all()
    assert Counter(row.university_id for row in rows) == Counter(expected_ids)
    by_uid = {row.university_id: row for row in rows}
    job_ids = {row.runtime_job_id for row in rows}
    assert all(row.status == "queued" for row in rows)
    assert Counter(redis_state.delivered) == Counter({jid: 1 for jid in job_ids})
    assert redis_state.delay.call_args_list == [call(jid) for jid in redis_state.delivered]
    assert redis_state.factory_jobs == redis_state.delivered

    for index, (endpoint, response, session) in enumerate(zip(
        (first_endpoint, second_endpoint), responses, sessions,
    )):
        assert response.status_code == 202, response.text
        payload = response.json()
        assert session.lock_attempts == (ids if endpoint == "bulk" else ids[:1])
        if endpoint == "start":
            assert payload == {
                "jobId": by_uid[ids[0]].runtime_job_id,
                "runtimeJobId": by_uid[ids[0]].runtime_job_id,
                "status": "queued", "reused": index == 1, "ok": True,
            }
        else:
            queued = len(ids) if index == 0 else (len(ids) - 1 if first_endpoint == "start" else 0)
            assert payload == {"sessionId": payload["sessionId"], "queued": queued, "ok": True}
            assert payload["sessionId"].startswith("bulk_")
            assert sum(
                (row.request_payload or {}).get("session_id") == payload["sessionId"]
                for row in rows
            ) == queued

    observer = redis_state.observer
    keys = [st._requeue_lock_key(jid) for jid in redis_state.delivered]
    if redis_failure == "creation_failure":
        assert redis_state.clients == redis_state.set_results == []
        assert observer.dbsize() == 0
    else:
        collision = redis_failure == "nx_collision_close_failure"
        borrowed = redis_failure == "borrowed_close_failure"
        assert len(redis_state.clients) == len(job_ids)
        assert redis_state.set_results == [(key, None if collision else True) for key in keys]
        assert set(observer.scan_iter()) == set(keys)
        for key, owned_client in zip(keys, redis_state.clients):
            owned_client.set.assert_called_once_with(
                key, "1", nx=True, ex=st._REQUEUE_LOCK_TTL_S + 30,
            )
            owned_client.close.assert_called_once_with()
            owned_client.delete.assert_not_called()
            assert owned_client.auto_close_connection_pool is (not borrowed)
            if borrowed:
                owned_client.connection_pool.disconnect.assert_not_called()
                # Another borrower can still use the existing pool.
                peer = redis.Redis(connection_pool=owned_client.connection_pool)
                try:
                    assert peer.ping()
                finally:
                    peer.close()
            else:
                owned_client.connection_pool.disconnect.assert_called_once_with()
            assert observer.get(key) == ("other-owner" if collision else "1")
            assert observer.pexpiretime(key) == redis_state.written_expiries[key]
            if collision:
                assert observer.pexpiretime(key) == redis_state.existing_expiries[key]
            else:
                assert 0 < observer.pttl(key) <= (st._REQUEUE_LOCK_TTL_S + 30) * 1000
    expected_open = len(job_ids) if redis_failure == "borrowed_close_failure" else 0
    assert observer.info("clients")["connected_clients"] == redis_state.baseline + expected_open
    assert redis_state.prohibited == []

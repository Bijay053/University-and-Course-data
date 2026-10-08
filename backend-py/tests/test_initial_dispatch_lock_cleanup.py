"""Initial API dispatch owns its Redis client, but must leave the lock intact."""
from __future__ import annotations

import gc
import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import redis
from fastapi import FastAPI


@pytest.fixture
def isolated_redis(tmp_path):
    """No TCP listener, persistence, shared broker connection, or broker keys."""
    socket_path = str(tmp_path / "redis.sock")
    process = subprocess.Popen(
        ["redis-server", "--port", "0", "--unixsocket", socket_path,
         "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    observer = redis.Redis(
        unix_socket_path=socket_path, decode_responses=True,
        socket_connect_timeout=1, socket_timeout=1,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            assert process.poll() is None, "isolated Redis exited during startup"
            try:
                observer.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() >= deadline:
                    pytest.fail("isolated Redis did not become ready")
                time.sleep(0.01)
        yield socket_path, observer
    finally:
        observer.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("set_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_closes_owned_client_without_changing_lock_contract(
    monkeypatch, set_fails, close_fails,
):
    from app.tasks import scrape_tasks as st

    client = MagicMock()
    if set_fails:
        client.set.side_effect = redis.ConnectionError("SET unavailable")
    if close_fails:
        client.close.side_effect = redis.ConnectionError("close unavailable")
    monkeypatch.setattr(st, "_get_redis", lambda: client)

    assert st.set_initial_dispatch_lock("initial-dispatch") is None

    client.set.assert_called_once_with(
        "scrape:requeue_lock:initial-dispatch", "1",
        nx=True, ex=st._REQUEUE_LOCK_TTL_S + 30,
    )
    client.close.assert_called_once_with()
    client.delete.assert_not_called()


def test_client_creation_failure_remains_best_effort(monkeypatch):
    from app.tasks import scrape_tasks as st

    factory = MagicMock(side_effect=redis.ConnectionError("client unavailable"))
    monkeypatch.setattr(st, "_get_redis", factory)
    assert st.set_initial_dispatch_lock("initial-dispatch") is None
    factory.assert_called_once_with()


@pytest.mark.parametrize("outcome", ["success", "nx_collision", "command_failure"])
def test_repeated_calls_release_descriptors_and_preserve_keys(
    monkeypatch, isolated_redis, outcome,
):
    from app.tasks import scrape_tasks as st

    socket_path, observer = isolated_redis
    retained_clients = []  # Finalizers cannot conceal a missing close().
    key = st._requeue_lock_key("existing-lock")
    if outcome == "nx_collision":
        observer.set(key, "existing-owner", ex=600)
        original_expiry = observer.pexpiretime(key)

    def factory():
        client = redis.Redis(
            unix_socket_path=socket_path, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        retained_clients.append(client)
        client.close = MagicMock(wraps=client.close)
        if outcome == "command_failure":
            real_set = client.set

            def failing_set(name, value, **kwargs):
                # Real Redis error after connecting, not a pre-connect mock.
                kwargs["ex"] = 0
                return real_set(name, value, **kwargs)

            client.set = MagicMock(side_effect=failing_set)
        return client

    monkeypatch.setattr(st, "_get_redis", factory)
    baseline = len(os.listdir("/proc/self/fd"))
    baseline_connections = observer.info("clients")["connected_clients"]
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for index in range(64):
            job_id = "existing-lock" if outcome == "nx_collision" else f"isolated-{index}"
            assert st.set_initial_dispatch_lock(job_id) is None
            assert len(os.listdir("/proc/self/fd")) <= baseline
            retained_clients[-1].close.assert_called_once_with()
            if outcome == "success":
                current_key = st._requeue_lock_key(job_id)
                assert observer.get(current_key) == "1"
                assert 0 < observer.ttl(current_key) <= st._REQUEUE_LOCK_TTL_S + 30
            elif outcome == "command_failure":
                assert observer.get(st._requeue_lock_key(job_id)) is None
                retained_clients[-1].set.assert_called_once_with(
                    st._requeue_lock_key(job_id), "1",
                    nx=True, ex=st._REQUEUE_LOCK_TTL_S + 30,
                )
            else:
                assert observer.get(key) == "existing-owner"
                assert observer.pexpiretime(key) == original_expiry

        assert observer.dbsize() == (64 if outcome == "success" else
                                     1 if outcome == "nx_collision" else 0)
        assert observer.info("clients")["connected_clients"] == baseline_connections
    finally:
        for client in retained_clients:
            client.connection_pool.disconnect()
        if gc_was_enabled:
            gc.enable()


class _DispatchSession:
    """Request-owned rows only; no engine, database connection, or active jobs."""

    def __init__(self, events):
        from app.models import University

        self.events = events
        self.rows = []
        self.committed_ids = set()
        self.universities = {
            uid: University(
                id=uid, name=f"Isolated University {uid}", country="Australia",
                scrape_url=f"https://university-{uid}.example/courses",
                probe_status="completed",
            )
            for uid in (41, 42, 43)
        }

    async def get(self, model, pk):
        from app.models import University

        assert model is University
        return self.universities.get(pk)

    async def execute(self, statement, params=None):
        # Exercise the router's active-job lookup without touching PostgreSQL.
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def refresh(self, obj, *, attribute_names):
        assert attribute_names == ["probe_status"]

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.committed_ids.update(row.runtime_job_id for row in self.rows)
        self.events.append(("commit", tuple(self.committed_ids)))


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("endpoint", ["start", "bulk"])
@pytest.mark.parametrize("delivery", ["success", "failure", "alternating"])
async def test_repeated_api_dispatch_keeps_queued_contract_and_releases_redis(
    monkeypatch, isolated_redis, endpoint, delivery,
):
    """Real HTTP handlers + real lock helper; never use the shared broker."""
    from app.dependencies import get_current_user, get_db
    from app.routers.scrape import router
    from app.tasks import scrape_tasks as st

    socket_path, observer = isolated_redis
    events = []
    sessions = []
    retained_clients = []  # Do not let finalizers mask an unclosed connection.
    delivered_ids = []
    failed_ids = []

    def forbid_tcp(*args, **kwargs):
        pytest.fail("API dispatch test attempted a non-disposable Redis connection")

    # UnixDomainSocketConnection does not inherit the TCP Connection class.
    monkeypatch.setattr(redis.Connection, "connect", forbid_tcp)

    def redis_factory():
        client = redis.Redis(
            unix_socket_path=socket_path, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        client.close = MagicMock(wraps=client.close)
        retained_clients.append(client)
        return client

    real_lock = st.set_initial_dispatch_lock
    # Patch the actual helper globals, including after import/reload tests.
    monkeypatch.setitem(real_lock.__globals__, "_get_redis", redis_factory)

    def lock_after_delivery(job_id):
        assert events[-1] == ("delivered", job_id)
        events.append(("lock", job_id))
        return real_lock(job_id)

    lock_spy = MagicMock(side_effect=lock_after_delivery)
    monkeypatch.setattr(st, "set_initial_dispatch_lock", lock_spy)

    def dispatch(job_id):
        session = sessions[-1]
        assert job_id in session.committed_ids, "dispatch preceded row commit"
        row = next(row for row in session.rows if row.runtime_job_id == job_id)
        assert row.status == "queued"
        attempt = len(delivered_ids) + len(failed_ids)
        events.append(("dispatch", job_id))
        if delivery == "failure" or (delivery == "alternating" and attempt % 2 == 0):
            failed_ids.append(job_id)
            events.append(("failed", job_id))
            raise redis.ConnectionError("mocked Celery delivery failure")
        delivered_ids.append(job_id)
        events.append(("delivered", job_id))

    # Replace the entire task, not just a possibly stale registered task method.
    delay = MagicMock(side_effect=dispatch)
    monkeypatch.setattr(st, "scrape_university", SimpleNamespace(delay=delay))

    async def owned_db():
        session = _DispatchSession(events)
        sessions.append(session)
        yield session

    app = FastAPI()  # No production lifespan, worker startup, or shared overrides.
    app.include_router(router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = owned_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "isolated-test", "permissions": ["scraping.trigger"],
    }
    body = (
        {"university_id": 42, "fast_mode": False}
        if endpoint == "start"
        else {"university_ids": [43, 41, 42], "fast_mode": False}
    )
    jobs_per_request = 1 if endpoint == "start" else 3
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://isolated.test",
        ) as client:
            baseline = len(os.listdir("/proc/self/fd"))
            baseline_connections = observer.info("clients")["connected_clients"]
            for _ in range(32):
                response = await client.post(f"/api/scrape/{endpoint}", json=body)
                assert response.status_code == 202, response.text
                session = sessions[-1]
                assert len(session.rows) == jobs_per_request
                assert all(row.status == "queued" for row in session.rows)
                assert session.committed_ids == {
                    row.runtime_job_id for row in session.rows
                }
                if endpoint == "start":
                    job_id = session.rows[0].runtime_job_id
                    assert response.json() == {
                        "jobId": job_id, "runtimeJobId": job_id,
                        "status": "queued", "reused": False, "ok": True,
                    }
                else:
                    assert response.json() == {
                        "sessionId": session.rows[0].request_payload["session_id"],
                        "queued": 3, "ok": True,
                    }
                assert len(os.listdir("/proc/self/fd")) <= baseline
                assert lock_spy.call_count == len(delivered_ids)
                assert len(retained_clients) == len(delivered_ids)
                for row in session.rows:
                    key = st._requeue_lock_key(row.runtime_job_id)
                    if row.runtime_job_id in delivered_ids:
                        assert observer.get(key) == "1"
                        assert 0 < observer.ttl(key) <= st._REQUEUE_LOCK_TTL_S + 30
                    else:
                        assert observer.get(key) is None

            assert delay.call_count == 32 * jobs_per_request
            total = 32 * jobs_per_request
            expected_deliveries = (
                total if delivery == "success"
                else 0 if delivery == "failure"
                else total // 2
            )
            # Assertions raised inside dispatch can be swallowed by the route's
            # broad delivery exception handler; verify every outcome outside it.
            assert len(delivered_ids) == expected_deliveries
            assert len(failed_ids) == total - expected_deliveries
            assert len(set(delivered_ids + failed_ids)) == total
            assert [call.args[0] for call in lock_spy.call_args_list] == delivered_ids
            assert set(delivered_ids).isdisjoint(failed_ids)
            assert observer.dbsize() == len(delivered_ids)
            assert observer.info("clients")["connected_clients"] == baseline_connections
            for owned_client in retained_clients:
                owned_client.close.assert_called_once_with()
    finally:
        for owned_client in retained_clients:
            owned_client.connection_pool.disconnect()
        if gc_was_enabled:
            gc.enable()
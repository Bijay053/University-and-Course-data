"""Initial API dispatch owns its Redis client, but must leave the lock intact."""
from __future__ import annotations

from datetime import datetime, timezone
import gc
import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, call

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


class _ActiveDispatchSession(_DispatchSession):
    """Owned active rows, exercising the real router's lock and lookup calls."""

    def __init__(self, events, active_jobs):
        super().__init__(events)
        self.active_jobs = active_jobs
        self.locked_ids = []
        self.looked_up_ids = []

    async def execute(self, statement, params=None):
        from app.models import ScrapeRuntimeJob
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            assert str(statement) == "SELECT pg_advisory_xact_lock(:uid)"
            self.locked_ids.append(params["uid"])
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        assert statement.column_descriptions[0]["entity"] is ScrapeRuntimeJob
        uid = statement.compile().params["university_id_1"]
        assert self.locked_ids[-1] == uid
        self.looked_up_ids.append(uid)
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.active_jobs.get(uid),
        )


@pytest.fixture
def active_dispatch_http(monkeypatch):
    """Private app and sessions; neither PostgreSQL nor Celery/Redis is used."""
    from app.dependencies import get_current_user, get_db
    from app.routers.scrape import router
    from app.tasks import scrape_tasks as st

    events = []
    sessions = []
    active_jobs = {}
    delay = MagicMock(side_effect=lambda jid: events.append(("delivered", jid)))
    lock = MagicMock(side_effect=lambda jid: events.append(("lock", jid)))
    monkeypatch.setattr(st, "scrape_university", SimpleNamespace(delay=delay))
    monkeypatch.setattr(st, "set_initial_dispatch_lock", lock)

    async def owned_db():
        session = _ActiveDispatchSession(events, active_jobs)
        sessions.append(session)
        yield session

    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = owned_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "isolated-test", "permissions": ["scraping.trigger"],
    }
    return SimpleNamespace(
        app=app, events=events, sessions=sessions, active_jobs=active_jobs,
        delay=delay, lock=lock,
    )


def _owned_active_job(uid, status):
    from app.models import ScrapeRuntimeJob

    return ScrapeRuntimeJob(
        runtime_job_id=f"job_active_{uid}", university_id=uid,
        university_name=f"Isolated University {uid}",
        url=f"https://university-{uid}.example/courses", job_type="single",
        status=status, created_at=datetime.now(timezone.utc), request_payload={},
    )


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("active_status", ["queued", "running", "awaiting_approval"])
async def test_repeated_start_reuses_active_job_without_delivery_or_lock(
    active_dispatch_http, active_status,
):
    owned = active_dispatch_http
    active = _owned_active_job(42, active_status)
    owned.active_jobs[42] = active
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owned.app), base_url="http://isolated.test",
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/api/scrape/start", json={"university_id": 42, "fast_mode": False},
            )
            assert response.status_code == 202, response.text
            assert response.json() == {
                "jobId": active.runtime_job_id,
                "runtimeJobId": active.runtime_job_id,
                "status": active_status, "reused": True, "ok": True,
            }
            session = owned.sessions[-1]
            assert session.locked_ids == session.looked_up_ids == [42]
            assert session.rows == []
            assert session.committed_ids == set()
            owned.delay.assert_not_called()
            owned.lock.assert_not_called()

    assert owned.events == [("commit", ()), ("commit", ())]
    assert active.status == active_status
    assert active.request_payload == {}


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("all_active", [False, True], ids=["mixed", "all-active"])
async def test_bulk_skips_active_jobs_without_delivery_or_lock(
    active_dispatch_http, all_active,
):
    owned = active_dispatch_http
    statuses = {41: "queued", 42: "running", 43: "awaiting_approval"}
    active_ids = set(statuses) if all_active else {42}
    owned.active_jobs.update({
        uid: _owned_active_job(uid, statuses[uid]) for uid in active_ids
    })
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owned.app), base_url="http://isolated.test",
    ) as client:
        response = await client.post(
            "/api/scrape/bulk",
            json={"university_ids": [43, 41, 42], "fast_mode": False},
        )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["sessionId"].startswith("bulk_")
    assert payload == {
        "sessionId": payload["sessionId"], "queued": 0 if all_active else 2,
        "ok": True,
    }
    session, = owned.sessions
    assert session.locked_ids == session.looked_up_ids == [41, 42, 43]
    assert [row.university_id for row in session.rows] == ([] if all_active else [41, 43])
    new_ids = [row.runtime_job_id for row in session.rows]
    assert len(set(new_ids)) == len(new_ids)
    assert set(new_ids).isdisjoint(
        active.runtime_job_id for active in owned.active_jobs.values()
    )
    assert session.committed_ids == set(new_ids)
    for row in session.rows:
        assert row.status == "queued"
        assert row.request_payload["session_id"] == payload["sessionId"]
    # Exact external assertions catch both extra active deliveries and swallowed
    # dispatch/lock errors, including the no-work all-active response.
    assert owned.delay.call_args_list == [call(jid) for jid in new_ids]
    assert owned.lock.call_args_list == [call(jid) for jid in new_ids]
    assert owned.events[0][0] == "commit"
    assert owned.events[1:] == [
        event for jid in new_ids for event in [("delivered", jid), ("lock", jid)]
    ]
    if all_active:
        owned.delay.assert_not_called()
        owned.lock.assert_not_called()
    for uid, active in owned.active_jobs.items():
        assert active.status == statuses[uid]
        assert active.request_payload == {}


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("endpoint", ["start", "bulk"])
@pytest.mark.parametrize(
    ("delivery", "lock_outcome"),
    [
        ("success", "success"),
        ("failure", "success"),
        ("alternating", "success"),
        ("success", "command_failure"),
        ("success", "client_creation_failure"),
        ("success", "close_failure"),
        ("success", "nx_collision"),
    ],
)
async def test_repeated_api_dispatch_keeps_queued_contract_and_releases_redis(
    monkeypatch, isolated_redis, endpoint, delivery, lock_outcome,
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
    set_results = []
    set_failures = []
    existing_expiries = {}
    creation_failures = []
    close_attempts = []
    written_expiries = {}

    def forbid_tcp(*args, **kwargs):
        pytest.fail("API dispatch test attempted a non-disposable Redis connection")

    # UnixDomainSocketConnection does not inherit the TCP Connection class.
    monkeypatch.setattr(redis.Connection, "connect", forbid_tcp)

    def redis_factory():
        if lock_outcome == "client_creation_failure":
            # Exercise the actual helper's factory boundary, before a client
            # exists. Record externally so broad exception handlers cannot
            # conceal a missing helper invocation or delivery-order failure.
            creation_failures.append(events[-1])
            raise redis.ConnectionError("client unavailable")
        client = redis.Redis(
            unix_socket_path=socket_path, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
        real_close = client.close

        def observed_close():
            close_attempts.append(client)
            if lock_outcome == "close_failure":
                # Raise before releasing the real connection. Test teardown,
                # not a finalizer or the failing close(), must disconnect it.
                raise redis.ConnectionError("close unavailable")
            return real_close()

        client.close = MagicMock(side_effect=observed_close)
        client.connection_pool.disconnect = MagicMock(
            wraps=client.connection_pool.disconnect,
        )
        real_set = client.set

        def observed_set(name, value, **kwargs):
            if lock_outcome == "command_failure":
                # Connect to private Redis and fail the actual SET command.
                kwargs["ex"] = 0
            try:
                result = real_set(name, value, **kwargs)
            except redis.ResponseError:
                set_failures.append(name)
                raise
            set_results.append((name, result))
            if lock_outcome == "close_failure":
                written_expiries[name] = observer.pexpiretime(name)
            return result

        client.set = MagicMock(side_effect=observed_set)
        client.delete = MagicMock(wraps=client.delete)
        retained_clients.append(client)
        return client

    real_lock = st.set_initial_dispatch_lock
    # Patch the actual helper globals, including after import/reload tests.
    factory_spy = MagicMock(side_effect=redis_factory)
    monkeypatch.setitem(real_lock.__globals__, "_get_redis", factory_spy)

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
        if lock_outcome == "nx_collision":
            key = st._requeue_lock_key(job_id)
            observer.set(key, "existing-owner", ex=600)
            existing_expiries[key] = observer.pexpiretime(key)
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
    baseline = baseline_connections = None
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
                retained_connections = len(retained_clients) if lock_outcome == "close_failure" else 0
                assert len(os.listdir("/proc/self/fd")) <= baseline + retained_connections
                assert lock_spy.call_count == len(delivered_ids)
                assert factory_spy.call_count == len(delivered_ids)
                assert len(retained_clients) == (
                    0 if lock_outcome == "client_creation_failure" else len(delivered_ids)
                )
                for row in session.rows:
                    key = st._requeue_lock_key(row.runtime_job_id)
                    if lock_outcome == "nx_collision":
                        assert observer.get(key) == "existing-owner"
                        assert observer.pexpiretime(key) == existing_expiries[key]
                    elif lock_outcome in {"success", "close_failure"} and row.runtime_job_id in delivered_ids:
                        assert observer.get(key) == "1"
                        assert 0 < observer.ttl(key) <= st._REQUEUE_LOCK_TTL_S + 30
                        if lock_outcome == "close_failure":
                            assert observer.pexpiretime(key) == written_expiries[key]
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
            assert len(sessions) == 32
            assert len({id(session) for session in sessions}) == 32
            assert factory_spy.call_args_list == [call() for _ in delivered_ids]
            delivered_keys = [st._requeue_lock_key(job_id) for job_id in delivered_ids]
            # Check outcomes outside the best-effort helper/route exception
            # handlers, which could otherwise swallow a failed mock assertion.
            if lock_outcome == "client_creation_failure":
                assert creation_failures == [("lock", jid) for jid in delivered_ids]
                assert retained_clients == []
                assert set_results == set_failures == []
                assert observer.dbsize() == 0
            elif lock_outcome == "command_failure":
                assert set_failures == delivered_keys
                assert set_results == []
                assert observer.dbsize() == 0
            else:
                assert set_failures == []
                expected_result = None if lock_outcome == "nx_collision" else True
                assert set_results == [(key, expected_result) for key in delivered_keys]
                assert observer.dbsize() == len(delivered_ids)
            if lock_outcome == "nx_collision":
                assert set(existing_expiries) == set(delivered_keys)
            assert close_attempts == retained_clients
            retained_connections = len(retained_clients) if lock_outcome == "close_failure" else 0
            assert observer.info("clients")["connected_clients"] == baseline_connections + retained_connections
            if lock_outcome == "close_failure":
                assert set(written_expiries) == set(delivered_keys)
                for key in delivered_keys:
                    assert observer.get(key) == "1"
                    assert observer.pexpiretime(key) == written_expiries[key]
                for owned_client in retained_clients:
                    owned_client.connection_pool.disconnect.assert_not_called()
            client_ids = [] if lock_outcome == "client_creation_failure" else delivered_ids
            for job_id, owned_client in zip(client_ids, retained_clients, strict=True):
                owned_client.set.assert_called_once_with(
                    st._requeue_lock_key(job_id), "1",
                    nx=True, ex=st._REQUEUE_LOCK_TTL_S + 30,
                )
                owned_client.close.assert_called_once_with()
                owned_client.delete.assert_not_called()
    finally:
        try:
            # Direct pool teardown still runs when assertions or injected
            # close() fail; all clients remain retained until this point.
            for owned_client in retained_clients:
                owned_client.connection_pool.disconnect()
            if lock_outcome == "close_failure":
                for owned_client in retained_clients:
                    owned_client.connection_pool.disconnect.assert_called_once_with()
                if baseline is not None:
                    assert len(os.listdir("/proc/self/fd")) <= baseline
                    # Redis observes socket EOF asynchronously; boundedly wait
                    # for server-side accounting, without masking a leak.
                    deadline = time.monotonic() + 5
                    while observer.info("clients")["connected_clients"] != baseline_connections:
                        assert time.monotonic() < deadline, "retained Redis connections survived teardown"
                        time.sleep(0.01)
        finally:
            if gc_was_enabled:
                gc.enable()
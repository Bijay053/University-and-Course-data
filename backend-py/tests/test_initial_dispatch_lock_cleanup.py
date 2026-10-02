"""Initial API dispatch owns its Redis client, but must leave the lock intact."""
from __future__ import annotations

import gc
import os
import subprocess
import time
from unittest.mock import MagicMock

import pytest
import redis


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
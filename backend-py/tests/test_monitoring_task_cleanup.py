"""Watcher boundaries: no worker restarts, live writes, or broker dispatch."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.parametrize("failure", ["none", "enter", "cycle", "cancel"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_watcher_boundary_preserves_outcomes_and_session_order(monkeypatch, failure, close_fails):
    from app.tasks import monitoring_tasks as tasks
    from app.services import monitoring_engine as service

    events, loops = [], []
    payload = {"checked": 4, "changed": 2, "triggered": 1, "errors": 1}

    async def enter():
        events.append("enter")
        if failure == "enter":
            raise ValueError("original")
        return session

    async def exit(*_):
        events.append("exit")

    async def cycle(db):
        assert db is session
        events.append("cycle")
        if failure == "cycle":
            raise ValueError("original")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return payload

    async def dispose():
        loops.append(asyncio.get_running_loop())
        assert not loops[-1].is_closed()
        events.append("dispose")
        if close_fails:
            raise RuntimeError("close failed")

    engine = SimpleNamespace(
        sync_engine=SimpleNamespace(dispose=MagicMock(
            side_effect=lambda **_: events.append("invalidate"))),
        dispose=AsyncMock(side_effect=dispose),
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(side_effect=enter)
    session.__aexit__ = AsyncMock(side_effect=exit)
    monkeypatch.setattr(service, "run_monitoring_cycle", cycle)
    # Celery may retain a registered function across module reloads. Patch
    # its actual globals to keep this test off the application engine.
    task = tasks.check_watchers
    globals_ = getattr(task.run, "__func__", task.run).__globals__
    monkeypatch.setitem(globals_, "engine", engine)
    monkeypatch.setitem(globals_, "AsyncSessionLocal", lambda: session)
    monkeypatch.setitem(globals_, "_async_check_watchers", tasks._async_check_watchers)
    monkeypatch.setattr(tasks, "AsyncSessionLocal", lambda: session)
    retry = MagicMock(side_effect=AssertionError("watcher must not retry"))
    monkeypatch.setattr(task, "retry", retry)

    for _ in range(3):
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                task.run()
        else:
            result = task.run()
            assert result == (payload if failure == "none" else {
                "error": "original", "checked": 0, "changed": 0, "triggered": 0,
            })
    expected = ["invalidate", "enter"]
    if failure != "enter":
        expected += ["cycle", "exit"]
    assert events == (expected + ["dispose"]) * 3
    assert len(set(loops)) == 3
    assert all(loop.is_closed() for loop in loops)
    assert all(call.kwargs == {"close": False} for call in engine.sync_engine.dispose.call_args_list)
    retry.assert_not_called()
    assert task.max_retries == 0


def test_watcher_does_not_claim_scraper_resource_ownership(monkeypatch):
    from app.tasks import monitoring_tasks as tasks

    payload = {"checked": 0, "changed": 0, "triggered": 0, "errors": 0}
    async def cycle():
        return payload

    def runner(coro, *, engine, fetch=False):
        assert engine is sentinel
        assert fetch is False
        return asyncio.run(coro)

    sentinel = object()
    task = tasks.check_watchers
    globals_ = getattr(task.run, "__func__", task.run).__globals__
    monkeypatch.setitem(globals_, "engine", sentinel)
    monkeypatch.setitem(globals_, "run_task_coro", runner)
    monkeypatch.setitem(globals_, "_async_check_watchers", cycle)
    assert task.run() == payload


def test_invalidation_failure_returns_error_without_starting_loop(monkeypatch):
    from app.tasks import monitoring_tasks as tasks

    engine = SimpleNamespace(
        sync_engine=SimpleNamespace(dispose=MagicMock(side_effect=ValueError("entry failed"))),
        dispose=AsyncMock(),
    )
    operation = AsyncMock()
    task = tasks.check_watchers
    globals_ = getattr(task.run, "__func__", task.run).__globals__
    monkeypatch.setitem(globals_, "engine", engine)
    monkeypatch.setitem(globals_, "_async_check_watchers", operation)
    assert task.run() == {
        "error": "entry failed", "checked": 0, "changed": 0, "triggered": 0,
    }
    operation.assert_not_awaited()
    engine.dispose.assert_not_awaited()


@pytest.mark.parametrize("dispatch_fails", [False, True])
async def test_monitor_dispatch_commits_before_send_and_keeps_error_result(monkeypatch, dispatch_fails):
    from app.services.monitoring_engine import trigger_scrape
    from app.tasks import scrape_tasks

    events = []
    watcher = SimpleNamespace(university_id=42, total_scrapes_triggered=3)
    uni = SimpleNamespace(
        id=42, name="Isolated", country="GB", website="https://example.test",
        scrape_url="https://example.test/courses",
    )
    advisory, active, university = MagicMock(), MagicMock(), MagicMock()
    active.scalar_one_or_none.return_value = None
    university.scalar_one_or_none.return_value = uni
    rows = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[advisory, active, university]),
        add=rows.append,
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )

    def dispatch(job_id):
        assert events == ["commit"]
        assert rows[0].runtime_job_id == job_id
        events.append("dispatch")
        if dispatch_fails:
            raise RuntimeError("isolated broker failure")

    delay = MagicMock(side_effect=dispatch)
    lock = MagicMock(side_effect=lambda job_id: events.append("lock"))
    monkeypatch.setattr(scrape_tasks.scrape_university, "delay", delay)
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", lock)
    result = await trigger_scrape(watcher, db)
    job = rows[0]
    assert result == job.runtime_job_id == watcher.last_scrape_job_id
    assert watcher.total_scrapes_triggered == 4
    assert job.status == "queued" and job.fast_mode is False
    assert job.url == uni.scrape_url
    assert job.request_payload == {
        "url": uni.scrape_url, "universityId": 42, "universityName": "Isolated",
        "universityCountry": "GB", "fastMode": False, "triggeredBy": "monitor",
    }
    assert events == ["commit", "dispatch"] + ([] if dispatch_fails else ["lock"])
    delay.assert_called_once_with(result)
    if dispatch_fails:
        lock.assert_not_called()
    else:
        lock.assert_called_once_with(result)


def test_isolated_watcher_database_and_socket_acceptance():
    script = Path(__file__).parent / "helpers" / "monitoring_loop_fd_probe.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["loops"] == 36
    assert report["max_fds"] <= report["baseline_fds"]
    assert report["remaining_db_sessions"] == 0
    assert report["remaining_tcp_sockets"] == 0

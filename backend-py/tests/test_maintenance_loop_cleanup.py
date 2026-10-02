"""Maintenance boundaries: no job dispatch, database writes, or worker restart."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tasks import loop_resources as lr


@pytest.fixture
def resources(monkeypatch):
    from app import database
    from app.services.scraper import http_fetcher, scrape_do_semaphore

    events, loops = [], []

    async def close(label):
        loops.append(asyncio.get_running_loop())
        assert not loops[-1].is_closed()
        events.append(label)

    def closer(label):
        async def run():
            await close(label)
        return AsyncMock(side_effect=run)

    engine = SimpleNamespace(
        sync_engine=SimpleNamespace(dispose=MagicMock(side_effect=lambda **_: events.append("invalidate"))),
        dispose=closer("db"),
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(http_fetcher, "close_shared_client_for_current_loop",
                        closer("http"))
    monkeypatch.setattr(scrape_do_semaphore, "close_client_for_current_loop",
                        closer("redis"))
    return engine, events, loops


@pytest.mark.parametrize("fetch", [False, True])
@pytest.mark.parametrize("error", [None, ValueError("original"), asyncio.CancelledError()])
def test_boundaries_preserve_results_and_failures(resources, fetch, error):
    engine, events, loops = resources

    async def work():
        events.append("session entered")
        try:
            if error:
                raise error
            return {"unchanged": True}
        finally:
            events.append("session exited")

    for _ in range(3):
        if error:
            with pytest.raises(type(error)):
                lr.run_task_coro(work(), engine=engine, fetch=fetch)
        else:
            assert lr.run_task_coro(work(), engine=engine, fetch=fetch) == {"unchanged": True}
    assert events == (["invalidate", "session entered", "session exited"]
                      + (["http", "redis"] if fetch else []) + ["db"]) * 3
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("error", [RuntimeError("close failed"), asyncio.CancelledError(), TimeoutError()])
@pytest.mark.parametrize("fail", [False, True])
def test_cleanup_failure_is_isolated_and_bounded(resources, monkeypatch, error, fail):
    _, events, _ = resources
    wait = asyncio.wait_for
    count = 0

    async def broken_first_close(coro, timeout):
        nonlocal count
        assert timeout == lr.CLOSE_TIMEOUT_SECONDS
        count += 1
        if count == 1:
            coro.close()
            raise error
        return await wait(coro, timeout)

    monkeypatch.setattr(lr.asyncio, "wait_for", broken_first_close)

    async def work():
        if fail:
            raise ValueError("original")
        return 7

    if fail:
        with pytest.raises(ValueError, match="original"):
            lr.run_task_coro(work(), fetch=True)
    else:
        assert lr.run_task_coro(work(), fetch=True) == 7
    assert events == ["invalidate", "redis", "db"]


TASKS = [
    ("auto_repair_task", "generate_repair_suggestion", "_run", (7,), True, True),
    ("auto_repair_task", "monitor_ai_scrape_repair", "_reconcile_autonomous_repair",
     ("no-job", "session"), False, True),
    ("auto_repair_task", "recover_ai_repair_workflows", "_recover_ai_repair_workflows", (), False, True),
    ("health_snapshot", "snapshot_health_daily", "_run", (), False, False),
    ("snapshot_tasks", "snapshot_editable_tables", "_async_run_snapshot", (), False, False),
]


@pytest.mark.parametrize("module,name,operation,args,fetch,retry", TASKS)
@pytest.mark.parametrize("fail", [False, True])
def test_task_cleanup_precedes_result_or_retry(
    resources, monkeypatch, module, name, operation, args, fetch, retry, fail,
):
    import importlib
    engine, events, loops = resources
    mod = importlib.import_module(f"app.tasks.{module}")
    if hasattr(mod, "engine"):
        monkeypatch.setattr(mod, "engine", engine)

    async def work(*_, **__):
        events.append("work")
        if fail:
            raise ValueError("original")
        return {"ok": True}

    monkeypatch.setattr(mod, operation, work)
    task = getattr(mod, name)
    # Import-smoke tests reload task modules, but Celery retains the originally
    # registered function. Patch its real globals too; otherwise a stale task
    # can bypass the stub and execute a real snapshot against development data.
    task_globals = getattr(task.run, "__func__", task.run).__globals__
    monkeypatch.setitem(task_globals, operation, work)
    if "engine" in task_globals:
        monkeypatch.setitem(task_globals, "engine", engine)
    request_retry = MagicMock(side_effect=RuntimeError("retry"))
    monkeypatch.setattr(task, "retry", request_retry)
    if fail and module == "snapshot_tasks":
        with pytest.raises(ValueError, match="original"):
            task.run(*args)
    elif fail and retry:
        with pytest.raises(RuntimeError, match="retry"):
            task.run(*args)
        assert str(request_retry.call_args.kwargs["exc"]) == "original"
    else:
        result = task.run(*args)
        if fail:
            assert "original" in result["error"]
        else:
            assert result == {"ok": True}
    assert events == ["invalidate", "work"] + (["http", "redis"] if fetch else []) + ["db"]
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("outcome", ["autonomous", "legacy", "claim_failed", "legacy_failed", "autonomous_failed"])
def test_repair_multi_loop_paths_keep_claim_and_lease_fencing(resources, monkeypatch, outcome):
    from app.tasks import auto_repair_task as task
    from app.services.scraper import ai_repair_agent as agent

    _, events, loops = resources
    calls = []

    async def autonomous(*args):
        calls.append(("autonomous", args))
        if outcome == "autonomous_failed":
            raise ValueError("original")
        return {"status": "verifying"} if outcome == "autonomous" else None

    async def legacy(*args):
        calls.append(("legacy", args))
        if outcome == "legacy_failed":
            raise ValueError("original")
        return {"status": "done"}

    async def audit(*args):
        calls.append(("audit", args))
        return {}

    monkeypatch.setattr(task, "_run_autonomous_repair", autonomous)
    monkeypatch.setattr(task, "_run_ai_repair", legacy)
    monkeypatch.setattr(task, "_persist_ai_repair_failure", audit)
    claim = MagicMock(return_value=outcome != "claim_failed")
    release = MagicMock()
    monkeypatch.setattr(agent, "claim_repair_session", claim)
    monkeypatch.setattr(agent, "release_repair_lease", release)
    monkeypatch.setattr(agent, "_write_session", MagicMock())
    task.run_ai_scrape_repair.push_request(id="delivery-generation")
    try:
        if outcome == "autonomous_failed":
            with pytest.raises(ValueError, match="original"):
                task.run_ai_scrape_repair.run("no-job", 7, "session")
        else:
            task.run_ai_scrape_repair.run("no-job", 7, "session")
    finally:
        task.run_ai_scrape_repair.pop_request()
    assert calls[0] == ("autonomous", ("no-job", 7, "session", "delivery-generation"))
    fetch_close = ["invalidate", "http", "redis", "db"]
    if outcome in {"autonomous", "autonomous_failed"}:
        claim.assert_not_called()
        release.assert_not_called()
        assert events == fetch_close
    else:
        claim.assert_called_once_with("no-job", 7, "session")
        release.assert_called_once_with(7, "session")
        expected = fetch_close + (["invalidate", "db"] if outcome == "claim_failed" else fetch_close)
        if outcome == "legacy_failed":
            expected += ["invalidate", "db"]
        assert events == expected
        if outcome in {"claim_failed", "legacy_failed"}:
            assert calls[-1][0] == "audit"
            assert calls[-1][1][:3] == ("no-job", 7, "session")
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("fail", [False, True])
def test_dedicated_engine_cleanup_does_not_touch_shared_engine(resources, fail):
    shared, events, _ = resources
    dedicated = SimpleNamespace(dispose=AsyncMock())

    async def work():
        async with lr.owned_engine(dedicated):
            if fail:
                raise ValueError("original")
            return 9

    if fail:
        with pytest.raises(ValueError, match="original"):
            asyncio.run(work())
    else:
        assert asyncio.run(work()) == 9
    dedicated.dispose.assert_awaited_once()
    shared.dispose.assert_not_called()
    assert events == []


@pytest.mark.parametrize("failure", [RuntimeError("dispose failed"), asyncio.CancelledError(), TimeoutError()])
@pytest.mark.parametrize("fail", [False, True])
def test_dedicated_engine_close_failure_preserves_original_result(resources, failure, fail):
    dedicated = SimpleNamespace(dispose=AsyncMock(side_effect=failure))

    async def work():
        async with lr.owned_engine(dedicated):
            if fail:
                raise ValueError("original")
            return 9

    if fail:
        with pytest.raises(ValueError, match="original"):
            asyncio.run(work())
    else:
        assert asyncio.run(work()) == 9
    dedicated.dispose.assert_awaited_once()


def test_isolated_readonly_database_and_socket_acceptance():
    script = Path(__file__).parent / "helpers" / "maintenance_loop_fd_probe.py"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["loops"] >= 80
    assert report["max_fds"] <= report["baseline_fds"]
    assert report["remaining_db_sessions"] == 0
    assert report["remaining_tcp_sockets"] == 0
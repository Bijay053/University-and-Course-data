"""Task boundaries are tested without dispatching or touching user-job rows."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def resources(monkeypatch):
    from app.tasks import scrape_tasks as st
    from app.services.scraper import browser_pool, http_fetcher, scrape_do_semaphore

    events = []
    loops = []

    def closer(label):
        async def close():
            loop = asyncio.get_running_loop()
            assert not loop.is_closed()
            loops.append(loop)
            events.append(label)
        return AsyncMock(side_effect=close)

    closes = {name: closer(name) for name in ("browser", "http", "redis", "db")}
    monkeypatch.setattr(st, "engine", SimpleNamespace(dispose=closes["db"]))
    monkeypatch.setattr(st, "_sync_dispose", lambda: events.append("invalidate"))
    monkeypatch.setattr(browser_pool.pool, "close", closes["browser"])
    monkeypatch.setattr(http_fetcher, "close_shared_client_for_current_loop", closes["http"])
    monkeypatch.setattr(scrape_do_semaphore, "close_client_for_current_loop", closes["redis"])
    return st, events, loops, closes


def session_factory(monkeypatch, st, events, db=None, error=None):
    session = MagicMock()

    async def enter():
        events.append("enter")
        if error:
            raise error
        return db

    async def exit(*_):
        events.append("exit")
        return False

    session.__aenter__ = AsyncMock(side_effect=enter)
    session.__aexit__ = AsyncMock(side_effect=exit)
    monkeypatch.setattr(st, "AsyncSessionLocal", lambda: session)
    # Some nested maintenance coroutines import the factory locally.
    from app import database
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: session)
    return session


@pytest.mark.parametrize("error", [None, RuntimeError("repair failed"), asyncio.CancelledError()])
def test_repair_cleanup_follows_session_exit_and_preserves_failure(resources, monkeypatch, error):
    st, events, loops, _ = resources
    db = object()
    session_factory(monkeypatch, st, events, db)
    run = AsyncMock(side_effect=error)
    monkeypatch.setattr(st, "run_repair", run)
    if error:
        with pytest.raises(type(error)):
            asyncio.run(st._async_repair("isolated"))
    else:
        assert asyncio.run(st._async_repair("isolated")) is None
    run.assert_awaited_once_with(db, "isolated")
    assert events == ["enter", "exit", "browser", "http", "redis", "db"]
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("outcome", ["unclaimed", "missing", "failure"])
def test_bulk_fix_cleanup_includes_early_returns(resources, monkeypatch, outcome):
    from app.services.scraper import job_claim

    st, events, _, _ = resources
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    session_factory(monkeypatch, st, events, db)
    monkeypatch.setattr(job_claim, "claim_runtime_job", AsyncMock(
        return_value=outcome != "unclaimed",
        side_effect=RuntimeError("claim failed") if outcome == "failure" else None,
    ))
    if outcome == "failure":
        with pytest.raises(RuntimeError, match="claim failed"):
            asyncio.run(st._async_bulk_fix("isolated"))
    else:
        assert asyncio.run(st._async_bulk_fix("isolated")) is None
    assert events == ["enter", "exit", "browser", "http", "redis", "db"]


@pytest.mark.parametrize("scraper", [False, True])
def test_repeated_loop_runner_preserves_results_and_exceptions(resources, scraper):
    st, events, loops, _ = resources

    async def work(fail):
        events.append("work")
        if fail:
            raise ValueError("original error")
        return {"unchanged": True}

    runner = st._run_scraper_coro if scraper else st._run_db_coro
    for _ in range(4):
        assert runner(work(False)) == {"unchanged": True}
        with pytest.raises(ValueError, match="original error"):
            runner(work(True))
    expected = ["invalidate", "work"] + (["browser", "http", "redis"] if scraper else []) + ["db"]
    assert events == expected * 8
    assert all(loop.is_closed() for loop in loops)
    assert len(set(loops)) == 8


@pytest.mark.parametrize("failure", [False, True])
def test_cleanup_failure_does_not_mask_work_or_skip_other_resources(resources, failure):
    st, events, _, closes = resources
    closes["browser"].side_effect = RuntimeError("browser close failed")
    closes["http"].side_effect = asyncio.CancelledError()

    async def work():
        if failure:
            raise ValueError("work failed")
        return 42

    if failure:
        with pytest.raises(ValueError, match="work failed"):
            st._run_scraper_coro(work())
    else:
        assert st._run_scraper_coro(work()) == 42
    assert events == ["invalidate", "redis", "db"]


def test_cleanup_timeouts_remain_bounded_and_continue(resources, monkeypatch):
    st, events, _, _ = resources
    real_wait_for = asyncio.wait_for

    async def timeout_browser(coro, timeout):
        assert timeout == 10
        if coro.cr_code.co_name == "_execute_mock_call" and not events.count("http"):
            # Timeout the first closer only; don't execute its body.
            if not events.count("timeout"):
                coro.close()
                events.append("timeout")
                raise TimeoutError()
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(st.asyncio, "wait_for", timeout_browser)

    async def work():
        return "ok"

    assert st._run_scraper_coro(work()) == "ok"
    assert events == ["invalidate", "timeout", "http", "redis", "db"]


def test_explicit_failure_loop_closes_db_after_write(resources, monkeypatch):
    st, events, loops, _ = resources
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    session_factory(monkeypatch, st, events, db)
    st._run_in_fresh_loop(st._mark_failed("not-a-user-job", "failure"))
    assert events == ["enter", "exit", "db"]
    assert all(loop.is_closed() for loop in loops)


def test_direct_preclaim_writer_never_touches_shared_pool(resources, monkeypatch):
    from app.services.scraper.job_claim import RuntimeJobClaimError
    st, events, _, _ = resources
    direct = AsyncMock(return_value=True)
    monkeypatch.setattr(st, "fail_queued_runtime_job_direct", direct)
    st._handle_preclaim_failure("isolated", RuntimeJobClaimError("claim failed"))
    direct.assert_awaited_once_with("isolated", "claim failed")
    assert events == []


WRAPPERS = [
    ("probe_and_configure", (123,), {}, True, False),
    ("repair_extractor", (123,), {}, True, True),
    ("run_quality_actions", (123,), {"job_id": "isolated"}, True, True),
    ("repair_conflicts", (), {"job_id": "isolated"}, False, True),
    ("snapshot_storage_monitor", (), {}, False, False),
    ("refresh_baselines_weekly", (), {}, False, False),
    ("record_job_performance", (), {"university_id": 123, "job_id": "isolated"}, False, True),
]


@pytest.mark.parametrize("name,args,kwargs,scraper,retries", WRAPPERS)
def test_maintenance_wrappers_repeated_success_preserves_payloads(
    resources, monkeypatch, name, args, kwargs, scraper, retries,
):
    from app.services.scraper import review_policy

    st, events, loops, _ = resources
    row = MagicMock()
    row.one.return_value = (0, 0)
    row.scalar_one.return_value = 0
    row.all.return_value = []
    row.mappings.return_value.first.return_value = {
        "scrape_url": "https://example.test", "scrape_config": {}, "country": "GB",
    }
    db = SimpleNamespace(execute=AsyncMock(return_value=row), commit=AsyncMock())
    session_factory(monkeypatch, st, events, db)
    monkeypatch.setattr(review_policy, "blocks_automatic_followup", AsyncMock(return_value=False))
    if name == "probe_and_configure":
        from app.services.scraper import site_probe, auto_config_generator
        db.get = AsyncMock(return_value=SimpleNamespace(
            scrape_url="https://example.test", website="", scrape_config={}, name="Isolated",
        ))
        profile = SimpleNamespace(
            sample_course_urls=[], wayback_sample_urls=[], detected_apis=[], library_stack=None,
            recommended_strategy="http", strategy_confidence=0.9, is_cloudflare_blocked=False,
            is_js_spa=False, has_sitemap=True, wayback_course_count=0, to_dict=lambda: {},
        )
        monkeypatch.setattr(site_probe, "probe_site", AsyncMock(return_value=profile))
        monkeypatch.setattr(auto_config_generator, "generate_config", AsyncMock(return_value={"enabled": True}))
        expected = {"ok": True, "university_id": 123, "strategy": "http"}
    elif name == "repair_extractor":
        from app.services.scraper import ai_extractor_repair
        monkeypatch.setattr(ai_extractor_repair, "compute_field_fill_rates", AsyncMock(return_value={}))
        monkeypatch.setattr(ai_extractor_repair, "identify_failing_fields", lambda *_, **__: [])
        expected = {"ok": True, "fields_repaired": 0, "rescraped": False}
    elif name == "run_quality_actions":
        from app.services.scraper import ai_extractor_repair, quality_action_dispatcher
        monkeypatch.setattr(ai_extractor_repair, "compute_field_fill_rates", AsyncMock(return_value={}))
        monkeypatch.setattr(quality_action_dispatcher, "dispatch_quality_actions", AsyncMock(
            return_value=SimpleNamespace(to_dict=lambda: {"actions": ["isolated"]}),
        ))
        expected = {"ok": True, "job_id": "isolated", "actions": ["isolated"]}
    elif name == "repair_conflicts":
        from app.services.scraper import conflict_repair
        monkeypatch.setattr(conflict_repair, "repair_conflicts_for_job", AsyncMock(
            return_value=SimpleNamespace(
                courses_attempted=1, fields_attempted=2, fields_resolved=1, fields_unresolved=1,
                avg_confidence_before=0.5, avg_confidence_after=0.8,
            ),
        ))
        expected = {"ok": True, "job_id": "isolated", "fields_resolved": 1}
    elif name == "snapshot_storage_monitor":
        from app.services import snapshot_storage_monitor
        monkeypatch.setattr(snapshot_storage_monitor, "check_and_record_snapshot_storage",
                            AsyncMock(return_value={"status": "healthy"}))
        expected = {"ok": True, "total_snapshots": 0, "storage_health": {"status": "healthy"}}
    elif name == "refresh_baselines_weekly":
        from app.scripts import seed_baselines
        monkeypatch.setattr(seed_baselines, "seed_baselines", AsyncMock(return_value=3))
        expected = {"ok": True, "baselines_upserted": 3}
    else:
        from app.services import performance_intelligence
        monkeypatch.setattr(performance_intelligence, "compute_job_performance",
                            AsyncMock(return_value={"ok": True, "recorded": 4}))
        expected = {"ok": True, "recorded": 4}

    task = getattr(st, name)
    retry = MagicMock()
    monkeypatch.setattr(task, "retry", retry)
    for _ in range(2):
        result = task.run(*args, **kwargs)
        assert {key: result[key] for key in expected} == expected
    retry.assert_not_called()
    session_events = ["enter", "enter", "exit", "exit"] if name == "probe_and_configure" else ["enter", "exit"]
    assert events == (["invalidate"] + session_events
                      + (["browser", "http", "redis"] if scraper else []) + ["db"]) * 2
    assert len(set(loops)) == 2
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("name,args,kwargs,scraper,retries", WRAPPERS)
def test_maintenance_wrappers_cleanup_before_failure_result_or_retry(
    resources, monkeypatch, name, args, kwargs, scraper, retries,
):
    st, events, loops, _ = resources
    session_factory(monkeypatch, st, events, error=ValueError("session failed"))
    task = getattr(st, name)
    retry = MagicMock(side_effect=RuntimeError("retry requested"))
    monkeypatch.setattr(task, "retry", retry)
    if retries:
        with pytest.raises(RuntimeError, match="retry requested"):
            task.run(*args, **kwargs)
        assert str(retry.call_args.kwargs["exc"]) == "session failed"
    else:
        result = task.run(*args, **kwargs)
        assert result["ok"] is False
        assert "session failed" in str(result)
        retry.assert_not_called()
    assert events == ["invalidate", "enter"] + (["browser", "http", "redis"] if scraper else []) + ["db"]
    assert all(loop.is_closed() for loop in loops)


@pytest.mark.parametrize("fail", [False, True])
def test_stale_requeue_closes_sync_redis_even_on_lock_failure(resources, monkeypatch, fail):
    st, _, _, _ = resources
    async def empty():
        return []
    async def stale():
        return [("isolated", "repair", 0)]
    monkeypatch.setattr(st, "_async_requeue_abandoned_bulk_fixes", empty)
    monkeypatch.setattr(st, "_async_find_stale", stale)
    monkeypatch.setattr(st, "_async_increment_requeue", AsyncMock())
    redis = MagicMock()
    redis.set.side_effect = RuntimeError("lock failed") if fail else None
    redis.set.return_value = True
    monkeypatch.setattr(st, "_get_redis", lambda: redis)
    dispatch = MagicMock()
    monkeypatch.setattr(st.repair_university, "delay", dispatch)
    if fail:
        with pytest.raises(RuntimeError, match="lock failed"):
            st.requeue_stale_queued.run()
    else:
        assert st.requeue_stale_queued.run()["requeued"] == ["isolated"]
        dispatch.assert_called_once_with("isolated")
    redis.close.assert_called_once()
    redis.delete.assert_not_called()


def test_isolated_real_socket_descriptor_counts():
    """The child owns its engine/clients; no workers, jobs, or Redis keys change."""
    script = Path(__file__).parent / "helpers" / "task_loop_fd_probe.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report["loops"] == 48
    assert report["max_fds"] <= report["baseline_fds"] + 2
    assert report["final_fds"] <= report["baseline_fds"]
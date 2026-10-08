"""Release samples must return, not merely publish database DONE evidence."""
from argparse import Namespace
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from deploy import safe_restart_smoke as smoke
from app.tasks.celery_app import celery_app


def task(name="scrape.university", task_id="sample-task", job_id="sample-job"):
    return {"name": name, "id": task_id, "args": [job_id]}


def snapshot(*active, reserved=(), scheduled=()):
    return {
        "active": list(active), "reserved": list(reserved),
        "scheduled": list(scheduled), "_workers": ["worker"],
    }


@pytest.fixture
def lifecycle(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    async def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(smoke.asyncio, "sleep", sleep)
    def observe(timeout, sample_task_id):
        states = smoke._worker_snapshot(celery_app, timeout)
        result = None
        if sample_task_id:
            sample = celery_app.AsyncResult(sample_task_id)
            result = {
                "state": sample.state,
                "value": getattr(sample, "result", None),
            }
        return states, result

    monkeypatch.setattr(smoke, "_worker_observation", observe)
    return clock


@pytest.mark.asyncio
async def test_completed_database_sample_waits_for_task_return(monkeypatch, lifecycle):
    reads = iter([snapshot(task()), snapshot(task()), snapshot()])
    states = iter(["STARTED", "SUCCESS", "SUCCESS"])
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: next(reads))
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state=next(states), result={"ok": True, "id": "sample-job"},
    ))

    await smoke._wait_for_worker_idle(
        5, sample_task_id="sample-task", sample_job_id="sample-job"
    )

    assert lifecycle.sleeps == [1, 1]


@pytest.mark.asyncio
async def test_completed_sample_still_active_times_out_without_recovery(monkeypatch, lifecycle):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot(task()))
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state="SUCCESS", result={"ok": True, "id": "sample-job"},
    ))
    revoke = MagicMock()
    monkeypatch.setattr(celery_app.control, "revoke", revoke)

    with pytest.raises(smoke.SmokeFailure, match="did not return"):
        await smoke._wait_for_worker_idle(
            2, sample_task_id="sample-task", sample_job_id="sample-job"
        )
    assert lifecycle.now == 2
    revoke.assert_not_called()


@pytest.mark.asyncio
async def test_empty_inspection_without_successful_return_is_not_completion(monkeypatch, lifecycle):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot())
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(state="PENDING"))
    with pytest.raises(smoke.SmokeFailure, match="did not return"):
        await smoke._wait_for_worker_idle(
            2, sample_task_id="sample-task", sample_job_id="sample-job"
        )


@pytest.mark.asyncio
async def test_brief_restart_maintenance_must_become_idle(monkeypatch, lifecycle):
    reads = iter([
        snapshot(task("scrape.requeue_stale", "beat-task", None)),
        snapshot(),
    ])
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: next(reads))
    await smoke._wait_for_worker_idle(5)
    assert lifecycle.sleeps == [1]


@pytest.mark.asyncio
async def test_persistent_restart_activity_refuses_release(monkeypatch, lifecycle):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot(
        task("scrape.requeue_stale", "beat-task", None)
    ))
    with pytest.raises(smoke.SmokeFailure, match="did not return"):
        await smoke._wait_for_worker_idle(2)


@pytest.mark.asyncio
async def test_post_restart_initial_readiness_waits_for_complete_idle(monkeypatch, lifecycle):
    reads = iter([None, snapshot(task("scrape.requeue_stale", "beat", None)), snapshot()])

    def observe(*_):
        view = next(reads)
        if view is None:
            raise smoke.WorkerNotReady("no ping replies")
        return view, None

    monkeypatch.setattr(smoke, "_worker_observation", observe)
    await smoke._wait_for_worker_idle(5, wait_for_startup=True)
    assert lifecycle.sleeps == [1, 1]


@pytest.mark.asyncio
async def test_post_restart_readiness_is_bounded(monkeypatch, lifecycle):
    def observe(*_):
        raise smoke.WorkerNotReady("no ping replies")

    monkeypatch.setattr(smoke, "_worker_observation", observe)
    with pytest.raises(smoke.SmokeFailure, match="did not return"):
        await smoke._wait_for_worker_idle(2, wait_for_startup=True)
    assert lifecycle.now == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("wait,observed", [(False, False), (True, True)])
async def test_readiness_never_excuses_missing_established_worker(monkeypatch, lifecycle, wait, observed):
    reads = iter([snapshot(task("scrape.requeue_stale", "beat", None)), None] if observed else [None])

    def observe(*_):
        view = next(reads)
        if view is None:
            raise smoke.WorkerNotReady("no ping replies")
        return view, None

    monkeypatch.setattr(smoke, "_worker_observation", observe)
    with pytest.raises(smoke.WorkerNotReady):
        await smoke._wait_for_worker_idle(5, wait_for_startup=wait)


@pytest.mark.asyncio
async def test_startup_malformed_inspection_is_not_retried(monkeypatch, lifecycle):
    def observe(*_):
        raise smoke.SmokeFailure("incomplete active replies")

    monkeypatch.setattr(smoke, "_worker_observation", observe)
    with pytest.raises(smoke.SmokeFailure, match="incomplete"):
        await smoke._wait_for_worker_idle(5, wait_for_startup=True)
    assert not lifecycle.sleeps


def performance_task(job_id="sample-job"):
    return {
        "name": "scrape.record_job_performance", "id": "performance-task",
        "args": [], "kwargs": {"university_id": 1, "job_id": job_id},
    }


@pytest.mark.parametrize("state", ["active", "reserved", "scheduled"])
@pytest.mark.asyncio
async def test_owned_sample_performance_must_really_finish(monkeypatch, lifecycle, state):
    view = snapshot()
    view[state] = [performance_task()]
    reads = iter([view, snapshot()])
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: next(reads))
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state="SUCCESS", result={"ok": True, "id": "sample-job"},
    ))
    await smoke._wait_for_worker_idle(5, sample_task_id="sample-task", sample_job_id="sample-job")
    assert lifecycle.sleeps == [1]


@pytest.mark.parametrize("bad", [
    performance_task("other-job"),
    {**performance_task(), "kwargs": "{'job_id':'sample-job'}"},
    {**performance_task(), "args": ["sample-job"]},
    {**performance_task(), "kwargs": {"job_id": "sample-job"}},
    {**performance_task(), "kwargs": {"job_id": "sample-job", "university_id": True}},
])
@pytest.mark.parametrize("state", ["active", "reserved", "scheduled"])
@pytest.mark.asyncio
async def test_unproven_performance_ownership_refuses_release(monkeypatch, lifecycle, bad, state):
    view = snapshot()
    view[state] = [bad]
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: view)
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state="SUCCESS", result={"ok": True, "id": "sample-job"},
    ))
    with pytest.raises(smoke.SmokeFailure, match="unrelated worker task"):
        await smoke._wait_for_worker_idle(5, sample_task_id="sample-task", sample_job_id="sample-job")
    assert not lifecycle.sleeps


@pytest.mark.asyncio
async def test_performance_is_not_allowed_without_owned_sample(monkeypatch, lifecycle):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot(performance_task()))
    with pytest.raises(smoke.SmokeFailure, match="unrelated worker task"):
        await smoke._wait_for_worker_idle(5)


@pytest.mark.asyncio
async def test_owned_performance_timeout_still_refuses_release(monkeypatch, lifecycle):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot(performance_task()))
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state="SUCCESS", result={"ok": True, "id": "sample-job"},
    ))
    with pytest.raises(smoke.SmokeFailure, match="did not return"):
        await smoke._wait_for_worker_idle(2, sample_task_id="sample-task", sample_job_id="sample-job")


def test_observer_preserves_only_explicit_not_ready_marker(monkeypatch):
    monkeypatch.setattr(smoke, "_run", lambda *a, **k: '{"not_ready":true}')
    with pytest.raises(smoke.WorkerNotReady):
        smoke._worker_observation(5, None)
    monkeypatch.setattr(smoke, "_run", lambda *a, **k: '{"not_ready":true,"active":[]}')
    with pytest.raises(smoke.SmokeFailure, match="observation deadline"):
        smoke._worker_observation(5, None)


def test_observer_loads_its_own_reviewed_helper(monkeypatch):
    def run(command, **_):
        assert command[-1] == str(Path(smoke.__file__).resolve())
        compile(command[3], "<worker-observer>", "exec")
        return '{"states":{"active":[],"reserved":[],"scheduled":[],"_workers":["worker"]},"result":null}'

    monkeypatch.setattr(smoke, "_run", run)
    assert smoke._worker_observation(5, None)[0] == snapshot()


def test_precheckout_smoke_uses_target_without_modifying_installed_helper():
    source = (Path(smoke.__file__).parent / "guarded_release.sh").read_text()
    fence = source.index('verify "$repo_root" "$predecessor" "$target"')
    extract = source.index('"$target:backend-py/deploy/safe_restart_smoke.py" > "$smoke_helper"')
    run = source.index('"$python_bin" -B "$smoke_helper"')
    cleanup = source.index('rm -f "$smoke_helper"', run)
    pause = source.index("# Pause consumption")
    assert fence < extract < run < cleanup < pause
    assert 'trap \'rm -f "$smoke_helper"\' EXIT' in source


@pytest.mark.asyncio
async def test_disappearing_worker_is_not_an_idle_worker(monkeypatch, lifecycle):
    first = snapshot(task("scrape.requeue_stale", "beat-task", None))
    first["_workers"] = ["worker", "other-worker"]
    reads = iter([first, snapshot()])
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: next(reads))
    with pytest.raises(smoke.SmokeFailure, match="worker set changed"):
        await smoke._wait_for_worker_idle(5)


@pytest.mark.parametrize("state", ["active", "reserved", "scheduled"])
@pytest.mark.parametrize("unrelated", [
    task(task_id="business-task", job_id="user-job"),
    task("scrape.repair", "business-task", "user-job"),
    task("unknown.maintenance", "unknown-task", None),
    task(job_id="wrong-job"),
])
@pytest.mark.asyncio
async def test_unrelated_work_fails_immediately_in_every_state(
    monkeypatch, lifecycle, state, unrelated
):
    view = snapshot()
    view[state] = [unrelated]
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: view)
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(state="PENDING"))
    with pytest.raises(smoke.SmokeFailure, match="unrelated worker task"):
        await smoke._wait_for_worker_idle(
            5, sample_task_id="sample-task", sample_job_id="sample-job"
        )
    assert not lifecycle.sleeps


@pytest.mark.parametrize("result_state,value", [
    ("FAILURE", None), ("REVOKED", None),
    ("SUCCESS", {"ok": False, "id": "sample-job"}),
    ("SUCCESS", {"ok": True, "id": "other-job"}),
])
@pytest.mark.asyncio
async def test_failed_or_mismatched_sample_result_refuses_release(
    monkeypatch, lifecycle, result_state, value
):
    monkeypatch.setattr(smoke, "_worker_snapshot", lambda *_: snapshot())
    monkeypatch.setattr(celery_app, "AsyncResult", lambda _: SimpleNamespace(
        state=result_state, result=value
    ))
    with pytest.raises(smoke.SmokeFailure, match="sample Celery task"):
        await smoke._wait_for_worker_idle(
            5, sample_task_id="sample-task", sample_job_id="sample-job"
        )


@pytest.mark.parametrize("reply", [None, {}, {"worker-a": []}, {"worker-a": [], "worker-b": None}])
def test_uninspectable_or_partial_worker_replies_fail(reply):
    inspect = MagicMock()
    inspect.ping.return_value = {"worker-a": {"ok": "pong"}, "worker-b": {"ok": "pong"}}
    inspect.reserved.return_value = reply
    with pytest.raises(smoke.SmokeFailure, match="cannot inspect"):
        smoke._worker_snapshot(SimpleNamespace(
            control=SimpleNamespace(inspect=lambda **_: inspect)
        ), 1)


def test_snapshot_preserves_scheduled_task_identity():
    inspect = MagicMock()
    inspect.ping.return_value = {"worker": {"ok": "pong"}}
    inspect.reserved.return_value = {"worker": []}
    inspect.active.return_value = {"worker": []}
    inspect.scheduled.return_value = {"worker": [{"eta": "later", "request": task()}]}
    view = smoke._worker_snapshot(SimpleNamespace(
        control=SimpleNamespace(inspect=lambda **_: inspect)
    ), 1)
    assert view == snapshot(scheduled=[task()])


def test_observer_timeout_cannot_touch_worker_or_queues(monkeypatch):
    run = MagicMock(side_effect=subprocess.TimeoutExpired("observer", 2))
    monkeypatch.setattr(smoke, "_run", run)
    with pytest.raises(smoke.SmokeFailure, match="observation deadline"):
        smoke._worker_observation(2, "sample-task")
    command = run.call_args.args[0]
    assert run.call_args.kwargs["timeout"] == 2
    assert command[:3] == [smoke.sys.executable, "-B", "-c"]
    assert "revoke" not in command[3]
    assert "purge" not in command[3]
    assert "shutdown" not in command[3]


@pytest.mark.asyncio
async def test_smoke_success_is_printed_only_after_proof_and_task_return(monkeypatch, capsys):
    calls = []
    args = Namespace(
        release_identity_only=False, database_rehearsal_proof=None,
        expected_rehearsal_account_id="test", max_rehearsal_age_hours=24,
        api_health_url="test", course_url="test", university_id=1, timeout_seconds=5,
    )
    monkeypatch.setattr(smoke, "validate_database_rehearsal_requirement", lambda *_, **__: calls.append("proof"))
    monkeypatch.setattr(smoke, "_verify_release_and_services", lambda: ("revision", {}))
    monkeypatch.setattr(smoke, "_verify_api_and_celery", lambda *_: None)
    monkeypatch.setattr(smoke, "_verify_ordinary_html", lambda *_: None)
    monkeypatch.setattr(smoke, "_active_counts", AsyncMock(return_value={}))
    monkeypatch.setattr(smoke, "_resolve_university_id", AsyncMock(return_value=1))
    monkeypatch.setattr(smoke, "_create_and_dispatch", AsyncMock(return_value=("sample-job", "sample-task")))
    monkeypatch.setattr(smoke, "_wait_for_done", AsyncMock(return_value=({
        "totalFound": 1, "imported": 0, "skipped": 1,
        "skip_reasons": {"domestic_only": 1},
    }, 0)))

    async def idle(*_, **kwargs):
        if kwargs:
            assert "safe restart smoke passed" not in capsys.readouterr().out
            calls.append("return")
            raise smoke.SmokeFailure("still active")

    monkeypatch.setattr(smoke, "_wait_for_worker_idle", idle)
    with pytest.raises(smoke.SmokeFailure, match="still active"):
        await smoke._main(args)
    assert calls == ["proof", "return"]
    assert "safe restart smoke passed" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_smoke_closes_own_database_loop_even_on_failure(monkeypatch):
    dispose = AsyncMock()
    monkeypatch.setattr(smoke, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(smoke, "_main", AsyncMock(side_effect=smoke.SmokeFailure("proof")))
    with pytest.raises(smoke.SmokeFailure, match="proof"):
        await smoke._main_with_cleanup(Namespace())
    dispose.assert_awaited_once()


def test_guarded_release_waits_after_restart_without_replacing_idle_guard():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "guarded_release.sh").read_text()
    restart = source.index("systemctl restart uni-api-py.service uni-celery.service")
    idle = source.index("asyncio.run(_wait_for_worker_idle(120, wait_for_startup=True))", restart)
    identity = source.index("--release-identity-only", restart)
    assert restart < idle < identity
    assert 'states = {state:getattr(inspect,state)() for state in ("reserved","scheduled","active")}' in source
    assert "Workers did not become idle; release left unchanged" in source
    assert 'checkout "$repo_root" "$predecessor" "$target"' in source
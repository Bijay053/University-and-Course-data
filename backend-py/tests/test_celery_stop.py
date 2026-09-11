from __future__ import annotations

import signal
from pathlib import Path

from deploy import celery_stop

DEPLOY_DIR = Path(__file__).parents[1] / "deploy"


def test_celery_unit_uses_idle_aware_stop_with_full_active_grace() -> None:
    unit = (DEPLOY_DIR / "uni-celery.service").read_text()

    assert "ExecStop=" in unit
    assert "deploy/celery_stop.py" in unit
    assert "--main-pid $MAINPID" in unit
    assert "--hostname=uni-celery@%H" in unit
    assert "--node-name uni-celery@%H" in unit
    assert "TimeoutStopSec=90" in unit


def test_active_worker_gets_warm_signal_without_early_kill(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(celery_stop, "_quiesce_and_check_idle", lambda *_args: False)
    monkeypatch.setattr(celery_stop, "_capture_process_tree", lambda _pid: {10: 1, 12: 2})
    monkeypatch.setattr(celery_stop.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    idle = celery_stop.stop_worker(
        10,
        celery_bin="celery",
        node_name="uni-celery@host",
        inspect_timeout_seconds=1,
        idle_grace_seconds=0,
    )

    assert idle is False
    assert signals == [(10, signal.SIGTERM)]


def test_idle_worker_cleans_up_only_captured_tree_after_grace(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals | int]] = []
    monkeypatch.setattr(celery_stop, "_quiesce_and_check_idle", lambda *_args: True)
    monkeypatch.setattr(
        celery_stop, "_capture_process_tree", lambda _pid: {10: 1, 11: 2, 12: 3}
    )
    monkeypatch.setattr(celery_stop, "_is_same_process", lambda *_args: True)
    monkeypatch.setattr(celery_stop.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    idle = celery_stop.stop_worker(
        10,
        celery_bin="celery",
        node_name="uni-celery@host",
        inspect_timeout_seconds=1,
        idle_grace_seconds=0,
    )

    assert idle is True
    assert signals == [
        (10, signal.SIGTERM),
        (12, signal.SIGKILL),
        (11, signal.SIGKILL),
        (10, signal.SIGKILL),
    ]


def test_task_becoming_active_after_quiesce_blocks_early_cleanup(monkeypatch) -> None:
    outputs = iter(
        [
            "uni-celery@host: OK\nok",
            "uni-celery@host: OK\n- empty -",
            "uni-celery@host: OK\n- empty -",
            "uni-celery@host: OK\n* task newly active",
        ]
    )
    monkeypatch.setattr(celery_stop, "_remote", lambda *_args: next(outputs))

    assert (
        celery_stop._quiesce_and_check_idle("celery", "uni-celery@host", 1)
        is False
    )


def test_remote_options_precede_control_command_arguments(monkeypatch) -> None:
    seen: list[str] = []

    class Result:
        returncode = 0
        stdout = "uni-celery@host: OK\nok"
        stderr = ""

    def fake_run(command, **_kwargs):
        seen.extend(command)
        return Result()

    monkeypatch.setattr(celery_stop.subprocess, "run", fake_run)

    assert celery_stop._remote(
        "celery",
        "uni-celery@host",
        ["control", "cancel_consumer", "scrape"],
        1,
    )
    assert seen == [
        "celery",
        "-A",
        "app.tasks.celery_app",
        "control",
        "--destination=uni-celery@host",
        "--timeout=1",
        "cancel_consumer",
        "scrape",
    ]


def test_reused_pid_is_not_killed(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals | int]] = []
    monkeypatch.setattr(celery_stop, "_quiesce_and_check_idle", lambda *_args: True)
    monkeypatch.setattr(
        celery_stop, "_capture_process_tree", lambda _pid: {10: 1, 11: 2}
    )
    monkeypatch.setattr(
        celery_stop, "_is_same_process", lambda pid, _started: pid == 10
    )
    monkeypatch.setattr(celery_stop.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    celery_stop.stop_worker(
        10,
        celery_bin="celery",
        node_name="uni-celery@host",
        inspect_timeout_seconds=1,
        idle_grace_seconds=0,
    )

    assert signals == [(10, signal.SIGTERM), (10, signal.SIGKILL)]
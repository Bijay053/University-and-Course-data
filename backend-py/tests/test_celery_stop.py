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
    assert "TimeoutStopSec=90" in unit


def test_active_worker_gets_warm_signal_without_early_kill(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(celery_stop, "_has_active_tasks", lambda *_args: True)
    monkeypatch.setattr(celery_stop, "_child_pids", lambda _pid: {12})
    monkeypatch.setattr(celery_stop.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    idle = celery_stop.stop_worker(
        10, celery_bin="celery", inspect_timeout_seconds=1, idle_grace_seconds=0
    )

    assert idle is False
    assert signals == [(10, signal.SIGTERM)]


def test_idle_worker_cleans_up_only_captured_tree_after_grace(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals | int]] = []
    monkeypatch.setattr(celery_stop, "_has_active_tasks", lambda *_args: False)
    monkeypatch.setattr(celery_stop, "_child_pids", lambda _pid: {11, 12})
    monkeypatch.setattr(celery_stop, "_is_alive", lambda _pid: True)
    monkeypatch.setattr(celery_stop.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    idle = celery_stop.stop_worker(
        10, celery_bin="celery", inspect_timeout_seconds=1, idle_grace_seconds=0
    )

    assert idle is True
    assert signals == [
        (10, signal.SIGTERM),
        (12, signal.SIGKILL),
        (11, signal.SIGKILL),
        (10, signal.SIGKILL),
    ]


def test_inspect_failure_is_treated_as_active(monkeypatch) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "broker unavailable"

    monkeypatch.setattr(celery_stop.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert celery_stop._has_active_tasks("celery", 1) is True
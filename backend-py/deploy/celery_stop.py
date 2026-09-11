#!/usr/bin/env python3
"""Stop Celery quickly when idle while preserving active-task grace."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import time
from pathlib import Path


def _process_identity(pid: int) -> tuple[int, int] | None:
    """Return (parent pid, kernel start time) for a live process."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        fields = value[value.rfind(") ") + 2 :].split()
        return int(fields[1]), int(fields[19])
    except (FileNotFoundError, IndexError, PermissionError, ValueError):
        return None


def _capture_process_tree(root_pid: int) -> dict[int, int]:
    identities: dict[int, tuple[int, int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        identity = _process_identity(pid)
        if identity is not None:
            identities[pid] = identity

    captured: set[int] = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _start_time) in identities.items():
            if pid not in captured and parent in captured:
                captured.add(pid)
                changed = True
    return {
        pid: identities[pid][1]
        for pid in captured
        if pid in identities
    }


def _remote(
    celery_bin: str,
    node_name: str,
    command: list[str],
    timeout_seconds: float,
) -> str | None:
    group, *arguments = command
    try:
        result = subprocess.run(
            [
                celery_bin,
                "-A",
                "app.tasks.celery_app",
                group,
                f"--destination={node_name}",
                f"--timeout={timeout_seconds:g}",
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or node_name not in output:
        return None
    return output


def _quiesce_and_check_idle(
    celery_bin: str, node_name: str, timeout_seconds: float
) -> bool:
    cancelled = _remote(
        celery_bin,
        node_name,
        ["control", "cancel_consumer", "scrape"],
        timeout_seconds,
    )
    if cancelled is None or "ok" not in cancelled.lower():
        return False

    # Once this exact worker has stopped consuming, tasks can only move toward
    # active. Query in that order so every transition is seen in its source
    # state before it moves or in active after it moves.
    for state in ("reserved", "scheduled", "active"):
        output = _remote(
            celery_bin, node_name, ["inspect", state], timeout_seconds
        )
        if output is None or "- empty -" not in output:
            return False
    return True


def _is_same_process(pid: int, start_time: int) -> bool:
    identity = _process_identity(pid)
    return identity is not None and identity[1] == start_time


def stop_worker(
    main_pid: int,
    *,
    celery_bin: str,
    node_name: str,
    inspect_timeout_seconds: float,
    idle_grace_seconds: float,
) -> bool:
    """Return True when the worker was confirmed idle before shutdown."""
    idle = _quiesce_and_check_idle(celery_bin, node_name, inspect_timeout_seconds)
    captured = _capture_process_tree(main_pid)
    try:
        os.kill(main_pid, signal.SIGTERM)
    except ProcessLookupError:
        return idle

    if not idle:
        return False

    deadline = time.monotonic() + idle_grace_seconds
    while time.monotonic() < deadline:
        if not any(_is_same_process(pid, started) for pid, started in captured.items()):
            return True
        time.sleep(0.1)

    # Consumption was stopped before the idle checks. Only identities captured
    # from that exact worker tree are eligible for forced idle cleanup.
    for pid, started in sorted(captured.items(), reverse=True):
        if not _is_same_process(pid, started):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-pid", type=int, required=True)
    parser.add_argument("--celery-bin", required=True)
    parser.add_argument(
        "--node-name", default=f"uni-celery@{socket.gethostname()}"
    )
    parser.add_argument("--inspect-timeout-seconds", type=float, default=10)
    parser.add_argument("--idle-grace-seconds", type=float, default=10)
    args = parser.parse_args()
    idle = stop_worker(
        args.main_pid,
        celery_bin=args.celery_bin,
        node_name=args.node_name,
        inspect_timeout_seconds=args.inspect_timeout_seconds,
        idle_grace_seconds=args.idle_grace_seconds,
    )
    print(f"celery_stop active_tasks={'no' if idle else 'yes'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
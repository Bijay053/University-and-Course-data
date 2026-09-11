#!/usr/bin/env python3
"""Stop Celery quickly when idle while preserving active-task grace."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from pathlib import Path


def _child_pids(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().split()
            parents[int(entry.name)] = int(fields[3])
        except (FileNotFoundError, IndexError, PermissionError, ValueError):
            continue

    descendants: set[int] = set()
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if pid not in descendants and (parent == root_pid or parent in descendants):
                descendants.add(pid)
                changed = True
    return descendants


def _has_active_tasks(celery_bin: str, timeout_seconds: float) -> bool:
    try:
        result = subprocess.run(
            [
                celery_bin,
                "-A",
                "app.tasks.celery_app",
                "inspect",
                "active",
                f"--timeout={timeout_seconds:g}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode != 0 or "- empty -" not in output


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_worker(
    main_pid: int,
    *,
    celery_bin: str,
    inspect_timeout_seconds: float,
    idle_grace_seconds: float,
) -> bool:
    """Return True when the worker was confirmed idle before shutdown."""
    idle = not _has_active_tasks(celery_bin, inspect_timeout_seconds)
    captured = {main_pid, *_child_pids(main_pid)}
    try:
        os.kill(main_pid, signal.SIGTERM)
    except ProcessLookupError:
        return idle

    if not idle:
        return False

    deadline = time.monotonic() + idle_grace_seconds
    while time.monotonic() < deadline:
        if not any(_is_alive(pid) for pid in captured):
            return True
        time.sleep(0.1)

    # The active-task check was completed before SIGTERM. Only processes from
    # that exact worker tree are eligible for forced idle cleanup.
    for pid in sorted(captured, reverse=True):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-pid", type=int, required=True)
    parser.add_argument("--celery-bin", required=True)
    parser.add_argument("--inspect-timeout-seconds", type=float, default=10)
    parser.add_argument("--idle-grace-seconds", type=float, default=10)
    args = parser.parse_args()
    idle = stop_worker(
        args.main_pid,
        celery_bin=args.celery_bin,
        inspect_timeout_seconds=args.inspect_timeout_seconds,
        idle_grace_seconds=args.idle_grace_seconds,
    )
    print(f"celery_stop active_tasks={'no' if idle else 'yes'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
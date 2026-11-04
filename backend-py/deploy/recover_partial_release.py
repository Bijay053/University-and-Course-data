#!/usr/bin/env python3
"""Explicit operator CLI: inspect/recover a pre-restart failure, never publish."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import sys

try:
    from .partial_release_host import RecoveryHost
    from .partial_release_recovery import Recovery, RecoveryPlan, RecoveryRefused
except ImportError:
    from partial_release_host import RecoveryHost
    from partial_release_recovery import Recovery, RecoveryPlan, RecoveryRefused


class RecoveryInterrupted(BaseException):
    pass


@contextmanager
def release_lock(path=Path("/run/university-portal-release.lock")):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryRefused("another release or recovery is running") from None
        yield
    finally:
        os.close(descriptor)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("check", "recover", "resume"))
    result.add_argument("--repo-root", type=Path, default=Path("/opt/university-portal"))
    result.add_argument("--predecessor", required=True)
    result.add_argument("--target", required=True)
    result.add_argument("--failed-manifest", type=Path, required=True)
    result.add_argument("--expected-rehearsal-account-id", required=True)
    result.add_argument("--state-root", type=Path,
                        default=Path("/var/lib/university-portal/recovery"))
    result.add_argument("--state", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if os.geteuid() != 0:
        print("Root is required to verify service identities and protected proof.", file=sys.stderr)
        return 2
    if args.action == "resume" and args.state is None:
        print("Explicit --state is required for resume.", file=sys.stderr)
        return 2
    previous_umask = os.umask(0o077)
    def interrupted(signum, frame):
        raise RecoveryInterrupted()
    previous = {sig: signal.signal(sig, interrupted)
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        host = RecoveryHost(
            args.repo_root, owner_command=("sudo", "-H", "-u", "ubuntu"),
            python=str(args.repo_root / "backend-py/.venv/bin/python"),
            expected_origin="https://github.com/Bijay053/University-and-Course-data.git",
            account=args.expected_rehearsal_account_id,
        )
        recovery = Recovery(host, RecoveryPlan(
            args.predecessor, args.target, args.failed_manifest.absolute(),
        ))
        with release_lock():
            if args.action == "check":
                recovery.check()
                output = {"status": "check_passed", "release_completed": False}
            else:
                state = (recovery.resume(args.state) if args.action == "resume"
                         else recovery.start(args.state_root))
                output = {
                    "status": "baseline_restored", "state": str(state),
                    "release_completed": False,
                    "next_step": "Operator may start a new ordinary guarded release; "
                                 "fresh preflight and post-restart receipts are required.",
                }
        print(json.dumps(output, sort_keys=True))
        return 0
    except (Exception, RecoveryInterrupted, KeyboardInterrupt) as exc:
        # No environment values, subprocess argv or provider responses are logged.
        detail = str(exc) if isinstance(exc, RecoveryRefused) else type(exc).__name__
        print("Recovery stopped; original evidence and recovery state retained: " + detail,
              file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())

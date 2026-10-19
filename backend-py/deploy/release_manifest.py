"""Pair original ordinary-smoke receipts with one exact guarded release attempt.

Only local acceptance evidence is read. Later inspections cannot supply proof.
This helper is pinned from the reviewed target and kept outside checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from uuid import uuid4


MAX_RECEIPT_BYTES = 8 * 1024 * 1024
PHASES = ("preflight", "post_restart")


def now():
    return datetime.now(UTC).isoformat()


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamp must have a timezone")
    return parsed


def write_manifest(path, record):
    """Atomic private replacement; never delete receipts, including on failure."""
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(directory, predecessor, target):
    if any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (predecessor, target)):
        raise ValueError("exact full revisions required")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt = "release_" + uuid4().hex
    root = directory / attempt
    root.mkdir(mode=0o700)
    path = root / "manifest.json"
    write_manifest(path, {
        "schema_version": 1, "kind": "guarded_release_acceptance",
        "release_id": attempt, "predecessor_revision": predecessor,
        "target_revision": target, "started_at": now(), "status": "started",
        "preflight_passed": False, "release_completed": False, "phases": {},
    })
    return path


def verify_receipt(directory, *, revision, started_at, finished_at):
    paths = list(directory.iterdir())
    if len(paths) != 1:
        raise ValueError("exactly one original receipt required")
    path = paths[0]
    if path.is_symlink() or not re.fullmatch(r"safe_restart_smoke_[a-f0-9]{32}\.json", path.name):
        raise ValueError("invalid receipt identity")
    with path.open("rb") as stream:
        data = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt too large")
    receipt = json.loads(data)
    job = path.stem
    database = receipt["database"]
    celery = receipt["celery"]
    task_id = celery["task_id"]
    if (
        receipt["kind"] != "ordinary_smoke_acceptance"
        or receipt["schema_version"] != 1
        or receipt["revision"] != revision
        or receipt["restart_performed"] is not False
        or database["runtime_job_id"] != job
        or database["status"] != "completed"
        or celery["job_id"] != job
        or not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id)
        or celery["state"] != "SUCCESS"
        or celery["result"].get("ok") is not True
        or celery["result"].get("id") != job
    ):
        raise ValueError("receipt revision or sample identity mismatch")
    window = receipt["preflight"]
    if window["maximum_new_samples"] != 1 or not (
        timestamp(started_at) <= timestamp(window["started_at"])
        <= timestamp(receipt["worker_observed_at"])
        <= timestamp(window["finished_at"]) <= timestamp(finished_at)
    ):
        raise ValueError("stale or out-of-window acceptance")
    replies = receipt["complete_worker_replies"]
    workers = set(replies["ping"])
    if not workers or any(item.get("ok") != "pong" for item in replies["ping"].values()):
        raise ValueError("missing worker replies")
    for state in ("active", "reserved", "scheduled"):
        if set(replies[state]) != workers or any(items != [] for items in replies[state].values()):
            raise ValueError("incomplete or non-idle worker evidence")
    if any(value != 0 for value in receipt["active_database_counts"].values()):
        raise ValueError("non-idle database evidence")
    done = database["canonical_done_payload"]
    if (
        database["staged_rows"] != 0 or done.get("totalFound") != 1
        or done.get("skipped") != 1 or done.get("imported", 0) != 0
        or done.get("errors", 0) != 0
        or done.get("skip_reasons") != {"domestic_only": 1}
    ):
        raise ValueError("invalid ordinary sample outcome")
    # Allowlist only: no command, host, paths supplied by receipt, payload,
    # environment, exception text or later observations enter the manifest.
    return {
        "receipt_file": f"{directory.name}/{path.name}",
        "sha256": hashlib.sha256(data).hexdigest(), "revision": revision,
        "runtime_job_id": job, "celery_task_id": task_id,
        "accepted_at": window["finished_at"],
    }


def verify_phase(path, record, phase):
    entry = record["phases"][phase]
    revision = record["predecessor_revision" if phase == "preflight" else "target_revision"]
    actual = verify_receipt(path.parent / phase, revision=revision,
                            started_at=entry["started_at"], finished_at=entry["finished_at"])
    if actual != entry["receipt"]:
        raise ValueError("accepted receipt changed")
    return actual


def fail(path):
    record = json.loads(path.read_text())
    if record["release_completed"]:
        return
    record["status"] = "failed"
    record["finished_at"] = now()
    write_manifest(path, record)


def run_sample(path, phase, command):
    record = json.loads(path.read_text())
    required_status = "started" if phase == "preflight" else "preflight_passed"
    if record["status"] != required_status or phase in record["phases"] or not command:
        raise ValueError("invalid sample phase transition")
    if phase == "post_restart":
        verify_phase(path, record, "preflight")
    directory = path.parent / phase
    directory.mkdir(mode=0o700)  # never reuse an earlier invocation's directory
    entry = {"started_at": now(), "status": "running"}
    record["phases"][phase] = entry
    record["status"] = phase + "_running"
    write_manifest(path, record)
    try:
        # stdout is not parsed or persisted: only the command's own private
        # receipt in this fresh directory can become acceptance evidence.
        result = subprocess.run([*command, "--receipt-directory", str(directory)], check=False)
        entry["finished_at"] = now()
        entry["exit_code"] = result.returncode
        if result.returncode:
            raise ValueError("ordinary smoke command failed")
        revision = record["predecessor_revision" if phase == "preflight" else "target_revision"]
        proof = verify_receipt(directory, revision=revision,
                               started_at=entry["started_at"], finished_at=entry["finished_at"])
        if phase == "post_restart":
            before = verify_phase(path, record, "preflight")
            if any(proof[key] == before[key] for key in ("runtime_job_id", "celery_task_id")):
                raise ValueError("sample identity reused")
        entry.update(status="passed", receipt=proof)
        record["status"] = phase + "_passed"
        if phase == "preflight":
            record["preflight_passed"] = True
    except Exception:
        entry.update(status="failed", finished_at=now())
        record.update(status="failed", finished_at=now())
        write_manifest(path, record)
        raise
    write_manifest(path, record)


def complete(path):
    record = json.loads(path.read_text())
    if record["status"] != "post_restart_passed":
        raise ValueError("release lacks both original acceptances")
    before, after = (verify_phase(path, record, phase) for phase in PHASES)
    if any(before[key] == after[key] for key in ("runtime_job_id", "celery_task_id")):
        raise ValueError("sample identity reused")
    record.update(status="completed", release_completed=True, finished_at=now())
    write_manifest(path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init")
    init.add_argument("--directory", type=Path, required=True)
    init.add_argument("--predecessor", required=True)
    init.add_argument("--target", required=True)
    for action in ("sample", "complete", "fail"):
        child = sub.add_parser(action)
        child.add_argument("--manifest", type=Path, required=True)
        if action == "sample":
            child.add_argument("--phase", choices=PHASES, required=True)
            child.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "init":
            print(initialize(args.directory, args.predecessor, args.target))
        elif args.action == "sample":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            run_sample(args.manifest, args.phase, command)
        elif args.action == "complete":
            complete(args.manifest)
        else:
            fail(args.manifest)
        return 0
    except Exception:
        # Exceptions/commands may contain credentials. Preserve proof, not errors.
        print("Release evidence refused; retained existing evidence, do not rerun samples.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

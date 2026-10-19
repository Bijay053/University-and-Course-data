#!/usr/bin/env python3
"""Observe an existing smoke sample; never create work or certify past idleness."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Mapping

from sqlalchemy import text

try:
    from . import safe_restart_smoke as smoke
except ImportError:
    import safe_restart_smoke as smoke


def now():
    return datetime.now(UTC).isoformat()


async def database_evidence(job_id):
    """One consistent, server-enforced read-only view; no ORM autoflush."""
    async with smoke.AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        row = (await db.execute(text("""
            SELECT runtime_job_id,status,job_type,request_payload,worker_id,worker_pid,
                   release_revision,imported,skipped,errors,requeue_count,claim_count,
                   created_at,completed_at
            FROM scrape_runtime_jobs WHERE runtime_job_id=:job_id
        """), {"job_id": job_id})).mappings().first()
        evidence = dict(row) if row else {}
        done = (await db.execute(text("""
            SELECT sequence,created_at,payload FROM scrape_runtime_logs
            WHERE runtime_job_id=:job_id AND event='done'
            ORDER BY sequence DESC LIMIT 1
        """), {"job_id": job_id})).mappings().first()
        evidence.update(
            staged_rows=(await db.execute(text(
                "SELECT count(*) FROM scraped_courses WHERE scrape_job_id=:job_id"
            ), {"job_id": job_id})).scalar_one(),
            done_sequence=done["sequence"] if done else None,
            done_created_at=done["created_at"] if done else None,
            canonical_done_payload=done["payload"] if done else None,
        )
        # Keep the sample contract, not arbitrary future request credentials.
        payload = evidence.get("request_payload") or {}
        evidence["request_payload"] = {k: payload.get(k) for k in ("safeRestartSmoke", "courseUrls")}
        counts = dict((await db.execute(text("""
            SELECT status,count(*) FROM scrape_runtime_jobs
            WHERE status IN ('queued','running','awaiting_approval') GROUP BY status
        """))).all())
    return evidence, counts


def release_evidence():
    deployed = smoke._run([
        "git", "-c", f"safe.directory={smoke.REPO_ROOT}", "-C", str(smoke.REPO_ROOT),
        "rev-parse", "HEAD",
    ], stdout_only=True)
    services = {}
    for unit in ("uni-api-py", "uni-celery"):
        pid = smoke._run(["systemctl", "show", unit, "--property=MainPID", "--value"])
        if not pid.isdigit() or int(pid) <= 0:
            raise smoke.SmokeFailure(f"{unit} has no live MainPID")
        revisions = [
            item.split(b"=", 1)[1].decode()
            for item in smoke._read_process_environment(pid)
            if item.startswith(b"RELEASE_REVISION=")
        ]
        services[unit] = {"pid": int(pid), "release_revision": revisions[0] if len(revisions) == 1 else None}
    return {
        "revision": deployed,
        "release_file_revision": smoke.resolve_expected_release(fallback=smoke.UNKNOWN_RELEASE),
        "processes": services,
        "smoke_sha256": hashlib.sha256(Path(smoke.__file__).read_bytes()).hexdigest(),
    }


def celery_evidence(task_id, timeout, celery):
    result = celery.AsyncResult(task_id)
    state = result.state
    # No get(), join(), forget(), task dispatch, or recovery call.
    value = result.result
    inspection = celery.control.inspect(timeout=timeout)
    replies = {}
    for key in ("ping", "active", "reserved", "scheduled"):
        try:
            replies[key] = getattr(inspection, key)()
        except Exception:
            # Preserve other observations while failing closed on this reply.
            replies[key] = None
    return {"task_id": task_id, "job_id": value.get("id") if isinstance(value, Mapping) else None,
            "state": state, "result": value}, replies


def validate(report, expected_workers, expected_skip_reason):
    errors = []
    db, celery = report["database"], report["celery"]
    if db.get("runtime_job_id") != report["requested_job_id"]:
        errors.append("Sample job not found")
    if db.get("job_type") != "safe_restart_smoke" or db.get("request_payload", {}).get("safeRestartSmoke") is not True:
        errors.append("Job is not a safe restart smoke sample")
    if db.get("status") != "completed":
        errors.append("Sample is not completed")
    result = celery.get("result")
    if celery.get("state") != "SUCCESS" or not isinstance(result, Mapping) or result.get("ok") is not True or result.get("id") != report["requested_job_id"]:
        errors.append("Celery task SUCCESS result does not identify the requested sample job")
    try:
        smoke.validate_done_payload(db.get("canonical_done_payload"),
                                    staged_rows=db.get("staged_rows", 0),
                                    expected_skip_reason=expected_skip_reason)
    except (smoke.SmokeFailure, ValueError, TypeError) as exc:
        errors.append(str(exc))
    done = db.get("canonical_done_payload") or {}
    if any(db.get(key) != done.get(key) for key in ("imported", "skipped", "errors")):
        errors.append("Persisted job counters do not match DONE")
    replies = report["complete_worker_replies"]
    for kind in ("ping", "active", "reserved", "scheduled"):
        values = replies.get(kind)
        if not isinstance(values, Mapping) or set(values) != set(expected_workers):
            errors.append(f"Incomplete {kind} replies for expected worker set")
            continue
        for value in values.values():
            if kind == "ping":
                if not isinstance(value, Mapping) or value.get("ok") != "pong":
                    errors.append("Malformed ping reply")
            elif not isinstance(value, list):
                errors.append(f"Malformed {kind} reply")
            elif value:
                errors.append(f"Workers are not currently idle ({kind})")
    if any(report["active_database_counts"].values()):
        errors.append("Database is not currently idle")
    release = report["release"]
    revision = release["revision"]
    if not revision or revision == smoke.UNKNOWN_RELEASE or release["release_file_revision"] != revision or any(
        p["release_revision"] != revision for p in release["processes"].values()
    ):
        errors.append("Current deployed, release-file and process revisions differ")
    return errors


async def inspect_existing(args, celery=None):
    report = {
        "observation_kind": "later_read_only_inspection",
        "execution_started_at": now(), "requested_job_id": args.job_id,
        "certifies_original_acceptance": False, "certifies_earlier_idle_state": False,
        "restart_performed": False, "dispatch_performed": False,
        "expected_workers": args.expected_worker,
    }
    try:
        report["database"], report["active_database_counts"] = await database_evidence(args.job_id)
        report["observed_at"] = now()
        report["release"] = release_evidence()
        if celery is None:
            from app.tasks.celery_app import celery_app
            celery = celery_app
        report["celery"], report["complete_worker_replies"] = celery_evidence(args.task_id, args.timeout, celery)
        report["validation_errors"] = validate(report, args.expected_worker, args.expected_skip_reason)
        report["observation_passed"] = not report["validation_errors"]
        return report
    finally:
        # Must be awaited in the same asyncio.run as the database reads.
        await asyncio.wait_for(smoke.engine.dispose(), timeout=10)
        report["execution_finished_at"] = now()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-worker", action="append", required=True,
                        help="Exact worker nodename; repeat for every expected worker")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--expected-skip-reason", default=smoke.DEFAULT_EXPECTED_SKIP_REASON)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("--timeout must be finite, greater than zero and at most 60 seconds")
    try:
        report = asyncio.run(inspect_existing(args))
        print(json.dumps(report, default=str, sort_keys=True))
        return 0 if report["observation_passed"] else 1
    except Exception as exc:
        # A failed observation is not an acceptance receipt; no retries or repair.
        print(json.dumps({"observation_kind": "later_read_only_inspection",
                          "observed_at": now(), "observation_passed": False,
                          "certifies_original_acceptance": False,
                          "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

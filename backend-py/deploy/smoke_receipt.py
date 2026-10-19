"""Sanitized acceptance evidence from the ordinary smoke's own observations."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def sanitize(value):
    """Retain full evidence structure, redacting credential fields and URL secrets."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if re.search(
                r"password|secret|token|credential|authorization|cookie|api.?key|"
                r"connection.?string|database.?url|broker.?url|environment",
                str(key), re.I,
            ) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/]*@[^\s/]*", r"\1[REDACTED]", value)
        value = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 [REDACTED]", value)
        value = re.sub(
            r"(?i)\b(password|secret|token|api[_-]?key)\s*[=:]\s*\S+",
            r"\1=[REDACTED]", value,
        )
        return value
    return value


def save_receipt(directory, *, release, job_id, task_id, payload, staged_rows,
                 worker_evidence, counts, started_at):
    """No fetching or dispatch: validate and persist the exact accepted observations."""
    if not re.fullmatch(r"safe_restart_smoke_[a-f0-9]{32}", job_id):
        raise ValueError("invalid smoke job identity")
    if not task_id or worker_evidence["task_id"] != task_id or worker_evidence["job_id"] != job_id:
        raise ValueError("mismatched task identity")
    result = worker_evidence["result"]
    if result["state"] != "SUCCESS" or result["value"].get("ok") is not True or result["value"].get("id") != job_id:
        raise ValueError("mismatched task result")
    replies = worker_evidence["complete_worker_replies"]
    workers = set(replies["ping"])
    if not workers or any(reply.get("ok") != "pong" for reply in replies["ping"].values()):
        raise ValueError("missing ping replies")
    for state in ("active", "reserved", "scheduled"):
        if set(replies[state]) != workers or any(items != [] for items in replies[state].values()):
            raise ValueError("incomplete or non-idle worker replies")
    if any(counts.values()):
        raise ValueError("database not idle")
    record = sanitize({
        "schema_version": 1, "kind": "ordinary_smoke_acceptance",
        "revision": release,
        "preflight": {"started_at": started_at, "finished_at": datetime.now(UTC).isoformat(),
                      "maximum_new_samples": 1},
        "database": {"runtime_job_id": job_id, "status": "completed",
                     "canonical_done_payload": payload, "staged_rows": staged_rows},
        "celery": {"task_id": task_id, "job_id": job_id,
                   "state": result["state"], "result": result["value"]},
        "complete_worker_replies": replies,
        "worker_observed_at": worker_evidence["observed_at"],
        "active_database_counts": counts,
        "restart_performed": False,
    })
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Never overwrite an earlier receipt for the same sample.
        os.link(temporary, directory / f"{job_id}.json")
    finally:
        os.unlink(temporary)
    return record

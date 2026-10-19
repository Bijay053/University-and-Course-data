"""Offline ordinary-smoke stand-in; never imports application or provider code."""
import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.smoke_receipt import save_receipt


parser = argparse.ArgumentParser()
parser.add_argument("--receipt-directory", type=Path, required=True)
parser.add_argument("--revision")
parser.add_argument("--case", default="valid")
parser.add_argument("--state-directory", type=Path)
args = parser.parse_args()
phase = args.receipt_directory.name
if args.state_directory:
    args.revision = (args.state_directory / "revision").read_text()
    args.case = (args.state_directory / phase).read_text()
    with (args.state_directory / "calls").open("a") as stream:
        stream.write(phase + "\n")
if args.case == "failed":
    raise SystemExit(7)

started = datetime.now(UTC)
job = "safe_restart_smoke_" + uuid4().hex
task = str(uuid4())
worker = {
    "task_id": task, "job_id": job, "observed_at": started.isoformat(),
    "result": {"state": "SUCCESS", "value": {"ok": True, "id": job}},
    "complete_worker_replies": {
        "ping": {"worker-a": {"ok": "pong"}, "worker-b": {"ok": "pong"}},
        **{state: {"worker-a": [], "worker-b": []}
           for state in ("active", "reserved", "scheduled")},
    },
}
record = save_receipt(
    args.receipt_directory, release=args.revision, job_id=job, task_id=task,
    payload={"totalFound": 1, "skipped": 1, "imported": 0, "errors": 0,
             "skip_reasons": {"domestic_only": 1},
             "token": "secret-not-in-manifest", "note": "private-payload-not-in-manifest"},
    staged_rows=0, counts={"queued": 0, "running": 0, "awaiting_approval": 0},
    worker_evidence=worker, started_at=started.isoformat(),
)
if args.case == "stale":
    record["preflight"]["started_at"] = (started - timedelta(days=1)).isoformat()
elif args.case == "future":
    record["preflight"]["finished_at"] = (started + timedelta(days=1)).isoformat()
elif args.case == "revision":
    record["revision"] = "f" * 40
elif args.case == "job":
    record["celery"]["job_id"] = "safe_restart_smoke_" + "f" * 32
elif args.case == "result":
    record["celery"]["result"]["id"] = "other"
elif args.case == "observation":
    record["kind"] = "later_read_only_observation"
elif args.case == "partial":
    del record["complete_worker_replies"]["reserved"]["worker-b"]
elif args.case == "busy":
    record["complete_worker_replies"]["active"]["worker-b"] = [{"id": "business"}]
elif args.case == "database":
    record["active_database_counts"]["running"] = 1
elif args.case == "done":
    record["database"]["canonical_done_payload"]["imported"] = 1
elif args.case == "reuse":
    before_path = next((args.receipt_directory.parent / "preflight").glob("*.json"))
    before = json.loads(before_path.read_text())
    record["celery"]["task_id"] = before["celery"]["task_id"]
path = args.receipt_directory / f"{job}.json"
path.write_text(json.dumps(record))
if args.case == "symlink":
    moved = path.with_suffix(".original")
    path.rename(moved)
    path.symlink_to(moved)
elif args.case == "multiple":
    (args.receipt_directory / ("safe_restart_smoke_" + "c" * 32 + ".json")).write_text(json.dumps(record))
elif args.case == "missing":
    path.unlink()
elif args.case == "nonzero":
    raise SystemExit(9)

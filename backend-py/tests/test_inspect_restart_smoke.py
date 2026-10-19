"""Read-only observer contracts; no production access or broker dispatch."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from deploy import inspect_restart_smoke as observer

CONTRACT = Path(__file__).resolve().parents[1] / "docs/production/release-sample-lifecycle-evidence-2026-10-08.json"


def evidence():
    source = json.loads(CONTRACT.read_text())
    return {
        "requested_job_id": source["database"]["runtime_job_id"],
        "database": source["database"], "celery": source["celery"],
        "complete_worker_replies": source["complete_worker_replies"],
        "active_database_counts": {},
        "release": {"revision": source["revision"], "release_file_revision": source["revision"],
                    "processes": {unit: {"pid": 42, "release_revision": source["revision"]}
                                  for unit in ("uni-api-py", "uni-celery")}},
    }


def errors(report):
    return observer.validate(report, ["uni-celery@ip-172-31-40-159"], "domestic_only")


def test_evidence_contract_passes():
    assert errors(evidence()) == []


def test_historical_sample_does_not_masquerade_as_current_release():
    report = evidence()
    report["database"]["release_revision"] = "older-release"
    assert errors(report) == []
    assert report["database"]["release_revision"] != report["release"]["revision"]


def test_exact_release_and_process_evidence_only_exposes_revision(monkeypatch):
    revision = evidence()["release"]["revision"]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return revision if command[0] == "git" else "123"
    monkeypatch.setattr(observer.smoke, "_run", run)
    monkeypatch.setattr(observer.smoke, "_read_process_environment",
                        lambda pid: [b"SECRET=do-not-print", f"RELEASE_REVISION={revision}".encode()])
    def release_file(**kwargs):
        assert kwargs["fallback"] == observer.smoke.UNKNOWN_RELEASE
        return revision
    monkeypatch.setattr(observer.smoke, "resolve_expected_release", release_file)
    result = observer.release_evidence()
    assert result["revision"] == revision
    assert result["processes"]["uni-api-py"] == {"pid": 123, "release_revision": revision}
    assert "SECRET" not in json.dumps(result) and "do-not-print" not in json.dumps(result)
    assert all(c[1] == "show" for c in commands if c[0] == "systemctl")


@pytest.mark.parametrize("change", [
    lambda r: r["celery"]["result"].update(id="wrong-job"),
    lambda r: r["celery"].update(state="PENDING"),
    lambda r: r["database"].update(canonical_done_payload=None),
    lambda r: r["database"].update(staged_rows=1),
    lambda r: r["database"].update(job_type="scrape"),
    lambda r: r["database"].update(status="running"),
    lambda r: r["database"].update(imported=1),
    lambda r: r["active_database_counts"].update(running=1),
    lambda r: r["release"]["processes"]["uni-celery"].update(release_revision="other"),
    lambda r: r["complete_worker_replies"]["active"].update({"uni-celery@ip-172-31-40-159": [{"id": "t", "name": "busy"}]}),
])
def test_invalid_evidence_fails_closed(change):
    report = evidence()
    change(report)
    assert errors(report)


@pytest.mark.parametrize("channel", ["ping", "active", "reserved", "scheduled"])
@pytest.mark.parametrize("replacement", [None, {}, {"other-worker": []}])
def test_incomplete_replies_fail(channel, replacement):
    report = evidence()
    report["complete_worker_replies"][channel] = replacement
    assert errors(report)


def install_fakes(monkeypatch):
    """Reject every database operation except the exact read-only query path."""
    report = evidence()
    calls, loops = [], []
    class Rows:
        def __init__(self, value): self.value = value
        def mappings(self): return self
        def first(self): return self.value
        def scalar_one(self): return self.value
        def all(self): return self.value
    class Session:
        async def __aenter__(self):
            self.loop = asyncio.get_running_loop()
            loops.append(self.loop)
            return self
        async def __aexit__(self, *args): pass
        async def execute(self, statement, params=None):
            assert asyncio.get_running_loop() is self.loop
            sql = str(statement).strip()
            calls.append(sql)
            if sql.startswith("SET TRANSACTION"):
                assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                return None
            assert sql.startswith("SELECT")
            if "count(*) FROM scraped_courses" in sql: return Rows(0)
            if "FROM scrape_runtime_logs" in sql:
                db = report["database"]
                return Rows({"sequence": db["done_sequence"], "created_at": db["done_created_at"],
                             "payload": db["canonical_done_payload"]})
            if "GROUP BY" in sql: return Rows([])
            return Rows(copy.deepcopy(report["database"]))
    async def dispose():
        assert asyncio.get_running_loop() is loops[-1]
        calls.append("DISPOSE")
    class Inspector:
        def __getattr__(self, name):
            assert name in {"ping", "active", "reserved", "scheduled"}
            return lambda: report["complete_worker_replies"][name]
    class Celery:
        control = SimpleNamespace(inspect=lambda **kw: Inspector())
        def AsyncResult(self, task_id):
            assert task_id == report["celery"]["task_id"]
            return SimpleNamespace(state="SUCCESS", result=report["celery"]["result"])
    monkeypatch.setattr(observer.smoke, "AsyncSessionLocal", Session)
    monkeypatch.setattr(observer.smoke, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(observer, "release_evidence", lambda: report["release"])
    monkeypatch.setitem(sys.modules, "app.tasks.celery_app", SimpleNamespace(celery_app=Celery()))
    args = ["--job-id", report["requested_job_id"], "--task-id", report["celery"]["task_id"],
            "--expected-worker", "uni-celery@ip-172-31-40-159"]
    return args, calls, loops


def test_repeated_invocation_reads_only_and_disposes_in_own_loop(monkeypatch, capsys):
    args, calls, loops = install_fakes(monkeypatch)
    for _ in range(2):
        assert observer.main(args) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        report = json.loads(captured.out)
        assert report["observation_kind"] == "later_read_only_inspection"
        assert not report["certifies_earlier_idle_state"]
        assert not report["certifies_original_acceptance"]
        assert not report["dispatch_performed"]
        assert report["execution_started_at"] <= report["observed_at"] <= report["execution_finished_at"]
    assert loops[0] is not loops[1]
    assert all(loop.is_closed() for loop in loops)
    assert calls.count("DISPOSE") == 2
    assert len(calls) == 12


def test_cleanup_on_observation_failure(monkeypatch, capsys):
    args, calls, _ = install_fakes(monkeypatch)
    def fail(): raise OSError("sensitive connection details")
    monkeypatch.setattr(observer, "release_evidence", fail)
    assert observer.main(args) == 1
    captured = capsys.readouterr()
    assert captured.err == "" and "sensitive" not in captured.out
    assert calls[-1] == "DISPOSE"


def test_repeated_process_exit_has_clean_stderr():
    code = """
import runpy, pytest
ns = runpy.run_path('tests/test_inspect_restart_smoke.py')
patch = pytest.MonkeyPatch()
args, calls, loops = ns['install_fakes'](patch)
for _ in range(2):
    assert ns['observer'].main(args) == 0
assert all(loop.is_closed() for loop in loops)
"""
    for _ in range(2):
        result = subprocess.run([sys.executable, "-W", "error::ResourceWarning", "-c", code],
                                cwd=CONTRACT.parents[2], capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": "."}, timeout=40)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert len(result.stdout.splitlines()) == 2


def test_inspection_channel_failure_retains_other_replies():
    class Inspector:
        def ping(self): return {"worker": {"ok": "pong"}}
        def active(self): raise TimeoutError()
        def reserved(self): return {"worker": []}
        def scheduled(self): return {"worker": []}
    celery = SimpleNamespace(
        AsyncResult=lambda _: SimpleNamespace(state="SUCCESS", result={"id": "sample", "ok": True}),
        control=SimpleNamespace(inspect=lambda **_: Inspector()),
    )
    _, replies = observer.celery_evidence("task", 1, celery)
    assert replies == {"ping": {"worker": {"ok": "pong"}}, "active": None,
                       "reserved": {"worker": []}, "scheduled": {"worker": []}}

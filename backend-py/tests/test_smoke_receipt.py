import copy
import json
from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from deploy import safe_restart_smoke as smoke
from deploy.smoke_receipt import save_receipt, sanitize


@pytest.fixture
def evidence():
    job = "safe_restart_smoke_" + "a" * 32
    return dict(
        release="b" * 40, job_id=job, task_id="exact-task",
        payload={"totalFound": 1, "skipped": 1, "imported": 0,
                 "skip_reasons": {"domestic_only": 1}, "nested": {"all": [1, 2]}},
        staged_rows=0, counts={}, started_at="2026-10-19T00:00:00+00:00",
        worker_evidence={
            "task_id": "exact-task", "job_id": job, "observed_at": "now",
            "result": {"state": "SUCCESS", "value": {"ok": True, "id": job}},
            "complete_worker_replies": {
                "ping": {"worker-a": {"ok": "pong"}, "worker-b": {"ok": "pong"}},
                **{state: {"worker-a": [], "worker-b": []}
                   for state in ("active", "reserved", "scheduled")},
            },
        },
    )


def test_complete_receipt_is_private_and_preserves_full_evidence(tmp_path, evidence):
    original = copy.deepcopy(evidence)
    result = save_receipt(tmp_path, **evidence)
    path = tmp_path / (evidence["job_id"] + ".json")
    assert json.loads(path.read_text()) == result
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["database"]["canonical_done_payload"] == evidence["payload"]
    assert result["complete_worker_replies"] == evidence["worker_evidence"]["complete_worker_replies"]
    assert evidence == original
    with pytest.raises(FileExistsError):
        save_receipt(tmp_path, **evidence)
    assert json.loads(path.read_text()) == result
    assert not list(tmp_path.glob(".receipt-*"))


@pytest.mark.parametrize("case", ["task", "job", "result", "state", "partial", "busy", "database"])
def test_rejects_mismatched_or_incomplete_evidence(tmp_path, evidence, case):
    worker = evidence["worker_evidence"]
    if case in ("task", "job"):
        worker[case + "_id"] = "other"
    elif case == "result":
        worker["result"]["value"]["id"] = "other"
    elif case == "state":
        worker["result"]["state"] = "PENDING"
    elif case == "partial":
        del worker["complete_worker_replies"]["reserved"]["worker-b"]
    elif case == "busy":
        worker["complete_worker_replies"]["active"]["worker-b"] = [{"id": "other"}]
    else:
        evidence["counts"] = {"running": 1}
    with pytest.raises(ValueError):
        save_receipt(tmp_path, **evidence)
    assert list(tmp_path.iterdir()) == []


def test_recursive_secret_redaction(tmp_path, evidence):
    evidence["payload"]["nested"] = {
        "authorization": "private-header",
        "other": ["https://user:password@host/path?token=secret",
                  "postgresql://user:password@host/db", "Bearer private-token",
                  "api_key=private-key"],
        "environment": {"anything": "private-env"},
    }
    result = save_receipt(tmp_path, **evidence)
    encoded = json.dumps(result)
    for secret in ("private-header", "user:password", "token=secret",
                   "private-token", "private-key", "private-env"):
        assert secret not in encoded
    assert sanitize({"value": "ordinary text", "counts": [1, None]}) == {
        "value": "ordinary text", "counts": [1, None]}


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True, "receipt"])
async def test_ordinary_command_retains_same_sample_only_after_all_guards(
    monkeypatch, tmp_path, evidence, capsys, fail,
):
    args = Namespace(
        release_identity_only=False, database_rehearsal_proof=None,
        expected_rehearsal_account_id="test", max_rehearsal_age_hours=24,
        api_health_url="test", course_url="test", university_id=1,
        timeout_seconds=5, receipt_directory=tmp_path,
    )
    monkeypatch.setattr(smoke, "validate_database_rehearsal_requirement", lambda *a, **k: None)
    monkeypatch.setattr(smoke, "_verify_release_and_services", lambda: (evidence["release"], {}))
    monkeypatch.setattr(smoke, "_verify_api_and_celery", lambda *a: None)
    monkeypatch.setattr(smoke, "_verify_ordinary_html", lambda *a: None)
    monkeypatch.setattr(smoke, "_active_counts", AsyncMock(side_effect=[{}, {}, {"running": 1} if fail is True else {}]))
    monkeypatch.setattr(smoke, "_resolve_university_id", AsyncMock(return_value=1))
    dispatch = AsyncMock(return_value=(evidence["job_id"], evidence["task_id"]))
    monkeypatch.setattr(smoke, "_create_and_dispatch", dispatch)
    done = AsyncMock(return_value=(evidence["payload"], 0))
    monkeypatch.setattr(smoke, "_wait_for_done", done)
    idle = AsyncMock(return_value=evidence["worker_evidence"])
    monkeypatch.setattr(smoke, "_wait_for_worker_idle", idle)
    if fail == "receipt":
        args.receipt_directory = tmp_path / "not-a-directory"
        args.receipt_directory.write_text("occupied")
    if fail:
        with pytest.raises(smoke.SmokeFailure) as error:
            await smoke._main(args)
        if fail == "receipt":
            assert evidence["job_id"] in str(error.value)
            assert evidence["task_id"] in str(error.value)
            assert "do not rerun" in str(error.value)
        assert not list(tmp_path.glob("*.json"))
        assert not capsys.readouterr().out
    else:
        await smoke._main(args)
        lines = capsys.readouterr().out.splitlines()
        record = json.loads(lines[0])
        assert record["celery"]["task_id"] == evidence["task_id"]
        assert "safe restart smoke passed" in lines[1]
        assert len(list(tmp_path.glob("*.json"))) == 1
    dispatch.assert_awaited_once()
    done.assert_awaited_once_with(evidence["job_id"], 5)
    assert idle.await_count == 3
    idle.assert_awaited_with(
        smoke.DEFAULT_WORKER_RETURN_TIMEOUT_SECONDS,
        sample_task_id=evidence["task_id"], sample_job_id=evidence["job_id"],
    )


def test_write_failure_leaves_no_receipt_or_partial_file(monkeypatch, tmp_path, evidence):
    from deploy import smoke_receipt
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(smoke_receipt.os, "fsync", fail)
    with pytest.raises(OSError):
        save_receipt(tmp_path, **evidence)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_final_idle_returns_original_complete_worker_maps(monkeypatch, evidence):
    from types import SimpleNamespace
    replies = evidence["worker_evidence"]["complete_worker_replies"]
    inspector = SimpleNamespace(**{
        state: (lambda state=state: replies[state]) for state in replies
    })
    celery = SimpleNamespace(control=SimpleNamespace(inspect=lambda **kw: inspector))
    snapshots = []
    def observe(timeout, task_id):
        assert task_id == evidence["task_id"]
        snapshot = smoke._worker_snapshot(celery, timeout)
        snapshots.append(snapshot)
        return snapshot, evidence["worker_evidence"]["result"]
    monkeypatch.setattr(smoke, "_worker_observation", observe)
    observed = await smoke._wait_for_worker_idle(
        5, sample_task_id=evidence["task_id"], sample_job_id=evidence["job_id"],
    )
    assert observed["complete_worker_replies"] == replies
    assert len(snapshots) == 1
    assert observed["result"] == evidence["worker_evidence"]["result"]

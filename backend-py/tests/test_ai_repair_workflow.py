"""Isolated lifecycle tests: no AI, broker, network, or production database."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import ai_repair_workflow as workflow


def session(**state):
    return {
        "session_id": "session-1", "job_id": "parent", "university_id": 7,
        "status": "queued", "queued_at": workflow.now(), "attempts": [],
        "autonomous": {**workflow.autonomous_state(), **state},
    }


class Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Store:
    def __init__(self, evidence):
        self.evidence = evidence
        self.mutex = asyncio.Lock()
        self.jobs = {
            "parent": SimpleNamespace(
                runtime_job_id="parent", university_id=7, university_name="Test University",
                url="https://university.test/courses", status="completed",
            ),
        }
        self.added = []


class DB:
    def __init__(self, store):
        self.store = store
        self.locked = False

    async def commit(self):
        if self.locked:
            self.locked = False
            self.store.mutex.release()

    async def rollback(self):
        await self.commit()

    async def get(self, model, key, **kwargs):
        return self.store.jobs.get(key)

    def add(self, job):
        self.store.jobs[job.runtime_job_id] = job
        self.store.added.append(job)

    async def execute(self, query, params=None):
        active = next(
            (job for job in self.store.jobs.values() if job.status in workflow.CHILD_ACTIVE), None,
        )
        return Result(active)


@pytest.fixture
def memory(monkeypatch):
    store = Store(session())

    async def lock(db, university_id):
        assert university_id == 7
        if not db.locked:
            await store.mutex.acquire()
            db.locked = True

    async def load(job_id, db, session_id=None):
        if session_id and session_id != store.evidence["session_id"]:
            return {}
        return copy.deepcopy(store.evidence)

    async def save(evidence, db):
        store.evidence = copy.deepcopy(evidence)
        await db.commit()

    async def owns(evidence, db):
        return evidence["session_id"] == store.evidence["session_id"]

    monkeypatch.setattr(workflow, "lock", lock)
    monkeypatch.setattr(workflow, "load", load)
    monkeypatch.setattr(workflow, "save", save)
    monkeypatch.setattr(workflow, "owns", owns)
    for name in ("renew_repair_lease", "acquire_repair_lease", "claim_repair_session"):
        monkeypatch.setattr(workflow.agent, name, Mock(return_value=True))
    monkeypatch.setattr(workflow.agent, "_write_session", Mock())
    monkeypatch.setattr(workflow.agent, "read_session", Mock(return_value={}))
    monkeypatch.setattr(workflow.agent, "release_repair_lease", Mock())
    return store


@pytest.mark.asyncio
async def test_durable_claim_fences_concurrent_and_duplicate_delivery(memory):
    first, second = await asyncio.gather(
        workflow.claim("parent", 7, "session-1", "delivery-a", DB(memory)),
        workflow.claim("parent", 7, "session-1", "delivery-b", DB(memory)),
    )
    assert bool(first) != bool(second)
    assert memory.evidence["status"] == "running"
    assert workflow.agent.claim_repair_session.call_count == 1
    assert not await workflow.claim("parent", 7, "session-1", "delivery-a", DB(memory))


@pytest.mark.asyncio
async def test_queued_claim_restores_missing_redis_lease_but_never_reclaims_worker(memory):
    workflow.agent.renew_repair_lease.return_value = False
    assert await workflow.claim("parent", 7, "session-1", "delivery", DB(memory))
    assert workflow.agent.acquire_repair_lease.call_count == 1
    assert not await workflow.claim("parent", 7, "session-1", "duplicate", DB(memory))
    assert workflow.agent.acquire_repair_lease.call_count == 1


@pytest.mark.asyncio
async def test_claim_does_not_replace_newer_session(memory):
    memory.evidence["session_id"] = "new-owner"
    assert not await workflow.claim("parent", 7, "session-1", "delivery", DB(memory))
    workflow.agent.claim_repair_session.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_final_delivery_creates_exactly_one_child(memory, monkeypatch):
    evidence = session(worker_claim="delivery", config_loop_done=True)
    evidence.update(status="completed", live_probe={"accepted": True, "status": "accepted"},
                    attempts=[{"outcome": "no_change", "root_cause": "stale_job_evidence"}])
    memory.evidence = copy.deepcopy(evidence)
    dispatch = AsyncMock(side_effect=lambda evidence, db: evidence)
    monkeypatch.setattr(workflow, "dispatch_verification", dispatch)
    results = await asyncio.gather(
        workflow.queue_verification(copy.deepcopy(evidence), DB(memory)),
        workflow.queue_verification(copy.deepcopy(evidence), DB(memory)),
    )
    assert len(memory.added) == 1
    assert dispatch.await_count == 1
    assert {r["autonomous"]["verification_job_id"] for r in results} == {
        workflow.verification_id("session-1")
    }
    assert memory.evidence["status"] == "running"
    assert memory.added[0].job_type == "scrape"
    assert memory.added[0].request_payload["autonomousVerification"]["max_courses"] == 50
    workflow.agent.release_repair_lease.assert_not_called()


@pytest.mark.asyncio
async def test_lost_lease_never_launches_child(memory):
    evidence = session(worker_claim="delivery", config_loop_done=True)
    evidence.update(status="completed", live_probe={"accepted": True, "status": "accepted"},
                    attempts=[{"outcome": "no_change", "root_cause": "stale_job_evidence"}])
    memory.evidence = evidence
    workflow.agent.renew_repair_lease.return_value = False
    workflow.agent.acquire_repair_lease.return_value = False
    result = await workflow.queue_verification(evidence, DB(memory))
    assert result["autonomous"]["phase"] == "blocked"
    assert result["status"] == "failed"
    assert not memory.added
    workflow.agent.acquire_repair_lease.assert_called_once()


@pytest.mark.asyncio
async def test_missing_completion_signal_stays_terminal_on_second_reconcile(memory):
    evidence = session(worker_claim="delivery")
    evidence.update(status="completed")
    memory.evidence = copy.deepcopy(evidence)
    result = await workflow.queue_verification(evidence, DB(memory))
    assert result["autonomous"]["phase"] == "blocked"
    assert result["status"] == "completed"
    again = await workflow.reconcile("parent", "session-1", DB(memory))
    assert again["autonomous"]["phase"] == "blocked"
    assert again["status"] == "completed"
    assert not memory.added


@pytest.mark.asyncio
async def test_completed_loop_reacquires_available_lease_before_verification(memory, monkeypatch):
    evidence = session(worker_claim="delivery", config_loop_done=True)
    evidence.update(
        status="completed",
        live_probe={"accepted": True, "status": "accepted"},
        attempts=[{"outcome": "no_change", "root_cause": "stale_job_evidence"}],
    )
    memory.evidence = copy.deepcopy(evidence)
    workflow.agent.renew_repair_lease.return_value = False
    workflow.agent.acquire_repair_lease.return_value = True
    dispatch = AsyncMock(side_effect=lambda current, db: current)
    monkeypatch.setattr(workflow, "dispatch_verification", dispatch)
    result = await workflow.queue_verification(evidence, DB(memory))
    assert workflow.agent.acquire_repair_lease.called
    assert result["autonomous"]["verification_job_id"] == workflow.verification_id("session-1")
    assert len(memory.added) == 1
    assert dispatch.await_count == 1


@pytest.mark.asyncio
async def test_active_scrape_collision_recovers_without_duplicate_child(memory, monkeypatch):
    evidence = session(worker_claim="delivery", config_loop_done=True)
    evidence.update(
        status="completed",
        live_probe={"accepted": True, "status": "accepted"},
        attempts=[{"outcome": "no_change", "root_cause": "stale_job_evidence"}],
    )
    memory.evidence = copy.deepcopy(evidence)
    memory.jobs["active"] = child("running")
    result = await workflow.queue_verification(evidence, DB(memory))
    assert result["autonomous"]["phase"] == "recovering"
    assert result["autonomous"]["recovery_target"] == "verification"
    assert result["status"] == "completed"
    assert result["autonomous"]["recovery"]["wait_attempts"] == 1
    assert not memory.added

    memory.jobs["active"].status = "completed"
    memory.evidence["autonomous"]["last_dispatch_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=3)
    ).isoformat()
    dispatch = AsyncMock(side_effect=lambda current, db: current)
    monkeypatch.setattr(workflow, "dispatch_verification", dispatch)
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["autonomous"]["verification_job_id"] == workflow.verification_id("session-1")
    assert len(memory.added) == 1
    assert dispatch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [{}, {"accepted": False}, {"status": "failed", "accepted": True}])
async def test_simulation_or_unaccepted_live_evidence_never_verifies(memory, probe):
    evidence = session(worker_claim="delivery")
    evidence.update(status="completed", live_probe=probe, final_verdict="Simulation improved")
    memory.evidence = evidence
    result = await workflow.queue_verification(evidence, DB(memory))
    assert result["autonomous"]["phase"] == "blocked"
    assert not memory.added


def child(status="completed", **overrides):
    values = {
        "runtime_job_id": "child", "status": status, "total_found": 3, "imported": 3,
        "current": 3, "skipped": 0, "errors": 0, "cost_ceiling_hit": False,
        "discovered_config": {"autonomousVerification": {
            "coverage_measured": True, "capped": False, "limit_reached": False, "max_courses": 50,
        }},
        "request_payload": {}, "gate_skip_counts": {},
        "claimed_at": None, "worker_id": None, "heartbeat_at": None,
        "started_at": datetime.now(timezone.utc),
    }
    return SimpleNamespace(**{**values, **overrides})


def quality(**overrides):
    return {
        "total_staged": 3, "fee_pct": 100, "ielts_pct": 100, "location_pct": 100,
        "duration_pct": 100, "course_name_pct": 100, "intakes_pct": 100,
        "mode_pct": 100, "degree_level_pct": 100, **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job,after,expected",
    [
        (child("failed"), quality(), "needs_review"),
        (child(), quality(fee_pct=0), "needs_review"),
        (child(), quality(), "verified"),
        (child(total_found=50, imported=50), quality(), "needs_review"),
        (child(cost_ceiling_hit=True), quality(), "needs_review"),
        (child(gate_skip_counts={"catalogue_guard": {"kind": "catalogue_below_expected_min"}}),
         quality(), "needs_review"),
        (child(discovered_config={"pipeline_stats": {"contamination_count": 1}}), quality(), "needs_review"),
        (child(discovered_config={"autonomousVerification": {"max_courses": 3, "capped": True}}),
         quality(), "needs_review"),
        (child(discovered_config={"autonomousVerification": {
            "max_courses": 50, "effective_max_courses": 3, "limit_reached": True,
        }}), quality(), "needs_review"),
        (child(discovered_config={"autonomousVerification": {"warning": "Partial discovery"}}),
         quality(), "needs_review"),
        (child(skipped=1, imported=2), quality(), "needs_review"),
        (child(discovered_config={"autonomousVerification": {
            "coverage_measured": False, "capped": None,
        }}), quality(), "needs_review"),
        (child(discovered_config={"autonomousVerification": {
            "coverage_measured": True, "capped": None,
        }}), quality(), "needs_review"),
        (child(discovered_config={}), quality(), "needs_review"),
    ],
)
async def test_child_reconciliation_is_truthful(memory, monkeypatch, job, after, expected):
    memory.evidence = session(
        worker_claim="delivery", phase="verifying", verification_job_id="child",
    )
    memory.evidence.update(status="running", quality_before=quality())
    memory.jobs["child"] = job
    monkeypatch.setattr(workflow.agent, "_quality_snapshot", AsyncMock(return_value=after))
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["status"] == "completed"
    assert result["autonomous"]["phase"] == expected
    assert result["autonomous"]["comparison"]["full_catalogue_verified"] is False
    assert result["autonomous"]["verification_status"] == job.status
    workflow.agent.release_repair_lease.assert_called_once()


@pytest.mark.asyncio
async def test_time_budget_exhaustion_is_not_reported_as_course_cap(memory, monkeypatch):
    memory.evidence = session(
        worker_claim="delivery", phase="verifying", verification_job_id="child",
    )
    memory.evidence.update(status="running", quality_before=quality())
    metadata = {
        "coverage_measured": True,
        "capped": False,
        "limit_reached": True,
        "max_courses": 50,
        "selected_courses": 50,
        "staged_courses": 30,
        "budget_exhausted": "time_budget_exhausted",
        "time_budget_seconds": 600,
    }
    memory.jobs["child"] = child(
        "failed_degraded",
        total_found=50,
        current=31,
        imported=30,
        discovered_config={"autonomousVerification": metadata},
    )
    monkeypatch.setattr(
        workflow.agent,
        "_quality_snapshot",
        AsyncMock(return_value=quality(ielts_pct=70, duration_pct=97, mode_pct=30)),
    )

    result = await workflow.reconcile("parent", "session-1", DB(memory))
    comparison = result["autonomous"]["comparison"]

    assert comparison["capped"] is False
    assert comparison["stop_reason"] == "time_budget_exhausted"
    assert comparison["counters"]["current"] == 31
    assert result["autonomous"]["phase"] == "needs_review"


@pytest.mark.asyncio
async def test_child_queue_failure_is_recoverable_and_idempotent(memory, monkeypatch):
    from app.tasks import scrape_tasks

    memory.evidence = session(
        phase="verification_queued", verification_job_id="child", verification_requeues=0,
    )
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("queued")
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", Mock())
    monkeypatch.setattr(scrape_tasks.scrape_university, "apply_async", Mock(side_effect=RuntimeError("broker down")))
    result = await workflow.dispatch_verification(memory.evidence, DB(memory))
    assert result["status"] == "queued"
    assert memory.jobs["child"].status == "queued"
    assert result["autonomous"]["phase"] == "recovering"
    assert result["autonomous"]["recovery"]["dispatch_attempts"] == 1
    assert result["autonomous"]["verification_job_id"] == "child"


@pytest.mark.asyncio
async def test_repeated_broker_failures_exhaust_dispatch_cap_without_duplicate_child(memory, monkeypatch):
    from app.tasks import scrape_tasks

    memory.evidence = session(
        phase="verification_queued", verification_job_id="child", verification_requeues=0,
    )
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("queued")
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", Mock())
    dispatch = Mock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(scrape_tasks.scrape_university, "apply_async", dispatch)

    result = await workflow.dispatch_verification(memory.evidence, DB(memory))
    assert result["autonomous"]["verification_requeues"] == 1
    for attempt in (2, 3):
        memory.evidence["autonomous"]["verification_last_dispatch_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=3)
        ).isoformat()
        result = await workflow.reconcile("parent", "session-1", DB(memory))
        if attempt == 2:
            assert result["autonomous"]["verification_requeues"] == 2
            assert result["status"] == "queued"
        else:
            assert result["status"] == "failed"
            assert result["autonomous"]["recovery_exhausted"] is True
    assert dispatch.call_count == 2
    assert len(memory.added) == 0
    assert memory.evidence["autonomous"]["verification_job_id"] == "child"


@pytest.mark.asyncio
async def test_crashed_dispatch_round_is_retried_after_stale_generation(memory, monkeypatch):
    from app.tasks import scrape_tasks

    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    memory.evidence = session(
        phase="verification_queued", verification_job_id="child",
        verification_requeues=1, verification_dispatch_round=1,
        verification_dispatch_started_at=old, verification_last_dispatch_at=old,
    )
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("queued")
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", Mock())
    dispatch = Mock()
    monkeypatch.setattr(scrape_tasks.scrape_university, "apply_async", dispatch)
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["autonomous"]["verification_requeues"] == 2
    assert dispatch.call_count == 1


@pytest.mark.asyncio
async def test_successful_lost_dispatch_retries_from_expired_publish_timestamp(memory, monkeypatch):
    from app.tasks import scrape_tasks

    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    memory.evidence = session(
        phase="verification_queued", verification_job_id="child",
        verification_requeues=1, verification_dispatch_round=1,
        verification_last_dispatch_at=old,
    )
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("queued")
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", Mock())
    dispatch = Mock()
    monkeypatch.setattr(scrape_tasks.scrape_university, "apply_async", dispatch)

    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert dispatch.call_count == 1
    assert result["autonomous"]["verification_requeues"] == 2


@pytest.mark.asyncio
async def test_repeated_collision_wait_does_not_consume_repair_dispatch_cap(memory, monkeypatch):
    from app.tasks import auto_repair_task

    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    memory.evidence = session(phase="recovering", recovery_target="repair",
                               last_dispatch_at=old, requeues=0)
    memory.jobs["active"] = child("running")
    dispatch = Mock()
    monkeypatch.setattr(auto_repair_task.run_ai_scrape_repair, "apply_async", dispatch)
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["autonomous"]["recovery_wait_attempts"] == 1
    assert result["autonomous"]["requeues"] == 0
    dispatch.assert_not_called()

    memory.jobs["active"].status = "completed"
    memory.evidence["autonomous"]["last_dispatch_at"] = old
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["autonomous"]["requeues"] == 1
    dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_duplicate_dispatch_is_fenced_by_durable_round(memory, monkeypatch):
    from app.tasks import scrape_tasks

    memory.evidence = session(
        phase="verification_queued", verification_job_id="child", verification_requeues=0,
    )
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("queued")
    monkeypatch.setattr(scrape_tasks, "set_initial_dispatch_lock", Mock())
    dispatch = Mock()
    monkeypatch.setattr(scrape_tasks.scrape_university, "apply_async", dispatch)
    await asyncio.gather(
        workflow.dispatch_verification(copy.deepcopy(memory.evidence), DB(memory)),
        workflow.dispatch_verification(copy.deepcopy(memory.evidence), DB(memory)),
    )
    assert dispatch.call_count == 1
    assert dispatch.call_args.kwargs["soft_time_limit"] == 600
    assert dispatch.call_args.kwargs["time_limit"] == 660


@pytest.mark.asyncio
async def test_delivery_recovery_exhausts_at_two_without_running_again(memory, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    memory.evidence = session(requeues=2, last_dispatch_at=old)
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["status"] == "failed"
    assert result["autonomous"]["recovery_exhausted"] is True
    assert "Automatic recovery exhausted after 2 broker dispatch attempts" in result["autonomous"]["reason"]
    assert not memory.added


@pytest.mark.asyncio
async def test_active_child_is_not_requeued_and_gets_time_bound(memory, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(minutes=11)
    memory.evidence = session(
        phase="verifying", verification_job_id="child", verification_requeues=0,
    )
    memory.jobs["child"] = child("running", claimed_at=old, worker_id="worker")
    dispatch = AsyncMock()
    monkeypatch.setattr(workflow, "dispatch_verification", dispatch)
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["status"] == "running"
    assert memory.jobs["child"].stop_requested is True
    dispatch.assert_not_awaited()
    workflow.agent.acquire_repair_lease.assert_not_called()


@pytest.mark.asyncio
async def test_audit_only_reload_and_old_agent_backup(monkeypatch):
    backup = session(phase="verifying", verification_job_id="child")
    backup["status"] = "running"
    monkeypatch.setattr(workflow.agent, "load_repair_audit", AsyncMock(return_value={
        "session_id": "session-1", "status": "completed", "attempts": [{"outcome": "accepted"}],
    }))
    db = SimpleNamespace(execute=AsyncMock(return_value=Result({"aiRepairWorkflow": backup})))
    result = await workflow.load("parent", db)
    assert result["status"] == "running"
    assert result["autonomous"]["verification_job_id"] == "child"
    assert result["attempts"] == backup["attempts"]  # child lifecycle freezes its audited result
    assert result["max_attempts"] == 5


@pytest.mark.asyncio
async def test_legacy_audit_remains_readable_without_new_metadata(monkeypatch):
    legacy = {"session_id": "old", "status": "completed", "attempts": []}
    monkeypatch.setattr(workflow.agent, "load_repair_audit", AsyncMock(return_value=legacy))
    db = SimpleNamespace(execute=AsyncMock(return_value=Result({})))
    assert await workflow.load("parent", db) == legacy


def test_limits_and_internal_review_only_contract():
    evidence = session()
    parent = child()
    parent.university_id, parent.university_name, parent.url = 7, "Test", "https://university.test"
    payload = workflow.verification_payload(evidence, parent)
    assert evidence["autonomous"]["limits"] == {
        "max_attempts": 5, "max_live_pages": 12, "max_live_seconds": 180, "max_verification_runs": 1,
    }
    assert payload["autonomousVerification"] == {
        "parent_job_id": "child", "session_id": "session-1",
        "max_courses": 50, "time_budget_seconds": 600, "cost_cap_usd": 2.0,
    }
    assert payload["forceDiscovery"] is True
    assert payload["fastMode"] is False
    assert not any("publish" in key.lower() or "approve" in key.lower() for key in payload)
    assert workflow.verification_id("session-1") == workflow.verification_id("session-1")
    assert workflow.verification_id("session-2") != workflow.verification_id("session-1")


def test_evidence_eligibility_is_measured_not_university_specific():
    assert workflow.has_catalogue_evidence({
        "pipeline_stats": {"expected_min_courses": 50, "after_filter": 7},
    })
    assert workflow.has_catalogue_evidence({"pipeline_stats": {"contamination_count": 3}})
    assert not workflow.has_catalogue_evidence({"university": "Any University"})
    assert not workflow.has_catalogue_evidence({
        "pipeline_stats": {"expected_min_courses": 5, "after_filter": 7},
    })


@pytest.mark.asyncio
async def test_database_writes_are_audit_and_parent_metadata_only(monkeypatch):
    evidence = session()
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(workflow.agent, "read_session", Mock(return_value={}))
    monkeypatch.setattr(workflow.agent, "_write_session", Mock())
    await workflow.lock(db, 7)
    await workflow.save(evidence, db)
    sql = "\n".join(str(call.args[0]) for call in db.execute.await_args_list)
    assert "pg_advisory_xact_lock" in sql
    assert "ai_repair_audits" in sql and "aiRepairWorkflow" in sql
    assert "scraped_courses" not in sql
    assert "DELETE" not in sql and "published" not in sql
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["rejected", "failed", "rolled_back"])
def test_accepted_initial_probe_cannot_bypass_rejected_proposals(outcome):
    evidence = session()
    evidence.update(
        status="completed", live_probe={"accepted": True, "status": "accepted"},
        attempts=[{"outcome": outcome, "patches_proposed": [{"field": "unsafe"}]}],
    )
    assert not workflow.accepted_live_probe(evidence)


def test_no_improvement_requires_independently_valid_recipe():
    evidence = session()
    evidence.update(
        status="completed", live_probe={"accepted": True, "status": "accepted"},
        attempts=[{"outcome": "no_change", "patches_proposed": [{"field": "unsafe"}]}],
    )
    assert not workflow.accepted_live_probe(evidence)
    evidence["attempts"] = [{
        "outcome": "no_change",
        "patches_proposed": [],
        "patches_applied": [],
        "rollback_status": "unchanged",
        "success_criteria": {"overall_ok": True, "criteria_pass": 6},
    }]
    assert workflow.accepted_live_probe(evidence)
    evidence["attempts"][0]["success_criteria"]["overall_ok"] = False
    assert not workflow.accepted_live_probe(evidence)
    evidence["attempts"] = [{"outcome": "no_change", "root_cause": "stale_job_evidence"}]
    assert workflow.accepted_live_probe(evidence)
    evidence["attempts"] = [{
        "outcome": "accepted", "patch_applied_ok": True, "live_validation": {"accepted": True},
    }]
    assert workflow.accepted_live_probe(evidence)
    evidence["attempts"][0]["live_validation"]["accepted"] = False
    assert not workflow.accepted_live_probe(evidence)


@pytest.mark.asyncio
async def test_monitor_persists_same_worker_live_phase_and_evidence(monkeypatch):
    evidence = session(worker_claim="delivery", phase="repairing")
    evidence["status"] = "running"
    live = {
        "session_id": "session-1", "current_attempt": 2,
        "attempts": [{"outcome": "accepted"}],
        "live_probe": {"accepted": True, "status": "accepted"},
        "autonomous": {"phase": "validating", "worker_claim": "delivery", "verification_job_id": "must-not-copy"},
    }
    db = SimpleNamespace(execute=AsyncMock(return_value=Result()), commit=AsyncMock())
    monkeypatch.setattr(workflow.agent, "read_session", Mock(return_value=live))
    monkeypatch.setattr(workflow.agent, "_write_session", Mock())
    await workflow.save(evidence, db)
    assert evidence["autonomous"]["phase"] == "validating"
    assert evidence["autonomous"]["worker_claim"] == "delivery"
    assert evidence["live_probe"]["accepted"] is True
    durable = db.execute.await_args_list[1].args[1]["evidence"]
    assert '"phase": "validating"' in durable
    assert '"accepted": true' in durable
    evidence["autonomous"].update(phase="verifying", verification_job_id="child")
    workflow.merge_live_progress(evidence, live)
    assert evidence["autonomous"]["phase"] == "verifying"


@pytest.mark.asyncio
async def test_hard_killed_repair_remains_fenced(memory):
    old = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    memory.evidence = session(worker_claim="original", worker_started_at=old, phase="repairing")
    memory.evidence["status"] = "running"
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["status"] == "failed"
    assert result["autonomous"]["phase"] == "blocked"
    workflow.agent.claim_repair_session.assert_not_called()
    workflow.agent.acquire_repair_lease.assert_not_called()


@pytest.mark.asyncio
async def test_stale_child_remains_fenced_without_reclaim(memory):
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    memory.evidence = session(phase="verifying", verification_job_id="child")
    memory.evidence["status"] = "running"
    memory.jobs["child"] = child("running", claimed_at=old, heartbeat_at=old, worker_id="dead")
    result = await workflow.reconcile("parent", "session-1", DB(memory))
    assert result["status"] == "failed"
    assert result["autonomous"]["phase"] == "blocked"
    assert memory.jobs["child"].status == "running"
    assert memory.jobs["child"].stop_requested is True
    assert not memory.added


def test_polling_independent_recovery_is_registered_on_existing_beat():
    from app.tasks.auto_repair_task import celery_app, recover_ai_repair_workflows

    schedule = celery_app.conf.beat_schedule["reconcile-autonomous-ai-repairs"]
    assert schedule["task"] == "ai_repair.reconcile_active"
    assert schedule["schedule"] == 60.0
    assert schedule["options"]["queue"] == "beat"
    assert recover_ai_repair_workflows.name == schedule["task"]


@pytest.mark.parametrize("stale_status", ["queued", "completed", "failed"])
def test_stale_audit_cannot_regress_or_finish_claimed_workflow(stale_status):
    backup = session(worker_claim="owner", phase="validating")
    backup.update(status="running", current_attempt=3)
    stale = {
        "session_id": "session-1", "status": stale_status, "current_attempt": 1,
        "autonomous": {"phase": "queued", "worker_claim": "owner"},
    }
    loaded = workflow.merge_audit(backup, stale)
    assert loaded["status"] == "running"
    assert loaded["autonomous"]["phase"] == "validating"
    assert loaded["current_attempt"] == 3
    assert not loaded["autonomous"].get("config_loop_done")


def test_only_current_worker_explicit_done_audit_advances_completion():
    backup = session(worker_claim="owner", phase="validating")
    backup["status"] = "running"
    completed = {
        "session_id": "session-1", "status": "completed",
        "autonomous": {"worker_claim": "wrong-owner", "config_loop_done": True},
    }
    assert workflow.merge_audit(backup, completed)["status"] == "running"
    completed["autonomous"]["worker_claim"] = "owner"
    merged = workflow.merge_audit(backup, completed)
    assert merged["status"] == "completed"
    assert merged["autonomous"]["config_loop_done"] is True
    assert merged["autonomous"]["config_loop_status"] == "completed"
    stale = {"session_id": "session-1", "status": "queued", "autonomous": {"phase": "queued"}}
    assert workflow.merge_audit(merged, stale)["status"] == "completed"
    queued = session()
    assert workflow.merge_audit(queued, completed)["status"] == "queued"


def test_stale_redis_cannot_invent_completion_child_or_replace_worker():
    durable = session(worker_claim="owner", phase="repairing")
    durable["status"] = "running"
    cached = {
        "session_id": "session-1", "status": "completed", "current_attempt": 2,
        "autonomous": {
            "worker_claim": "owner", "phase": "verified", "config_loop_done": True,
            "verification_job_id": "rogue-child",
        },
    }
    workflow.merge_live_progress(durable, cached)
    assert durable["status"] == "running"
    assert durable["autonomous"]["phase"] == "repairing"
    assert not durable["autonomous"].get("config_loop_done")
    assert not durable["autonomous"].get("verification_job_id")
    cached["autonomous"].update(worker_claim="wrong-owner", phase="validating")
    workflow.merge_live_progress(durable, cached)
    assert durable["autonomous"]["worker_claim"] == "owner"
    assert durable["autonomous"]["phase"] == "repairing"


@pytest.mark.asyncio
async def test_monitor_save_cannot_erase_concurrent_final_agent_audit(monkeypatch):
    evidence = session(worker_claim="owner", phase="repairing")
    evidence["status"] = "running"
    completed = {
        "session_id": "session-1", "status": "completed", "current_attempt": 1,
        "live_probe": {"accepted": True, "status": "accepted"},
        "autonomous": {"worker_claim": "owner", "config_loop_done": True},
    }
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(completed)), commit=AsyncMock())
    monkeypatch.setattr(workflow.agent, "read_session", Mock(return_value={}))
    monkeypatch.setattr(workflow.agent, "_write_session", Mock())
    await workflow.save(evidence, db)
    assert "FOR UPDATE" in str(db.execute.await_args_list[0].args[0])
    assert evidence["status"] == "completed"
    assert evidence["autonomous"]["config_loop_done"] is True
    assert evidence["live_probe"]["accepted"] is True


@pytest.mark.asyncio
async def test_newest_parent_binding_wins_over_late_older_audit(monkeypatch):
    backup = session(worker_claim="new-owner", phase="repairing")
    backup.update(session_id="new-session", status="running")
    stale = {"session_id": "old-session", "status": "completed"}
    correct = {**backup, "current_attempt": 2}
    monkeypatch.setattr(
        workflow.agent, "load_repair_audit", AsyncMock(side_effect=[stale, correct]),
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=Result({"aiRepairWorkflow": backup})))
    result = await workflow.load("parent", db)
    assert result["session_id"] == "new-session"
    assert result["status"] == "running"
    assert result["current_attempt"] == 2
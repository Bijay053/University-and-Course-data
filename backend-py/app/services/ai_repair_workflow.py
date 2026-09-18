"""Durable, fenced orchestration around the bounded repair agent.

Verification uses the normal scraper's internal ``autonomousVerification``
option: fresh extraction, isolated review rows and no resume checkpoints.
This is a bounded 50-course run, not a full catalogue coverage claim.
Celery delivery has a 10 minute soft / 11 minute hard limit;
the monitor also requests stop after ten minutes (including recovered delivery).
Normal scraper cost ceilings and per-course deadlines remain in force.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.scraper import ai_repair_agent as agent

LIMITS = {
    "max_attempts": 5,
    "max_live_pages": 12,
    "max_live_seconds": 180,
    "max_verification_runs": 1,
}
ACTIVE = {"queued", "running", "starting"}
CHILD_ACTIVE = {"queued", "running", "awaiting_approval"}
VERIFY_SECONDS = 600
REPAIR_SECONDS = 960
REQUEUE_SECONDS = 120
MAX_REQUEUES = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def age(value: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(value or "")).total_seconds()
    except (TypeError, ValueError):
        return 0


def autonomous_state() -> dict:
    return {
        "enabled": True, "phase": "queued", "limits": dict(LIMITS),
        "verification_limits": {
            "max_courses": 50, "time_budget_seconds": VERIFY_SECONDS, "cost_cap_usd": 2.0,
            "scope": "bounded fresh catalogue verification; not full catalogue coverage",
            "cost_scope": "returned-course extraction Gemini costs; not a hard total provider-spend budget",
        },
    }


def verification_id(session_id: str) -> str:
    return "job_air_" + hashlib.sha256(session_id.encode()).hexdigest()[:24]


def merge_live_progress(session: dict, live: dict) -> dict:
    """Enrich only this fenced worker's progress, never its child lifecycle."""
    state = session.get("autonomous") or {}
    if (
        session.get("status") != "running" or not state.get("worker_claim")
        or state.get("verification_job_id") or state.get("phase") in {"blocked", "needs_review", "verified"}
        or live.get("session_id") != session.get("session_id")
        or (live.get("autonomous") or {}).get("worker_claim") != state["worker_claim"]
        or int(live.get("current_attempt") or 0) < int(session.get("current_attempt") or 0)
    ):
        return session
    for key in ("current_attempt", "attempts", "live_probe"):
        if key in live:
            session[key] = live[key]
    phase = (live.get("autonomous") or {}).get("phase")
    if phase in {"live_probe", "repairing", "validating"}:
        state["phase"] = phase
    return session


def has_catalogue_evidence(config: dict) -> bool:
    """Require measured shortfall/contamination, never a university allowlist."""
    stats = (config or {}).get("pipeline_stats") or {}
    guard = (config or {}).get("catalogue_floor_guard") or stats.get("catalogue_floor_guard") or {}
    if guard.get("kind") in {"catalogue_below_expected_min", "discovery_filter_collapse"}:
        return True
    expected = stats.get("expected_min_courses") or guard.get("expected_min_courses") or 0
    actual = stats.get("after_filter")
    if isinstance(expected, (int, float)) and expected > 0 and isinstance(actual, (int, float)):
        if actual < expected:
            return True
    return any(
        isinstance(stats.get(key), (int, float)) and stats[key] > 0
        for key in ("contamination_count", "category_landing_page_count", "non_course_count")
    )


async def lock(db, university_id: int) -> None:
    # Same namespace as ordinary /start: child creation and normal starts serialize.
    await db.execute(text("SELECT pg_advisory_xact_lock(:uid)"), {"uid": university_id})


async def load(job_id: str, db, session_id: str | None = None) -> dict:
    session = await agent.load_repair_audit(job_id, db, session_id=session_id)
    # Older agent versions persist only a whitelist of evidence keys. Keep a
    # transactional backup on the *parent job*, never in published course data.
    payload = (await db.execute(
        text("SELECT request_payload FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
        {"j": job_id},
    )).scalar_one_or_none() or {}
    backup = payload.get("aiRepairWorkflow") or {}
    if backup and (not session_id or backup.get("session_id") == session_id):
        if session and session.get("session_id") != backup.get("session_id"):
            # updated_at is not a fencing token: a delayed older worker may
            # have written its audit after a newer session was created.
            session = await agent.load_repair_audit(
                job_id, db, session_id=backup["session_id"],
            )
        if not session or session.get("session_id") == backup.get("session_id"):
            session = merge_audit(backup, session)
    return session


def merge_audit(backup: dict, audit: dict) -> dict:
    """The workflow owns lifecycle; only its claimed agent can signal loop done."""
    state = dict(backup["autonomous"])
    terminal = state.get("phase") in {"blocked", "needs_review", "verified"}
    if terminal or state.get("verification_job_id") or state.get("config_loop_done"):
        merged = {**audit, **backup, "autonomous": state, "max_attempts": 5}
        if state.get("verification_job_id") and not terminal:
            merged.update(status="running", completed_at=None)
        return merged
    if not state.get("worker_claim"):
        # A stale queued/completed audit cannot invent a claim or finish an
        # as-yet unclaimed durable delivery.
        return {**audit, **backup, "autonomous": state, "status": "queued", "max_attempts": 5}
    loop_state = audit.get("autonomous") or {}
    owned = (
        audit.get("session_id") == backup.get("session_id")
        and loop_state.get("worker_claim") == state["worker_claim"]
    )
    merged = {**audit, **backup, "autonomous": state, "status": "running",
              "completed_at": None, "max_attempts": 5}
    if owned:
        if int(audit.get("current_attempt") or 0) >= int(backup.get("current_attempt") or 0):
            for key in (
                "current_attempt", "attempts", "live_probe", "quality_before",
                "uni_name", "rollback_status", "final_verdict", "error",
            ):
                if key in audit:
                    merged[key] = audit[key]
            if loop_state.get("phase") in {"live_probe", "repairing", "validating"}:
                state["phase"] = loop_state["phase"]
        if loop_state.get("config_loop_done") is True and audit.get("status") in {"completed", "failed"}:
            state.update(config_loop_done=True, config_loop_status=audit["status"])
            merged.update(status=audit["status"], completed_at=audit.get("completed_at"))
    return merged


async def active_audit(university_id: int, db) -> dict:
    """Include the durable workflow fence while an older agent writes completed."""
    row = (await db.execute(text(
        "SELECT a.evidence FROM ai_repair_audits a "
        "JOIN scrape_runtime_jobs j ON j.runtime_job_id = a.scrape_job_id "
        "WHERE a.university_id = :uid AND (a.status IN ('queued', 'starting', 'running') "
        "OR (j.request_payload->'aiRepairWorkflow'->>'session_id' = a.session_id "
        "AND j.request_payload->'aiRepairWorkflow'->'autonomous'->>'enabled' = 'true' "
        "AND j.request_payload->'aiRepairWorkflow'->'autonomous'->>'phase' "
        "IN ('queued', 'live_probe', 'repairing', 'validating', 'verification_queued', 'verifying'))) "
        "ORDER BY a.created_at DESC LIMIT 1"
    ), {"uid": university_id})).scalar_one_or_none()
    return dict(row or {})


async def save(session: dict, db) -> None:
    """Caller holds the university transaction lock; commit audit + backup together."""
    session["max_attempts"] = 5
    if (
        session.get("status") == "running"
        and session["autonomous"].get("worker_claim")
        and not session["autonomous"].get("verification_job_id")
        and session["autonomous"].get("phase") not in {"blocked", "needs_review", "verified"}
    ):
        # Serialize with the agent's audit upsert. Otherwise a monitor that
        # loaded running just before the final agent commit could overwrite
        # completed/config_loop_done and lose the crash-recovery boundary.
        existing = (await db.execute(text(
            "SELECT evidence FROM ai_repair_audits WHERE session_id = :sid FOR UPDATE"
        ), {"sid": session["session_id"]})).scalar_one_or_none()
        if existing:
            session.update(merge_audit(session, dict(existing)))
    # Capture progress BEFORE durable write so Redis loss does not erase the
    # live evidence or its current phase between agent audit checkpoints.
    merge_live_progress(session, agent.read_session(session["job_id"]))
    await db.execute(text(
        "INSERT INTO ai_repair_audits "
        "(session_id, scrape_job_id, university_id, status, evidence) "
        "VALUES (:sid, :jid, :uid, :status, CAST(:evidence AS JSONB)) "
        "ON CONFLICT (session_id) DO UPDATE SET status = EXCLUDED.status, "
        "evidence = ai_repair_audits.evidence || EXCLUDED.evidence, updated_at = NOW()"
    ), {"sid": session["session_id"], "jid": session["job_id"],
        "uid": session["university_id"], "status": session["status"],
        "evidence": json.dumps(session)})
    await db.execute(text(
        "UPDATE scrape_runtime_jobs SET request_payload = "
        "COALESCE(request_payload, '{}'::jsonb) || jsonb_build_object("
        "'aiRepairWorkflow', CAST(:evidence AS JSONB)) WHERE runtime_job_id = :jid"
    ), {"jid": session["job_id"], "evidence": json.dumps(session)})
    await db.commit()
    agent._write_session(session["job_id"], session)


async def owns(session: dict, db) -> bool:
    newest = await load(session["job_id"], db)
    if newest.get("session_id") != session["session_id"]:
        return False
    active = await active_audit(int(session["university_id"]), db)
    return not active or active.get("session_id") == session["session_id"]


async def finish(session: dict, db, phase: str, reason: str, *, failed: bool = False) -> dict:
    session["autonomous"].update(phase=phase, reason=reason)
    session.update(status="failed" if failed else "completed", completed_at=now())
    if failed:
        session["error"] = reason
    await save(session, db)
    agent.release_repair_lease(int(session["university_id"]), session["session_id"])
    return session


async def claim(job_id: str, university_id: int, session_id: str, claim_id: str, db) -> dict:
    await lock(db, university_id)
    session = await load(job_id, db, session_id)
    if not session or not await owns(session, db):
        await db.rollback()
        return {}
    state = session.get("autonomous") or {}
    # Durable claim fencing is authoritative even when Redis was flushed.
    if session.get("status") != "queued" or state.get("worker_claim"):
        await db.rollback()
        return {}
    active = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == university_id,
        ScrapeRuntimeJob.status.in_(CHILD_ACTIVE),
    ).limit(1))).scalar_one_or_none()
    if active:
        await finish(
            session, db, "blocked",
            "A university scrape became active before the repair worker claimed; no configuration changed.",
            failed=True,
        )
        return {}
    if not (agent.renew_repair_lease(university_id, session_id)
            or agent.acquire_repair_lease(university_id, session_id)):
        await db.rollback()
        return {}
    agent._write_session(job_id, session)
    if not agent.claim_repair_session(job_id, university_id, session_id):
        await db.rollback()
        return {}
    state.update(phase="live_probe", worker_claim=claim_id, worker_started_at=now())
    session.update(status="running", started_at=now(), autonomous=state)
    await save(session, db)
    return session


def accepted_live_probe(session: dict) -> bool:
    probe = session.get("live_probe") or {}
    accepted = (
        probe.get("accepted") is True
        and probe.get("status") == "accepted"
        and session.get("status") != "failed"
    )
    attempts = session.get("attempts") or []
    if not accepted or not attempts:
        return False
    last = attempts[-1]
    if last.get("validation_errors") or last.get("outcome") not in {"accepted", "no_change"}:
        return False
    if last.get("rollback_status") in {"failed", "restored"}:
        return False
    if last.get("outcome") == "accepted":
        return last.get("patch_applied_ok") is True and (last.get("live_validation") or {}).get("accepted") is True
    # A known-valid current recipe is the sole no-improvement exception.
    # Rejected/stripped unsafe proposals must not piggyback on an initial probe.
    return not last.get("patches_proposed") and (
        (last.get("live_validation") or {}).get("accepted") is True
        or last.get("root_cause") == "stale_job_evidence"
    )


def verification_payload(session: dict, parent: ScrapeRuntimeJob) -> dict:
    return {
        "url": parent.url, "universityId": parent.university_id,
        "universityName": parent.university_name, "university_id": parent.university_id,
        "fastMode": False, "fast_mode": False, "forceDiscovery": True,
        "autonomousVerification": {
            "parent_job_id": parent.runtime_job_id, "session_id": session["session_id"],
            "max_courses": 50, "time_budget_seconds": VERIFY_SECONDS, "cost_cap_usd": 2.0,
        },
        "aiRepairSessionId": session["session_id"], "aiRepairParentJobId": session["job_id"],
        "aiRepairIdempotencyKey": verification_id(session["session_id"]),
    }


async def queue_verification(session: dict, db) -> dict:
    await lock(db, int(session["university_id"]))
    if not await owns(session, db):
        await db.rollback()
        return session
    durable = await load(session["job_id"], db, session["session_id"])
    state = durable.get("autonomous") or session["autonomous"]
    result_state = session["autonomous"]
    if (
        result_state.get("config_loop_done") is True
        and result_state.get("worker_claim") == state.get("worker_claim")
    ):
        state.update(config_loop_done=True, config_loop_status=session.get("status"))
    session["autonomous"] = state
    if state.get("verification_job_id") or state.get("phase") in {"blocked", "needs_review", "verified"}:
        await db.rollback()
        return durable
    lease_owned = agent.renew_repair_lease(int(session["university_id"]), session["session_id"])
    if (
        not lease_owned and state.get("config_loop_done") is True
        and durable.get("status") == "completed"
    ):
        # Redis was flushed after the durable final config audit. Only a
        # finished loop may restore its own missing key, never an active worker.
        lease_owned = agent.acquire_repair_lease(int(session["university_id"]), session["session_id"])
    if not lease_owned:
        return await finish(session, db, "blocked", "Repair lease lost before verification.", failed=True)
    if state.get("config_loop_done") is not True:
        return await finish(session, db, "blocked", "The claimed repair worker has no durable loop-completion signal.")
    if not accepted_live_probe(session):
        return await finish(
            session, db, "blocked", "Accepted live validation is required; no scrape launched.",
            failed=session.get("status") == "failed",
        )
    parent = await db.get(ScrapeRuntimeJob, session["job_id"])
    active = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == session["university_id"],
        ScrapeRuntimeJob.status.in_(CHILD_ACTIVE),
    ).limit(1))).scalar_one_or_none()
    if active:
        return await finish(session, db, "blocked", "Another university scrape is active; no verification launched.")
    child_id = verification_id(session["session_id"])
    child = await db.get(ScrapeRuntimeJob, child_id)
    if child is None:
        child = ScrapeRuntimeJob(
            runtime_job_id=child_id, university_id=parent.university_id,
            university_name=parent.university_name, url=parent.url, job_type="scrape",
            status="queued", fast_mode=False, request_payload=verification_payload(session, parent),
        )
        db.add(child)
    state.update(phase="verification_queued", verification_job_id=child_id,
                 verification_status=child.status, verification_queued_at=now(),
                 verification_requeues=0)
    session.update(status="running", completed_at=None)
    # The row and idempotency binding are committed BEFORE delivery. Recovery
    # can redeliver this ID, but can never create another verification run.
    await save(session, db)
    return await dispatch_verification(session, db)


async def dispatch_verification(session: dict, db) -> dict:
    from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock
    await lock(db, int(session["university_id"]))
    if not await owns(session, db):
        await db.rollback()
        return session
    session = await load(session["job_id"], db, session["session_id"])
    state = session["autonomous"]
    dispatch_round = int(state.get("verification_requeues") or 0)
    if int(state.get("verification_dispatch_round", -1)) >= dispatch_round:
        await db.rollback()
        return session
    child = await db.get(
        ScrapeRuntimeJob, state["verification_job_id"],
        populate_existing=True, with_for_update=True,
    )
    if not child or child.status != "queued" or child.claimed_at or child.worker_id:
        await db.rollback()
        return session
    try:
        set_initial_dispatch_lock(child.runtime_job_id)
        scrape_university.apply_async(
            args=[child.runtime_job_id], queue="scrape",
            soft_time_limit=VERIFY_SECONDS, time_limit=VERIFY_SECONDS + 60,
        )
    except Exception as exc:
        child.status = "failed"
        child.error_message = f"Verification queue delivery failed: {exc}"
        child.completed_at = datetime.now(timezone.utc)
        state["verification_status"] = "failed"
        return await finish(session, db, "blocked", child.error_message, failed=True)
    state["verification_last_dispatch_at"] = now()
    state["verification_dispatch_round"] = dispatch_round
    await save(session, db)
    return session


def compare_quality(before: dict, after: dict, child: ScrapeRuntimeJob) -> dict:
    fields = (
        "fee_pct", "ielts_pct", "location_pct", "duration_pct", "course_name_pct",
        "intakes_pct", "mode_pct", "degree_level_pct",
    )
    regressions = [key for key in fields if after.get(key, 0) < before.get(key, 0)]
    unresolved = [key for key in fields if after.get(key, 0) < 100]
    stats = (child.discovered_config or {}).get("pipeline_stats") or {}
    gates = child.gate_skip_counts or {}
    guard = gates.get("catalogue_guard") or stats.get("catalogue_floor_guard")
    contamination = {
        key: stats.get(key) or gates.get(key) for key in
        ("contamination_count", "non_course_count", "category_landing_page_count")
        if stats.get(key) or gates.get(key)
    }
    clean = (
        child.status == "completed" and child.imported > 0 and child.errors == 0
        and child.imported >= child.total_found and child.skipped == 0
        and after.get("total_staged", 0) > 0 and not child.cost_ceiling_hit
        and not regressions and not unresolved
        and not after.get("bad_course_names") and not after.get("bad_locations")
        and not guard and not contamination
    )
    return {
        "baseline": before, "verification": after, "regressions": regressions,
        "unresolved_fields": unresolved, "sample_verified": clean,
        "catalogue_guard": guard, "contamination": contamination,
        "full_catalogue_verified": False, "scope": "bounded fresh verification; full catalogue coverage unverified",
        "counters": {key: getattr(child, key, None)
                     for key in ("total_found", "imported", "skipped", "errors", "cost_ceiling_hit")},
    }


def verification_metadata(child: ScrapeRuntimeJob) -> dict:
    payload = (child.request_payload or {}).get("autonomousVerification") or {}
    measured = (child.discovered_config or {}).get("autonomousVerification") or {}
    return {**payload, **measured}


async def run(job_id: str, university_id: int, session_id: str, claim_id: str, db) -> dict:
    session = await claim(job_id, university_id, session_id, claim_id, db)
    if not session:
        return {"job_id": job_id, "status": "duplicate_or_unowned"}
    try:
        result = await agent.run_ai_repair_loop(
            job_id, db, lease_token=session_id, durable_session=session,
        )
    except Exception as exc:
        await db.rollback()
        await lock(db, university_id)
        if not await owns(session, db):
            await db.rollback()
            return {"job_id": job_id, "status": "superseded"}
        return await finish(session, db, "blocked", f"Repair worker failed: {exc}", failed=True)
    session = {
        **session, **result,
        "autonomous": {**(result.get("autonomous") or {}), **session["autonomous"]},
        "max_attempts": 5,
    }
    return await queue_verification(session, db)


async def reconcile(job_id: str, session_id: str, db) -> dict:
    session = await load(job_id, db, session_id)
    if not session or not session.get("autonomous", {}).get("enabled"):
        return session
    await lock(db, int(session["university_id"]))
    if not await owns(session, db):
        await db.rollback()
        return session
    # Reload after obtaining the lock so concurrent poll/monitor calls cannot
    # consume the same delivery budget or overwrite newer worker progress.
    session = await load(job_id, db, session_id)
    state = session["autonomous"]
    phase = state.get("phase")
    if phase in {"verified", "needs_review", "blocked"}:
        await db.rollback()
        return session
    merge_live_progress(session, agent.read_session(job_id))
    child_id = state.get("verification_job_id")
    if child_id:
        child = await db.get(ScrapeRuntimeJob, child_id, populate_existing=True, with_for_update=True)
        if not child:
            return await finish(session, db, "blocked", "Durable verification job is missing.", failed=True)
        state["verification_status"] = child.status
        if child.status == "awaiting_approval":
            return await finish(
                session, db, "needs_review",
                "Verification requires a human decision; no approval or publication was performed.",
            )
        if child.status not in CHILD_ACTIVE:
            after = await agent._quality_snapshot(child_id, int(session["university_id"]), db)
            comparison = compare_quality(session.get("quality_before") or {}, after, child)
            comparison["baseline_counters"] = state.get("baseline_counters") or {}
            state["comparison"] = comparison
            metadata = verification_metadata(child)
            cap = int(
                metadata.get("effective_max_courses") or metadata.get("max_courses")
                or state.get("verification_limits", {}).get("max_courses") or 50
            )
            capped = bool(metadata.get("capped") or metadata.get("limit_reached")) or child.total_found >= cap
            comparison["capped"] = capped
            comparison["verification_limits"] = metadata
            verified = (
                comparison["sample_verified"] and not state.get("catalogue_problem") and not capped
                and metadata.get("coverage_measured") is True
                and metadata.get("capped") is False and not metadata.get("limit_reached")
                and not metadata.get("budget_exhausted") and not metadata.get("warning")
            )
            return await finish(
                session, db, "verified" if verified else "needs_review",
                "Bounded fresh verification passed; full catalogue coverage is not certified." if verified
                else "Verification failed, regressed, or left unresolved quality/coverage; manual review required.",
            )
        session.update(status="running", completed_at=None)
        if not (agent.renew_repair_lease(int(session["university_id"]), session_id)
                or agent.acquire_repair_lease(int(session["university_id"]), session_id)):
            # Never replace another lease. Request cancellation; retain the
            # durable active fence until the normal child acknowledges stopping.
            child.stop_requested = True
            state.update(phase="verifying", reason="Verification lease lost; cancellation requested.")
            await save(session, db)
            return session
        if child.status == "queued" and not child.claimed_at and not child.worker_id:
            state["phase"] = "verification_queued"
            since = state.get("verification_last_dispatch_at") or state.get("verification_queued_at")
            if age(since) > REQUEUE_SECONDS:
                count = int(state.get("verification_requeues") or 0)
                if count >= MAX_REQUEUES:
                    child.status = "failed"
                    child.error_message = "Verification delivery recovery exhausted (2 requeues)."
                    state["verification_status"] = "failed"
                    return await finish(session, db, "blocked", child.error_message, failed=True)
                state["verification_requeues"] = count + 1
                await save(session, db)
                return await dispatch_verification(session, db)
        else:
            state["phase"] = "verifying"
            started = child.claimed_at or child.started_at
            if started and age(started.isoformat()) > VERIFY_SECONDS:
                child.stop_requested = True
                state["reason"] = "Verification time limit reached; cancellation requested."
                heartbeat = child.heartbeat_at or started
                if age(started.isoformat()) > VERIFY_SECONDS + 180 and age(heartbeat.isoformat()) > 180:
                    # No distributed process-death proof exists here. Do NOT
                    # reset/requeue a possibly live child or fabricate a failed
                    # job status. Its active row remains the university fence
                    # until the standard dead-worker recovery handles it.
                    return await finish(
                        session, db, "blocked",
                        "Verification deadline and heartbeat expired; stop requested. "
                        "The existing child remains fenced for dead-worker recovery; no replacement was launched.",
                        failed=True,
                    )
        await save(session, db)
        return session
    if state.get("worker_claim"):
        if session.get("status") in {"completed", "failed"}:
            # Worker died after the agent's final durable audit but before
            # child creation. Never rerun the AI loop to recover this boundary.
            await db.commit()
            return await queue_verification(session, db)
        if age(state.get("worker_started_at")) > REPAIR_SECONDS + 120:
            return await finish(session, db, "blocked", "Repair worker exceeded its bounded lifetime.", failed=True)
        agent.renew_repair_lease(int(session["university_id"]), session_id)
        session["status"] = "running"
        await save(session, db)
        if session["autonomous"].get("config_loop_done") and session.get("status") in {"completed", "failed"}:
            return await queue_verification(session, db)
        return session
    if age(state.get("last_dispatch_at") or session.get("queued_at")) > REQUEUE_SECONDS:
        count = int(state.get("requeues") or 0)
        if count >= MAX_REQUEUES:
            return await finish(session, db, "blocked", "Repair delivery recovery exhausted (2 requeues).", failed=True)
        if not (agent.renew_repair_lease(int(session["university_id"]), session_id)
                or agent.acquire_repair_lease(int(session["university_id"]), session_id)):
            return await finish(session, db, "blocked", "Queued repair no longer owns the lease.", failed=True)
        state.update(requeues=count + 1, last_dispatch_at=now())
        await save(session, db)
        try:
            from app.tasks.auto_repair_task import run_ai_scrape_repair
            run_ai_scrape_repair.apply_async(args=[job_id, session["university_id"], session_id], queue="scrape")
        except Exception as exc:
            await lock(db, int(session["university_id"]))
            if await owns(session, db):
                current = await load(job_id, db, session_id)
                if current.get("autonomous", {}).get("worker_claim"):
                    await db.rollback()
                    return current
                return await finish(current, db, "blocked", f"Repair queue delivery failed: {exc}", failed=True)
    else:
        await db.rollback()
    return session
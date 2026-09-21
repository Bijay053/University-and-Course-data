"""Durable, fenced orchestration around the bounded repair agent.

Verification uses the normal scraper's internal ``autonomousVerification``
option: fresh extraction, isolated review rows and no resume checkpoints.
This is at most two bounded children over one 50-course sample, not a full
catalogue coverage claim. A second child may process only the exact URLs that
the terminal first child selected but did not stage.
Each Celery delivery has a 10 minute soft / 11 minute hard limit;
the monitor also requests stop after ten minutes (including recovered delivery).
Normal scraper cost ceilings and per-course deadlines remain in force.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models import ScrapedCourse
from app.services.scraper import ai_repair_agent as agent
from app.services.scraper.url_identity import canonical_course_url_key

LIMITS = {
    "max_attempts": 5,
    "max_live_pages": 12,
    "max_live_seconds": 180,
    "max_verification_runs": 2,
}
ACTIVE = {"queued", "running", "starting"}
CHILD_ACTIVE = {"queued", "running", "awaiting_approval"}
RECOVERING = "recovering"
VERIFY_SECONDS = 600
MAX_VERIFICATION_RUNS = 2
TOTAL_VERIFY_SECONDS = VERIFY_SECONDS * MAX_VERIFICATION_RUNS
TOTAL_VERIFY_COST_USD = 2.0
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
            "max_runs": MAX_VERIFICATION_RUNS,
            "total_time_budget_seconds": TOTAL_VERIFY_SECONDS,
            "total_cost_cap_usd": TOTAL_VERIFY_COST_USD,
            "scope": "bounded fresh catalogue verification; not full catalogue coverage",
            "cost_scope": "returned-course extraction Gemini costs; not a hard total provider-spend budget",
        },
    }


def verification_id(session_id: str, round_index: int = 0) -> str:
    seed = session_id if round_index == 0 else f"{session_id}:continuation:{round_index}"
    return "job_air_" + hashlib.sha256(seed.encode()).hexdigest()[:24]


def merge_live_progress(session: dict, live: dict) -> dict:
    """Enrich only this fenced worker's progress, never its child lifecycle."""
    state = session.get("autonomous") or {}
    if (
        session.get("status") != "running" or not state.get("worker_claim")
        or state.get("verification_job_id") or state.get("phase") in {"blocked", "needs_review", "verified"}
        or live.get("session_id") != session.get("session_id")
        or (live.get("autonomous") or {}).get("worker_claim") != state["worker_claim"]
        or (live.get("autonomous") or {}).get("worker_generation") != state.get("worker_generation")
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
        and loop_state.get("worker_generation") == state.get("worker_generation")
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
        "IN ('queued', 'live_probe', 'repairing', 'validating', 'verification_queued', 'verifying', 'recovering'))) "
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
    session.update(
        status="failed" if failed else "completed",
        completed_at=now(),
        final_verdict=reason,
    )
    if failed:
        session["error"] = reason
    await save(session, db)
    agent.release_repair_lease(int(session["university_id"]), session["session_id"])
    return session


async def block_unclaimed_initialization(
    job_id: str, university_id: int, session_id: str, db, *, code: str, reason: str,
) -> dict:
    """Record a known pre-claim failure without revoking or replacing ownership."""
    await lock(db, university_id)
    session = await load(job_id, db, session_id)
    if not session or not await owns(session, db):
        await db.rollback()
        return {}
    state = session.get("autonomous") or {}
    if (
        session.get("status") != "queued" or state.get("worker_claim") or state.get("worker_generation")
        or state.get("verification_job_id")
        or state.get("phase") in {"blocked", "needs_review", "verified"}
    ):
        await db.rollback()
        return session
    state["initialization_failure"] = {
        "code": code, "stage": "pre_claim", "observed_at": now(),
        "message": reason,
    }
    return await finish(session, db, "blocked", reason, failed=True)


async def schedule_recovery(
    session: dict,
    db,
    reason: str,
    next_action: str,
    *,
    target: str = "repair",
    wait_for_collision: bool = False,
    dispatch_exhausted: bool = False,
) -> dict:
    """Persist a bounded, idempotent retry instead of stranding infrastructure failures."""
    state = session["autonomous"]
    wait_attempts = int(state.get("recovery_wait_attempts") or 0)
    if wait_for_collision:
        wait_attempts += 1
        state["recovery_wait_attempts"] = wait_attempts
    recovery = dict(state.get("recovery") or {})
    dispatch_attempts = int(
        state.get("verification_requeues" if target == "verification" else "requeues") or 0
    )
    if dispatch_exhausted or wait_attempts > MAX_REQUEUES:
        recovery.update(
            wait_attempts=wait_attempts,
            dispatch_attempts=dispatch_attempts,
            cap=MAX_REQUEUES,
            exhausted=True,
            last_reason=reason,
            next_action=next_action,
        )
        state.update(
            recovery=recovery,
            recovery_exhausted=True,
            next_action=next_action,
        )
        return await finish(
            session,
            db,
            "blocked",
            f"Automatic recovery exhausted after "
            f"{dispatch_attempts if dispatch_exhausted else wait_attempts} "
            f"{'worker redispatch attempts (in addition to initial delivery)' if dispatch_exhausted and target == 'repair' else 'worker dispatch attempts' if dispatch_exhausted else 'wait attempts'}. "
            f"{reason} "
            f"{next_action}",
            failed=True,
        )

    recovery.update(
        wait_attempts=wait_attempts,
        dispatch_attempts=dispatch_attempts,
        cap=MAX_REQUEUES,
        exhausted=False,
        last_reason=reason,
        next_action=next_action,
        target=target,
    )
    state.update(
        phase=RECOVERING,
        recovery=recovery,
        recovery_exhausted=False,
        next_action=next_action,
        recovery_target=target,
        last_dispatch_at=now(),
    )
    # A claimed repair worker remains fenced. Verification recovery after a
    # completed config loop deliberately remains completed so the next
    # queue_verification call can reacquire its lease.
    session.update(
        status="completed" if target == "verification" and state.get("config_loop_done") else "queued",
        completed_at=None,
    )
    await save(session, db)
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
        await schedule_recovery(
            session, db,
            "A university scrape became active before the repair worker claimed; no configuration changed.",
            "The active scrape will finish automatically, then the repair will retry.",
            target="repair",
            wait_for_collision=True,
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
    from app.services.worker_fencing import claim as claim_generation
    owner = await claim_generation(db, f"repair:{session_id}", claim_id)
    if owner is None:
        await db.rollback()
        return {}
    state["worker_generation"] = owner.generation
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
    criteria = last.get("success_criteria") or {}
    unchanged_valid_recipe = (
        criteria.get("overall_ok") is True
        and not last.get("patches_applied")
        and last.get("rollback_status") in {None, "unchanged"}
    )
    return not last.get("patches_proposed") and (
        (last.get("live_validation") or {}).get("accepted") is True
        or last.get("root_cause") == "stale_job_evidence"
        or unchanged_valid_recipe
    )


def verification_payload(
    session: dict,
    parent: ScrapeRuntimeJob,
    *,
    round_index: int = 0,
    course_urls: list[str] | None = None,
    cost_cap_usd: float = TOTAL_VERIFY_COST_USD,
    time_budget_seconds: float = VERIFY_SECONDS,
) -> dict:
    payload = {
        "url": parent.url, "universityId": parent.university_id,
        "universityName": parent.university_name, "university_id": parent.university_id,
        "fastMode": False, "fast_mode": False, "forceDiscovery": True,
        "autonomousVerification": {
            "parent_job_id": parent.runtime_job_id, "session_id": session["session_id"],
            "max_courses": 50, "time_budget_seconds": time_budget_seconds,
            "cost_cap_usd": cost_cap_usd, "round_index": round_index,
        },
        "aiRepairSessionId": session["session_id"], "aiRepairParentJobId": session["job_id"],
        "aiRepairIdempotencyKey": verification_id(session["session_id"], round_index),
    }
    if course_urls:
        payload["courseUrls"] = list(course_urls)
    return payload


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
        and result_state.get("worker_generation") == state.get("worker_generation")
    ):
        state.update(config_loop_done=True, config_loop_status=session.get("status"))
    session["autonomous"] = state
    if state.get("verification_job_id") or state.get("phase") in {"blocked", "needs_review", "verified"}:
        await db.rollback()
        return durable
    if state.get("config_loop_done") is not True:
        return await finish(
            session, db, "blocked",
            "The claimed repair worker has no durable loop-completion signal.",
        )
    lease_owned = agent.renew_repair_lease(int(session["university_id"]), session["session_id"])
    if (
        not lease_owned and state.get("config_loop_done") is True
        and durable.get("status") in {"completed", "queued"}
    ):
        # Redis was flushed after the durable final config audit. Only a
        # finished loop may restore its own missing key, never an active worker.
        lease_owned = agent.acquire_repair_lease(int(session["university_id"]), session["session_id"])
    if not lease_owned:
        return await finish(session, db, "blocked", "Repair lease lost before verification.", failed=True)
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
        return await schedule_recovery(
            session, db,
            "Another university scrape is active; verification was not launched.",
            "The active scrape will finish automatically, then verification will retry.",
            target="verification",
            wait_for_collision=True,
        )
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
                  verification_job_ids=[child_id], verification_round=0,
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
    dispatch_attempts = int(state.get("verification_requeues") or 0)
    dispatch_timestamp = (
        state.get("verification_dispatch_started_at")
        or state.get("verification_last_dispatch_at")
    )
    if (
        int(state.get("verification_dispatch_round", -1)) == dispatch_attempts
        and age(dispatch_timestamp) < REQUEUE_SECONDS
    ):
        await db.rollback()
        return session
    if dispatch_attempts >= MAX_REQUEUES:
        state.update(
            recovery_exhausted=True,
            next_action="Check the application's repair worker, task queue, and broker connection before retrying; this is not evidence of a university website failure.",
        )
        recovery = dict(state.get("recovery") or {})
        recovery.update(
            dispatch_attempts=dispatch_attempts,
            cap=MAX_REQUEUES,
            exhausted=True,
            next_action=state["next_action"],
        )
        state["recovery"] = recovery
        return await finish(
            session, db, "blocked",
            f"Automatic recovery exhausted after {dispatch_attempts} worker dispatch attempts without a durable worker claim. "
            f"{state['next_action']}",
            failed=True,
        )
    child = await db.get(
        ScrapeRuntimeJob, state["verification_job_id"],
        populate_existing=True, with_for_update=True,
    )
    if not child or child.status != "queued" or child.claimed_at or child.worker_id:
        await db.rollback()
        return session
    dispatch_attempts += 1
    state["verification_requeues"] = dispatch_attempts
    state["verification_dispatch_round"] = dispatch_attempts
    state["verification_dispatch_started_at"] = now()
    recovery = dict(state.get("recovery") or {})
    recovery.update(dispatch_attempts=dispatch_attempts, cap=MAX_REQUEUES)
    state["recovery"] = recovery
    await save(session, db)
    try:
        set_initial_dispatch_lock(child.runtime_job_id)
        scrape_university.apply_async(
            args=[child.runtime_job_id], queue="scrape",
            soft_time_limit=VERIFY_SECONDS, time_limit=VERIFY_SECONDS + 60,
        )
    except Exception as exc:
        child.error_message = f"Verification queue delivery deferred: {exc}"
        state["verification_status"] = "queued"
        # A failed attempt is retryable after the recovery delay.
        state["verification_dispatch_round"] = dispatch_attempts - 1
        state.pop("verification_dispatch_started_at", None)
        return await schedule_recovery(
            session,
            db,
            "Verification queue delivery failed.",
            "Verification will be redispatched automatically; no second verification job will be created.",
            target="verification",
        )
    state.pop("verification_dispatch_started_at", None)
    state["verification_last_dispatch_at"] = now()
    await save(session, db)
    return session


def compare_quality(
    before: dict,
    after: dict,
    child: ScrapeRuntimeJob,
    *,
    combined_safety: dict | None = None,
) -> dict:
    fields = (
        "fee_pct", "ielts_pct", "location_pct", "duration_pct", "course_name_pct",
        "intakes_pct", "mode_pct", "degree_level_pct",
    )
    regressions = [key for key in fields if after.get(key, 0) < before.get(key, 0)]
    unresolved = [key for key in fields if after.get(key, 0) < 100]
    stats = (child.discovered_config or {}).get("pipeline_stats") or {}
    gates = child.gate_skip_counts or {}
    persisted_quality = gates.get("data_quality") or {}
    guard = gates.get("catalogue_guard") or stats.get("catalogue_floor_guard")
    contamination = {
        key: stats.get(key) or gates.get(key) for key in
        ("contamination_count", "non_course_count", "category_landing_page_count")
        if stats.get(key) or gates.get(key)
    }
    safety = combined_safety or {
        "safe": (
            child.status == "completed" and child.errors == 0 and child.skipped == 0
            and not child.cost_ceiling_hit and not guard and not contamination
        ),
        "catalogue_guards": [guard] if guard else [],
        "contamination": contamination,
        "unsafe_children": [],
    }
    critical_quality_count = max(
        int(after.get("critical_quality_count") or 0),
        int(persisted_quality.get("affected_course_count") or 0),
        int(persisted_quality.get("critical_count") or 0),
        int(safety.get("critical_quality_count") or 0),
    )
    clean = (
        child.status == "completed" and child.imported > 0 and child.errors == 0
        and child.imported >= child.total_found and child.skipped == 0
        and after.get("total_staged", 0) > 0 and not child.cost_ceiling_hit
        and not regressions and not unresolved
        and not after.get("bad_course_names") and not after.get("bad_locations")
        and critical_quality_count == 0
        and not guard and not contamination
        and safety.get("safe") is True
    )
    return {
        "baseline": before, "verification": after, "regressions": regressions,
        "unresolved_fields": unresolved, "sample_verified": clean,
        "critical_quality_count": critical_quality_count,
        "critical_quality": persisted_quality,
        "catalogue_guard": guard, "contamination": contamination,
        "combined_safety": safety,
        "full_catalogue_verified": False, "scope": "bounded fresh verification; full catalogue coverage unverified",
        "counters": {key: getattr(child, key, None)
                     for key in ("total_found", "current", "imported", "skipped", "errors", "cost_ceiling_hit")},
    }


def verification_metadata(child: ScrapeRuntimeJob) -> dict:
    payload = (child.request_payload or {}).get("autonomousVerification") or {}
    measured = (child.discovered_config or {}).get("autonomousVerification") or {}
    return {**payload, **measured}


def _url_key(value: str | None) -> str:
    return canonical_course_url_key(value or "") or ""


async def _staged_url_keys(child_id: str, university_id: int, db) -> set[str]:
    rows = (await db.execute(
        select(ScrapedCourse.canonical_course_url, ScrapedCourse.course_website).where(
            ScrapedCourse.scrape_job_id == child_id,
            ScrapedCourse.university_id == university_id,
        )
    )).all()
    keys: set[str] = set()
    for canonical, legacy_url in rows:
        # New rows persist the exact canonical identity used by staging.
        # Legacy/null rows fall back to deriving that same identity from the
        # stored course URL. Never treat a null identity as completed.
        key = _url_key(canonical) if canonical else _url_key(legacy_url)
        if key:
            keys.add(key)
    return keys


def _canonical_dedup_urls(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        key = _url_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _child_elapsed(child: ScrapeRuntimeJob) -> float:
    if not child:
        return 0.0
    started = child.started_at or child.claimed_at
    ended = child.completed_at
    if not started or not ended:
        return 0.0
    return max(0.0, (ended - started).total_seconds())


async def _combined_quality(child_ids: list[str], university_id: int, db) -> dict:
    snapshots = [
        await agent._quality_snapshot(child_id, university_id, db)
        for child_id in child_ids
    ]
    total = sum(int(item.get("total_staged") or 0) for item in snapshots)
    if not total:
        return snapshots[-1] if snapshots else {}
    result = dict(snapshots[-1])
    result["total_staged"] = total
    for key in (
        "fee_pct", "ielts_pct", "location_pct", "duration_pct", "course_name_pct",
        "intakes_pct", "mode_pct", "degree_level_pct", "academic_level_pct",
    ):
        result[key] = round(sum(
            float(item.get(key) or 0) * int(item.get("total_staged") or 0)
            for item in snapshots
        ) / total)
    for key in ("bad_course_names", "bad_locations", "critical_quality_count"):
        result[key] = sum(int(item.get(key) or 0) for item in snapshots)
    for key in (
        "sample_locations", "sample_bad_locations", "sample_degrees",
        "sample_modes", "sample_bad_course_names",
    ):
        combined: list = []
        for item in snapshots:
            for value in item.get(key) or []:
                if value not in combined:
                    combined.append(value)
        result[key] = combined[:8]
    critical_rows: list[dict] = []
    for child_id, item in zip(child_ids, snapshots):
        for row in item.get("critical_quality_rows") or []:
            detail = {**row, "verification_job_id": child_id}
            if detail not in critical_rows:
                critical_rows.append(detail)
    result["critical_quality_rows"] = critical_rows[:20]
    result["critical_quality_issues"] = list(result["critical_quality_rows"])
    return result


def _combined_child_safety(children: list[ScrapeRuntimeJob]) -> dict:
    """Aggregate every safety signal; only prior acknowledged timeout status is exempt."""
    unsafe_children: list[dict] = []
    catalogue_guards: list[dict] = []
    contamination: dict[str, int] = {}
    critical_quality_count = 0
    cumulative_elapsed = 0.0
    for index, item in enumerate(children):
        if not item:
            unsafe_children.append({"reason": "missing_child"})
            continue
        metadata = verification_metadata(item)
        stats = (item.discovered_config or {}).get("pipeline_stats") or {}
        gates = item.gate_skip_counts or {}
        persisted_quality = gates.get("data_quality") or {}
        child_critical_quality_count = max(
            int(persisted_quality.get("affected_course_count") or 0),
            int(persisted_quality.get("critical_count") or 0),
        )
        critical_quality_count += child_critical_quality_count
        guard = gates.get("catalogue_guard") or stats.get("catalogue_floor_guard")
        if guard:
            catalogue_guards.append({"job_id": item.runtime_job_id, "guard": guard})
        child_contamination = {
            key: int(stats.get(key) or gates.get(key) or 0)
            for key in ("contamination_count", "non_course_count", "category_landing_page_count")
            if stats.get(key) or gates.get(key)
        }
        for key, value in child_contamination.items():
            contamination[key] = contamination.get(key, 0) + value
        prior_timeout = (
            index < len(children) - 1
            and item.status == "failed_degraded"
            and metadata.get("budget_exhausted") == "time_budget_exhausted"
            and bool(getattr(item, "completed_at", None))
        )
        reasons: list[str] = []
        if item.status != "completed" and not prior_timeout:
            reasons.append(f"status:{item.status}")
        if metadata.get("budget_exhausted") and not prior_timeout:
            reasons.append(f"budget_exhausted:{metadata['budget_exhausted']}")
        warning = metadata.get("warning")
        expected_timeout_warning = (
            prior_timeout
            and isinstance(warning, str)
            and warning.startswith("Autonomous verification exceeded ")
        )
        if warning and not expected_timeout_warning:
            reasons.append("warning")
        if metadata.get("capped") is True:
            reasons.append("capped")
        if metadata.get("limit_reached") is True:
            reasons.append("limit_reached")
        if int(getattr(item, "errors", 0) or 0):
            reasons.append("errors")
        if int(getattr(item, "skipped", 0) or 0):
            reasons.append("skipped")
        if bool(getattr(item, "cost_ceiling_hit", False)):
            reasons.append("cost_ceiling_hit")
        if guard:
            reasons.append("catalogue_guard")
        if child_contamination:
            reasons.append("contamination")
        if child_critical_quality_count:
            reasons.append("critical_data_quality")
        if reasons:
            unsafe_children.append({"job_id": item.runtime_job_id, "reasons": reasons})
        cumulative_elapsed += _child_elapsed(item)
    return {
        "safe": not unsafe_children,
        "unsafe_children": unsafe_children,
        "catalogue_guards": catalogue_guards,
        "contamination": contamination,
        "critical_quality_count": critical_quality_count,
        "cumulative_elapsed_seconds": round(cumulative_elapsed, 3),
    }


async def _queue_verification_continuation(
    session: dict,
    child: ScrapeRuntimeJob,
    metadata: dict,
    db,
) -> dict | None:
    """Queue one exact-remaining-URL child after terminal timeout acknowledgement."""
    state = session["autonomous"]
    round_index = int(state.get("verification_round") or 0)
    if (
        metadata.get("budget_exhausted") != "time_budget_exhausted"
        or round_index + 1 >= MAX_VERIFICATION_RUNS
        or not getattr(child, "completed_at", None)
    ):
        return None
    selected = _canonical_dedup_urls(metadata.get("selected_urls"))
    if not selected:
        return None
    completed = await _staged_url_keys(
        child.runtime_job_id, int(session["university_id"]), db,
    )
    remaining = [url for url in selected if _url_key(url) not in completed]
    if not remaining:
        return None
    ids = list(state.get("verification_job_ids") or [child.runtime_job_id])
    prior_children = [await db.get(ScrapeRuntimeJob, jid) for jid in ids]
    elapsed = sum(_child_elapsed(item) for item in prior_children)
    spent = sum(
        float(getattr(item, "total_gemini_cost_usd", 0) or 0)
        for item in prior_children
    )
    remaining_cost = TOTAL_VERIFY_COST_USD - spent
    remaining_time = TOTAL_VERIFY_SECONDS - elapsed
    if remaining_time <= 0 or remaining_cost <= 0:
        state["continuation_blocked_reason"] = (
            "total_time_budget_exhausted" if remaining_time <= 0
            else "total_gemini_primary_budget_exhausted"
        )
        return None
    next_round = round_index + 1
    next_id = verification_id(session["session_id"], next_round)
    next_child = await db.get(ScrapeRuntimeJob, next_id)
    if next_child is None:
        parent = await db.get(ScrapeRuntimeJob, session["job_id"])
        next_child = ScrapeRuntimeJob(
            runtime_job_id=next_id, university_id=parent.university_id,
            university_name=parent.university_name, url=parent.url, job_type="scrape",
            status="queued", fast_mode=False,
            request_payload=verification_payload(
                session, parent, round_index=next_round, course_urls=remaining,
                cost_cap_usd=min(TOTAL_VERIFY_COST_USD, remaining_cost),
                time_budget_seconds=min(VERIFY_SECONDS, remaining_time),
            ),
        )
        db.add(next_child)
    ids.append(next_id)
    state.update(
        phase="verification_queued", verification_job_id=next_id,
        verification_job_ids=ids, verification_round=next_round,
        verification_status=next_child.status, verification_queued_at=now(),
        verification_requeues=0,
        continuation={
            "status": "queued", "round": next_round + 1,
            "max_runs": MAX_VERIFICATION_RUNS,
            "remaining_courses": len(remaining),
            "completed_courses": len(selected) - len(remaining),
            "total_time_budget_seconds": TOTAL_VERIFY_SECONDS,
            "total_cost_cap_usd": TOTAL_VERIFY_COST_USD,
        },
        reason=(
            f"Verification timed out after {len(selected) - len(remaining)} of "
            f"{len(selected)} selected courses; automatically continuing the exact remaining sample."
        ),
    )
    session.update(status="running", completed_at=None)
    await save(session, db)
    return await dispatch_verification(session, db)


async def run(job_id: str, university_id: int, session_id: str, claim_id: str, db) -> dict:
    session = await claim(job_id, university_id, session_id, claim_id, db)
    if not session:
        return {"job_id": job_id, "status": "duplicate_or_unowned"}
    from app.services.worker_fencing import (
        Ownership, OwnershipLost, ownership_scope, acknowledge_stop,
    )
    owner = Ownership(f"repair:{session_id}", session["autonomous"]["worker_generation"])
    try:
        with ownership_scope(owner):
            return await _run_owned(session, job_id, university_id, session_id, db)
    except OwnershipLost:
        await db.rollback()
        return {"job_id": job_id, "status": "superseded"}
    finally:
        await acknowledge_stop(db, owner)


async def _run_owned(session, job_id, university_id, session_id, db):
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
        from app.services.worker_fencing import revoke_stopped
        recovered_child = await revoke_stopped(db, f"verification:{child_id}")
        child = await db.get(ScrapeRuntimeJob, child_id, populate_existing=True, with_for_update=True)
        if not child:
            return await finish(session, db, "blocked", "Durable verification job is missing.", failed=True)
        fence_state = (await db.execute(text(
            "SELECT state FROM autonomous_worker_claims WHERE claim_key = :key"
        ), {"key": f"verification:{child_id}"})).scalar_one_or_none()
        if child.status not in CHILD_ACTIVE and fence_state == "active":
            # A lifecycle commit can precede cancellation/draining of detached
            # tasks. Do not launch a continuation before its stop acknowledgement.
            await db.rollback()
            return session
        if recovered_child and (child.request_payload or {}).get("autonomousStopRequested"):
            child.status = "stopped"
            child.completed_at = datetime.now(timezone.utc)
            return await finish(
                session, db, "needs_review", "Verification stopped at the user's request; partial sample only.",
            )
        if recovered_child and child.status in CHILD_ACTIVE - {"awaiting_approval"}:
            payload = dict(child.request_payload or {})
            policy = dict(payload.get("autonomousVerification") or {})
            started = child.claimed_at or child.started_at
            remaining = float(policy.get("time_budget_seconds") or VERIFY_SECONDS)
            if started:
                remaining -= max(0, age(started.isoformat()))
            if remaining <= 0:
                child.status = "failed_degraded"
                child.completed_at = datetime.now(timezone.utc)
                child.error_message = "Stopped execution exhausted the verification time budget; partial sample only."
                return await finish(
                    session, db, "needs_review", child.error_message,
                )
            policy["time_budget_seconds"] = remaining
            payload["autonomousVerification"] = policy
            child.request_payload = payload
            config = dict(child.discovered_config or {})
            metadata = dict(config.get("autonomousVerification") or {})
            metadata["time_budget_seconds"] = remaining
            config["autonomousVerification"] = metadata
            child.discovered_config = config
            child.status = "queued"
            child.claimed_at = None
            child.worker_id = None
            child.started_at = None
            child.completed_at = None
            child.stop_requested = False
            state.update(phase="verification_queued", last_dispatch_at=now())
            await save(session, db)
            return await dispatch_verification(session, db)
        state["verification_status"] = child.status
        if child.status == "awaiting_approval":
            return await finish(
                session, db, "needs_review",
                "Verification requires a human decision; no approval or publication was performed.",
            )
        if child.status not in CHILD_ACTIVE:
            if session.get("course_report"):
                state["verification_status"] = child.status
                return await finish(
                    session, db, "needs_review",
                    f"Reported-course recovery ended ({child.status}) in a bounded, review-only run. "
                    "Review the staged evidence; neither resolution of the reported "
                    "fields nor complete catalogue coverage is automatically certified.",
                )
            metadata = verification_metadata(child)
            continuation = await _queue_verification_continuation(
                session, child, metadata, db,
            )
            if continuation is not None:
                return continuation
            child_ids = list(state.get("verification_job_ids") or [child_id])
            after = await _combined_quality(
                child_ids, int(session["university_id"]), db,
            )
            verification_children = [
                await db.get(ScrapeRuntimeJob, verification_child_id)
                for verification_child_id in child_ids
            ]
            combined_safety = _combined_child_safety(verification_children)
            comparison = compare_quality(
                session.get("quality_before") or {},
                after,
                child,
                combined_safety=combined_safety,
            )
            comparison["baseline_counters"] = state.get("baseline_counters") or {}
            state["comparison"] = comparison
            cap = int(
                metadata.get("effective_max_courses") or metadata.get("max_courses")
                or state.get("verification_limits", {}).get("max_courses") or 50
            )
            stop_reason = metadata.get("budget_exhausted")
            capped = bool(metadata.get("capped")) or bool(
                not stop_reason
                and not metadata.get("warning")
                and (
                    getattr(child, "current", 0) >= cap
                    or (
                        child.total_found >= cap
                        and child.imported >= cap
                    )
                )
            )
            comparison["capped"] = capped
            comparison["stop_reason"] = (
                stop_reason
                or ("course_limit_reached" if capped else None)
                or ("warning" if metadata.get("warning") else None)
            )
            comparison["verification_limits"] = metadata
            comparison["verification_runs"] = len(child_ids)
            comparison["verification_job_ids"] = child_ids
            comparison["cumulative_staged_courses"] = after.get("total_staged", 0)
            comparison["cumulative_counters"] = {
                key: sum(int(getattr(item, key, 0) or 0) for item in verification_children if item)
                for key in ("total_found", "current", "imported", "skipped", "errors")
            }
            comparison["cumulative_counters"]["gemini_cost_usd"] = round(sum(
                float(getattr(item, "total_gemini_cost_usd", 0) or 0)
                for item in verification_children if item
            ), 6)
            comparison["cumulative_elapsed_seconds"] = combined_safety[
                "cumulative_elapsed_seconds"
            ]
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
                    return await schedule_recovery(
                        session, db,
                        "No durable verification worker claim was recorded; delivery or pre-claim initialization may have failed.",
                        "Check the application's repair worker, task queue, and broker connection before retrying; this is not evidence of a university website failure.",
                        target="verification",
                        dispatch_exhausted=True,
                    )
                # dispatch_verification is the only place that increments
                # this counter: one count equals one broker attempt.
                return await dispatch_verification(session, db)
        else:
            state["phase"] = "verifying"
            started = child.claimed_at or child.started_at
            if started and age(started.isoformat()) > VERIFY_SECONDS:
                child.stop_requested = True
                state["reason"] = "Verification time limit reached; cancellation requested."
        await save(session, db)
        return session
    if (
        state.get("phase") == RECOVERING
        and state.get("recovery_target") == "verification"
        and state.get("config_loop_done") is True
        and age(state.get("last_dispatch_at")) > REQUEUE_SECONDS
    ):
        # Active-scrape collisions happen before the deterministic child exists.
        # Re-enter the normal child-creation path after the collision clears;
        # verification_id() keeps this idempotent.
        return await queue_verification(session, db)
    if state.get("worker_claim"):
        if session.get("status") in {"completed", "failed"}:
            # Worker died after the agent's final durable audit but before
            # child creation. Never rerun the AI loop to recover this boundary.
            await db.commit()
            return await queue_verification(session, db)
        from app.services.worker_fencing import revoke_stopped
        if state.get("worker_generation") and await revoke_stopped(
            db, f"repair:{session_id}", state["worker_generation"],
        ):
            state.pop("worker_claim", None)
            state.pop("worker_generation", None)
            session.update(status="queued", completed_at=None)
            return await schedule_recovery(
                session, db, "The owning repair execution has authoritatively stopped.",
                "Redispatch the same repair session with a new generation.",
                target="repair",
            )
        agent.renew_repair_lease(int(session["university_id"]), session_id)
        session["status"] = "running"
        await save(session, db)
        if session["autonomous"].get("config_loop_done") and session.get("status") in {"completed", "failed"}:
            return await queue_verification(session, db)
        return session
    if age(state.get("last_dispatch_at") or session.get("queued_at")) > REQUEUE_SECONDS:
        active = (await db.execute(select(ScrapeRuntimeJob).where(
            ScrapeRuntimeJob.university_id == session["university_id"],
            ScrapeRuntimeJob.status.in_(CHILD_ACTIVE),
        ).limit(1))).scalar_one_or_none()
        if active:
            return await schedule_recovery(
                session, db,
                "A university scrape became active before repair redispatch.",
                "The active scrape will finish automatically, then the repair will retry.",
                target="repair",
                wait_for_collision=True,
            )
        count = int(state.get("requeues") or 0)
        if count >= MAX_REQUEUES:
            return await schedule_recovery(
                session, db,
                "No durable repair worker claim was recorded; delivery or pre-claim initialization may have failed.",
                "Check the application's repair worker, task queue, and broker connection before retrying; this is not evidence of a university website failure.",
                target="repair",
                dispatch_exhausted=True,
            )
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
                return await schedule_recovery(
                    current, db,
                    f"Repair queue delivery failed: {exc}",
                    "The repair will be redispatched automatically; no duplicate repair session will be created.",
                    target="repair",
                )
    else:
        await db.rollback()
    return session

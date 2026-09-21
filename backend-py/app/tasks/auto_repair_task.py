"""Celery tasks: auto-repair suggestions and AI-powered repair loop.

generate_repair_suggestion — enqueued by health snapshot when regression
  alerts are created, or via manual trigger API.

run_ai_scrape_repair — OpenAI-powered iterative repair loop.
  Triggered from POST /api/scrape/jobs/{job_id}/ai-repair.
  Progress stored in Redis under key ``ai_repair:{job_id}`` (TTL 24 h).

Beat reconciles autonomous sessions every minute, including after Redis loss.
"""

from __future__ import annotations

import asyncio
import logging

from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _sync_dispose() -> None:
    from app.database import engine
    try:
        engine.sync_engine.dispose(close=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("auto_repair_task _sync_dispose: %s", exc)


async def _run(university_id: int, regression_alert_id: int | None) -> dict:
    from app.database import AsyncSessionLocal
    from app.services.auto_repair import run_auto_repair_pipeline

    async with AsyncSessionLocal() as db:
        suggestion_id = await run_auto_repair_pipeline(university_id, regression_alert_id, db)
    return {"university_id": university_id, "suggestion_id": suggestion_id}


@celery_app.task(
    name="auto_repair.generate_suggestion",
    bind=True,
    max_retries=1,
    default_retry_delay=120,
    queue="scrape",
)
def generate_repair_suggestion(
    self,  # noqa: ANN001
    university_id: int,
    regression_alert_id: int | None = None,
) -> dict:
    """Generate an auto-repair suggestion for one university."""
    log.info("auto_repair.generate_suggestion: uni=%d alert=%s", university_id, regression_alert_id)
    _sync_dispose()
    try:
        result = asyncio.run(_run(university_id, regression_alert_id))
        log.info("auto_repair.generate_suggestion: done — %s", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("auto_repair.generate_suggestion: failed uni=%d: %s", university_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {"university_id": university_id, "error": str(exc)}


# ── AI-powered repair loop ────────────────────────────────────────────────────

async def _run_ai_repair(job_id: str, lease_token: str | None = None) -> dict:
    from app.database import AsyncSessionLocal
    from app.services.scraper.ai_repair_agent import run_ai_repair_loop

    async with AsyncSessionLocal() as db:
        return await run_ai_repair_loop(job_id, db, lease_token=lease_token)


async def _run_autonomous_repair(
    job_id: str, university_id: int, session_id: str, claim_id: str,
) -> dict | None:
    from app.database import AsyncSessionLocal
    from app.services.ai_repair_workflow import load, run
    from app.services.scraper.ai_repair_agent import ensure_ai_repair_audit_schema

    async with AsyncSessionLocal() as db:
        await ensure_ai_repair_audit_schema(db)
        existing = await load(job_id, db, session_id)
        if not existing.get("autonomous", {}).get("enabled"):
            return None  # Delivery from before the autonomous session contract.
        return await run(job_id, university_id, session_id, claim_id, db)


async def _reconcile_autonomous_repair(job_id: str, session_id: str) -> dict:
    from app.database import AsyncSessionLocal
    from app.services.ai_repair_workflow import reconcile
    from app.services.scraper.ai_repair_agent import ensure_ai_repair_audit_schema

    async with AsyncSessionLocal() as db:
        await ensure_ai_repair_audit_schema(db)
        return await reconcile(job_id, session_id, db)


@celery_app.task(
    name="ai_repair.monitor", bind=True, max_retries=180,
    default_retry_delay=30, queue="scrape",
)
def monitor_ai_scrape_repair(self, job_id: str, session_id: str) -> dict:  # noqa: ANN001
    """Reconcile durable delivery/child state without sleeping on a worker."""
    _sync_dispose()
    try:
        result = asyncio.run(_reconcile_autonomous_repair(job_id, session_id))
    except Exception as exc:
        # Database/broker outages are observable and bounded; status polling
        # can also reconcile from the same durable state after an outage.
        raise self.retry(exc=exc, countdown=30)
    state = result.get("autonomous") or {}
    if state.get("enabled") and state.get("phase") not in {"verified", "needs_review", "blocked"}:
        raise self.retry(countdown=30)
    return result


async def _recover_ai_repair_workflows() -> dict:
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    from app.services.ai_repair_workflow import reconcile
    from app.services.scraper.ai_repair_agent import ensure_ai_repair_audit_schema

    # A bounded durable scan survives loss of both queued worker messages AND
    # countdown-monitor messages. Never infer ownership from a Redis key.
    async with AsyncSessionLocal() as db:
        await ensure_ai_repair_audit_schema(db)
        rows = (await db.execute(text(
            "SELECT a.scrape_job_id, a.session_id FROM ai_repair_audits a "
            "JOIN scrape_runtime_jobs j ON j.runtime_job_id = a.scrape_job_id "
            "WHERE (j.request_payload->'aiRepairWorkflow'->>'session_id' = a.session_id "
            "AND j.request_payload->'aiRepairWorkflow'->'autonomous'->>'enabled' = 'true' "
            "AND j.request_payload->'aiRepairWorkflow'->'autonomous'->>'phase' "
            "IN ('queued', 'live_probe', 'repairing', 'validating', 'verification_queued', 'verifying', 'recovering')) "
            "OR (a.evidence->'autonomous'->>'enabled' = 'true' "
            "AND a.evidence->'autonomous'->>'phase' "
            "IN ('queued', 'live_probe', 'repairing', 'validating', 'verification_queued', 'verifying', 'recovering') "
            "AND j.request_payload->'aiRepairWorkflow' IS NULL) "
            "ORDER BY a.updated_at ASC LIMIT 100"
        ))).mappings().all()
    reconciled, errors = 0, []
    for row in rows:
        try:
            async with AsyncSessionLocal() as db:
                await reconcile(row["scrape_job_id"], row["session_id"], db)
            reconciled += 1
        except Exception as exc:
            log.warning("ai_repair recovery failed for %s: %s", row["session_id"], exc)
            errors.append({"session_id": row["session_id"], "error": str(exc)})
    return {"reconciled": reconciled, "errors": errors}


@celery_app.task(
    name="ai_repair.reconcile_active", bind=True, max_retries=1,
    default_retry_delay=30, queue="beat", soft_time_limit=120, time_limit=150,
)
def recover_ai_repair_workflows(self) -> dict:  # noqa: ANN001
    _sync_dispose()
    try:
        return asyncio.run(_recover_ai_repair_workflows())
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.on_after_finalize.connect
def register_ai_repair_recovery(sender, **kwargs) -> None:  # noqa: ANN001
    """Task modules are included by celery_app; register after task discovery."""
    sender.add_periodic_task(
        60.0, recover_ai_repair_workflows.s(),
        name="reconcile-autonomous-ai-repairs", queue="beat",
    )


# Also register the declarative entry at module import. API imports may finalize
# Celery before this included task module is loaded; Beat must not depend on
# that signal's timing. The stable name makes both registration paths idempotent.
celery_app.conf.beat_schedule.setdefault("reconcile-autonomous-ai-repairs", {
    "task": "ai_repair.reconcile_active", "schedule": 60.0,
    "args": (), "options": {"queue": "beat"},
})


async def _persist_ai_repair_failure(
    job_id: str,
    university_id: int,
    lease_token: str,
    error: str,
) -> dict:
    from app.database import AsyncSessionLocal
    from app.services.scraper.ai_repair_agent import fail_repair_audit

    async with AsyncSessionLocal() as db:
        return await fail_repair_audit(
            job_id, university_id, lease_token, error, db
        )


@celery_app.task(
    name="ai_repair.run_loop",
    bind=True,
    max_retries=0,
    queue="scrape",
    soft_time_limit=900,
    time_limit=960,
)
def run_ai_scrape_repair(
    self,  # noqa: ANN001
    job_id: str,
    university_id: int | None = None,
    lease_token: str | None = None,
) -> dict:
    """AI-powered iterative repair loop for a scrape job.

    Analyses discovery failures, generates config patches via OpenAI,
    applies them, simulates URL filter improvement, and repeats up to
    5 times.  Progress is written to Redis after every attempt so the
    frontend can poll ``GET /api/scrape/jobs/{job_id}/ai-repair-status``.
    """
    log.info("ai_repair.run_loop: job=%s", job_id)
    from app.services.scraper.ai_repair_agent import (
        claim_repair_session,
        read_session,
        release_repair_lease,
    )
    _sync_dispose()
    if university_id is not None and lease_token is not None:
        # Durable claim fencing handles duplicate delivery and Redis loss.
        # Do NOT release the lease here: verification retains it until terminal.
        autonomous_result = asyncio.run(_run_autonomous_repair(
            job_id, university_id, lease_token, str(self.request.id or lease_token),
        ))
        if autonomous_result is not None:
            return autonomous_result
    if university_id is not None and lease_token is not None:
        if not claim_repair_session(job_id, university_id, lease_token):
            error = "Repair session ownership expired before the worker started."
            try:
                asyncio.run(
                    _persist_ai_repair_failure(
                        job_id, university_id, lease_token, error
                    )
                )
            except Exception as audit_exc:  # noqa: BLE001
                log.error(
                    "ai_repair.run_loop: could not persist claim failure job=%s: %s",
                    job_id,
                    audit_exc,
                )
            release_repair_lease(university_id, lease_token)
            return {
                "job_id": job_id,
                "status": "failed",
                "error": error,
            }

    try:
        result = asyncio.run(_run_ai_repair(job_id, lease_token))
        log.info("ai_repair.run_loop: completed job=%s status=%s", job_id, result.get("status"))
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("ai_repair.run_loop: failed job=%s: %s", job_id, exc)
        # Best-effort — write failure to Redis so the poller sees it
        try:
            from app.services.scraper.ai_repair_agent import _write_session
            from datetime import datetime, timezone
            failure_session = {
                "session_id": lease_token,
                "job_id":       job_id,
                "university_id": university_id,
                "status":       "failed",
                "error":        str(exc),
                "attempts":     [],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_session(job_id, failure_session)
            if university_id is not None and lease_token is not None:
                _sync_dispose()
                asyncio.run(
                    _persist_ai_repair_failure(
                        job_id, university_id, lease_token, str(exc)
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        return {"job_id": job_id, "error": str(exc)}
    finally:
        if university_id is not None and lease_token is not None:
            release_repair_lease(university_id, lease_token)

"""Celery tasks: auto-repair suggestions and AI-powered repair loop.

generate_repair_suggestion — enqueued by health snapshot when regression
  alerts are created, or via manual trigger API.

run_ai_scrape_repair — OpenAI-powered iterative repair loop.
  Triggered from POST /api/scrape/jobs/{job_id}/ai-repair.
  Progress stored in Redis under key ``ai_repair:{job_id}`` (TTL 24 h).

Beat schedule: neither task is scheduled — both triggered on-demand only.
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


@celery_app.task(
    name="ai_repair.run_loop",
    bind=True,
    max_retries=0,
    queue="scrape",
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
    if university_id is not None and lease_token is not None:
        if not claim_repair_session(job_id, university_id, lease_token):
            release_repair_lease(university_id, lease_token)
            return {
                "job_id": job_id,
                "status": "failed",
                "error": "Repair session ownership expired before the worker started.",
            }

    _sync_dispose()
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
            _write_session(job_id, {
                "job_id":       job_id,
                "status":       "failed",
                "error":        str(exc),
                "attempts":     [],
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:  # noqa: BLE001
            pass
        return {"job_id": job_id, "error": str(exc)}
    finally:
        if university_id is not None and lease_token is not None:
            release_repair_lease(university_id, lease_token)

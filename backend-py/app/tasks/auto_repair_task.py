"""Celery task: generate an auto-repair suggestion for a university.

Enqueued by the health snapshot task whenever regression alerts are created,
and also via the manual trigger API endpoint.

Beat schedule: NOT a scheduled task — triggered on-demand only.
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

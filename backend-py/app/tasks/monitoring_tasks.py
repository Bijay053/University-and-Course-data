"""Phase 13 — Autonomous Monitoring Engine — Celery tasks.

Beat schedule entry added to celery_app.py:
  monitoring.check_watchers  runs every 30 minutes.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import AsyncSessionLocal, engine
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


def _sync_dispose() -> None:
    try:
        engine.sync_engine.dispose(close=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("monitoring _sync_dispose: %s", exc)


async def _async_check_watchers() -> dict:
    from app.services.monitoring_engine import run_monitoring_cycle
    async with AsyncSessionLocal() as db:
        return await run_monitoring_cycle(db)


@celery_app.task(name="monitoring.check_watchers", bind=True, max_retries=0)
def check_watchers(self) -> dict:  # noqa: ANN001
    """Run a full monitoring cycle: probe all due watchers, trigger scrapes where changed."""
    log.info("monitoring.check_watchers: starting cycle")
    _sync_dispose()
    try:
        result = asyncio.run(_async_check_watchers())
        log.info("monitoring.check_watchers: %s", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.error("monitoring.check_watchers: cycle failed: %s", exc)
        return {"error": str(exc), "checked": 0, "changed": 0, "triggered": 0}

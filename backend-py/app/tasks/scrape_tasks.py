"""Celery tasks. Each task opens its own async session because Celery workers
run sync; we use ``asyncio.run`` to bridge.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import AsyncSessionLocal
from app.services.scraper.orchestrator import run_scrape
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)


async def _async_scrape(runtime_job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await run_scrape(db, runtime_job_id)


@celery_app.task(name="scrape.university", bind=True, max_retries=2, default_retry_delay=60)
def scrape_university(self, runtime_job_id: str) -> dict:  # noqa: ANN001
    log.info("Celery task scrape_university start id=%s", runtime_job_id)
    try:
        asyncio.run(_async_scrape(runtime_job_id))
        return {"ok": True, "id": runtime_job_id}
    except Exception as exc:
        log.exception("Task failed: %s", exc)
        raise self.retry(exc=exc) from exc

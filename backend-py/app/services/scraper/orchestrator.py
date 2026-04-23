"""Top-level scraping orchestrator. Currently a SCAFFOLD.

The real work — discovering course URLs, running each extractor, normalising
output — must be ported from ``artifacts/api-server/src/routes/scrape.ts``
(~13K lines) one extractor at a time. Until then, ``run_scrape`` records
the job lifecycle so the admin UI shows "ran but no courses found" instead
of an unbounded "running" state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScrapeRuntimeJob, University

log = logging.getLogger(__name__)


async def run_scrape(db: AsyncSession, runtime_job_id: str) -> None:
    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_scrape: no job %s", runtime_job_id)
        return
    job.status = "running"
    job.claimed_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()

    try:
        uni = (
            await db.execute(select(University).where(University.id == job.university_id))
        ).scalar_one_or_none()
        if not uni or not uni.scrape_url:
            raise RuntimeError("University missing scrape_url")

        # TODO(extractors): run extractors here. For now we mark complete with
        # zero results — this matches the legacy "no extractor matched" path.
        log.info(
            "Scraping %s (%s) — extractor pipelines not yet ported", uni.name, uni.scrape_url
        )

        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        log.exception("Scrape job %s failed: %s", runtime_job_id, exc)
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = str(exc)[:1000]
        await db.commit()

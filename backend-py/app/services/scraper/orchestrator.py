"""Top-level scraping orchestrator.

For a runtime job: discover course URLs from the university's scrape_url,
run the per-course extractor pipeline, stage each result as a row in
``scraped_courses`` (with the bug-#7 7-day rejection guard), and update
the job status. Failures in one course never abort the whole run.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ScrapeRuntimeJob, University
from app.services.scraper.discovery import discover_course_links
from app.services.scraper.pipelines.single_course import extract_course
from app.services.scraper.stage_course import stage_course

log = logging.getLogger(__name__)


# Cap the per-job work so a runaway crawl can't pin a worker.
_MAX_COURSES_PER_JOB = 60
_MAX_PARALLEL = 4


async def _process_one(
    db: AsyncSession,
    *,
    runtime_job_id: str,
    university_id: int,
    country: str | None,
    link: dict,
) -> tuple[str, str]:
    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]
    try:
        extracted = await extract_course(url, country=country)
    except Exception as exc:
        log.warning("extract failed %s: %s", url, exc)
        return ("error", f"extract failed: {exc}")
    payload = extracted.get("payload") or {}
    if extracted.get("error"):
        return ("error", extracted["error"])
    result = await stage_course(
        db,
        scrape_job_id=runtime_job_id,
        university_id=university_id,
        course_name=name,
        payload=payload,
    )
    return ("staged" if result.saved else "skipped", result.reason)


async def run_scrape(db: AsyncSession, runtime_job_id: str) -> dict:
    """Execute one scrape job. Returns a small summary dict for logging."""
    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_scrape: no job %s", runtime_job_id)
        return {"ok": False, "reason": "job_not_found"}

    job.status = "running"
    job.claimed_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()

    summary = {"discovered": 0, "staged": 0, "skipped": 0, "errors": 0}
    try:
        uni = (
            await db.execute(select(University).where(University.id == job.university_id))
        ).scalar_one_or_none()
        if not uni or not uni.scrape_url:
            raise RuntimeError("University missing scrape_url")

        max_pages = 12 if job.fast_mode else 25
        max_courses = 20 if job.fast_mode else _MAX_COURSES_PER_JOB
        log.info("Discovering course links from %s (fast_mode=%s)", uni.scrape_url, job.fast_mode)
        links = await discover_course_links(
            uni.scrape_url, max_pages=max_pages, max_courses=max_courses
        )
        summary["discovered"] = len(links)
        log.info("Discovered %d candidate course links for %s", len(links), uni.name)

        sem = asyncio.Semaphore(_MAX_PARALLEL)

        async def _worker(link: dict) -> None:
            async with sem:
                status, reason = await _process_one(
                    db,
                    runtime_job_id=runtime_job_id,
                    university_id=uni.id,
                    country=uni.country,
                    link=link,
                )
                if status == "staged":
                    summary["staged"] += 1
                elif status == "skipped":
                    summary["skipped"] += 1
                else:
                    summary["errors"] += 1
                # Heartbeat so the admin UI knows we're alive.
                job.heartbeat_at = datetime.now(timezone.utc)

        await asyncio.gather(*[_worker(lk) for lk in links], return_exceptions=True)

        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        log.info("Scrape %s completed: %s", runtime_job_id, summary)
        return {"ok": True, **summary}
    except Exception as exc:
        log.exception("Scrape job %s failed: %s", runtime_job_id, exc)
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = str(exc)[:1000]
        await db.commit()
        return {"ok": False, "reason": str(exc), **summary}

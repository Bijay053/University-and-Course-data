"""Top-level scraping orchestrator.

For a runtime job: discover course URLs from the university's scrape_url,
run the per-course extractor pipeline IN PARALLEL, then stage results
SERIALLY against a single AsyncSession (SQLAlchemy AsyncSession is not
safe for concurrent task use). Failures in one course never abort the
whole run; exceptions from gather() are surfaced into the summary so
the job is never silently marked complete with hidden failures.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import ScrapeRuntimeJob, University
from app.services.scraper.discovery import discover_course_links
from app.services.scraper.pipelines.single_course import extract_course
from app.services.scraper.stage_course import stage_course

log = logging.getLogger(__name__)


_MAX_COURSES_PER_JOB = 60
_MAX_PARALLEL_FETCH = 4


async def _extract_only(link: dict, country: str | None) -> dict:
    """Network-bound work — safe to parallelise across coroutines."""
    name = (link.get("name") or "").strip() or "Unknown course"
    url = link["url"]
    try:
        out = await extract_course(url, country=country)
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "url": url, "error": f"extract: {exc}"}
    return {"name": name, "url": url, **out}


async def run_scrape(db: AsyncSession, runtime_job_id: str) -> dict:
    """Execute one scrape job.

    Note: ``db`` is used only for the job-lifecycle bookkeeping (running →
    completed/failed). Per-course staging uses a fresh AsyncSession from
    AsyncSessionLocal so we never share a session across coroutines.
    """
    job = await db.get(ScrapeRuntimeJob, runtime_job_id)
    if not job:
        log.warning("run_scrape: no job %s", runtime_job_id)
        return {"ok": False, "reason": "job_not_found"}

    job.status = "running"
    job.claimed_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    await db.commit()

    summary = {"discovered": 0, "staged": 0, "skipped": 0, "errors": 0, "fetch_failed": 0}
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

        # 1) Extraction phase — parallel network calls, no DB shared state.
        sem = asyncio.Semaphore(_MAX_PARALLEL_FETCH)

        async def _bounded(link: dict) -> dict:
            async with sem:
                return await _extract_only(link, uni.country)

        results = await asyncio.gather(
            *[_bounded(lk) for lk in links], return_exceptions=True
        )

        # 2) Staging phase — serial writes through one fresh session per course.
        for r in results:
            if isinstance(r, Exception):
                summary["errors"] += 1
                log.warning("worker raised: %s", r)
                continue
            if r.get("error"):
                if r["error"].startswith("fetch") or "fetch_failed" in r.get("error", ""):
                    summary["fetch_failed"] += 1
                else:
                    summary["errors"] += 1
                continue
            payload = r.get("payload") or {}
            try:
                async with AsyncSessionLocal() as stage_db:
                    res = await stage_course(
                        stage_db,
                        scrape_job_id=runtime_job_id,
                        university_id=uni.id,
                        course_name=r["name"],
                        payload=payload,
                    )
                if res.saved:
                    summary["staged"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                log.warning("stage_course failed for %s: %s", r.get("url"), exc)

            # Heartbeat between batches so the admin UI sees progress.
            job.heartbeat_at = datetime.now(timezone.utc)

        # If every result blew up, mark the job failed instead of completed.
        finished_cleanly = summary["errors"] == 0 or (
            summary["staged"] + summary["skipped"] > 0
        )
        job.status = "completed" if finished_cleanly else "failed"
        if not finished_cleanly:
            job.error_message = (
                f"all {summary['errors']} workers errored "
                f"(discovered={summary['discovered']})"
            )[:1000]
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        log.info("Scrape %s %s: %s", runtime_job_id, job.status, summary)
        return {"ok": finished_cleanly, **summary}
    except Exception as exc:
        log.exception("Scrape job %s failed: %s", runtime_job_id, exc)
        job.status = "failed"
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = str(exc)[:1000]
        await db.commit()
        return {"ok": False, "reason": str(exc), **summary}

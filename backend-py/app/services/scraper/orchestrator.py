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

async def _emit(db, runtime_job_id: str, sequence: int, event: str, message: str, payload: dict | None = None) -> None:
    """Write a row to scrape_runtime_logs so UI can show progress."""
    from sqlalchemy import text as _text
    from datetime import datetime as _dt, timezone as _tz
    import json as _json
    p = {"message": message}
    if payload:
        p.update(payload)
    try:
        await db.execute(_text("""
            INSERT INTO scrape_runtime_logs (runtime_job_id, sequence, event, payload, created_at)
            VALUES (:rid, :seq, :ev, CAST(:pl AS jsonb), :ts)
        """), {"rid": runtime_job_id, "seq": sequence, "ev": event, "pl": _json.dumps(p), "ts": _dt.now(_tz.utc)})
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("emit log failed: %s", exc)



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
    _seq = [1]
    async def emit(event: str, message: str, **kw):
        await _emit(db, runtime_job_id, _seq[0], event, message, kw or None)
        _seq[0] += 1
    await emit("status", "Worker claimed queued scrape job", phase="queue")

    summary = {"discovered": 0, "staged": 0, "skipped": 0, "errors": 0, "fetch_failed": 0}
    try:
        # Snapshot uni fields to plain locals — the session will be used
        # by other coroutines during gather() and we must NOT touch `uni`.
        uni = (
            await db.execute(select(University).where(University.id == job.university_id))
        ).scalar_one_or_none()
        if not uni:
            raise RuntimeError("University not found")
        uni_id = uni.id
        uni_name = uni.name
        uni_country = uni.country
        uni_scrape_url = uni.scrape_url or ""
        # Use the URL captured on the job at API time, fall back to uni snapshot.
        scrape_url = (job.url or "").strip() or uni_scrape_url.strip()
        if not scrape_url:
            raise RuntimeError("University missing scrape_url")

        max_pages = 12 if job.fast_mode else 25
        max_courses = 20 if job.fast_mode else _MAX_COURSES_PER_JOB
        log.info("Discovering course links from %s (fast_mode=%s)", scrape_url, job.fast_mode)
        await emit("status", f"Fetching {scrape_url}...", phase="fetch")
        await emit("status", "Discovering candidate course pages...", phase="discover")
        links = await discover_course_links(
            scrape_url, max_pages=max_pages, max_courses=max_courses
        )
        summary["discovered"] = len(links)
        log.info("Discovered %d candidate course links for %s", len(links), uni_name)
        await emit("status", f"Discovered {len(links)} candidate course links", phase="discover", count=len(links))
        # Update progress counters so UI sees total_found
        job.total_found = len(links)
        job.heartbeat_at = datetime.now(timezone.utc)
        await db.commit()
        await emit("status", f"Extracting course details ({len(links)} pages)...", phase="extract")

        # 1) Extraction phase — parallel network calls, no DB shared state.
        sem = asyncio.Semaphore(_MAX_PARALLEL_FETCH)

        async def _bounded(link: dict) -> dict:
            async with sem:
                return await _extract_only(link, uni_country)

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
                        university_id=uni_id,
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
        await emit("status", f"Staged {summary['staged']} courses, {summary['skipped']} skipped, {summary['fetch_failed']} fetch errors", phase="complete", **summary)
        finished_cleanly = summary["errors"] == 0 or (
            summary["staged"] + summary["skipped"] > 0
        )
        job.status = "completed" if finished_cleanly else "failed"
        # Always update progress counters from this run.
        job.total_found = summary["discovered"]
        job.current = summary["discovered"]
        job.imported = summary["staged"]
        job.skipped = summary["skipped"]
        job.errors = summary["errors"]
        if finished_cleanly:
            job.error_message = None  # clear any stale message
        else:
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

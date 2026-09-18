#!/usr/bin/env python3
"""Run real UEL discovery/extraction/staging after isolated env is established."""
from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


# This assertion intentionally precedes every app import: app.database constructs
# its engine at import time.
expected_db = os.environ["UEL_AUDIT_EXPECTED_DB"]
configured_db = urlsplit(
    os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
).path.lstrip("/")
if configured_db != expected_db or not expected_db.startswith("uel_audit_"):
    raise SystemExit("SAFETY ABORT: runner is not pointed at its disposable database")
if os.environ.get("REDIS_URL") != "redis://127.0.0.1:6379/15":
    raise SystemExit("SAFETY ABORT: isolated Redis URL is missing")

from celery.app.task import Task
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.university import University
from app.services.scraper.orchestrator import run_scrape
from app.services.scraper import uel_transport
from app.services.scraper import browser_pool
from app.services.scraper import discovery
from app.services.scraper import http_fetcher
from app.services.scraper.pipelines import single_course
from uel_source_capture import SourceCapture, is_uel_detail_source


AUDIT_DIR = Path(os.environ["UEL_AUDIT_DIR"])
HTML_DIR = AUDIT_DIR / "html"
HTML_DIR.mkdir(exist_ok=True)
dispatch_attempts: list[dict] = []
PROGRESS_FILE = AUDIT_DIR / "progress.json"


def write_progress(state: str, **details) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(
            {
                "audit_run_id": os.environ["UEL_AUDIT_RUN_ID"],
                "state": state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **details,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def blocked_dispatch(self, *args, **kwargs):
    dispatch_attempts.append(
        {"task": getattr(self, "name", type(self).__name__), "blocked": True}
    )
    return None


# run_scrape's post-run quality hooks are allowed to calculate and persist local
# evidence, but none may enqueue autonomous work onto any broker/worker.
Task.delay = blocked_dispatch
Task.apply_async = blocked_dispatch

def capture_progress() -> None:
    write_progress(
        "running",
        source_fetches=len(source_capture.manifest),
        all_source_html_fetches=len(source_capture.source_manifest),
        source_fetch_attempts=len(source_capture.attempts),
        source_fetch_errors=len(source_capture.errors),
        unique_source_urls=len({item["url"] for item in source_capture.manifest}),
    )


source_capture = SourceCapture(
    AUDIT_DIR,
    on_record=capture_progress,
    is_detail_source=is_uel_detail_source,
)

# Patch every extraction route that can supply the UEL source page.  The
# single_course names are import-time aliases, while browser_pool.pool is the
# shared object used by both initial and sparse-page browser fallbacks.
uel_transport.fetch_uel_source = source_capture.wrap(
    "uel_source", uel_transport.fetch_uel_source
)
single_course.fetch_html = source_capture.wrap(
    "general_http", single_course.fetch_html
)
single_course.fetch_html_scrape_do = source_capture.wrap(
    "general_scrape_do", single_course.fetch_html_scrape_do
)
discovery.fetch_html = source_capture.wrap(
    "discovery_http", discovery.fetch_html
)
browser_pool.pool.fetch_html = source_capture.wrap(
    "browser", browser_pool.pool.fetch_html
)
# Preserve the original callables before replacing their module attributes.
# Modules which resolve these functions lazily (discovery and central-page
# fetchers included) now contribute to the all-source manifest. The already
# bound single_course wrappers above continue to call the same real originals,
# avoiding wrapper-on-wrapper recursion.
real_module_fetch_html = http_fetcher.fetch_html
real_module_fetch_html_scrape_do = http_fetcher.fetch_html_scrape_do
http_fetcher.fetch_html = source_capture.wrap(
    "http_fetcher", real_module_fetch_html
)
http_fetcher.fetch_html_scrape_do = source_capture.wrap(
    "scrape_do", real_module_fetch_html_scrape_do
)


async def main() -> None:
    started = datetime.now(timezone.utc)
    job_id = "uel_audit_" + os.environ["UEL_AUDIT_RUN_ID"].replace("-", "_").lower()
    write_progress("starting", job_id=job_id, source_fetches=0)
    async with AsyncSessionLocal() as db:
        identity = (
            await db.execute(
                text(
                    "SELECT current_database(), "
                    "coalesce(inet_server_addr()::text,'local-socket'), inet_server_port()"
                )
            )
        ).one()
        if identity[0] != expected_db:
            raise RuntimeError("database identity changed after app import")
        if (identity[1] not in {"127.0.0.1", "::1", "local-socket"}):
            raise RuntimeError("database server is not local")

        uni = University(
            name="University of East London",
            country="United Kingdom",
            city="London",
            website="https://www.uel.ac.uk",
            scrape_url="https://www.uel.ac.uk/study/undergraduate/courses",
            scrape_config={},
        )
        db.add(uni)
        await db.flush()
        job = ScrapeRuntimeJob(
            runtime_job_id=job_id,
            university_id=uni.id,
            university_name=uni.name,
            url=uni.scrape_url,
            job_type="single",
            status="queued",
            fast_mode=False,
            request_payload={
                "url": uni.scrape_url,
                "universityId": uni.id,
                "universityName": uni.name,
                "universityCountry": uni.country,
                "university_id": uni.id,
                "fastMode": False,
                "fast_mode": False,
                "forceDiscovery": True,
                "courseUrls": [],
                "course_urls": [],
            },
        )
        db.add(job)
        await db.commit()

        result = await run_scrape(db, job_id)
        db.expire_all()
        job_row = (
            await db.execute(
                text(
                    "SELECT status,total_found,current,imported,skipped,errors,"
                    "error_message,started_at,completed_at,approval_decision "
                    "FROM scrape_runtime_jobs WHERE runtime_job_id=:j"
                ),
                {"j": job_id},
            )
        ).mappings().one()
        staged = (
            await db.execute(
                text(
                    "SELECT status,count(*) AS n,count(DISTINCT canonical_course_url) AS urls "
                    "FROM scraped_courses WHERE scrape_job_id=:j GROUP BY status ORDER BY status"
                ),
                {"j": job_id},
            )
        ).mappings().all()
        approvals = (
            await db.execute(
                text(
                    "SELECT count(*) FROM scraped_courses "
                    "WHERE scrape_job_id=:j AND status <> 'pending'"
                ),
                {"j": job_id},
            )
        ).scalar_one()
        levels = (
            await db.execute(
                text(
                    "SELECT coalesce(degree_level,'unknown') AS degree_level,count(*) "
                    "FROM scraped_courses WHERE scrape_job_id=:j GROUP BY 1 ORDER BY 1"
                ),
                {"j": job_id},
            )
        ).all()
        log_events = (
            await db.execute(
                text(
                    "SELECT event,payload FROM scrape_runtime_logs "
                    "WHERE runtime_job_id=:j ORDER BY sequence"
                ),
                {"j": job_id},
            )
        ).all()

    source_capture.flush()
    event_kinds = Counter(
        str((payload or {}).get("kind") or event) for event, payload in log_events
    )
    # Store aggregate evidence only; raw request configuration is deliberately
    # excluded. Runtime details remain in the isolated DB.
    summary = {
        "audit_run_id": os.environ["UEL_AUDIT_RUN_ID"],
        "job_id": job_id,
        "database_guard": {
            "database": expected_db,
            "server": identity[1],
            "port": identity[2],
            "passed": True,
        },
        "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 3),
        "run_scrape_returned": result is not None,
        "job": dict(job_row),
        "staging": [dict(row) for row in staged],
        "degree_level_counts": {str(level): count for level, count in levels},
        "non_pending_rows": approvals,
        "approvals_forbidden_and_absent": approvals == 0 and job_row["approval_decision"] is None,
        "source_capture": {
            "fetches": len(source_capture.manifest),
            "all_source_html_fetches": len(source_capture.source_manifest),
            "supporting_html_fetches": (
                len(source_capture.source_manifest) - len(source_capture.manifest)
            ),
            "attempts": len(source_capture.attempts),
            "unique_urls": len({item["url"] for item in source_capture.manifest}),
            "unique_html_documents": len(
                {item["sha256"] for item in source_capture.manifest}
            ),
            "bytes": sum(item["bytes"] for item in source_capture.manifest),
            "errors": source_capture.errors,
            "no_html_results": source_capture.no_html,
        },
        "reported_errors": {
            "job_error_count": int(job_row["errors"] or 0),
            "job_error_message": job_row["error_message"],
            "transport_error_count": len(source_capture.errors),
            "transport_no_html_count": len(source_capture.no_html),
        },
        "quality_success_claimed": False,
        "completion_note": (
            "A completed lifecycle status is not a claim that catalogue quality "
            "checks passed; inspect reported_errors and staged evidence."
        ),
        "runtime_event_kind_counts": dict(sorted(event_kinds.items())),
        "autonomous_dispatch_attempts_blocked": dispatch_attempts,
        "force_discovery": True,
        "actual_http": True,
        "scheduled_tasks_started": False,
    }
    (AUDIT_DIR / "run-summary.json").write_text(
        json.dumps(summary, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_progress(
        "finished",
        job_id=job_id,
        job_status=job_row["status"],
        source_fetches=len(source_capture.manifest),
        source_fetch_errors=len(source_capture.errors),
        job_errors=int(job_row["errors"] or 0),
        staged_rows=sum(int(row["n"]) for row in staged),
    )
    print(json.dumps({"job_status": job_row["status"], "staged": [dict(r) for r in staged], "audit_dir": str(AUDIT_DIR)}))
    await engine.dispose()


async def entry() -> None:
    from app.services.scraper.browser_pool import pool
    from app.services.scraper.http_fetcher import close_shared_client_for_current_loop

    try:
        await main()
    except BaseException as exc:
        source_capture.flush()
        failure = {
            "audit_run_id": os.environ["UEL_AUDIT_RUN_ID"],
            "state": "runner_error",
            "error_type": type(exc).__name__,
            "source_capture": {
                "fetches": len(source_capture.manifest),
                "all_source_html_fetches": len(source_capture.source_manifest),
                "attempts": len(source_capture.attempts),
                "errors": source_capture.errors,
                "no_html_results": source_capture.no_html,
            },
            "quality_success_claimed": False,
        }
        (AUDIT_DIR / "run-summary.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_progress(
            "runner_error",
            error_type=type(exc).__name__,
            source_fetches=len(source_capture.manifest),
            source_fetch_errors=len(source_capture.errors),
        )
        raise
    finally:
        try:
            await pool.close()
        finally:
            try:
                await close_shared_client_for_current_loop()
            finally:
                await engine.dispose()


if __name__ == "__main__":
    asyncio.run(entry())
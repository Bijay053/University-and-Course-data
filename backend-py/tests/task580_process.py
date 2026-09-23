"""Child-process entrypoint for the disposable task-580 acceptance harness."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from tests import task564_process


async def seed() -> None:
    """Build the production-shaped schema, then add durable recovery evidence."""
    await task564_process.seed()

    from app.database import AsyncSessionLocal, engine
    from sqlalchemy import select

    from app.models import Course, ScrapeRuntimeJob, ScrapedCourse, University
    from app.models.scrape_runtime import ScrapeRuntimeLog

    selected = [
        "https://task564.example.test/courses/blocked-alpha",
        "https://task564.example.test/courses/blocked-beta",
    ]
    async with AsyncSessionLocal() as db:
        source = await db.get(ScrapeRuntimeJob, task564_process.SOURCE)
        assert source is not None and source.university_id is not None
        source.errors = 7
        source.error_message = "Seven preserved errors from the completed full scrape"

        review = (
            await db.execute(
                select(ScrapedCourse).where(
                    ScrapedCourse.scrape_job_id == task564_process.SOURCE
                )
            )
        ).scalar_one()
        assert review is not None
        published = Course(
            university_id=source.university_id,
            name=review.course_name,
            course_website=review.course_website,
            status="active",
            approval_status="approved",
        )
        db.add(published)
        await db.flush()
        review.status = "approved"
        review.auto_publish_status = "auto_published"
        review.course_id = published.id

        university = await db.get(University, source.university_id)
        assert university is not None
        university.scrape_config = {
            "requires_browser": False,
            "recipe": {"block_url_patterns": ["/blocked-"]},
        }
        for sequence, url in enumerate(selected, 1):
            db.add(ScrapeRuntimeLog(
                runtime_job_id=source.runtime_job_id,
                sequence=sequence,
                event="error",
                payload={
                    "message": f"Unresolved source extraction: {url}",
                    "kind": "sweep_unresolved",
                    "url": url,
                    "reason": "fetch_failed",
                    "retryable": True,
                },
            ))
        source.log_count = len(selected)
        await db.commit()
    await engine.dispose()


async def seed_continuation(resume: str | None = None) -> None:
    """Seed the persisted payload produced by a reviewed report continuation."""
    await seed()
    from app.database import AsyncSessionLocal, engine
    from app.models import ScrapeRuntimeJob, ScrapedCourse

    async with AsyncSessionLocal() as db:
        source = await db.get(ScrapeRuntimeJob, task564_process.SOURCE)
        report = {
            "id": "task582-report",
            "source_job_id": source.runtime_job_id,
            "course_urls": [
                "https://task564.example.test/courses/blocked-alpha",
                "https://task564.example.test/courses/blocked-beta",
            ],
            "fields": ["fee"],
        }
        parent_id = "task582-report-parent"
        source.request_payload = {
            "aiRepairWorkflow": {
                "session_id": report["id"], "job_id": source.runtime_job_id,
                "university_id": source.university_id, "status": "running",
                "autonomous": {
                    "phase": "verification_queued",
                    "verification_job_id": "job_task582_continuation",
                    "verification_job_ids": [parent_id, "job_task582_continuation"],
                },
            },
        }
        db.add(ScrapeRuntimeJob(
            runtime_job_id=parent_id, university_id=source.university_id,
            university_name=source.university_name, url=source.url,
            job_type="scrape", status="completed_with_errors", errors=3,
            imported=1,
            request_payload={
                "retrySourceJobId": source.runtime_job_id, "courseReport": report,
            },
        ))
        db.add(ScrapedCourse(
            scrape_job_id=parent_id, university_id=source.university_id,
            course_name="Bachelor of Earlier Report Evidence", status="pending",
            course_website="https://task564.example.test/courses/earlier-report",
            canonical_course_url="https://task564.example.test/courses/earlier-report",
        ))
        db.add(ScrapeRuntimeJob(
            runtime_job_id="job_task582_continuation",
            university_id=source.university_id, university_name=source.university_name,
            url=source.url, job_type="scrape", status="queued",
            request_payload={
                "url": source.url,
                "universityId": source.university_id,
                "university_id": source.university_id,
                "courseUrls": report["course_urls"],
                "course_urls": report["course_urls"],
                "courseReportRemainingUrls": report["course_urls"],
                "retrySourceJobId": parent_id,
                "courseReport": report,
                "autonomousVerification": {
                    "parent_job_id": source.runtime_job_id,
                    "session_id": report["id"], "round_index": 1,
                    "max_courses": 50, "time_budget_seconds": 600,
                    "cost_cap_usd": 2,
                },
            },
        ))
        await db.commit()
    await engine.dispose()

    if resume:
        # A row and a checkpoint-only URL exercise both sources of prior work.
        # The checkpoint URL deliberately also matches the active URL filter.
        from tests.task580_acceptance import PRIOR, SELECTED
        async with AsyncSessionLocal() as db:
            child = await db.get(ScrapeRuntimeJob, "job_task582_continuation")
            selected = PRIOR + (SELECTED if resume == "mixed" else [])
            payload = dict(child.request_payload)
            payload.update({
                "courseUrls": selected,
                "course_urls": selected,
                "courseReportRemainingUrls": selected,
                "courseReport": {**payload["courseReport"], "course_urls": selected},
            })
            child.request_payload = payload
            child.discovered_config = {
                "autonomousVerification": {"completed_urls": [PRIOR[1]]},
            }
            child.imported = 1
            db.add(ScrapedCourse(
                scrape_job_id=child.runtime_job_id,
                university_id=child.university_id,
                course_name="Bachelor of Previously Reviewed Evidence",
                status="pending",
                course_website=PRIOR[0],
                canonical_course_url=PRIOR[0],
            ))
            await db.commit()
        await engine.dispose()


async def requeue_continuation() -> None:
    """Redeliver the exact durable child after its completed worker lifecycle."""
    from app.database import AsyncSessionLocal, engine
    from app.models import ScrapeRuntimeJob
    from app.services.worker_fencing import revoke_stopped

    async with AsyncSessionLocal() as db:
        child = await db.get(ScrapeRuntimeJob, "job_task582_continuation")
        assert child is not None
        assert child.status in {"failed", "completed"}
        assert await revoke_stopped(
            db, f"verification:{child.runtime_job_id}"
        ), "completed autonomous worker claim was not revocable"
        child.status = "queued"
        child.completed_at = None
        child.stop_requested = False
        await db.commit()
    await engine.dispose()


def install_task_postrun_marker() -> None:
    """Record completion only after the real Celery task has returned."""
    from celery.signals import task_postrun

    root = Path(os.environ["TASK580_POSTRUN_DIR"]).resolve()
    assert root.parent.name.startswith("task580-private-")
    root.mkdir(parents=True, exist_ok=True)

    @task_postrun.connect(weak=False)
    def record_postrun(
        sender=None,
        task_id=None,
        task=None,
        args=None,
        kwargs=None,
        retval=None,
        state=None,
        **_extra,
    ) -> None:
        task_name = getattr(sender, "name", None) or getattr(task, "name", None)
        if task_name != "scrape.university":
            return
        runtime_job_id = (
            str(args[0])
            if isinstance(args, (list, tuple)) and args
            else str((kwargs or {}).get("job_id") or "")
        )
        if not runtime_job_id.startswith("job_"):
            return
        marker = {
            "task_name": task_name,
            "task_id": str(task_id or ""),
            "runtime_job_id": runtime_job_id,
            "state": str(state or ""),
            "return_value": retval,
            "worker_pid": os.getpid(),
        }
        destination = root / f"{runtime_job_id}.json"
        temporary = root / f".{runtime_job_id}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(marker, default=str))
        os.replace(temporary, destination)


if __name__ == "__main__":
    assert os.environ.get("TASK564_ISOLATED") == "yes"
    assert "@127.0.0.1:" in os.environ["DATABASE_URL"]
    assert os.environ["REDIS_URL"].startswith("redis://127.0.0.1:")
    task564_process.isolate_recipe_files()
    task564_process.install_http_fixture()
    if sys.argv[1] == "seed":
        asyncio.run(seed())
    elif sys.argv[1] == "seed-continuation":
        asyncio.run(seed_continuation())
    elif sys.argv[1] in ("seed-mixed", "seed-resolved"):
        asyncio.run(seed_continuation(sys.argv[1].removeprefix("seed-")))
    elif sys.argv[1] == "dispatch-continuation":
        from app.tasks.celery_app import celery_app
        celery_app.send_task(
            "scrape.university", args=["job_task582_continuation"], queue="scrape",
        )
    elif sys.argv[1] == "requeue-continuation":
        asyncio.run(requeue_continuation())
    elif sys.argv[1] == "api":
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=int(os.environ["TASK564_API_PORT"]),
        )
    elif sys.argv[1] == "worker":
        install_task_postrun_marker()
        from app.tasks.celery_app import celery_app
        celery_app.worker_main([
            "worker", "--pool=prefork", "--concurrency=1", "--loglevel=INFO",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
            "-Q", "scrape", "-n", "task580@%h",
        ])
    else:
        raise SystemExit("Expected seed, api or worker")
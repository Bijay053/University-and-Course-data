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
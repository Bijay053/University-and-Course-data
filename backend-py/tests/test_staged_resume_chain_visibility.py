"""/api/scrape/staged/{job_id} is an exact review view for one scrape run.

Pending rows from interrupted attempts remain available to resume/recovery
logic, but must not be mixed with the fresh values produced by a later job.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models import ScrapedCourse, University
from app.models.scrape_runtime import ScrapeRuntimeJob


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _pick_university() -> int:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(University.id).order_by(University.id).limit(1))).first()
    if not row:
        pytest.skip("need at least one university in the DB to run integration test")
    return row[0]


async def _cleanup(job_ids: list[str]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scraped_courses WHERE scrape_job_id = ANY(:ids)"),
            {"ids": job_ids},
        )
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = ANY(:ids)"),
            {"ids": job_ids},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_staged_list_is_scoped_to_current_job_id():
    uni_id = await _pick_university()
    old_job_id = f"test_resume_old_{uuid.uuid4().hex[:8]}"
    new_job_id = f"test_resume_new_{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=old_job_id,
                    university_id=uni_id,
                    university_name="Test Uni",
                    url="https://example.edu",
                    job_type="scrape",
                    status="failed",
                    error_message="Worker restarted — slot freed on startup",
                    total_found=10,
                    imported=0,
                    skipped=0,
                    errors=0,
                )
            )
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=new_job_id,
                    university_id=uni_id,
                    university_name="Test Uni",
                    url="https://example.edu",
                    job_type="scrape",
                    status="completed",
                    total_found=10,
                    imported=10,
                    skipped=0,
                    errors=0,
                    started_at=datetime.now(timezone.utc),
                    completed_at=datetime.now(timezone.utc),
                )
            )
            db.add(
                ScrapedCourse(
                    scrape_job_id=old_job_id,
                    university_id=uni_id,
                    course_name="Bachelor of Old Run Course",
                    status="pending",
                )
            )
            db.add(
                ScrapedCourse(
                    scrape_job_id=new_job_id,
                    university_id=uni_id,
                    course_name="Bachelor of New Run Course",
                    status="pending",
                )
            )
            await db.commit()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/scrape/staged/{new_job_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = {c["courseName"] for c in body["courses"]}
        assert "Bachelor of New Run Course" in names
        assert "Bachelor of Old Run Course" not in names
        assert body["lastScrape"]["jobId"] == new_job_id
    finally:
        await _cleanup([old_job_id, new_job_id])

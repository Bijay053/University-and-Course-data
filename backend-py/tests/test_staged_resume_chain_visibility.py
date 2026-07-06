"""QMUL under-reporting fix: /api/scrape/staged/{job_id} must surface every
pending course for the university, not just rows tagged with that exact
job_id.

Root cause: large scrapes can resume across multiple ``scrape_runtime_jobs``
rows (task229 resume checkpoint). A worker restart/timeout leaves a partial
job's staged courses behind with status='pending'; the next run for the same
university picks up where it left off under a NEW job_id and never re-tags
the earlier rows. Filtering the staged-courses list strictly by
``scrape_job_id`` therefore hid the earlier-job rows from reviewers even
though they were still legitimately 'pending' (e.g. QMUL: 122 of 409 shown,
228 invisible under two stale job_ids from interrupted runs, 59 genuinely
skipped).
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
async def test_staged_list_spans_resume_chain_job_ids():
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
        # Both the current job's own row AND the earlier resumed job's row
        # must be visible — that's the whole point of the fix.
        assert "Bachelor of New Run Course" in names
        assert "Bachelor of Old Run Course" in names
        assert body["lastScrape"]["jobId"] == new_job_id
    finally:
        await _cleanup([old_job_id, new_job_id])

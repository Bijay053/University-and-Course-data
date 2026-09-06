from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.dependencies import get_current_user
from app.main import app
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.release_info import UNKNOWN_RELEASE
from app.schemas.scrape import ScrapeJobRead
from app.services.scraper.job_claim import claim_runtime_job


def _job(**values) -> ScrapeRuntimeJob:
    return ScrapeRuntimeJob(
        runtime_job_id=values.pop("runtime_job_id"),
        job_type=values.pop("job_type", "full"),
        status=values.pop("status", "completed"),
        imported=0,
        skipped=0,
        errors=0,
        total_found=0,
        current=0,
        **values,
    )


def test_job_schema_exposes_release_and_claim_history() -> None:
    job = _job(
        runtime_job_id="release-normal",
        release_revision="abc123def456",
        release_history=[
            {
                "claim": 1,
                "release": "abc123def456",
                "claimedAt": "2026-09-06T01:02:03.000000Z",
            }
        ],
    )

    payload = ScrapeJobRead.model_validate(job).model_dump()

    assert payload["release_revision"] == "abc123def456"
    assert payload["release_history"] == job.release_history


def test_job_schema_preserves_requeued_mixed_release_history() -> None:
    history = [
        {"claim": 1, "release": "release-a", "claimedAt": "2026-09-06T01:00:00Z"},
        {"claim": 2, "release": "release-b", "claimedAt": "2026-09-06T02:00:00Z"},
    ]
    job = _job(
        runtime_job_id="release-resumed",
        requeue_count=1,
        release_revision="release-b",
        release_history=history,
    )

    payload = ScrapeJobRead.model_validate(job).model_dump()

    assert payload["release_revision"] == "release-b"
    assert payload["release_history"] == history
    assert len({item["release"] for item in payload["release_history"]}) == 2


def test_job_schema_handles_missing_release_for_legacy_jobs() -> None:
    job = _job(
        runtime_job_id="release-legacy",
    )

    payload = ScrapeJobRead.model_validate(job).model_dump()

    assert payload["release_revision"] is None
    assert payload["release_history"] is None


@pytest.mark.asyncio
async def test_claims_persist_normal_resumed_and_missing_release_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper import job_claim

    job_id = f"test_release_{uuid.uuid4().hex[:12]}"
    await engine.dispose()
    try:
        async with AsyncSessionLocal() as db:
            db.add(_job(runtime_job_id=job_id, status="queued"))
            await db.commit()

            monkeypatch.setattr(job_claim, "get_release_revision", lambda: "release-a")
            assert await claim_runtime_job(db, job_id) is True
            await db.refresh(await db.get(ScrapeRuntimeJob, job_id))
            first = await db.get(ScrapeRuntimeJob, job_id)
            assert first is not None
            assert first.claim_count == 1
            assert first.release_revision == "release-a"
            assert [entry["release"] for entry in first.release_history] == ["release-a"]

            # A duplicate delivery cannot add a second claim event.
            assert await claim_runtime_job(db, job_id) is False

            # Requeue/resume the same runtime job under a different release.
            first.status = "queued"
            await db.commit()
            monkeypatch.setattr(job_claim, "get_release_revision", lambda: "release-b")
            assert await claim_runtime_job(db, job_id) is True
            await db.refresh(first)
            assert first.claim_count == 2
            assert first.release_revision == "release-b"
            assert [entry["release"] for entry in first.release_history] == [
                "release-a",
                "release-b",
            ]
            assert [entry["claim"] for entry in first.release_history] == [1, 2]

            # Resolution failures are explicit rather than silently blank.
            first.status = "queued"
            await db.commit()
            monkeypatch.setattr(job_claim, "get_release_revision", lambda: UNKNOWN_RELEASE)
            assert await claim_runtime_job(db, job_id) is True
            await db.refresh(first)
            assert first.release_revision == UNKNOWN_RELEASE
            assert first.release_history[-1]["release"] == UNKNOWN_RELEASE
    finally:
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
                {"jid": job_id},
            )
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_release_job_endpoints_require_view_permission() -> None:
    job_id = f"test_release_auth_{uuid.uuid4().hex[:12]}"
    transport = httpx.ASGITransport(app=app)
    async with AsyncSessionLocal() as db:
        db.add(_job(runtime_job_id=job_id))
        await db.commit()

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/api/scrape/jobs",
                f"/api/scrape/jobs/{job_id}",
                "/api/scrape/history",
            ):
                assert (await client.get(path)).status_code == 401

            async def view_user() -> dict:
                return {"permissions": ["scraping.view"]}

            app.dependency_overrides[get_current_user] = view_user
            detail = await client.get(f"/api/scrape/jobs/{job_id}")
            assert detail.status_code == 200
            assert "release_revision" in detail.json()
            assert "release_history" in detail.json()

            listing = await client.get("/api/scrape/jobs")
            assert listing.status_code == 200

            history = await client.get("/api/scrape/history")
            assert history.status_code == 200
            matching = next(
                run for run in history.json()["runs"]
                if run["runtimeJobId"] == job_id
            )
            assert matching["releaseRevision"] is None
            assert matching["releaseHistory"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
                {"jid": job_id},
            )
            await cleanup.commit()
        await engine.dispose()
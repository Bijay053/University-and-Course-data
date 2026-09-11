from __future__ import annotations

import uuid

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.dependencies import get_current_user
from app.main import app
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.scrape_run_alert import ScrapeRunAlert
from app.models.university import University
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
async def test_claim_sql_creates_one_mixed_release_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper import job_claim

    db = MagicMock()
    result = MagicMock()
    result.first.return_value = ("job-1",)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    monkeypatch.setattr(job_claim, "get_release_revision", lambda: "release-b")

    assert await claim_runtime_job(db, "job-1") is True

    statement = str(db.execute.await_args.args[0])
    assert "WITH ORDINALITY" in statement
    assert "'mixed_release_execution'" in statement
    assert "NOT EXISTS" in statement
    assert "string_agg(release, ', ' ORDER BY first_claim)" in statement
    assert "UPDATE scrape_run_alerts existing" in statement
    db.commit.assert_awaited_once()


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
            warnings = (
                await db.execute(
                    text(
                        "SELECT message FROM scrape_run_alerts "
                        "WHERE scrape_run_id = :jid "
                        "AND rule_id = 'mixed_release_execution'"
                    ),
                    {"jid": job_id},
                )
            ).all()
            assert len(warnings) == 1
            assert warnings[0].message == (
                "Scrape job spans multiple releases: release-a, release-b"
            )

            # Another claim from the current release does not duplicate the warning.
            first.status = "queued"
            await db.commit()
            assert await claim_runtime_job(db, job_id) is True
            warning_count = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM scrape_run_alerts "
                        "WHERE scrape_run_id = :jid "
                        "AND rule_id = 'mixed_release_execution'"
                    ),
                    {"jid": job_id},
                )
            ).scalar_one()
            assert warning_count == 1

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
            assert matching["releaseWarnings"] == []
            assert matching["catalogueGuard"] is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
                {"jid": job_id},
            )
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_exposes_persisted_catalogue_guard() -> None:
    job_id = f"test_catalogue_guard_{uuid.uuid4().hex[:12]}"
    guard = {
        "kind": "discovery_filter_collapse",
        "status": "failed_degraded",
        "level": "error",
        "message": "Catalogue discovery/filter collapse",
        "raw_discovered": 236,
        "extractable": 0,
        "staged": 0,
        "expected_min_courses": 200,
    }
    transport = httpx.ASGITransport(app=app)
    async with AsyncSessionLocal() as db:
        db.add(_job(runtime_job_id=job_id, gate_skip_counts={"catalogue_guard": guard}))
        await db.commit()

    async def view_user() -> dict:
        return {"permissions": ["scraping.view"]}

    try:
        app.dependency_overrides[get_current_user] = view_user
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/scrape/history")
        assert response.status_code == 200
        matching = next(
            run for run in response.json()["runs"]
            if run["runtimeJobId"] == job_id
        )
        assert matching["catalogueGuard"] == guard
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :jid"),
                {"jid": job_id},
            )
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_filters_release_mixed_status_university_and_pagination() -> None:
    prefix = f"test_release_filter_{uuid.uuid4().hex[:12]}"
    job_ids = [f"{prefix}_{suffix}" for suffix in ("match_old", "match_new", "other_release", "other_uni")]
    transport = httpx.ASGITransport(app=app)
    await engine.dispose()
    try:
        async with AsyncSessionLocal() as db:
            university_ids = list(
                (await db.execute(select(University.id).order_by(University.id).limit(2))).scalars()
            )
            if len(university_ids) < 2:
                pytest.skip("history filter test requires two universities")
            primary_university_id, other_university_id = university_ids
            db.add_all([
                _job(runtime_job_id=job_ids[0], university_id=primary_university_id, release_revision="release-filter-a"),
                _job(runtime_job_id=job_ids[1], university_id=primary_university_id, release_revision="release-filter-a"),
                _job(runtime_job_id=job_ids[2], university_id=primary_university_id, release_revision="release-filter-b"),
                _job(runtime_job_id=job_ids[3], university_id=other_university_id, release_revision="release-filter-a"),
            ])
            await db.flush()
            for job_id in (job_ids[0], job_ids[1], job_ids[3]):
                db.add(ScrapeRunAlert(
                    scrape_run_id=job_id,
                    rule_id="mixed_release_execution",
                    severity="warning",
                    message="Scrape job spans multiple releases",
                ))
            await db.commit()

        async def view_user() -> dict:
            return {"permissions": ["scraping.view"]}

        app.dependency_overrides[get_current_user] = view_user
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/scrape/history",
                params={
                    "release_revision": "release-filter-a",
                    "mixed_release": "true",
                    "university_id": primary_university_id,
                    "limit": 1,
                    "offset": 1,
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert payload["limit"] == 1
        assert payload["offset"] == 1
        assert len(payload["runs"]) == 1
        assert payload["runs"][0]["runtimeJobId"] in job_ids[:2]
        assert payload["runs"][0]["releaseRevision"] == "release-filter-a"
        assert payload["runs"][0]["universityId"] == primary_university_id
        assert payload["runs"][0]["releaseWarnings"][0]["ruleId"] == "mixed_release_execution"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id LIKE :prefix"),
                {"prefix": f"{prefix}%"},
            )
            await cleanup.commit()
        await engine.dispose()

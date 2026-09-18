import uuid

import httpx
import pytest
from sqlalchemy import delete

from app.database import AsyncSessionLocal, engine
from app.dependencies import get_current_user
from app.main import app
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.routers.scrape import _job_quality_report


def test_lifecycle_completion_is_independent_of_extraction_quality() -> None:
    report = _job_quality_report("completed", 82)

    assert report["lifecycleStatus"] == "completed"
    assert report["extractionQuality"]["status"] == "extraction_errors"
    assert report["extractionQuality"]["successful"] is False
    assert report["extractionQuality"]["errorCount"] == 82


def test_in_progress_job_does_not_claim_quality_success() -> None:
    assert _job_quality_report("running", 0)["extractionQuality"] == {
        "status": "pending",
        "successful": None,
        "errorCount": 0,
    }


def test_failed_lifecycle_without_counted_errors_is_not_quality_success() -> None:
    assert _job_quality_report("failed", 0)["extractionQuality"] == {
        "status": "not_assessed",
        "successful": None,
        "errorCount": 0,
    }


@pytest.mark.parametrize(
    "status", ["completed", "completed_with_errors", "completed_with_warnings"]
)
def test_clean_terminal_job_reports_no_extraction_errors(status) -> None:
    assert _job_quality_report(status, 0)["extractionQuality"] == {
        "status": "no_extraction_errors",
        "successful": True,
        "errorCount": 0,
    }


@pytest.mark.asyncio
async def test_history_reports_clean_and_error_jobs_together() -> None:
    """A clean job must not break history or hide a neighbouring failed result."""
    release = "quality-test-" + uuid.uuid4().hex
    cases = {
        release + "-clean": ("completed", 0, "no_extraction_errors", True),
        release + "-errors": ("completed", 82, "extraction_errors", False),
        release + "-running": ("running", 0, "pending", None),
        release + "-failed": ("failed", 0, "not_assessed", None),
    }
    await engine.dispose()
    try:
        async with AsyncSessionLocal() as db:
            db.add_all([
                ScrapeRuntimeJob(
                    runtime_job_id=job_id,
                    job_type="full",
                    status=status,
                    errors=errors,
                    release_revision=release,
                )
                for job_id, (status, errors, _, _) in cases.items()
            ])
            await db.commit()

        async def view_user():
            return {"permissions": ["scraping.view"]}

        app.dependency_overrides[get_current_user] = view_user
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/scrape/history", params={"release_revision": release}
            )
        assert response.status_code == 200
        rows = {row["runtimeJobId"]: row for row in response.json()["runs"]}
        assert set(rows) == set(cases)
        for job_id, (status, errors, quality, successful) in cases.items():
            assert rows[job_id]["status"] == status
            assert rows[job_id]["lifecycleStatus"] == status
            assert rows[job_id]["extractionQuality"] == {
                "status": quality,
                "successful": successful,
                "errorCount": errors,
            }
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(ScrapeRuntimeJob).where(
                    ScrapeRuntimeJob.runtime_job_id.in_(list(cases))
                )
            )
            await db.commit()
        await engine.dispose()
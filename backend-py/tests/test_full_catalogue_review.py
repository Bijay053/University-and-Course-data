"""Full-catalogue review contract and shared isolation boundary regression."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.schemas.scrape import StartScrapeBody
from app.services.scraper.review_policy import (
    full_catalogue_review, prepare_full_catalogue_review, persist_review_policy,
)
from tests.test_scrape_payload_compat import client_with_uni
from tests.test_autonomous_verification_isolation import staging, StageDB, stage_args


@pytest.mark.parametrize("value", ["true", 1, None, {}])
def test_mode_requires_explicit_boolean(value):
    with pytest.raises(ValidationError):
        StartScrapeBody.model_validate({"fullCatalogueReviewOnly": value})


@pytest.mark.parametrize("conflict", [
    {"fastMode": True}, {"bulkMode": True},
    {"courseUrls": ["https://example.edu/course"]},
    {"retrySourceJobId": "old"}, {"browserRescueAttempted": True},
])
def test_conflicting_scopes_rejected(conflict):
    with pytest.raises(ValidationError):
        StartScrapeBody.model_validate({"fullCatalogueReviewOnly": True, **conflict})


def test_fresh_policy_drops_checkpoints_without_verification_binding():
    job = SimpleNamespace(
        request_payload={
            "fullCatalogueReviewOnly": True, "resumeCourseIds": [4],
            "resumeSourceJobIds": ["old"], "courseUrls": ["old"],
            "course_urls": ["old"], "forceDiscovery": False,
        }, discovered_config={}, fast_mode=True,
    )
    assert prepare_full_catalogue_review(job)
    assert job.request_payload == {
        "fullCatalogueReviewOnly": True, "forceDiscovery": True,
        "fastMode": False, "fast_mode": False,
    }
    original = job.discovered_config
    persist_review_policy(job, counters={"staged": 70, "skipped": 3})
    assert job.discovered_config is not original
    policy = job.discovered_config["fullCatalogueReviewPolicy"]
    assert policy["verification_sample_cap"] is None
    assert not policy["full_catalogue_verified"]
    assert not policy["removal_reconciliation"]
    assert policy["counters"] == {"staged": 70, "skipped": 3}
    policy = persist_review_policy(job, skip_reasons={
        "duplicate_url_in_job": 2, "duplicate_name_deduplicated": 1,
        "online_only": 4,
    })
    assert policy["duplicate_skips"] == 3


def test_full_review_does_not_enter_bounded_verification_or_resume_paths():
    import inspect
    from app.services.scraper import orchestrator
    source = inspect.getsource(orchestrator._run_claimed_scrape)
    assert "_preserve_review = bool(_verification) or _full_review" in source
    assert "max_courses = sys.maxsize" in source
    assert source.index("max_courses = sys.maxsize") < source.index("links = cap_verification_links")
    assert "and not _preserve_review\n            and job.university_id" in source
    assert source.index('if _full_review:\n            persist_review_policy(') < source.index(
        "# Stop before ALL mutation/dispatch hooks"
    )


def test_review_route_preserves_contract(client_with_uni, monkeypatch):
    client, db = client_with_uni
    monkeypatch.setattr("app.tasks.scrape_tasks.scrape_university", MagicMock())
    monkeypatch.setattr("app.tasks.scrape_tasks.set_initial_dispatch_lock", MagicMock())
    response = client.post("/api/scrape/start", json={
        "universityId": 42, "fullCatalogueReviewOnly": True,
    })
    assert response.status_code == 202, response.text
    job = db.added[0]
    assert full_catalogue_review(job.request_payload)
    assert job.request_payload["forceDiscovery"]
    assert "autonomousVerification" not in job.request_payload
    assert job.discovered_config["fullCatalogueReviewPolicy"]["preserve_existing"]


def test_review_route_requires_permission(client_with_uni):
    from app.main import app
    from app.dependencies import get_current_user
    client, db = client_with_uni
    app.dependency_overrides[get_current_user] = lambda: {"sub": "limited", "permissions": []}
    response = client.post("/api/scrape/start", json={
        "universityId": 42, "fullCatalogueReviewOnly": True,
    })
    assert response.status_code == 403
    assert not db.added


def test_review_route_rejects_unsafe_active_job(client_with_uni):
    client, db = client_with_uni
    db.active_job = SimpleNamespace(request_payload={}, runtime_job_id="old", status="running")
    response = client.post("/api/scrape/start", json={
        "universityId": 42, "fullCatalogueReviewOnly": True,
    })
    assert response.status_code == 409
    assert not db.added


@pytest.mark.asyncio
async def test_full_review_stages_over_fifty_and_preserves_existing(staging):
    class CatalogueDB(StageDB):
        async def execute(self, statement):
            if not statement.is_delete and len(statement.selected_columns) == 1:
                params = statement.compile().params
                matched = next(
                    (row.id for row in self.added
                     if row.canonical_course_url in params.values()), None,
                )
                return SimpleNamespace(scalar_one_or_none=lambda: matched)
            return await super().execute(statement)

    db = CatalogueDB()
    for index in range(75):
        args = stage_args()
        args["source_url"] = f"https://example.edu/courses/course-{index}"
        args["payload"]["course_website"] = args["source_url"]
        args["course_name"] = f"Bachelor of Business {index}"
        result = await staging.stage_course(db, **args, preserve_existing=True)
        assert result.saved, result.reason
    assert len(db.added) == 75
    assert not db.deleted
    assert not db.inheritance_read
    assert all(row.status == "pending" and row.auto_publish_status == "review" for row in db.added)
    duplicate = await staging.stage_course(db, **args, preserve_existing=True)
    assert not duplicate.saved
    assert duplicate.reason == "rejected: duplicate_url_in_job"
    assert len(db.added) == 75


@pytest.mark.asyncio
async def test_worker_followup_uses_durable_source_policy():
    from unittest.mock import AsyncMock
    from app.services.scraper.review_policy import blocks_automatic_followup
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        request_payload={"fullCatalogueReviewOnly": True},
    )))
    assert await blocks_automatic_followup(db, "review")
    db.get.return_value.request_payload = {}
    assert not await blocks_automatic_followup(db, "ordinary")


@pytest.mark.asyncio
async def test_review_run_never_authorizes_removals():
    from unittest.mock import AsyncMock
    from app.services.scraper.removal_reconciliation import get_removal_reconciliation
    from tests.test_removal_reconciliation import _result
    job = SimpleNamespace(
        university_id=4, status="completed", errors=0, runtime_job_id="review",
        request_payload={"fullCatalogueReviewOnly": True}, job_type="single",
    )
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[
        _result(scalars=[SimpleNamespace(status="approved", course_id=11)]),
        _result(), _result(),
    ])
    result = await get_removal_reconciliation(db, "review")
    assert not result["ready"]
    assert result["courses"] == []


@pytest.mark.parametrize("status", ["queued", "running", "completed", "stopped", "failed"])
@pytest.mark.parametrize("review", [True, False])
def test_authenticated_status_and_reloaded_job_preserve_mode(client_with_uni, status, review):
    from fastapi.testclient import TestClient
    from jose import jwt
    from app.main import app
    from app.config import settings
    from app.dependencies import get_current_user
    from app.models import ScrapeRuntimeJob

    client, db = client_with_uni
    app.dependency_overrides.pop(get_current_user)
    token = jwt.encode(
        {"sub": "review-reader", "permissions": ["scraping.view"]},
        settings.session_secret, algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    db.jobs["review-status"] = ScrapeRuntimeJob(
        runtime_job_id="review-status", university_id=42, status=status,
        fast_mode=False, request_payload={"fullCatalogueReviewOnly": review},
        imported=3, errors=1, skipped=2, current=6, total_found=6,
    )
    response = client.get("/api/scrape/status/review-status", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["fullCatalogueReviewOnly"] is review
    assert response.json()["fastMode"] is False
    # A new browser client has no initiating component state. Restore only from
    # the persisted job, including the generic details endpoint used elsewhere.
    reloaded_client = TestClient(app)
    for path in ("/api/scrape/status/review-status?since=42", "/api/scrape/jobs/review-status"):
        restored = reloaded_client.get(path, headers=headers)
        assert restored.status_code == 200, restored.text
        assert restored.json()["fullCatalogueReviewOnly"] is review
    assert client.get("/api/scrape/status/review-status").status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["early_return", "failed", "cancelled", "completed"])
async def test_review_finalizer_audits_every_exit(outcome):
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from app.services.scraper.review_policy import run_full_catalogue_review
    job = SimpleNamespace(
        runtime_job_id="review", request_payload={"fullCatalogueReviewOnly": True},
        discovered_config={}, imported=4, skipped=2, errors=1,
        status="stopped" if outcome == "cancelled" else outcome,
        completed_at=datetime.now(timezone.utc), error_message="failure" if outcome == "failed" else None,
    )
    persist_review_policy(job, skip_reasons={"duplicate_url_in_job": 2})
    db = SimpleNamespace(
        get=AsyncMock(return_value=job), rollback=AsyncMock(), commit=AsyncMock(),
    )

    async def run():
        if outcome == "failed":
            raise RuntimeError("failed pipeline")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return {"ok": outcome == "completed", "staged": 4, "skipped": 2, "errors": 1}

    if outcome in {"failed", "cancelled"}:
        with pytest.raises(RuntimeError if outcome == "failed" else asyncio.CancelledError):
            await run_full_catalogue_review(db, job, run)
    else:
        await run_full_catalogue_review(db, job, run)
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()
    metadata = job.discovered_config["fullCatalogueReviewPolicy"]
    assert metadata["outcome"] == job.status
    assert metadata["completed_at"] == job.completed_at.isoformat()
    assert metadata["counters"] == {"staged": 4, "skipped": 2, "errors": 1}
    assert metadata["duplicate_skips"] == 2


@pytest.mark.asyncio
async def test_queued_stop_persists_review_outcome(client_with_uni):
    from app.routers.scrape import _hard_stop_job
    from app.models import ScrapeRuntimeJob
    _, db = client_with_uni
    job = ScrapeRuntimeJob(
        runtime_job_id="queued-review", status="queued",
        request_payload={"fullCatalogueReviewOnly": True},
        imported=0, skipped=0, errors=0,
    )
    await _hard_stop_job(db, job)
    metadata = job.discovered_config["fullCatalogueReviewPolicy"]
    assert metadata["outcome"] == "stopped"
    assert metadata["completed_at"]
    assert metadata["error_message"] == "Stopped by user"


@pytest.mark.asyncio
async def test_worker_failure_persists_review_outcome(monkeypatch):
    from unittest.mock import AsyncMock
    from app.tasks import scrape_tasks
    job = SimpleNamespace(
        request_payload={"fullCatalogueReviewOnly": True},
        discovered_config={}, status="running", imported=9, skipped=2, errors=1,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=job), commit=AsyncMock())

    class Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", Session)
    await scrape_tasks._mark_failed("review", "worker timeout")
    metadata = job.discovered_config["fullCatalogueReviewPolicy"]
    assert metadata["outcome"] == "failed"
    assert metadata["completed_at"]
    assert metadata["counters"] == {"staged": 9, "skipped": 2, "errors": 1}
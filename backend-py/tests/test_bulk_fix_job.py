from __future__ import annotations

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class _Session:
    def __init__(self, job):
        self.job = job
        self.snapshots = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, _job_id):
        return self.job

    async def commit(self):
        self.snapshots.append(copy.deepcopy(self.job.approval_summary))

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_bulk_fix_persists_post_batch_counts_and_audit_metadata(monkeypatch):
    from app.routers import scrape as scrape_router
    from app.services.scraper import job_claim
    from app.tasks import scrape_tasks

    job = SimpleNamespace(
        request_payload={
            "courseIds": [11, 12],
            "targetFields": ["duration"],
            "aiProvider": "openai",
        },
        approval_summary={"counts": {"queued": 2}, "results": []},
        university_id=7,
        current=0,
        imported=0,
        skipped=0,
        errors=0,
        heartbeat_at=None,
        status="queued",
        completed_at=None,
    )
    session = _Session(job)
    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", lambda: session)

    async def claim(_db, _job_id):
        job.status = "running"
        return True

    async def extract(body, _db):
        assert body.ids == [11, 12]
        assert body.target_fields == ["duration"]
        return {
            "results": [
                {
                    "id": 11,
                    "ok": True,
                    "updated_fields": ["duration"],
                    "refreshed_evidence_fields": ["duration"],
                    "extraction_passes": 2,
                    "ai_provider": "openai",
                },
                {
                    "id": 12,
                    "ok": True,
                    "updated_fields": [],
                    "refreshed_evidence_fields": ["duration"],
                    "extraction_passes": 1,
                    "ai_provider": "openai",
                },
            ]
        }

    monkeypatch.setattr(job_claim, "claim_runtime_job", claim)
    monkeypatch.setattr(scrape_router, "re_extract_staged", extract)

    await scrape_tasks._async_bulk_fix("fix-test")

    post_batch = session.snapshots[-2]
    assert post_batch["counts"] == {
        "queued": 0,
        "running": 0,
        "completed": 1,
        "noProgress": 1,
        "failed": 0,
    }
    assert post_batch["results"][0]["ai_provider"] == "openai"
    assert post_batch["results"][0]["extraction_passes"] == 2
    assert post_batch["results"][1]["outcome"] == "no_progress"
    assert job.status == "completed"
    assert job.completed_at <= datetime.now(timezone.utc)


def test_bulk_fix_active_job_equivalence_includes_targets_and_source_job():
    from app.routers.scrape import _bulk_fix_request_matches

    job = SimpleNamespace(
        request_payload={
            "courseIds": [11, 12],
            "targetFields": ["international_fee"],
            "sourceJobId": "review-a",
        }
    )

    assert _bulk_fix_request_matches(
        job,
        course_ids=[12, 11],
        target_fields=["international_fee"],
        source_job_id="review-a",
    )
    assert not _bulk_fix_request_matches(
        job,
        course_ids=[11, 12],
        target_fields=["duration"],
        source_job_id="review-a",
    )
    assert not _bulk_fix_request_matches(
        job,
        course_ids=[11, 12],
        target_fields=["international_fee"],
        source_job_id="review-b",
    )


def test_bulk_fix_active_job_equivalence_includes_forced_fields_and_reasons():
    from app.routers.scrape import _bulk_fix_request_matches

    job = SimpleNamespace(
        request_payload={
            "courseIds": [11],
            "targetFields": [],
            "forceFields": ["course_location"],
            "forceReasons": {"course_location": "Campus list is known stale"},
            "sourceJobId": None,
        }
    )

    assert _bulk_fix_request_matches(
        job,
        course_ids=[11],
        target_fields=[],
        force_fields=["course_location"],
        force_reasons={"course_location": "Campus list is known stale"},
        source_job_id=None,
    )
    assert not _bulk_fix_request_matches(
        job,
        course_ids=[11],
        target_fields=[],
        force_fields=["course_location"],
        force_reasons={"course_location": "Different rationale"},
        source_job_id=None,
    )


def test_force_fields_are_limited_and_require_nonblank_reasons():
    from pydantic import ValidationError

    from app.routers.scrape import ReExtractBody, StartBulkFixBody

    valid = ReExtractBody(
        ids=[1],
        universityId=7,
        forceFields=["international_fee"],
        forceReasons={"international_fee": "Fee was copied from an old page"},
    )
    assert valid.force_reasons == {
        "international_fee": "Fee was copied from an old page"
    }
    with pytest.raises(ValidationError):
        StartBulkFixBody(
            ids=[1],
            universityId=7,
            forceFields=["duration"],
            forceReasons={"duration": "Required"},
        )
    with pytest.raises(ValidationError):
        ReExtractBody(
            ids=[1],
            universityId=7,
            forceFields=["course_location"],
            forceReasons={"course_location": "   "},
        )


def test_fee_target_does_not_allow_unrelated_reextract_fields():
    from app.routers.scrape import _targeted_reextract_fields

    allowed = _targeted_reextract_fields(["international_fee"])

    assert allowed == {"international_fee", "fee_term", "fee_year", "currency"}
    assert "other_requirement" not in allowed
    assert "extraction_method" not in allowed
    assert "cricos_code" not in allowed


def test_bulk_fix_retry_finds_older_match_after_newer_nonmatching_job():
    from app.routers.scrape import _matching_bulk_fix_job

    older_match = SimpleNamespace(
        request_payload={
            "courseIds": [11, 12],
            "targetFields": ["international_fee"],
            "sourceJobId": "review-a",
        }
    )
    newer_nonmatch = SimpleNamespace(
        request_payload={
            "courseIds": [11, 12],
            "targetFields": ["duration"],
            "sourceJobId": "review-a",
        }
    )

    assert _matching_bulk_fix_job(
        [newer_nonmatch, older_match],
        course_ids=[12, 11],
        target_fields=["international_fee"],
        source_job_id="review-a",
    ) is older_match


@pytest.mark.asyncio
async def test_bulk_fix_ignores_unrelated_updates_when_target_gap_remains(monkeypatch):
    from app.routers import scrape as scrape_router
    from app.services.scraper import job_claim
    from app.tasks import scrape_tasks

    job = SimpleNamespace(
        request_payload={
            "courseIds": [11],
            "targetFields": ["international_fee", "duration"],
            "aiProvider": "openai",
        },
        approval_summary={"counts": {"queued": 1}, "results": []},
        university_id=7,
        current=0,
        imported=0,
        skipped=0,
        errors=0,
        heartbeat_at=None,
        status="queued",
        completed_at=None,
    )
    session = _Session(job)
    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", lambda: session)

    async def claim(_db, _job_id):
        job.status = "running"
        return True

    async def extract(_body, _db):
        return {
            "results": [{
                "id": 11,
                "ok": True,
                "updated_fields": ["other_requirement", "extraction_method"],
                "refreshed_evidence_fields": ["academic_level"],
            }]
        }

    monkeypatch.setattr(job_claim, "claim_runtime_job", claim)
    monkeypatch.setattr(scrape_router, "re_extract_staged", extract)

    await scrape_tasks._async_bulk_fix("fix-targeted")

    result = job.approval_summary["results"][0]
    assert result["outcome"] == "no_progress"
    assert result["updated_fields"] == []
    assert result["refreshed_evidence_fields"] == []
    assert result["all_updated_fields"] == [
        "other_requirement",
        "extraction_method",
    ]
    assert job.approval_summary["counts"]["completed"] == 0
    assert job.approval_summary["counts"]["noProgress"] == 1


@pytest.mark.asyncio
async def test_bulk_fix_forwards_forced_fields_and_reasons_to_each_batch(monkeypatch):
    from app.routers import scrape as scrape_router
    from app.services.scraper import job_claim
    from app.tasks import scrape_tasks

    job = SimpleNamespace(
        request_payload={
            "courseIds": [11, 12, 13, 14, 15, 16],
            "forceFields": ["course_location"],
            "forceReasons": {"course_location": "Campus locations were stale"},
        },
        approval_summary={"counts": {"queued": 6}, "results": []},
        university_id=7,
        current=0,
        imported=0,
        skipped=0,
        errors=0,
        heartbeat_at=None,
        status="queued",
        completed_at=None,
    )
    session = _Session(job)
    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", lambda: session)

    async def claim(_db, _job_id):
        job.status = "running"
        return True

    calls = []

    async def extract(body, _db):
        calls.append(body)
        return {"results": [{"id": course_id, "ok": True} for course_id in body.ids]}

    monkeypatch.setattr(job_claim, "claim_runtime_job", claim)
    monkeypatch.setattr(scrape_router, "re_extract_staged", extract)

    await scrape_tasks._async_bulk_fix("fix-forced")

    assert [call.ids for call in calls] == [[11, 12, 13, 14, 15], [16]]
    assert all(call.force_fields == ["course_location"] for call in calls)
    assert all(
        call.force_reasons == {"course_location": "Campus locations were stale"}
        for call in calls
    )
    assert all(call.target_fields == ["course_location"] for call in calls)


@pytest.mark.asyncio
async def test_bulk_fix_resumes_from_persisted_batch_checkpoint(monkeypatch):
    from app.routers import scrape as scrape_router
    from app.services.scraper import job_claim
    from app.tasks import scrape_tasks

    prior = {
        "id": 11,
        "ok": True,
        "outcome": "completed",
        "updated_fields": ["duration"],
        "ai_provider": "openai",
        "extraction_passes": 1,
    }
    job = SimpleNamespace(
        request_payload={"courseIds": [11, 12]},
        approval_summary={
            "counts": {"queued": 1, "completed": 1, "noProgress": 0, "failed": 0},
            "results": [prior],
        },
        university_id=7,
        current=1,
        imported=1,
        skipped=0,
        errors=0,
        heartbeat_at=None,
        status="queued",
        completed_at=None,
    )
    session = _Session(job)
    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", lambda: session)

    async def claim(_db, _job_id):
        job.status = "running"
        return True

    seen = []

    async def extract(body, _db):
        seen.extend(body.ids)
        return {"results": [{"id": 12, "ok": False, "error": "provider timeout"}]}

    monkeypatch.setattr(job_claim, "claim_runtime_job", claim)
    monkeypatch.setattr(scrape_router, "re_extract_staged", extract)

    await scrape_tasks._async_bulk_fix("fix-resume")

    assert seen == [12]
    assert [item["id"] for item in job.approval_summary["results"]] == [11, 12]
    assert job.approval_summary["counts"]["completed"] == 1
    assert job.approval_summary["counts"]["failed"] == 1
    assert job.status == "completed_with_errors"


def test_worker_startup_does_not_reset_active_bulk_fix_jobs():
    from app.tasks.celery_app import _RESET_SQL

    assert "job_type <> 'bulk_fix'" in _RESET_SQL
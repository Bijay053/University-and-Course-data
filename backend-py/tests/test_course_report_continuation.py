"""Reviewed continuation regression tests; no network, worker or paid calls."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import scrape_reports as reports
from app.services import ai_repair_workflow as workflow
from app.services.scraper.url_identity import canonical_course_url_key
from app.services.scraper.autonomous_verification import (
    VerificationLimits, cap_verification_links, persist_verification_metadata,
)


def child(job_id="first", urls=None, **overrides):
    values = dict(
        runtime_job_id=job_id, university_id=7, university_name="University",
        status="completed", completed_at=datetime.now(timezone.utc),
        request_payload={"courseReport": {
            "id": "report_1", "source_job_id": "source", "kind": "missing",
            "course_urls": urls or [], "requested_by": "original",
        }, "course_urls": urls or []},
        discovered_config={}, total_found=0, imported=0, errors=0, skipped=0,
        error_message=None, gate_skip_counts={}, url="https://uni.edu/catalogue",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_large_direct_report_retains_tail_but_only_executes_validated_slice():
    urls = [f"https://uni.edu/course?id={i}" for i in range(123)]
    body = reports.CourseReport(kind="missing", course_urls=urls)
    payload = reports.report_payload(child("source"), body, "report_1", 1)
    assert payload["courseReportRemainingUrls"] == urls
    assert payload["courseReport"]["course_urls"] == urls
    assert payload["course_urls"] == urls[:50]
    assert payload["autonomousVerification"]["time_budget_seconds"] == 600


def test_cap_keeps_exact_known_catalogue_candidates_and_attempted_outcomes():
    urls = [f"https://uni.edu/course?programme={i}" for i in range(120)]
    job = child()
    policy = VerificationLimits("source", "report_1", 50, 600, 2, 0)
    selected = cap_verification_links(job, policy, [{"url": u} for u in urls], 500)
    assert [link["url"] for link in selected] == urls[:50]
    metadata = job.discovered_config["autonomousVerification"]
    assert metadata["candidate_urls"] == urls
    # Completion means attempted, not successful. Skips and errors do not
    # become an infinite re-extraction loop; unfinished URLs do remain.
    metadata["completed_urls"] = urls[:20]
    result = reports.report_result(job, {"autonomous": {"phase": "needs_review"}})
    assert result["continuation"] == {
        "available": True, "remaining_urls": urls[20:], "remaining_count": 100,
        "selected_count": 120, "completed_count": 20, "run_count": 1,
        "reason": "review_required",
    }
    assert result["staged"] == 0
    assert result["catalogue_coverage"] == "not_verified"
    assert metadata["full_catalogue_verified"] is False
    # A replay may not change the frozen sample.
    again = cap_verification_links(job, policy, [{"url": urls[-1]}], 500)
    assert [link["url"] for link in again] == urls[:50]
    assert metadata["candidate_urls"] == urls


def test_history_preserves_identity_and_counts_staged_and_failed_attempts():
    urls = [f"https://uni.edu/course/{i}" for i in range(70)]
    first = child(urls=urls)
    first.discovered_config = {"autonomousVerification": {"completed_urls": urls[:50]}}
    second = child("second", urls=urls)
    second.request_payload["course_urls"] = urls[50:]
    second.discovered_config = {"autonomousVerification": {"completed_urls": urls[50:55]}}
    result = reports.report_result(second, {"autonomous": {"phase": "needs_review"}},
                                   [first, second], {canonical_course_url_key(u) for u in urls[55:57]})
    assert result["job_id"] == "second"
    assert result["report_id"] == "report_1"
    assert result["original_job_id"] == "first"
    assert [c["job_id"] for c in result["children"]] == ["first", "second"]
    assert result["continuation"]["remaining_urls"] == urls[57:]
    assert result["continuation"]["completed_count"] == 57
    assert result["continuation"]["run_count"] == 2


def test_programme_url_statuses_preserve_origin_and_settled_outcome():
    submitted, related, queued = (
        "https://uni.edu/course/submitted",
        "https://uni.edu/course/related",
        "https://uni.edu/course/queued",
    )
    job = child(urls=[submitted, related, queued], status="running", completed_at=None)
    job.discovered_config = {"autonomousVerification": {
        "candidate_urls": [submitted, related, queued],
        "selected_urls": [queued],
        "completed_urls": [submitted, related],
        "submitted_urls": [submitted],
        "related_urls": [related],
        "url_outcomes": {submitted: "skipped", related: "error"},
    }}
    result = reports.report_result(
        job, {"autonomous": {"phase": "recovering"}}, staged_keys={
            canonical_course_url_key(submitted),
        },
    )
    assert result["programme_urls"] == [
        {"url": submitted, "origin": "submitted", "status": "staged"},
        {"url": related, "origin": "related", "status": "error"},
        {"url": queued, "origin": "discovered", "status": "processing"},
    ]
    assert result["staged"] == 1


def test_staged_count_uses_persisted_rows_not_stale_import_counter():
    submitted = "https://uni.edu/course/submitted"
    job = child(urls=[submitted])
    job.imported = 1
    job.discovered_config = {"autonomousVerification": {
        "candidate_urls": [submitted],
        "completed_urls": [submitted],
        "submitted_urls": [submitted],
        "url_outcomes": {submitted: "skipped"},
    }}

    result = reports.report_result(
        job,
        {"autonomous": {"phase": "needs_review"}},
        staged_keys=set(),
    )

    assert result["staged"] == 0
    assert result["programme_urls"] == [{
        "url": submitted,
        "origin": "submitted",
        "status": "skipped",
    }]
    assert result["retry"] == {
        "available": True,
        "remaining_urls": [submitted],
        "remaining_count": 1,
    }


def test_programme_url_origins_survive_first_verification_metadata_write():
    job = child()
    job.request_payload["autonomousVerification"] = {
        "submitted_urls": ["https://uni.edu/course/submitted"],
        "related_urls": ["https://uni.edu/course/related"],
    }
    limits = VerificationLimits("source", "report_1", 50, 600, 2, 1)
    metadata = persist_verification_metadata(job, limits, candidate_urls=[])
    assert metadata["submitted_urls"] == ["https://uni.edu/course/submitted"]
    assert metadata["related_urls"] == ["https://uni.edu/course/related"]


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_requires_explicit_strict_review(value):
    with pytest.raises(ValidationError):
        reports.ReviewedContinuation(reviewed=value)


@pytest.mark.asyncio
async def test_course_reports_never_auto_continue():
    db = SimpleNamespace(get=AsyncMock())
    assert await workflow._queue_verification_continuation(
        {"course_report": {"id": "report_1"}}, child(),
        {"budget_exhausted": "time_budget_exhausted"}, db,
    ) is None
    db.get.assert_not_called()


@pytest.mark.asyncio
async def test_large_validation_network_fanout_is_bounded(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url", lambda url: (True, ""))
    real_client = httpx.AsyncClient
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="<html>Official course</html>", extensions={
            "network_stream": SimpleNamespace(get_extra_info=lambda key: ("8.8.8.8", 443)),
        })
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    urls = [f"https://uni.edu/course/{i}" for i in range(500)]
    await reports.validate_official_urls(
        reports.CourseReport(kind="missing", course_urls=urls),
        SimpleNamespace(website="https://uni.edu", scrape_url=None),
    )
    assert seen == urls[:50]
    # Static safety still checks all tail URLs.
    with pytest.raises(HTTPException):
        await reports.validate_official_urls(
            reports.CourseReport(kind="missing", course_urls=urls + ["http://127.0.0.1/secret"]),
            SimpleNamespace(website="https://uni.edu", scrape_url=None),
        )


@pytest.fixture
def endpoint(monkeypatch):
    urls = [f"https://uni.edu/course/{i}" for i in range(80)]
    parent, first = child("source"), child(urls=urls)
    parent.request_payload = {}
    first.discovered_config = {"autonomousVerification": {"completed_urls": urls[:40]}}
    session = {
        "session_id": "report_1", "job_id": "source", "university_id": 7,
        "course_report": first.request_payload["courseReport"], "status": "completed",
        "autonomous": {"phase": "needs_review", "verification_job_id": "first",
                       "verification_job_ids": ["first"]},
    }
    objects = {"source": parent, "first": first, 7: SimpleNamespace(id=7)}
    db = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key: objects.get(key)),
        refresh=AsyncMock(), rollback=AsyncMock(), flush=AsyncMock(),
        add=Mock(side_effect=lambda obj: objects.update({obj.runtime_job_id: obj})),
    )
    monkeypatch.setattr("app.routers.scrape._lock_and_find_active_job", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, "load", AsyncMock(side_effect=lambda *a: session))
    monkeypatch.setattr(workflow, "active_audit", AsyncMock(return_value={}))
    monkeypatch.setattr(workflow, "_staged_url_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(workflow, "save", AsyncMock())
    monkeypatch.setattr(workflow, "dispatch_verification", AsyncMock(side_effect=lambda s, db: s))
    monkeypatch.setattr(reports, "validate_official_urls", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.scraper.ai_repair_agent.acquire_repair_lease", lambda *a: True)
    return db, session, objects, urls


@pytest.mark.asyncio
async def test_review_creates_one_bounded_exact_remaining_child_and_rejects_stale(endpoint):
    db, session, objects, urls = endpoint
    result = await reports.continue_course_report(
        "source", "first", reports.ReviewedContinuation(reviewed=True), db, {"id": 99},
    )
    next_child = objects[result["job_id"]]
    assert next_child.request_payload["course_urls"] == urls[40:]
    assert next_child.request_payload["courseReport"]["requested_by"] == "original"
    assert next_child.request_payload["courseReport"]["source_job_id"] == "source"
    assert next_child.request_payload["retrySourceJobId"] == "first"
    policy = next_child.request_payload["autonomousVerification"]
    assert (policy["max_courses"], policy["time_budget_seconds"], policy["cost_cap_usd"]) == (50, 600, 2)
    assert result["original_job_id"] == "first"
    assert result["continuation"]["available"] is False
    assert db.add.call_count == 1
    with pytest.raises(HTTPException) as error:
        await reports.continue_course_report(
            "source", "first", reports.ReviewedContinuation(reviewed=True), db, {"id": 99},
        )
    assert error.value.status_code == 409
    assert db.add.call_count == 1


@pytest.mark.asyncio
async def test_unacknowledged_or_wrong_source_cannot_continue(endpoint):
    db, session, objects, urls = endpoint
    objects["first"].completed_at = None
    with pytest.raises(HTTPException) as error:
        await reports.continue_course_report(
            "source", "first", reports.ReviewedContinuation(reviewed=True), db, {"id": 1},
        )
    assert error.value.status_code == 409
    objects["first"].request_payload["courseReport"]["source_job_id"] = "other"
    with pytest.raises(HTTPException) as error:
        await reports.continue_course_report(
            "source", "first", reports.ReviewedContinuation(reviewed=True), db, {"id": 1},
        )
    assert error.value.status_code == 404
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_active_university_fence_prevents_second_child(endpoint, monkeypatch):
    db, _, _, _ = endpoint
    monkeypatch.setattr("app.routers.scrape._lock_and_find_active_job",
                        AsyncMock(return_value=child("unrelated-active")))
    with pytest.raises(HTTPException) as error:
        await reports.continue_course_report(
            "source", "first", reports.ReviewedContinuation(reviewed=True), db, {"id": 1},
        )
    assert error.value.status_code == 409
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_list_groups_children_under_one_original_identity(endpoint, monkeypatch):
    db, session, objects, urls = endpoint
    second = child("second", urls=urls)
    second.discovered_config = {"autonomousVerification": {"completed_urls": urls[40:55]}}
    db.execute = AsyncMock(side_effect=[
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [objects["first"], second])),
        SimpleNamespace(all=lambda: [("report_1", session)]),
    ])
    result = await reports.list_course_reports("source", db, {})
    assert len(result["reports"]) == 1
    report = result["reports"][0]
    assert report["job_id"] == "second"
    assert report["report_id"] == "report_1"
    assert report["original_job_id"] == "first"
    assert report["continuation"]["remaining_urls"] == urls[55:]


def test_pipeline_checkpoints_after_handling_and_preserves_source_reviews():
    import inspect
    from app.services.scraper import orchestrator

    source = inspect.getsource(orchestrator._run_claimed_scrape)
    # Retain all known candidates before the helper that truncates links.
    freeze = source.index('"candidate_urls": list(dict.fromkeys([')
    expansion = source.index("links = await _expand_uel_course_links_for_run(", freeze)
    assert freeze < expansion
    # Both bounded verification children and full-catalogue review runs must
    # stage without deleting or replacing source review rows.
    assert "_preserve_review = bool(_verification) or _full_review" in source
    assert source.count("preserve_existing=_preserve_review,") == 2
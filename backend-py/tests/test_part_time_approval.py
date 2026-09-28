"""Part-time-only offerings cannot cross any scraped-course publication boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models import ScrapedCourse
from app.routers import publishing, reviews, scrape, staged_selected_approval
from app.services.auto_publish import should_auto_publish
from app.services.scraper.approve_course import (
    ApprovalValidationError, PART_TIME_APPROVAL_MESSAGE,
    approve_scraped_course, is_part_time_only_course,
)


def row(**overrides):
    fields = dict(id=41, university_id=7, scrape_job_id="job", status="pending",
                  course_id=None, course_name="Master of Science",
                  study_mode="Part-time", study_load=None, extraction_method={},
                  pte_overall=None, toefl_overall=None, cambridge_overall=None,
                  duolingo_overall=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("mode,load,blocked", [
    ("Part-time", None, True),
    ("Part Time", "Full Time", True),
    ("Full-time or Part-time", None, False),
    ("Both", "Part Time", False),
    ("Both", None, False),
    ("Online", None, False),
    (None, None, False),
    ("Part-time only; 1 year equivalent full-time study", "Full Time", True),
    ("Part-time, 1 year equivalent full-time study", None, True),
])
def test_stored_modes(mode, load, blocked):
    assert is_part_time_only_course(row(study_mode=mode, study_load=load)) is blocked


def evidence(field, snippet, *, selected=True, page_type="course"):
    return SimpleNamespace(field_key=field, snippet=snippet, candidate_value=None,
                           normalized_value=None, raw_text=None, selected=selected, page_type=page_type)


def test_selected_bounded_evidence_and_generic_prose():
    sc = row(study_mode="Full-time", study_load=None)
    assert is_part_time_only_course(sc, [evidence("duration", "Only available part-time; 1 year equivalent full-time study")])
    assert not is_part_time_only_course(sc, [evidence("duration", "Some programs are only available part time")])
    assert not is_part_time_only_course(sc, [evidence("duration", "Only available part-time", selected=False)])
    assert not is_part_time_only_course(sc, [evidence("description", "Only available part-time")])
    assert not is_part_time_only_course(row(), [evidence("duration", "3 years full-time or part-time")])
    assert is_part_time_only_course(
        row(study_mode=None),
        [evidence("study_mode", "Some programs are only available part time",
                  selected=True),
         SimpleNamespace(field_key="study_mode", snippet="Some programs are only available part time",
                         candidate_value="Part-time", normalized_value="Part-time",
                         raw_text=None, selected=True, page_type="course")],
    )


@pytest.mark.asyncio
async def test_central_service_rejects_before_any_database_write(monkeypatch):
    monkeypatch.setattr("app.services.scraper.fee_selection.unresolved_fee_selection", lambda sc: False)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), flush=AsyncMock())
    for actor in ("system", "reviewer", "human"):
        with pytest.raises(ApprovalValidationError, match="offered part-time only"):
            await approve_scraped_course(db, row(), actor=actor)
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_staged_row_uses_selected_structured_evidence(monkeypatch):
    monkeypatch.setattr("app.services.scraper.fee_selection.unresolved_fee_selection", lambda sc: False)
    sc = ScrapedCourse(id=41, course_name="Master of Science", study_mode="Full-time",
                       study_load=None, extraction_method={}, status="pending")
    result = Mock()
    result.scalars.return_value.all.return_value = [
        evidence("duration", "Only available part-time; 1 year equivalent full-time study")
    ]
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    with pytest.raises(ApprovalValidationError, match="offered part-time only"):
        await approve_scraped_course(db, sc, actor="reviewer")
    db.execute.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_auto_publish_does_not_mark_part_time_ready():
    sc = row(completeness=100, decision_score=100)
    decision = should_auto_publish(sc)
    assert not decision.auto_publish
    assert decision.reason == PART_TIME_APPROVAL_MESSAGE


@pytest.fixture
def client_db(monkeypatch):
    app = FastAPI()
    app.include_router(reviews.router, prefix="/api")
    app.include_router(scrape.router, prefix="/api/scrape")
    sc = row(international_fee=20000, ielts_overall=6, duration=2,
             intake_months=["September"], has_central_fee_page=False)
    result = Mock()
    result.scalars.return_value.all.return_value = [sc]
    db = SimpleNamespace(get=AsyncMock(return_value=sc),
                         execute=AsyncMock(return_value=result),
                         rollback=AsyncMock(), commit=AsyncMock())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "reviewer", "permissions": ["staged.approve"],
    }
    monkeypatch.setattr("app.services.scraper.fee_selection.unresolved_fee_selection", lambda sc: False)
    with TestClient(app) as client:
        yield client, db, sc


def test_single_and_force_staged_endpoints(client_db):
    client, db, _ = client_db
    response = client.post("/api/scraped-courses/41/approve")
    assert response.status_code == 422
    assert response.json()["detail"] == PART_TIME_APPROVAL_MESSAGE
    response = client.post("/api/scrape/staged/41/approve", json={"force": True})
    assert response.status_code == 422
    assert response.json()["detail"] == PART_TIME_APPROVAL_MESSAGE
    db.commit.assert_not_awaited()


def test_bulk_endpoint_reports_actionable_row_failure(client_db):
    client, db, _ = client_db
    response = client.post("/api/scraped-courses/bulk-approve",
                           params={"university_id": 7})
    assert response.status_code == 200
    assert response.json()["failed"] == 1
    assert response.json()["failures"][0]["error"] == PART_TIME_APPROVAL_MESSAGE
    db.commit.assert_not_awaited()


def test_publishing_review_exposes_policy_rejection(monkeypatch):
    app = FastAPI()
    app.include_router(publishing.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    monkeypatch.setattr(publishing, "manually_approve",
                        AsyncMock(side_effect=ApprovalValidationError(PART_TIME_APPROVAL_MESSAGE)))
    with TestClient(app) as client:
        response = client.post("/api/publishing/review/41/approve", json={"reason": ""})
    assert response.status_code == 422
    assert response.json()["detail"] == PART_TIME_APPROVAL_MESSAGE


@pytest.mark.asyncio
async def test_selected_force_cannot_bypass_policy(monkeypatch):
    sc = row(international_fee=20000, ielts_overall=6, duration=2,
             intake_months=["September"], has_central_fee_page=False)
    result = Mock()
    result.scalar_one_or_none.return_value = sc
    db = SimpleNamespace(sync_session=Session(),
                         execute=AsyncMock(return_value=result),
                         get=AsyncMock(return_value=sc),
                         rollback=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(staged_selected_approval, "split_pending_course",
                        AsyncMock(return_value={"status": "unchanged", "courseIds": [41]}))
    monkeypatch.setattr("app.services.scraper.fee_selection.unresolved_fee_selection", lambda sc: False)
    try:
        response = await staged_selected_approval.approve_selected(
            staged_selected_approval.ApproveSelectedBody(courseIds=[41], force=True),
            db, {"email": "reviewer"},
        )
        assert response["approvedIds"] == []
        assert response["failed"][0]["error"] == PART_TIME_APPROVAL_MESSAGE
        db.commit.assert_not_awaited()
    finally:
        db.sync_session.close()
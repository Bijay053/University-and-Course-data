"""Only intentional validation guidance is exposed by the staged approval route."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.models import ScrapedCourse
from app.routers.scrape import router
from app.services.scraper.approve_course import ApprovalValidationError


@pytest.fixture
def approval_client():
    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    sc = ScrapedCourse(id=41, course_name="Example course", extraction_method={})
    db = SimpleNamespace(get=AsyncMock(return_value=sc), rollback=AsyncMock())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "reviewer", "permissions": ["staged.approve"],
    }
    with TestClient(app) as client:
        yield client, db, sc


def test_staged_approval_preserves_intentional_validation_message(
    approval_client, monkeypatch,
):
    client, db, _ = approval_client
    message = "Campus fee groups require explicit reviewer approval"
    approve = AsyncMock(side_effect=ApprovalValidationError(message))
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = client.post("/api/scrape/staged/41/approve", json={"force": True})

    assert response.status_code == 422
    assert response.json() == {"detail": message}
    approve.assert_awaited_once()
    assert approve.await_args.kwargs == {"actor": "reviewer"}
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_staged_approval_hides_unexpected_error_and_logs_traceback(
    approval_client, monkeypatch, caplog, error_type,
):
    client, db, _ = approval_client
    private_detail = "private-table private-bound-value"
    error = error_type(private_detail)
    approve = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = client.post("/api/scrape/staged/41/approve", json={"force": True})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Course publication failed; the row remains pending.",
    }
    assert private_detail not in response.text
    db.rollback.assert_awaited_once()
    approve.assert_awaited_once()
    record = next(
        r for r in caplog.records
        if r.name == "app.routers.scrape" and r.exc_info
    )
    assert "staged row 41" in record.message
    assert record.exc_info[1] is error
    assert private_detail in caplog.text


@pytest.mark.parametrize("phase", ["lookup", "fee_lock", "score"])
def test_staged_approval_precheck_failure_rolls_back_and_hides_details(
    approval_client, monkeypatch, caplog, phase,
):
    client, db, sc = approval_client
    error = RuntimeError("private-table private-bound-value")
    promote = AsyncMock()
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", promote,
    )
    if phase == "lookup":
        db.get.side_effect = error
    elif phase == "fee_lock":
        sc.extraction_method = {"fee_variants": {"status": "range"}}
        db.execute = AsyncMock(side_effect=error)
    else:
        def fail_score(_payload):
            raise error
        monkeypatch.setattr("app.services.scraper.confidence.score_payload", fail_score)

    response = client.post("/api/scrape/staged/41/approve")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Course approval could not be checked; the row remains pending.",
    }
    assert str(error) not in response.text
    db.rollback.assert_awaited_once()
    promote.assert_not_awaited()
    if phase == "fee_lock":
        db.execute.assert_awaited_once()
        statement = db.execute.await_args.args[0]
        assert statement._for_update_arg is not None
    record = next(
        r for r in caplog.records
        if r.name == "app.routers.scrape" and r.exc_info and "precheck" in r.message
    )
    assert "staged row 41" in record.message
    assert record.exc_info[1] is error
    assert str(error) in caplog.text


@pytest.mark.parametrize("phase", ["lookup", "fee_lock", "score", "promotion"])
def test_staged_approval_rollback_failure_preserves_original_error(
    approval_client, monkeypatch, caplog, phase,
):
    client, db, sc = approval_client
    original = RuntimeError("private-original-table private-original-value")
    rollback_error = RuntimeError("private-rollback-table private-rollback-value")
    promote = AsyncMock(side_effect=original)
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", promote,
    )
    if phase == "lookup":
        db.get.side_effect = original
    elif phase == "fee_lock":
        sc.extraction_method = {"fee_variants": {"status": "range"}}
        db.execute = AsyncMock(side_effect=original)
    elif phase == "score":
        def fail_score(_payload):
            raise original
        monkeypatch.setattr("app.services.scraper.confidence.score_payload", fail_score)

    async def fail_rollback():
        # The original traceback must already be recorded before cleanup starts.
        assert any(
            r.name == "app.routers.scrape" and r.exc_info
            and r.exc_info[1] is original
            for r in caplog.records
        )
        raise rollback_error

    db.rollback.side_effect = fail_rollback

    response = client.post("/api/scrape/staged/41/approve", json={"force": True})

    assert response.status_code == 500
    detail = (
        "Course publication failed; the row remains pending."
        if phase == "promotion"
        else "Course approval could not be checked; the row remains pending."
    )
    assert response.json() == {"detail": detail}
    assert str(original) not in response.text
    assert str(rollback_error) not in response.text
    db.rollback.assert_awaited_once()
    if phase == "promotion":
        promote.assert_awaited_once()
    else:
        promote.assert_not_awaited()
    records = [
        r for r in caplog.records
        if r.name == "app.routers.scrape" and r.exc_info
    ]
    assert len(records) == 2
    assert records[0].exc_info[1] is original
    assert records[1].exc_info[1] is rollback_error
    assert "rollback failed" in records[1].message
    for record in records:
        assert "staged row 41" in record.message
        assert record.exc_info[2] is not None
    assert str(original) in caplog.text
    assert str(rollback_error) in caplog.text


def test_staged_approval_missing_row_keeps_404_guidance(approval_client):
    client, db, _ = approval_client
    db.get.return_value = None

    response = client.post("/api/scrape/staged/41/approve")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    db.rollback.assert_not_awaited()


def test_staged_approval_unresolved_fee_keeps_422_guidance(
    approval_client, monkeypatch,
):
    client, db, sc = approval_client
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda row: True,
    )

    response = client.post("/api/scrape/staged/41/approve")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Select a current published fee option before approval",
    }
    db.rollback.assert_not_awaited()


def test_staged_approval_low_confidence_keeps_422_guidance(
    approval_client, monkeypatch,
):
    client, db, _ = approval_client
    monkeypatch.setattr(
        "app.services.scraper.confidence.score_payload",
        lambda payload: {"score": 40, "missing": ["international_fee"]},
    )

    response = client.post("/api/scrape/staged/41/approve")

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "confidence_too_low",
        "message": (
            "Cannot approve: confidence score 40/100 is below the 60-point "
            "minimum. Missing fields: international_fee. "
            "Fix the missing data in the edit panel before approving."
        ),
        "score": 40,
        "missing": ["international_fee"],
    }
    db.rollback.assert_not_awaited()
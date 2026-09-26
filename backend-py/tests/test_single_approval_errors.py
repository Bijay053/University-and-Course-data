"""Only intentional approval validation messages reach individual reviewers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.dependencies import get_current_user, get_db
from app.routers.reviews import router
from app.services.scraper.approve_course import ApprovalValidationError


@pytest.fixture
def approval_client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    sc = SimpleNamespace(
        id=41, university_id=1, status="pending", course_id=None,
        course_name="Example course", extraction_method={},
    )
    locked_row = Mock()
    locked_row.scalar_one.return_value = sc
    db = SimpleNamespace(
        get=AsyncMock(return_value=sc),
        execute=AsyncMock(return_value=locked_row),
        rollback=AsyncMock(),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"email": "reviewer"}
    with TestClient(app) as client:
        yield client, db, sc


def test_single_approval_preserves_intentional_validation_message(
    approval_client, monkeypatch,
):
    client, db, sc = approval_client
    sc.extraction_method = {"fee_variants": {"status": "unresolved"}}
    # Exercise the service's real validation boundary.
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda row: True,
    )

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Select a current published fee option before approval",
    }
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize("error", [
    ValueError("private-table private-bound-value"),
    RuntimeError("private-table private-bound-value"),
    IntegrityError(
        "INSERT INTO private_table VALUES (:payload)",
        {"payload": "private-bound-value"},
        Exception("private-driver-detail"),
    ),
])
def test_single_approval_hides_unexpected_error_but_logs_it_and_rolls_back(
    approval_client, monkeypatch, caplog, error,
):
    client, db, _ = approval_client
    approve = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 500
    assert response.json() == {
        "detail": (
            "This course could not be published because of an unexpected error. "
            "Please try again or contact support if the problem continues."
        ),
    }
    assert "private-bound-value" not in response.text
    assert "private-bound-value" in caplog.text
    assert "private-table" not in response.text
    assert "private_table" not in response.text
    approve.assert_awaited_once()
    assert approve.await_args.kwargs == {"actor": "reviewer"}
    db.rollback.assert_awaited_once()
    record = next(r for r in caplog.records if r.name == "app.routers.reviews")
    assert "staged row 41" in record.message
    assert record.exc_info[1] is error
    assert "Traceback (most recent call last)" in caplog.text


def test_failed_approval_leaves_session_reusable(approval_client, monkeypatch):
    client, db, _ = approval_client
    transaction_failed = False
    calls = 0

    async def get_row(model, sc_id):
        assert not transaction_failed, "session still has a failed transaction"
        assert sc_id == 41
        return db_row

    db_row = db.get.return_value
    db.get.side_effect = get_row

    async def approve(db_session, sc, *, actor):
        nonlocal transaction_failed, calls
        calls += 1
        assert not transaction_failed
        if calls == 1:
            transaction_failed = True
            raise IntegrityError(
                "INSERT INTO private_table VALUES (:payload)",
                {"payload": "private-bound-value"},
                Exception("private-driver-detail"),
            )
        return {"course_id": 77}

    async def rollback():
        nonlocal transaction_failed
        transaction_failed = False

    db.rollback.side_effect = rollback
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    first = client.post("/api/scraped-courses/41/approve")
    second = client.post("/api/scraped-courses/41/approve")

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json() == {"course_id": 77}
    db.rollback.assert_awaited_once()


def test_single_approval_logs_database_lookup_failure(approval_client, monkeypatch, caplog):
    client, db, _ = approval_client
    error = IntegrityError(
        "SELECT * FROM private_table", {"payload": "private-bound-value"},
        Exception("private-driver-detail"),
    )
    db.get.side_effect = error
    approve = AsyncMock()
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 500
    assert "private_table" not in response.text
    assert "private-bound-value" not in response.text
    db.rollback.assert_awaited_once()
    approve.assert_not_awaited()
    record = next(r for r in caplog.records if r.name == "app.routers.reviews")
    assert "staged row 41" in record.message
    assert record.exc_info[1] is error
    assert "private_table" in caplog.text


def test_single_approval_missing_row_stays_404(approval_client):
    client, db, _ = approval_client
    db.get.return_value = None

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    db.rollback.assert_not_awaited()


def test_explicit_validation_exception_still_returns_422(
    approval_client, monkeypatch,
):
    client, db, _ = approval_client
    message = "Campus fee groups require explicit reviewer approval"
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course",
        AsyncMock(side_effect=ApprovalValidationError(message)),
    )

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 422
    assert response.json() == {"detail": message}
    db.rollback.assert_not_awaited()

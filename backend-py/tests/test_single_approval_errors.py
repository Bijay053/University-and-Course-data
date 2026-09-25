"""Only intentional approval validation messages reach individual reviewers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.routers.reviews import router
from app.services.scraper.approve_course import ApprovalValidationError


@pytest.fixture
def approval_client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    sc = SimpleNamespace(id=41, course_name="Example course", extraction_method={})
    locked_row = Mock()
    locked_row.scalar_one.return_value = sc
    db = SimpleNamespace(
        get=AsyncMock(return_value=sc),
        execute=AsyncMock(return_value=locked_row),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"email": "reviewer"}
    with TestClient(app) as client:
        yield client, db, sc


def test_single_approval_preserves_intentional_validation_message(
    approval_client, monkeypatch,
):
    client, _, sc = approval_client
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


def test_single_approval_hides_unexpected_value_error_but_logs_it(
    approval_client, monkeypatch, caplog,
):
    client, _, _ = approval_client
    private_detail = "private-table private-bound-value"
    error = ValueError(private_detail)
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
    assert private_detail not in response.text
    approve.assert_awaited_once()
    assert approve.await_args.kwargs == {"actor": "reviewer"}
    record = next(r for r in caplog.records if r.name == "app.routers.reviews")
    assert "staged row 41" in record.message
    assert record.exc_info[1] is error
    assert private_detail in caplog.text


def test_explicit_validation_exception_still_returns_422(
    approval_client, monkeypatch,
):
    client, _, _ = approval_client
    message = "Campus fee groups require explicit reviewer approval"
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course",
        AsyncMock(side_effect=ApprovalValidationError(message)),
    )

    response = client.post("/api/scraped-courses/41/approve")

    assert response.status_code == 422
    assert response.json() == {"detail": message}
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
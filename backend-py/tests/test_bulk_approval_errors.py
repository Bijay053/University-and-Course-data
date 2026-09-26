"""Unexpected failures are diagnostic in logs, not in reviewer responses."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError

from app.dependencies import get_current_user, get_db
from app.routers.reviews import bulk_approve_scraped_courses


async def _post_bulk_approval(fastapi_app, db, monkeypatch):
    async def override_db():
        yield db

    async def override_user():
        return {"email": "reviewer"}

    monkeypatch.setitem(fastapi_app.dependency_overrides, get_db, override_db)
    monkeypatch.setitem(fastapi_app.dependency_overrides, get_current_user, override_user)
    transport = ASGITransport(app=fastapi_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/api/scraped-courses/bulk-approve",
            params={
                "university_id": 7,
                "status": "pending",
                "auto_publish_status": "ready",
                "dry_run": "false",
                "limit": 10,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [
    IntegrityError, OperationalError, StatementError,
    RuntimeError, ValueError, TypeError, KeyError,
])
async def test_individual_publication_failure_http_response_is_private(
    fastapi_app, error_type, monkeypatch,
):
    is_database_error = error_type in (IntegrityError, OperationalError, StatementError)
    error = error_type(
        statement="INSERT INTO private_table VALUES (:payload)",
        params={"payload": "private-bound-value"},
        orig=Exception("private-driver-detail"),
        **({"message": "private-driver-detail"} if error_type is StatementError else {}),
    ) if is_database_error else error_type(
        "INSERT private_table payload private-bound-value private-driver-detail"
    )
    rows = [SimpleNamespace(id=41), SimpleNamespace(id=42)]
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(side_effect=rows),
        rollback=AsyncMock(),
    )

    async def publish(db_arg, row, *, actor):
        assert db_arg is db
        assert actor == "reviewer"
        if row.id == 41:
            raise error
        # The failed transaction must be cleared before the later row runs.
        db.rollback.assert_awaited_once()
        assert row.id == 42
        return {"course_id": 100}

    approve = AsyncMock(side_effect=publish)
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = await _post_bulk_approval(fastapi_app, db, monkeypatch)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "ok": True,
        "university_id": 7,
        "status_filter": "pending",
        "auto_publish_status_filter": "ready",
        "approved": 1,
        "failed": 1,
        "failures": [{
            "scraped_course_id": 41,
            "error": (
                f"This course could not be published because of {'a database' if is_database_error else 'an unexpected'} error. "
                "Please try again or contact support if the problem continues."
            ),
        }],
    }
    for private_detail in (
        "INSERT", "private_table", "payload", "private-bound-value",
        "private-driver-detail", error_type.__name__, "Traceback",
    ):
        assert private_detail not in response.text
    db.rollback.assert_awaited_once()
    assert [call.args[1] for call in db.get.await_args_list] == [41, 42]
    assert [call.args[1].id for call in approve.await_args_list] == [41, 42]


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved,message", [
    (True, "Select a current published fee option before approval"),
    (False, "Resolve the published fee options before approving this course"),
])
async def test_real_fee_validation_http_response_remains_actionable(
    fastapi_app, unresolved, message, monkeypatch,
):
    sc = SimpleNamespace(
        id=41, university_id=7, course_name="Example course",
        status="pending", course_id=None,
        extraction_method={"fee_variants": {"status": "unresolved"}},
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [sc]
    result.scalar_one.return_value = sc
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(return_value=sc),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    # Keep the real publication service and its validation exception boundary.
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda row: unresolved,
    )
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.fee_selection", lambda row: None,
    )

    response = await _post_bulk_approval(fastapi_app, db, monkeypatch)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "ok": True,
        "university_id": 7,
        "status_filter": "pending",
        "auto_publish_status_filter": "ready",
        "approved": 0,
        "failed": 1,
        "failures": [{"scraped_course_id": 41, "error": message}],
    }
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True], ids=["approval", "dry-run"])
@pytest.mark.parametrize("error_type", [OperationalError, RuntimeError])
async def test_candidate_query_failure_http_response_is_private(
    fastapi_app, dry_run, error_type, monkeypatch,
):
    """Exercise the real app's routing, middleware and exception serialization."""
    error = (
        OperationalError(
            statement="SELECT private_table WHERE payload=:payload",
            params={"payload": "private-bound-value"},
            orig=Exception("private-driver-detail"),
        )
        if error_type is OperationalError
        else RuntimeError(
            "SELECT private_table payload private-bound-value private-driver-detail"
        )
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=error),
        get=AsyncMock(),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    approve = AsyncMock()
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    async def override_db():
        yield db

    async def override_user():
        return {"email": "reviewer"}

    # Restore existing overrides even if the request or an assertion fails.
    monkeypatch.setitem(fastapi_app.dependency_overrides, get_db, override_db)
    monkeypatch.setitem(fastapi_app.dependency_overrides, get_current_user, override_user)
    transport = ASGITransport(app=fastapi_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/scraped-courses/bulk-approve",
            params={
                "university_id": 7,
                "status": "pending",
                "auto_publish_status": "ready",
                "dry_run": str(dry_run).lower(),
                "limit": 10,
            },
        )

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "detail": (
            "The review batch could not be loaded because of an unexpected error. "
            "Please try again or contact support if the problem continues."
        ),
    }
    for private_detail in (
        "SELECT", "private_table", "payload", "private-bound-value", "private-driver-detail",
    ):
        assert private_detail not in response.text
    db.execute.assert_awaited_once()
    db.rollback.assert_awaited_once()
    db.get.assert_not_awaited()
    db.commit.assert_not_awaited()
    approve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("error_type", [OperationalError, RuntimeError])
async def test_candidate_query_failure_is_sanitized_and_rolls_back(
    dry_run, error_type, monkeypatch, caplog,
):
    if error_type is OperationalError:
        error = OperationalError(
            statement="SELECT private_table WHERE payload=:payload",
            params={"payload": "private-bound-value"},
            orig=Exception("private-driver-detail"),
        )
    else:
        error = RuntimeError("SELECT private_table payload private-bound-value private-driver-detail")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=error),
        get=AsyncMock(),
        rollback=AsyncMock(),
    )
    approve = AsyncMock()
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    with pytest.raises(HTTPException) as raised:
        await bulk_approve_scraped_courses(
            db, {"email": "reviewer"}, 7, "pending", "ready", dry_run, 10,
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == (
        "The review batch could not be loaded because of an unexpected error. "
        "Please try again or contact support if the problem continues."
    )
    for private_detail in ("SELECT", "private_table", "payload", "private-bound-value", "private-driver-detail"):
        assert private_detail not in json.dumps({"detail": raised.value.detail})
        assert private_detail in caplog.text
    db.rollback.assert_awaited_once()
    db.get.assert_not_awaited()
    approve.assert_not_awaited()
    record = next(r for r in caplog.records if r.name == "app.routers.reviews")
    assert "failed to load candidates uni=7" in record.message
    assert record.exc_info[1] is error


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [
    IntegrityError, OperationalError, StatementError,
    RuntimeError, ValueError, TypeError, KeyError,
])
async def test_database_errors_are_safe_and_batch_continues(error_type, monkeypatch, caplog):
    is_database_error = error_type in (IntegrityError, OperationalError, StatementError)
    error = error_type(
        statement="INSERT INTO private_table VALUES (:payload)",
        params={"payload": "private-bound-value"},
        orig=Exception("private-driver-detail"),
        **({"message": "private-driver-detail"} if error_type is StatementError else {}),
    ) if is_database_error else error_type(
        "INSERT private_table payload private-bound-value private-driver-detail"
    )
    rows = [SimpleNamespace(id=41), SimpleNamespace(id=42)]
    result = Mock()
    result.scalars.return_value.all.return_value = rows
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(side_effect=rows),
        rollback=AsyncMock(),
    )
    approve = AsyncMock(side_effect=[error, {"course_id": 100}])
    monkeypatch.setattr(
        "app.services.scraper.approve_course.approve_scraped_course", approve,
    )

    response = await bulk_approve_scraped_courses(
        db, {"email": "reviewer"}, 7, "pending", "ready", False, 10,
    )

    assert response["approved"] == response["failed"] == 1
    assert response["failures"] == [{
        "scraped_course_id": 41,
        "error": (
            f"This course could not be published because of {'a database' if is_database_error else 'an unexpected'} error. "
            "Please try again or contact support if the problem continues."
        ),
    }]
    for private_detail in ("INSERT", "private_table", "payload", "private-bound-value", "private-driver-detail"):
        assert private_detail not in json.dumps(response)
        assert private_detail in caplog.text
    db.rollback.assert_awaited_once()
    assert approve.await_count == 2
    assert approve.await_args.args[1].id == 42
    record = next(r for r in caplog.records if r.name == "app.routers.reviews")
    assert "sc_id=41 uni=7" in record.message
    assert record.exc_info[1] is error


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved,message", [
    (True, "Select a current published fee option before approval"),
    (False, "Resolve the published fee options before approving this course"),
])
async def test_real_fee_validation_messages_remain_actionable(unresolved, message, monkeypatch):
    sc = SimpleNamespace(
        id=41, university_id=7, course_name="Example course",
        status="pending", course_id=None,
        extraction_method={"fee_variants": {"status": "unresolved"}},
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [sc]
    result.scalar_one.return_value = sc
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(return_value=sc),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    # Exercise the real approval service's rejection types, not a mocked error.
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda row: unresolved,
    )
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.fee_selection", lambda row: None,
    )

    response = await bulk_approve_scraped_courses(
        db, {"email": "reviewer"}, 7, "pending", "ready", False, 10,
    )

    assert response["approved"] == 0
    assert response["failed"] == 1
    assert response["failures"] == [{"scraped_course_id": 41, "error": message}]
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()

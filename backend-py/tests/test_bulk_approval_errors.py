"""Database failures are diagnostic in logs, not in reviewer responses."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError

from app.routers.reviews import bulk_approve_scraped_courses


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [IntegrityError, OperationalError, StatementError])
async def test_database_errors_are_safe_and_batch_continues(error_type, monkeypatch, caplog):
    error = error_type(
        statement="INSERT INTO private_table VALUES (:payload)",
        params={"payload": "private-bound-value"},
        orig=Exception("private-driver-detail"),
        **({"message": "private-driver-detail"} if error_type is StatementError else {}),
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
            "This course could not be published because of a database error. "
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
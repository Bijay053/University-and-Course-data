"""Read-only reporting catalog contract and sanitized failure handling."""
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError

from app.services import reporting_schema as schema
from app.models import CourseErrorReport


def test_reporting_contract_matches_model():
    assert set(schema.REQUIRED_COLUMNS) == set(CourseErrorReport.__table__.columns.keys())
    for column in CourseErrorReport.__table__.columns:
        assert schema.REQUIRED_COLUMNS[column.name][1] == column.nullable


@pytest.mark.asyncio
async def test_reporting_catalog_query_is_read_only_and_search_path_aware():
    rows = [
        {"column_name": name, "column_type": kind, "nullable": nullable}
        for name, (kind, nullable) in schema.REQUIRED_COLUMNS.items()
    ]
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    db = Mock(execute=AsyncMock(return_value=result))
    await schema.require_reporting_schema(db)
    db.execute.assert_awaited_once()
    sql = str(db.execute.call_args.args[0]).strip()
    assert sql.startswith("SELECT")
    assert "to_regclass('course_error_reports')" in sql
    assert "alembic_version" not in sql


@pytest.mark.asyncio
async def test_reporting_connection_failure_is_sanitized():
    db = Mock(execute=AsyncMock(side_effect=OperationalError(
        "private-dsn", {}, RuntimeError("private-password"),
    )))
    with pytest.raises(schema.ReportingPrerequisiteError) as caught:
        await schema.require_reporting_schema(db)
    assert "private" not in str(caught.value)

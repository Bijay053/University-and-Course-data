"""Read-only admission checks: no database, Redis, or broker required."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.services import worker_fencing_schema as schema


def database(columns=None):
    rows = [
        {"relation_name": table, "column_name": column, "column_type": kind}
        for table, fields in (schema.REQUIRED_COLUMNS if columns is None else columns).items()
        for column, kind in fields.items()
    ]
    result = Mock()
    result.mappings.return_value.all.return_value = rows
    return SimpleNamespace(execute=AsyncMock(return_value=result), rollback=AsyncMock())


@pytest.mark.asyncio
async def test_schema_check_accepts_required_columns_without_migration_version_or_writes():
    db = database()
    await schema.require_worker_fencing_schema(db)
    statement = str(db.execute.call_args.args[0])
    assert statement.strip().startswith("SELECT")
    assert "alembic_version" not in statement
    assert not any(word in statement.upper().split() for word in ("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE"))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["tables", "lineage", "budgets"])
async def test_schema_check_refuses_missing_prerequisites(missing):
    columns = {table: dict(fields) for table, fields in schema.REQUIRED_COLUMNS.items()}
    if missing == "tables":
        columns = {}
    elif missing == "lineage":
        columns["autonomous_worker_claims"].pop("revoked_generations")
    else:
        columns.pop("autonomous_worker_budgets")
    with pytest.raises(schema.WorkerFencingSchemaMissing, match="382_worker_claims"):
        await schema.require_worker_fencing_schema(database(columns))


@pytest.mark.asyncio
async def test_schema_check_redacts_connection_errors():
    db = database()
    db.execute.side_effect = OperationalError("secret-dsn", {}, RuntimeError("private password"))
    with pytest.raises(schema.WorkerFencingPrerequisiteError) as caught:
        await schema.require_worker_fencing_schema(db)
    assert "private" not in str(caught.value)
    assert "secret-dsn" not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_schema_rejects_api_before_lease_or_publication(monkeypatch):
    from app.routers.scrape import start_ai_repair
    from app.services.scraper import ai_repair_agent
    from app.tasks import auto_repair_task
    lease = Mock()
    publish = Mock()
    monkeypatch.setattr(ai_repair_agent, "acquire_repair_lease", lease)
    monkeypatch.setattr(auto_repair_task, "dispatch_ai_repair", publish)
    db = database({})
    with pytest.raises(HTTPException) as caught:
        await start_ai_repair("parent", db, {})
    assert caught.value.status_code == 503
    assert "worker-fencing database schema" in caught.value.detail
    assert "383_claim_lock_lineage" in caught.value.detail
    assert db.execute.call_count == 1  # Catalog SELECT only: no queued audit.
    lease.assert_not_called()
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_delivered_task_classifies_missing_schema_without_entering_claim(monkeypatch):
    from app import database as db_module
    from app.services import ai_repair_workflow as workflow
    from app.services.scraper import ai_repair_agent
    from app.tasks.auto_repair_task import _run_autonomous_repair
    db = database({})
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock()
    monkeypatch.setattr(db_module, "AsyncSessionLocal", Mock(return_value=context))
    monkeypatch.setattr(ai_repair_agent, "ensure_ai_repair_audit_schema", AsyncMock())
    monkeypatch.setattr(workflow, "load", AsyncMock(return_value={"autonomous": {"enabled": True}}))
    blocked = AsyncMock(return_value={"status": "failed"})
    run = AsyncMock()
    monkeypatch.setattr(workflow, "block_unclaimed_initialization", blocked)
    monkeypatch.setattr(workflow, "run", run)
    assert await _run_autonomous_repair("parent", 7, "session", "delivery") == {"status": "failed"}
    assert blocked.call_args.kwargs["code"] == "worker_fencing_schema_missing"
    db.rollback.assert_awaited_once()
    run.assert_not_called()
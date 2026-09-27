"""Real migration/readiness and two independent PostgreSQL-session race."""
import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import ScrapedCourse
from app.schema_readiness import require_qualification_guidance_schema, ERROR
from app.services.scraper.approval_guidance import attempt_identity, persist_failure


@pytest.mark.asyncio
async def test_actual_migration_old_schema_gate_and_two_session_overlap():
    from app.database import engine as configured_engine
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    schema = "guidance_test_" + uuid4().hex
    engine = create_async_engine(configured_engine.url, poolclass=NullPool)
    scoped = create_async_engine(configured_engine.url, poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema + ",public"}})
    spec = spec_from_file_location("migration390",
        Path(__file__).resolve().parents[1] / "alembic/versions/390_qualification_approval_guidance.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(
                f'CREATE TABLE "{schema}".scraped_courses (LIKE public.scraped_courses INCLUDING DEFAULTS)'))
            await connection.execute(text(
                f'ALTER TABLE "{schema}".scraped_courses DROP COLUMN IF EXISTS last_qualification_approval'))
        async with scoped.begin() as connection:
            with pytest.raises(RuntimeError, match="apply migration 390"):
                await require_qualification_guidance_schema(connection)
            # Actual FastAPI lifespan must fail before other startup work.
            import app.schema_readiness as readiness
            from app.main import lifespan, app
            original_check = readiness.check_worker_schema
            async def scoped_startup_check():
                async with scoped.connect() as startup_connection:
                    await require_qualification_guidance_schema(startup_connection)
            readiness.check_worker_schema = scoped_startup_check
            try:
                with pytest.raises(RuntimeError, match="apply migration 390"):
                    async with lifespan(app):
                        pytest.fail("Old schema must not accept traffic")
            finally:
                readiness.check_worker_schema = original_check

            def upgrade(sync):
                migration.op = Operations(MigrationContext.configure(sync))
                migration.upgrade()
            await connection.run_sync(upgrade)
            await require_qualification_guidance_schema(connection)
        async with AsyncSession(scoped, expire_on_commit=False) as failure_session, \
                   AsyncSession(scoped, expire_on_commit=False) as edit_session:
            row = ScrapedCourse(id=765, university_id=1, scrape_job_id="isolated-job",
                                course_name="Schema race test", status="pending")
            failure_session.add(row)
            await failure_session.commit()
            identity = attempt_identity(row)
            await failure_session.rollback()
            await edit_session.execute(text(
                "UPDATE scraped_courses SET notes='concurrent committed edit' WHERE id=765"))
            # Editor now holds the tuple lock. The real durable writer must
            # block, then re-read post-commit evidence, not its old identity map.
            writer = asyncio.create_task(persist_failure(failure_session, identity, "changed_cohort"))
            await asyncio.sleep(0.15)
            assert not writer.done()
            await edit_session.commit()
            await asyncio.wait_for(writer, 5)
            await failure_session.refresh(row)
            assert row.notes == "concurrent committed edit"
            assert row.last_qualification_approval is None
            # Diagnostic-only writes remain allowed by the database trigger.
            await persist_failure(failure_session, attempt_identity(row), "unverified_page")
            await failure_session.refresh(row)
            assert row.last_qualification_approval["reasonCode"] == "unverified_page"
    finally:
        await scoped.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def test_celery_bootstep_fails_closed_on_missing_schema(monkeypatch):
    import app.schema_readiness as readiness
    from app.tasks.celery_app import QualificationSchemaReadiness, celery_app
    async def missing():
        raise RuntimeError(ERROR)
    monkeypatch.setattr(readiness, "check_worker_schema", missing)
    assert QualificationSchemaReadiness in celery_app.steps["worker"]
    with pytest.raises(RuntimeError, match="apply migration 390"):
        QualificationSchemaReadiness.start(None, None)
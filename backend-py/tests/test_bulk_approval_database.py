"""Real publication rollback through HTTP, using a disposable local PostgreSQL.

Run: python -m pytest tests/test_bulk_approval_database.py -q
No application database URL, production data, or mocked publication/session.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import Base
from app.models import (
    AcademicRequirement, Course, EnglishRequirement, Fee, Intake,
    ScrapedCourse, University,
)
from app.models.sub_category import CourseSubCategory
from tests.test_bulk_approval_errors import _post_bulk_approval


@pytest.fixture
def approval_postgres():
    for binary in ("initdb", "pg_ctl"):
        if not shutil.which(binary):
            pytest.fail(f"{binary} is required for isolated publication database tests")
    # A private Unix socket avoids TCP port races and cannot reach a shared DB.
    with tempfile.TemporaryDirectory(prefix="approval-pg-") as directory:
        root = Path(directory)
        data = root / "data"
        subprocess.run(
            ["initdb", "-D", str(data), "-A", "trust", "-U", "approval_test"],
            check=True, capture_output=True, timeout=60,
        )
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                 "-o", f"-h '' -k {root}", "-w", "start"],
                check=True, capture_output=True, timeout=60,
            )
            yield {
                "host": str(root), "user": "approval_test", "database": "postgres",
            }
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run(
                    ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                    check=True, capture_output=True, timeout=60,
                )
            assert not (data / "postmaster.pid").exists()


@pytest_asyncio.fixture
async def approval_database(approval_postgres):
    engine = create_async_engine(
        "postgresql+asyncpg://", connect_args=approval_postgres, poolclass=NullPool,
    )
    try:
        tables = {
            model.__table__ for model in (
                University, ScrapedCourse, Course, EnglishRequirement,
                Fee, Intake, AcademicRequirement,
                CourseSubCategory,
            )
        }
        while True:
            expanded = tables | {
                fk.column.table for table in tables for fk in table.foreign_keys
            }
            if expanded == tables:
                break
            tables = expanded
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(sync, tables=list(tables))
            )
            # Only publication is constrained: staging both rows remains valid.
            await connection.execute(text(
                "ALTER TABLE fees ADD CONSTRAINT private_publication_guard "
                "CHECK (international_fee <> 12345)"
            ))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bulk_approval_real_constraint_rollback_continues(
    fastapi_app, approval_database, monkeypatch, caplog,
):
    async with approval_database() as seed:
        seed.add(University(id=7, name="Test university", country="Test", city="Test"))
        await seed.flush()
        seed.add_all([
            ScrapedCourse(
                id=row_id, university_id=7, scrape_job_id="isolated-approval",
                course_name=name, status="pending", auto_publish_status="ready",
                international_fee=fee, fee_term="Annual", fee_year=2026,
                currency="AUD", ielts_overall=6.5, intake_months=["March"],
            )
            for row_id, name, fee in (
                (41, "private-bound-course", 12345),
                (42, "Successful course", 23456),
            )
        ])
        await seed.commit()

    async with approval_database() as db:
        loaded = {}
        rollbacks = []
        successful_writes = []
        writes_before_failure = []

        def remember_row(session, row):
            if isinstance(row, ScrapedCourse):
                loaded[inspect(row).identity[0]] = row

        def record_rollback(session, previous_transaction):
            # inspect() does not lazy-load: capture actual ORM expiration.
            rollbacks.append({
                "active": session.is_active,
                "expired": {
                    key: set(inspect(row).expired_attributes)
                    for key, row in loaded.items()
                },
            })

        def record_write(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO "):
                successful_writes.append(statement)

        def record_error(context):
            if "INSERT INTO fees" in context.statement:
                writes_before_failure.extend(successful_writes)

        event.listen(db.sync_session, "loaded_as_persistent", remember_row)
        event.listen(db.sync_session, "after_soft_rollback", record_rollback)
        event.listen(db.bind.sync_engine, "after_cursor_execute", record_write)
        event.listen(db.bind.sync_engine, "handle_error", record_error)
        try:
            response = await _post_bulk_approval(fastapi_app, db, monkeypatch)
        finally:
            event.remove(db.bind.sync_engine, "after_cursor_execute", record_write)
            event.remove(db.bind.sync_engine, "handle_error", record_error)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {
            "ok": True, "university_id": 7, "status_filter": "pending",
            "auto_publish_status_filter": "ready", "approved": 1, "failed": 1,
            "failures": [{
                "scraped_course_id": 41,
                "error": (
                    "This course could not be published because of a database error. "
                    "Please try again or contact support if the problem continues."
                ),
            }],
        }
        # Both candidate rows, including their IDs, really expired on rollback.
        assert any(
            rollback["active"]
            and all(
                {"id", "course_name"} <= rollback["expired"].get(key, set())
                for key in (41, 42)
            )
            for rollback in rollbacks
        )
        assert db.is_active
        # The failing fee came after successful parent and detail INSERTs,
        # not at the first publication write.
        assert any("INSERT INTO courses" in sql for sql in writes_before_failure)
        for table in ("english_requirements", "intakes"):
            assert any(f"INSERT INTO {table}" in sql for sql in writes_before_failure)

    # A separate connection proves the second publication committed rather than
    # merely surviving in the request session's identity map.
    async with approval_database() as observer:
        courses = (await observer.execute(select(Course))).scalars().all()
        assert len(courses) == 1
        assert courses[0].name == "Successful course"
        assert (await observer.execute(select(Fee.course_id, Fee.international_fee))).all() == [
            (courses[0].id, 23456)
        ]
        assert (await observer.execute(select(Intake.course_id, Intake.intake_month))).all() == [
            (courses[0].id, "March")
        ]
        assert (await observer.execute(
            select(EnglishRequirement.course_id, EnglishRequirement.test_type, EnglishRequirement.overall)
        )).all() == [(courses[0].id, "ielts", 6.5)]
        failed = await observer.get(ScrapedCourse, 41)
        succeeded = await observer.get(ScrapedCourse, 42)
        assert failed.status == "pending"
        assert failed.auto_publish_status == "ready"
        assert failed.course_id is None
        assert failed.international_fee == 12345
        assert failed.ielts_overall == 6.5
        assert failed.intake_months == ["March"]
        assert succeeded.status == "approved"
        assert succeeded.course_id == courses[0].id

    record = next(
        record for record in caplog.records
        if record.name == "app.routers.reviews" and record.exc_info
    )
    assert isinstance(record.exc_info[1], IntegrityError)
    for private_detail in (
        "private_publication_guard", "12345", "INSERT INTO fees",
    ):
        assert private_detail in str(record.exc_info[1])
        assert private_detail not in response.text
    for private_detail in ("IntegrityError", "asyncpg", "Traceback", "parameters"):
        assert private_detail not in response.text
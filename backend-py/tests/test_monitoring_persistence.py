"""Monitoring commits survive task-session close in a private PostgreSQL cluster.

No application database URL, live watcher, HTTP request, or broker is used.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import Base
from app.models.university import University
from app.models.university_watcher import UniversityWatcher
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services import monitoring_engine as monitoring


@pytest.fixture(scope="module")
def monitoring_postgres():
    for binary in ("initdb", "pg_ctl"):
        if not shutil.which(binary):
            pytest.fail(f"{binary} is required for disposable monitoring tests")
    with tempfile.TemporaryDirectory(prefix="monitoring-pg-") as directory:
        root = Path(directory)
        data = root / "data"
        subprocess.run(
            ["initdb", "-D", str(data), "-A", "trust", "-U", "monitor_test"],
            check=True, capture_output=True, timeout=60,
        )
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                 "-o", f"-h '' -k {root}", "-w", "start"],
                check=True, capture_output=True, timeout=60,
            )
            yield {"host": str(root), "user": "monitor_test", "database": "postgres"}
        finally:
            if (data / "postmaster.pid").exists():
                subprocess.run(
                    ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                    check=True, capture_output=True, timeout=60,
                )
            assert not (data / "postmaster.pid").exists()


@pytest_asyncio.fixture
async def monitoring_database(monitoring_postgres, monkeypatch):
    engine = create_async_engine(
        "postgresql+asyncpg://", connect_args=monitoring_postgres, poolclass=NullPool,
    )
    # Replace the dispatch module itself: no Celery/Redis initialization is needed.
    dispatch = SimpleNamespace(
        scrape_university=SimpleNamespace(delay=Mock()),
        set_initial_dispatch_lock=Mock(),
    )
    monkeypatch.setitem(sys.modules, "app.tasks.scrape_tasks", dispatch)
    monkeypatch.setattr(monitoring, "run_probe", AsyncMock())
    tables = {University.__table__, UniversityWatcher.__table__, ScrapeRuntimeJob.__table__}
    while True:
        expanded = tables | {fk.column.table for table in tables for fk in table.foreign_keys}
        if expanded == tables:
            break
        tables = expanded
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=list(tables)))
        yield async_sessionmaker(engine, expire_on_commit=False), dispatch
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=list(tables)))
        await engine.dispose()


async def seed_watchers(factory, *fingerprints):
    due = datetime.now(timezone.utc) - timedelta(hours=1)
    async with factory() as db:
        for uid, etag in enumerate(fingerprints, 1):
            db.add(University(id=uid, name=f"Private university {uid}", country="Test", city="Test"))
        await db.flush()
        for uid, etag in enumerate(fingerprints, 1):
            db.add(UniversityWatcher(
                id=uid, university_id=uid, probe_url=f"https://private-{uid}.invalid",
                etag=etag, next_check_at=due + timedelta(seconds=uid),
                total_checks=3, consecutive_unchanged=2,
            ))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unchanged", "baseline", "error"])
async def test_probe_state_survives_session_close_without_scrape(
    monitoring_database, outcome,
):
    factory, dispatch = monitoring_database
    baseline = None if outcome == "baseline" else "same"
    await seed_watchers(factory, baseline)
    probe = (
        {"status_code": None, "error": "private timeout"}
        if outcome == "error"
        else {"status_code": 200, "etag": "same", "page_hash": "page", "sitemap_hash": "map"}
    )
    monitoring.run_probe.return_value = probe
    before = datetime.now(timezone.utc)
    async with factory() as task_session:
        result = await monitoring.run_monitoring_cycle(task_session)
    # A fresh session, not an identity-map read, is the persistence acceptance.
    async with factory() as verify:
        watcher = await verify.get(UniversityWatcher, 1)
        assert watcher.total_checks == 4
        assert watcher.last_checked_at >= before
        assert watcher.next_check_at > before + timedelta(days=89)
        assert watcher.last_probe_result == ("error" if outcome == "error" else "unchanged")
        assert watcher.last_probe_status_code == probe["status_code"]
        assert watcher.last_probe_error == probe.get("error")
        assert watcher.consecutive_unchanged == (2 if outcome == "error" else 3)
        assert watcher.etag == (baseline if outcome == "error" else "same")
        assert watcher.page_hash == (None if outcome == "error" else "page")
        assert watcher.sitemap_hash == (None if outcome == "error" else "map")
        assert watcher.total_changes_detected == 0
        assert watcher.total_scrapes_triggered == 0
        assert list((await verify.scalars(select(ScrapeRuntimeJob))).all()) == []
    assert result == {"checked": 1, "changed": 0, "triggered": 0, "errors": int(outcome == "error")}
    dispatch.scrape_university.delay.assert_not_called()
    dispatch.set_initial_dispatch_lock.assert_not_called()
    # The very next cycle must not repeat an already rescheduled probe.
    monitoring.run_probe.reset_mock()
    async with factory() as task_session:
        assert await monitoring.run_monitoring_cycle(task_session) == {
            "checked": 0, "changed": 0, "triggered": 0, "errors": 0,
        }
    monitoring.run_probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_job,dispatch_fails", [(False, False), (True, False), (False, True)])
async def test_changed_commit_does_not_lose_trailing_checks(
    monitoring_database, active_job, dispatch_fails,
):
    factory, dispatch = monitoring_database
    await seed_watchers(factory, "old", "same", "same")
    if active_job:
        async with factory() as db:
            db.add(ScrapeRuntimeJob(
                runtime_job_id="private-active", university_id=1,
                job_type="single", status="running",
            ))
            await db.commit()
    monitoring.run_probe.side_effect = [
        {"status_code": 200, "etag": "new"},
        {"status_code": 200, "etag": "same"},
        {"status_code": None, "error": "private timeout"},
    ]
    operations = []
    async with factory() as task_session:
        def record_sql(conn, cursor, statement, parameters, context, executemany):
            if "pg_advisory_xact_lock" in statement:
                operations.append("lock")

        event.listen(task_session.bind.sync_engine, "before_cursor_execute", record_sql)
        event.listen(task_session.sync_session, "after_commit", lambda session: operations.append("commit"))

        def delay(job_id):
            operations.append("dispatch")
            assert operations[:2] == ["lock", "commit"]
            if dispatch_fails:
                raise RuntimeError("private broker failure")

        dispatch.scrape_university.delay.side_effect = delay
        try:
            result = await monitoring.run_monitoring_cycle(task_session)
        finally:
            event.remove(task_session.bind.sync_engine, "before_cursor_execute", record_sql)
    assert result == {"checked": 3, "changed": 1, "triggered": 1, "errors": 1}
    async with factory() as verify:
        watchers = list((await verify.scalars(select(UniversityWatcher).order_by(UniversityWatcher.id))).all())
        assert [w.total_checks for w in watchers] == [4, 4, 4]
        assert [w.last_probe_result for w in watchers] == ["changed", "unchanged", "error"]
        assert all(w.next_check_at > datetime.now(timezone.utc) for w in watchers)
        assert watchers[0].etag == "new"
        assert watchers[0].total_changes_detected == 1
        assert watchers[1].consecutive_unchanged == 3
        assert watchers[2].last_probe_error == "private timeout"
        jobs = list((await verify.scalars(select(ScrapeRuntimeJob))).all())
        assert len(jobs) == 1
        if active_job:
            assert jobs[0].runtime_job_id == "private-active"
            assert watchers[0].total_scrapes_triggered == 0
            dispatch.scrape_university.delay.assert_not_called()
            dispatch.set_initial_dispatch_lock.assert_not_called()
        else:
            assert jobs[0].status == "queued"
            assert jobs[0].request_payload["triggeredBy"] == "monitor"
            assert watchers[0].last_scrape_job_id == jobs[0].runtime_job_id
            assert watchers[0].total_scrapes_triggered == 1
            dispatch.scrape_university.delay.assert_called_once_with(jobs[0].runtime_job_id)
            if dispatch_fails:
                dispatch.set_initial_dispatch_lock.assert_not_called()
            else:
                dispatch.set_initial_dispatch_lock.assert_called_once_with(jobs[0].runtime_job_id)
        # A separate connection can acquire the transaction-scoped start lock.
        assert await verify.scalar(text("SELECT pg_try_advisory_xact_lock(1)")) is True


@pytest.mark.asyncio
async def test_unexpected_probe_failure_does_not_stop_other_watchers(monitoring_database):
    factory, dispatch = monitoring_database
    await seed_watchers(factory, "same", "same")
    monitoring.run_probe.side_effect = [
        RuntimeError("private unexpected failure"), {"status_code": 200, "etag": "same"},
    ]
    async with factory() as task_session:
        result = await monitoring.run_monitoring_cycle(task_session)
    assert result == {"checked": 1, "changed": 0, "triggered": 0, "errors": 1}
    async with factory() as verify:
        assert (await verify.get(UniversityWatcher, 1)).total_checks == 3
        assert (await verify.get(UniversityWatcher, 2)).total_checks == 4
    dispatch.scrape_university.delay.assert_not_called()


@pytest.mark.asyncio
async def test_cycle_commit_failure_is_not_reported_as_success(monitoring_database, monkeypatch):
    factory, _ = monitoring_database
    await seed_watchers(factory, "same")
    monitoring.run_probe.return_value = {"status_code": 200, "etag": "same"}
    async with factory() as task_session:
        monkeypatch.setattr(task_session, "commit", AsyncMock(side_effect=RuntimeError("private commit failure")))
        with pytest.raises(RuntimeError, match="private commit failure"):
            await monitoring.run_monitoring_cycle(task_session)

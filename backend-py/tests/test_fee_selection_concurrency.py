"""Real PostgreSQL contention; all committed records live in a disposable schema.

Run on a development/test database:
ALLOW_ISOLATED_FEE_SELECTION_TESTS=1 python -m pytest \
    tests/test_fee_selection_concurrency.py -q
"""
import asyncio
import copy
import json
import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, postgres_tls_connect_args
from app.dependencies import get_current_user, get_db
from app.models import Course, CourseAuditLog, ScrapedCourse, ScrapedFieldEvidence, University
from app.routers.scrape import router
from app.services.scraper.fee_selection import FIELDS, fee_selection
from tests.test_fee_selection import METHOD, URL, course


@pytest_asyncio.fixture
async def isolated_fee_database():
    if os.environ.get("ALLOW_ISOLATED_FEE_SELECTION_TESTS") != "1":
        pytest.skip("Enable only against a development/test PostgreSQL database")
    schema = "test_fee_selection_" + uuid.uuid4().hex
    admin = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args=postgres_tls_connect_args(),
    )
    # No public search-path fallback: even an accidental query cannot touch
    # application tables. Each connection and pool belongs solely to this test.
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool,
        connect_args={
            **postgres_tls_connect_args(),
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "15000",
                "lock_timeout": "10000",
            },
        },
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        tables = {
            model.__table__ for model in
            (University, Course, ScrapedCourse, ScrapedFieldEvidence, CourseAuditLog)
        }
        # Include FK dependencies without creating unrelated application tables.
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
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                assert (await connection.execute(text(
                    "SELECT count(*) FROM pg_namespace WHERE nspname = :schema"
                ), {"schema": schema})).scalar_one() == 0
        finally:
            await admin.dispose()


def evidence_snapshot(rows):
    return [
        {column.name: copy.deepcopy(getattr(row, column.name))
         for column in ScrapedFieldEvidence.__table__.columns}
        for row in rows
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [0, 1], ids=["first-option-wins", "second-option-wins"])
async def test_simultaneous_same_snapshot_has_one_winner(isolated_fee_database, first):
    engine = isolated_fee_database
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sessions() as db:
        university = University(name="Disposable fee race", country="Test", city="Test")
        db.add(university)
        await db.flush()
        sc = course()
        sc.id = None
        sc.university_id = university.id
        db.add(sc)
        await db.flush()
        variants = copy.deepcopy(sc.extraction_method["fee_variants"])
        raw = json.dumps(variants, ensure_ascii=False)
        proof = ScrapedFieldEvidence(
            scraped_course_id=sc.id, field_key="international_fee",
            source_url=URL, extraction_method=METHOD,
            snippet=raw[:1000], raw_text=raw,
        )
        unrelated = ScrapedFieldEvidence(
            scraped_course_id=sc.id, field_key="duration",
            source_url=URL, extraction_method="test_source",
            snippet="One year full-time", raw_text="Unrelated source evidence",
        )
        db.add_all([proof, unrelated])
        await db.commit()
        sc_id, proof_id = sc.id, proof.id
        state = fee_selection(sc)
        before_tuple = {key: getattr(sc, key) for key in FIELDS}
        before_evidence = evidence_snapshot([proof, unrelated])

    options = [state["options"][1], state["options"][2]]
    winner, loser = str(first), str(1 - first)
    locked, release, loser_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}

    class ContendingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            is_row_lock = (
                getattr(statement, "_for_update_arg", None) is not None
                and "scraped_courses" in str(statement)
            )
            actor = self.info["actor"]
            if is_row_lock and actor == loser:
                loser_started.set()
            result = await super().execute(statement, *args, **kwargs)
            if is_row_lock and actor == winner:
                # Hold the actual endpoint-acquired row lock, not a substitute
                # SELECT or mocked result, until PostgreSQL proves contention.
                locked.set()
                await asyncio.wait_for(release.wait(), timeout=10)
            return result

    async def database(request: Request):
        actor = request.headers["x-test-reviewer"]
        async with ContendingSession(
            engine, expire_on_commit=False, autoflush=False, info={"actor": actor},
        ) as db:
            pids[actor] = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            # Retain a stale identity-map object, just as a previously loaded
            # course would be. populate_existing must refresh it after waiting.
            stale = await db.get(ScrapedCourse, sc_id)
            assert fee_selection(stale)["snapshotToken"] == state["snapshotToken"]
            try:
                yield db
            finally:
                await db.rollback()

    async def reviewer(request: Request):
        return {
            "email": f"reviewer-{request.headers['x-test-reviewer']}@example.test",
            "permissions": ["staged.approve"],
        }

    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = reviewer
    tasks = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        def submit(actor):
            return asyncio.create_task(client.post(
                f"/api/scrape/staged/{sc_id}/fee-selection",
                headers={"x-test-reviewer": actor},
                json={"snapshotToken": state["snapshotToken"],
                      "optionId": options[int(actor)]["optionId"]},
            ))

        try:
            tasks.append(submit(winner))
            await asyncio.wait_for(locked.wait(), timeout=10)
            tasks.append(submit(loser))
            await asyncio.wait_for(loser_started.wait(), timeout=10)
            assert pids[winner] != pids[loser]

            async def wait_for_database_contention():
                async with engine.connect() as observer:
                    while True:
                        blocked = (await observer.execute(text(
                            "SELECT :winner = ANY(pg_blocking_pids(:loser))"
                        ), {"winner": pids[winner], "loser": pids[loser]})).scalar_one()
                        if blocked:
                            return
                        assert not tasks[1].done(), "Second request did not wait on the row lock"
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_database_contention(), timeout=5)
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert [response.status_code for response in responses] == [200, 409]
    assert responses[0].json()["success"] is True
    assert responses[1].json()["detail"] == "Fee snapshot changed; refresh before selecting"
    chosen = options[first]
    expected_tuple = dict(zip(FIELDS, (
        chosen["amount"], chosen["currency"], chosen["year"], chosen["period"],
    )))
    # A third, fresh session verifies committed state rather than either
    # request's cached ORM object.
    async with sessions() as db:
        saved = await db.get(ScrapedCourse, sc_id)
        assert {key: getattr(saved, key) for key in FIELDS} == expected_tuple
        saved_state = fee_selection(saved)
        assert saved_state["snapshotToken"] != state["snapshotToken"]
        assert saved_state["selectedOptionId"] == chosen["optionId"]
        assert saved_state["options"] == state["options"]
        assert saved.extraction_method["fee_variants"] == variants
        selection = saved.extraction_method["fee_selection"]
        assert selection["actor"] == f"reviewer-{winner}@example.test"
        assert selection["sourceEvidenceId"] == proof_id
        audits = (await db.execute(select(CourseAuditLog))).scalars().all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.scraped_course_id == sc_id and audit.course_id is None
        assert audit.source_evidence_id == proof_id
        assert audit.actor == selection["actor"]
        assert audit.action == "fee_option_selected"
        assert audit.field_key == "international_fee"
        assert json.loads(audit.old_value) == before_tuple
        assert json.loads(audit.new_value) == {
            "tuple": expected_tuple, "selection": selection,
            "option": variants["options"][[1, 2][first]],
        }
        evidence = (await db.execute(
            select(ScrapedFieldEvidence).order_by(ScrapedFieldEvidence.id)
        )).scalars().all()
        assert evidence_snapshot(evidence) == before_evidence
        assert saved.status == "pending" and saved.course_id is None
        assert (await db.execute(select(func.count()).select_from(Course))).scalar_one() == 0
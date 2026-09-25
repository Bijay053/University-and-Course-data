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
from contextlib import aclosing

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
from app.models import (
    AcademicRequirement, Course, CourseAuditLog, EnglishRequirement, Fee,
    Intake, ScrapedCourse, ScrapedFieldEvidence, University,
)
from app.models.sub_category import CourseSubCategory
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
            (University, Course, ScrapedCourse, ScrapedFieldEvidence, CourseAuditLog,
             Fee, EnglishRequirement, Intake, AcademicRequirement, CourseSubCategory)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_path", ["route", "service"])
@pytest.mark.parametrize("first,option_index", [
    ("approve", 2),
    ("select", 3),
    ("select", 2),
], ids=["approval-first", "selection-first-compatible", "selection-first-invalidates-approval"])
async def test_fee_selection_contends_with_approval(
    isolated_fee_database, approval_path, first, option_index,
):
    """Exercise real writers, including a deliberately stale ORM identity map.

    A uniform source can be approved before any reviewer selection. Another
    campus with the identical tuple remains approvable; selecting a different
    year/period fails the promotion service's source-authority validation.
    Neither outcome may publish the old cached tuple after selection commits.
    """
    from app.routers.scrape import _FeeSelectionBody, staged_fee_selection
    from app.services.scraper.approve_course import approve_scraped_course

    engine = isolated_fee_database
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sessions() as db:
        university = University(name="Disposable approval race", country="Test", city="Test")
        db.add(university)
        await db.flush()
        sc = course()
        sc.id = None
        sc.university_id = university.id
        variants = copy.deepcopy(sc.extraction_method["fee_variants"])
        variants.update(status="uniform", selected=variants["options"][:1], international_fee=19050)
        sc.extraction_method = {"international_fee": METHOD, "fee_variants": variants}
        sc.international_fee = 19050
        sc.ielts_overall = 6.5
        sc.duration = 1
        sc.intake_months = ["September"]
        sc.study_mode = "Full-time"
        db.add(sc)
        await db.flush()
        raw = json.dumps(variants, ensure_ascii=False)
        proof = ScrapedFieldEvidence(
            scraped_course_id=sc.id, field_key="international_fee",
            source_url=URL, extraction_method=METHOD, snippet=raw[:1000], raw_text=raw,
        )
        db.add(proof)
        await db.commit()
        await db.refresh(sc)
        sc_id, proof_id = sc.id, proof.id
        state = fee_selection(sc)
        before_tuple = {key: getattr(sc, key) for key in FIELDS}
        before_evidence = evidence_snapshot([proof])

    second = "select" if first == "approve" else "approve"
    locked, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}
    stale_objects = {}

    class ContendingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            is_row_lock = (
                getattr(statement, "_for_update_arg", None) is not None
                and "scraped_courses" in str(statement)
            )
            actor = self.info["actor"]
            if is_row_lock and actor == second:
                waiting.set()
            result = await super().execute(statement, *args, **kwargs)
            if is_row_lock and actor == first:
                locked.set()
                await asyncio.wait_for(release.wait(), timeout=10)
            return result

    async def database(request: Request):
        actor = request.headers["x-test-reviewer"]
        async with ContendingSession(
            engine, expire_on_commit=False, autoflush=False, info={"actor": actor},
        ) as db:
            pids[actor] = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            # Strong references ensure SQLAlchemy cannot evict these old rows.
            stale_objects[actor] = await db.get(ScrapedCourse, sc_id)
            assert fee_selection(stale_objects[actor])["snapshotToken"] == state["snapshotToken"]
            try:
                yield db
            finally:
                await db.rollback()

    async def reviewer(request: Request):
        return {"email": request.headers["x-test-reviewer"] + "@example.test",
                "permissions": ["staged.approve"]}

    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = reviewer

    # A private adapter uses the same isolated dependency but calls the real
    # promotion service directly, covering non-route callers without mocking it.
    @app.post("/service-approve")
    async def service_approve(request: Request):
        from fastapi import HTTPException
        async with aclosing(database(request)) as dependency:
            async for db in dependency:
                try:
                    return await approve_scraped_course(
                        db, stale_objects["approve"], actor="approve@example.test",
                    )
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc

    chosen = state["options"][option_index]
    tasks = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        def submit(actor):
            path = f"/api/scrape/staged/{sc_id}/fee-selection" if actor == "select" else (
                f"/api/scrape/staged/{sc_id}/approve" if approval_path == "route" else "/service-approve"
            )
            body = {"snapshotToken": state["snapshotToken"], "optionId": chosen["optionId"]} if actor == "select" else {}
            return asyncio.create_task(client.post(path, headers={"x-test-reviewer": actor}, json=body))

        async def wait_for_signal(signal, task):
            signal_task = asyncio.create_task(signal.wait())
            try:
                done, _ = await asyncio.wait(
                    [signal_task, task], timeout=10, return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    response = await task
                    pytest.fail(f"Writer exited before contention: {response.status_code} {response.text}")
                assert signal_task in done, "Writer did not reach its row lock"
            finally:
                signal_task.cancel()
                await asyncio.gather(signal_task, return_exceptions=True)

        try:
            tasks.append(submit(first))
            await wait_for_signal(locked, tasks[0])
            tasks.append(submit(second))
            await wait_for_signal(waiting, tasks[1])
            assert pids[first] != pids[second]

            async def observe_block():
                async with engine.connect() as observer:
                    while not (await observer.execute(text(
                        "SELECT :holder = ANY(pg_blocking_pids(:waiter))"
                    ), {"holder": pids[first], "waiter": pids[second]})).scalar_one():
                        assert not tasks[1].done(), "Competing writer bypassed the row lock"
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(observe_block(), timeout=5)
            release.set()
            responses = dict(zip([first, second], await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=10,
            )))
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    approved = first == "approve" or option_index == 3
    selected = first == "select"
    assert responses["approve"].status_code == (200 if approved else 422), responses["approve"].text
    assert responses["select"].status_code == (200 if selected else 409), responses["select"].text
    if not selected:
        assert responses["select"].json()["detail"] == "Only unpublished pending courses can select fees"
    if not approved:
        assert responses["approve"].json()["detail"] == "Resolve the published fee options before approving this course"
    expected_tuple = dict(zip(FIELDS, (
        chosen["amount"], chosen["currency"], chosen["year"], chosen["period"],
    ))) if selected else before_tuple

    async with sessions() as db:
        saved = await db.get(ScrapedCourse, sc_id)
        assert {key: getattr(saved, key) for key in FIELDS} == expected_tuple
        assert saved.extraction_method["fee_variants"] == variants
        assert saved.status == ("approved" if approved else "pending")
        assert fee_selection(saved)["selectedOptionId"] == (chosen["optionId"] if selected else None)
        published = (await db.execute(select(Course))).scalars().all()
        fees = (await db.execute(select(Fee))).scalars().all()
        assert len(published) == len(fees) == int(approved)
        if approved:
            assert saved.course_id == published[0].id == fees[0].course_id
            assert published[0].last_edited_by == "approve@example.test"
            assert {key: getattr(fees[0], key) for key in FIELDS} == expected_tuple
        else:
            assert saved.course_id is None
        audits = (await db.execute(select(CourseAuditLog))).scalars().all()
        assert len(audits) == int(selected)
        if selected:
            audit = audits[0]
            selection = saved.extraction_method["fee_selection"]
            assert audit.action == "fee_option_selected"
            assert audit.actor == selection["actor"] == "select@example.test"
            assert audit.scraped_course_id == sc_id and audit.course_id is None
            assert audit.source_evidence_id == selection["sourceEvidenceId"] == proof_id
            assert audit.field_key == "international_fee"
            assert json.loads(audit.old_value) == before_tuple
            assert json.loads(audit.new_value) == {
                "tuple": expected_tuple, "selection": selection,
                "option": variants["options"][option_index],
            }
        else:
            assert "fee_selection" not in saved.extraction_method
        evidence = (await db.execute(select(ScrapedFieldEvidence))).scalars().all()
        assert evidence_snapshot(evidence) == before_evidence

        # After a rejected approval the successful review remains retryable,
        # but the old client token must not overwrite its tuple or add an audit.
        if not approved:
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc:
                await staged_fee_selection(sc_id, _FeeSelectionBody(
                    snapshotToken=state["snapshotToken"],
                    optionId=state["options"][0]["optionId"],
                ), db, {"email": "stale@example.test"})
            assert exc.value.status_code == 409
            await db.rollback()
            assert (await db.execute(select(func.count()).select_from(CourseAuditLog))).scalar_one() == 1
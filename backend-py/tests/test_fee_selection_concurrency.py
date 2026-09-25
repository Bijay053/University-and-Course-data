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
@pytest.mark.parametrize("reverse_order", [False, True], ids=["forward", "reverse"])
async def test_bulk_approval_publishes_only_current_selected_fees(
    isolated_fee_database, reverse_order,
):
    """Real selection and bulk routes, real promotion, schema-local records only."""
    from app.routers.reviews import router as reviews_router

    sessions = async_sessionmaker(
        isolated_fee_database, expire_on_commit=False, autoflush=False,
    )
    cases = ["selected_range", "unresolved", "changed_year_period", "stale_source", "stale_tuple"]
    if reverse_order:
        cases.reverse()
    ids, original_tuples, variants_by_case = {}, {}, {}
    async with sessions() as db:
        university = University(name="Disposable bulk fees", country="Test", city="Test")
        db.add(university)
        await db.flush()
        university_id = university.id
        for case in cases:
            sc = course()
            sc.id = None
            sc.university_id = university_id
            sc.scrape_job_id = f"bulk-selection-{case}"
            # Distinct names prevent promotion's legitimate deduplication from
            # conflating the synthetic courses, which share an official URL.
            sc.course_name = f"MSc Healthcare Management {case}"
            sc.ielts_overall = 6.5
            sc.duration = 1
            sc.intake_months = ["September"]
            sc.study_mode = "Full-time"
            sc.auto_publish_status = "pending_review"
            db.add(sc)
            await db.flush()
            ids[case] = sc.id
            original_tuples[case] = {key: getattr(sc, key) for key in FIELDS}
            variants_by_case[case] = copy.deepcopy(sc.extraction_method["fee_variants"])
            raw = json.dumps(variants_by_case[case], ensure_ascii=False)
            db.add_all([
                ScrapedFieldEvidence(
                    scraped_course_id=sc.id, field_key="international_fee",
                    source_url=URL, extraction_method=METHOD,
                    snippet=raw[:1000], raw_text=raw,
                ),
                ScrapedFieldEvidence(
                    scraped_course_id=sc.id, field_key="duration",
                    source_url=URL, extraction_method="test_source",
                    snippet="One year full-time", raw_text="Unrelated evidence",
                ),
            ])
        await db.commit()

    async def database():
        async with sessions() as db:
            yield db

    async def reviewer():
        return {"email": "bulk-reviewer@example.test", "permissions": ["staged.approve"]}

    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    app.include_router(reviews_router, prefix="/api/reviews")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = reviewer
    chosen_by_case = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for case in cases:
            if case == "unresolved":
                continue
            async with sessions() as db:
                state = fee_selection(await db.get(ScrapedCourse, ids[case]))
            chosen = state["options"][2 if case == "changed_year_period" else 1]
            chosen_by_case[case] = chosen
            response = await client.post(
                f"/api/scrape/staged/{ids[case]}/fee-selection",
                json={"snapshotToken": state["snapshotToken"], "optionId": chosen["optionId"]},
            )
            assert response.status_code == 200, response.text

        async with sessions() as db:
            stale_source = await db.get(ScrapedCourse, ids["stale_source"])
            metadata = copy.deepcopy(stale_source.extraction_method)
            metadata["fee_variants"]["options"][2]["snippet"] += " updated source"
            stale_source.extraction_method = metadata
            stale_tuple = await db.get(ScrapedCourse, ids["stale_tuple"])
            stale_tuple.fee_year = 2028
            await db.commit()

        # Snapshot committed data immediately before bulk approval. Failed rows
        # must remain byte-for-byte equivalent at the mapped-column level.
        async with sessions() as db:
            before_rows = {
                case: {
                    column.name: copy.deepcopy(getattr(sc, column.name))
                    for column in ScrapedCourse.__table__.columns
                }
                for case, sc in [
                    (case, await db.get(ScrapedCourse, sc_id)) for case, sc_id in ids.items()
                ]
            }
            before_evidence = evidence_snapshot((await db.execute(
                select(ScrapedFieldEvidence).order_by(ScrapedFieldEvidence.id)
            )).scalars().all())
            before_audits = [
                {column.name: copy.deepcopy(getattr(row, column.name))
                 for column in CourseAuditLog.__table__.columns}
                for row in (await db.execute(
                    select(CourseAuditLog).order_by(CourseAuditLog.id)
                )).scalars().all()
            ]
            assert len(before_audits) == 4
            for case, chosen in chosen_by_case.items():
                audit = next(a for a in before_audits if a["scraped_course_id"] == ids[case])
                selection = before_rows[case]["extraction_method"]["fee_selection"]
                proof = next(e for e in before_evidence
                             if e["scraped_course_id"] == ids[case]
                             and e["field_key"] == "international_fee")
                assert audit["actor"] == selection["actor"] == "bulk-reviewer@example.test"
                assert audit["source_evidence_id"] == selection["sourceEvidenceId"] == proof["id"]
                assert audit["action"] == "fee_option_selected"
                assert audit["field_key"] == "international_fee"
                assert audit["course_id"] is None
                assert json.loads(audit["old_value"]) == original_tuples[case]
                assert json.loads(audit["new_value"]) == {
                    "tuple": dict(zip(FIELDS, (
                        chosen["amount"], chosen["currency"], chosen["year"], chosen["period"],
                    ))),
                    "selection": selection,
                    "option": variants_by_case[case]["options"][
                        2 if case == "changed_year_period" else 1
                    ],
                }

        response = await client.post(
            "/api/reviews/scraped-courses/bulk-approve",
            params={"university_id": university_id, "status": "pending",
                    "auto_publish_status": "pending_review", "dry_run": False, "limit": 10},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"] is True
        assert result["approved"] == 2
        assert result["failed"] == 3
        invalid = {"unresolved", "stale_source", "stale_tuple"}
        assert {failure["scraped_course_id"] for failure in result["failures"]} == {
            ids[case] for case in invalid
        }
        assert all(failure["error"] == "Select a current published fee option before approval"
                   for failure in result["failures"])

    # Fresh session proves persisted promotion, not response counts or ORM cache.
    async with sessions() as db:
        published = (await db.execute(select(Course))).scalars().all()
        fees = (await db.execute(select(Fee))).scalars().all()
        assert len(published) == len(fees) == 2
        published_ids = set()
        for case, sc_id in ids.items():
            saved = await db.get(ScrapedCourse, sc_id)
            if case in invalid:
                assert {
                    column.name: getattr(saved, column.name)
                    for column in ScrapedCourse.__table__.columns
                } == before_rows[case]
                assert saved.status == "pending" and saved.course_id is None
                assert fee_selection(saved)["selectedOptionId"] is None
                continue
            chosen = chosen_by_case[case]
            expected_tuple = dict(zip(FIELDS, (
                chosen["amount"], chosen["currency"], chosen["year"], chosen["period"],
            )))
            assert {key: getattr(saved, key) for key in FIELDS} == expected_tuple
            assert saved.status == "approved" and saved.reviewed_at is not None
            assert saved.extraction_method == before_rows[case]["extraction_method"]
            assert fee_selection(saved)["selectedOptionId"] == chosen["optionId"]
            live = next(row for row in published if row.id == saved.course_id)
            published_ids.add(live.id)
            assert live.name == saved.course_name
            assert live.university_id == university_id
            assert live.last_edited_by == "bulk-reviewer@example.test"
            fee = next(row for row in fees if row.course_id == live.id)
            assert {key: getattr(fee, key) for key in FIELDS} == expected_tuple
        assert published_ids == {row.id for row in published}
        assert evidence_snapshot((await db.execute(
            select(ScrapedFieldEvidence).order_by(ScrapedFieldEvidence.id)
        )).scalars().all()) == before_evidence
        assert [
            {column.name: getattr(row, column.name) for column in CourseAuditLog.__table__.columns}
            for row in (await db.execute(
                select(CourseAuditLog).order_by(CourseAuditLog.id)
            )).scalars().all()
        ] == before_audits


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
@pytest.mark.parametrize("first,option_index,source_status", [
    ("approve", 2, "uniform"),
    ("select", 3, "uniform"),
    ("select", 2, "uniform"),
    ("select", 1, "uniform"),
    ("select", 2, "range"),
    ("select", 1, "range"),
], ids=["approval-first", "selection-first-compatible", "selection-first-year-period",
        "selection-first-amount", "range-selection-year-period", "range-selection-amount"])
async def test_fee_selection_contends_with_approval(
    isolated_fee_database, approval_path, first, option_index, source_status,
):
    """Exercise real writers, including a deliberately stale ORM identity map.

    A uniform source can be approved before any reviewer selection. Another
    campus, amount or year/period is approvable after a source-backed selection,
    including an originally unresolved range. Approval must never publish the
    old cached tuple after selection commits.
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
        if source_status == "uniform":
            variants.update(status="uniform", selected=variants["options"][:1], international_fee=19050)
        sc.extraction_method = {"international_fee": METHOD, "fee_variants": variants}
        sc.international_fee = variants["international_fee"]
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

    selected = first == "select"
    assert responses["approve"].status_code == 200, responses["approve"].text
    assert responses["select"].status_code == (200 if selected else 409), responses["select"].text
    if not selected:
        assert responses["select"].json()["detail"] == "Only unpublished pending courses can select fees"
    expected_tuple = dict(zip(FIELDS, (
        chosen["amount"], chosen["currency"], chosen["year"], chosen["period"],
    ))) if selected else before_tuple

    async with sessions() as db:
        saved = await db.get(ScrapedCourse, sc_id)
        assert {key: getattr(saved, key) for key in FIELDS} == expected_tuple
        assert saved.extraction_method["fee_variants"] == variants
        assert saved.status == "approved"
        assert fee_selection(saved)["selectedOptionId"] == (chosen["optionId"] if selected else None)
        published = (await db.execute(select(Course))).scalars().all()
        fees = (await db.execute(select(Fee))).scalars().all()
        assert len(published) == len(fees) == 1
        assert saved.course_id == published[0].id == fees[0].course_id
        assert published[0].last_edited_by == "approve@example.test"
        assert {key: getattr(fees[0], key) for key in FIELDS} == expected_tuple
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

        # Neither an old nor a current token can mutate the published fee.
        if selected:
            from fastapi import HTTPException
            for token in (state["snapshotToken"], fee_selection(saved)["snapshotToken"]):
                with pytest.raises(HTTPException) as exc:
                    await staged_fee_selection(sc_id, _FeeSelectionBody(
                        snapshotToken=token,
                        optionId=state["options"][0]["optionId"],
                    ), db, {"email": "stale@example.test"})
                assert exc.value.status_code == 409
                assert exc.value.detail == "Only unpublished pending courses can select fees"
            await db.rollback()
            assert (await db.execute(select(func.count()).select_from(CourseAuditLog))).scalar_one() == 1
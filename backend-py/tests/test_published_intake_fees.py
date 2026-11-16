from copy import deepcopy
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Course, Fee, FeeIntakeApplicability, Intake, ScrapedCourse
from app.services import assistant_intake_consolidation as merge
from app.services import scrape_assistant as service
from app.services import published_intake_fees as published
from app.services.assistant_intake_fees import KEY, digest
from app.services.scraper.approve_course import approve_scraped_course, ApprovalValidationError
from tests.test_assistant_intake_fee_schedules import prepared, samples
from tests.test_assistant_intake_consolidation import HOME
from tests.test_scrape_prompt_assistant import store, assistant_postgres


async def merged(store):
    action = await prepared(store)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()


def test_manifest_validation_is_not_just_a_self_supplied_digest():
    rows = samples()
    plan = merge.make_plan(rows, HOME)
    row = rows[0]
    row.course_name, row.course_website = plan["courseName"], plan["canonicalUrl"]
    row.intake_months = plan["intakes"]
    row.extraction_method = {KEY: plan["_feeSchedules"]}
    assert published.validate_manifest(row)
    mutations = [
        lambda m: m["schedules"][0].update(intakes=["Rolling"]),
        lambda m: m["schedules"][1].update(intakes=["January"]),
        lambda m: m["schedules"][1].update(sourceUrls=["https://evil.example/course"]),
        lambda m: m["schedules"][1].update(amount=999999),
        lambda m: m["schedules"][1].update(entryYear=2028),
        lambda m: m["schedules"][1].update(applicabilityNeedsReview=True),
    ]
    for change in mutations:
        manifest = deepcopy(plan["_feeSchedules"])
        change(manifest)
        manifest["fingerprint"] = digest({k: v for k, v in manifest.items() if k != "fingerprint"})
        row.extraction_method = {KEY: manifest}
        with pytest.raises(ValueError):
            published.validate_manifest(row)


@pytest.mark.asyncio
async def test_real_approval_roundtrip_keeps_both_years_and_intake_applicability(store):
    await merged(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        with pytest.raises(ApprovalValidationError):
            await approve_scraped_course(db, row, actor="reviewer", commit=False)
        result = await approve_scraped_course(
            db, row, actor="reviewer", commit=False, allow_intake_schedules=True)
        await db.commit()
        cid = result["course_id"]
        fees = list((await db.execute(select(Fee).where(Fee.course_id == cid).order_by(Fee.fee_year))).scalars())
        assert [f.fee_year for f in fees] == [2026, 2027]
        links = list((await db.execute(select(FeeIntakeApplicability).order_by(FeeIntakeApplicability.fee_id))).scalars())
        assert [l.months for l in links] == [["January", "April"], ["September"]]
        intakes = list((await db.execute(select(Intake).where(Intake.course_id == cid))).scalars())
        assert {(i.intake_month, i.intake_year) for i in intakes} == {
            ("January", 2027), ("April", 2027), ("September", 2027)}
        dto = [{"id": cid, "internationalFee": 123, "feeYear": 2027, "currency": "GBP"}]
        await published.attach(db, dto)
        assert dto[0]["internationalFee"] is None and dto[0]["feeYear"] is None
        assert [(s["feeYear"], s["intakes"]) for s in dto[0]["feeSchedules"]] == [
            (2026, ["January", "April"]), (2027, ["September"])]
        assert all("sourceRowIds" not in s for s in dto[0]["feeSchedules"])
        # Approval is idempotent; repeating it cannot append another fee cohort.
        await approve_scraped_course(db, row, actor="reviewer", allow_intake_schedules=True)
        assert len(list((await db.execute(select(Fee))).scalars())) == 2
        # Old editors cannot silently strip an association or change its year.
        from app.routers.per_course_resources import (
            list_course_fees, update_fee, delete_fee, create_course_fee,
            create_course_intake, update_intake, delete_intake)
        wire = await list_course_fees(cid, db)
        assert len(wire) == 2 and all(r["intakeApplicability"] for r in wire)
        for call in [
            lambda: update_fee(fees[0].id, db, {"feeYear": 2030}),
            lambda: delete_fee(fees[0].id, db),
            lambda: create_course_fee(cid, db, {"internationalFee": 1}),
            lambda: create_course_intake(cid, db, {"intakeMonth": "August"}),
            lambda: update_intake(intakes[0].id, db, {"intakeMonth": "August"}),
            lambda: delete_intake(intakes[0].id, db),
        ]:
            with pytest.raises(HTTPException) as exc:
                await call()
            assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_storage_missing_fails_before_publishing_any_course(store, monkeypatch):
    await merged(store)
    async def missing(db):
        return False
    monkeypatch.setattr(published, "storage_ready", missing)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        with pytest.raises(ApprovalValidationError, match="storage"):
            await approve_scraped_course(db, row, actor="reviewer", allow_intake_schedules=True)
        assert not list((await db.execute(select(Course))).scalars())
        assert row.status == "pending"


@pytest.mark.asyncio
async def test_partial_fee_persistence_rolls_back_the_whole_approval_savepoint(store, monkeypatch):
    await merged(store)
    async def fail(db, course_id, manifest):
        db.add(Fee(course_id=course_id, international_fee=manifest["schedules"][0]["amount"]))
        await db.flush()
        raise ValueError("injected second schedule failure")
    monkeypatch.setattr(published, "persist", fail)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        with pytest.raises(ValueError, match="second schedule"):
            async with db.begin_nested():
                await approve_scraped_course(db, row, actor="reviewer", commit=False,
                                             allow_intake_schedules=True)
        assert not list((await db.execute(select(Course))).scalars())
        assert not list((await db.execute(select(Fee))).scalars())
        assert (await db.get(ScrapedCourse, 1)).status == "pending"


@pytest.mark.asyncio
async def test_incomplete_published_mapping_never_falls_back_to_one_scalar_fee(store):
    await merged(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        result = await approve_scraped_course(db, row, actor="reviewer", allow_intake_schedules=True)
        cid = result["course_id"]
        db.add(Fee(course_id=cid, international_fee=999999, fee_year=2030))
        await db.flush()
        dto = [{"id": cid, "internationalFee": 999999}]
        await published.attach(db, dto)
        assert dto[0]["feeSchedules"] == [] and dto[0]["feeScheduleReviewRequired"] is True
        assert dto[0]["internationalFee"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", ["intake", "fee"])
async def test_missing_schedule_members_are_not_silently_hidden(store, lost):
    from sqlalchemy import delete
    await merged(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        cid = (await approve_scraped_course(
            db, row, actor="reviewer", allow_intake_schedules=True))["course_id"]
        if lost == "intake":
            await db.execute(delete(Intake).where(Intake.course_id == cid, Intake.intake_month == "September"))
        else:
            await db.execute(delete(Fee).where(Fee.course_id == cid, Fee.fee_year == 2027))
        dto = [{"id": cid, "internationalFee": 24800}]
        await published.attach(db, dto)
        assert dto[0]["feeSchedules"] == [] and dto[0]["feeScheduleReviewRequired"]
        assert dto[0]["internationalFee"] is None


@pytest.mark.asyncio
async def test_overlapping_manual_approvals_create_one_complete_fee_cohort(store):
    import asyncio
    await merged(store)
    async def approve():
        async with store() as db:
            row = await db.get(ScrapedCourse, 1)
            return await approve_scraped_course(
                db, row, actor="reviewer", allow_intake_schedules=True)
    first, second = await asyncio.gather(approve(), approve())
    assert first["course_id"] == second["course_id"]
    async with store() as db:
        assert len(list((await db.execute(select(Course))).scalars())) == 1
        assert len(list((await db.execute(select(Fee))).scalars())) == 2
        assert len(list((await db.execute(select(FeeIntakeApplicability))).scalars())) == 2
        assert len(list((await db.execute(select(Intake))).scalars())) == 3


@pytest.mark.asyncio
async def test_legacy_fee_rows_and_courses_stay_unchanged(store):
    await prepared(store)
    async with store() as db:
        db.add(Course(id=51, university_id=1, name="Legacy course"))
        await db.flush()
        db.add(Fee(course_id=51, international_fee=1000, fee_year=2020, currency="GBP"))
        await db.flush()
        rows = [{"id": 51, "internationalFee": 1000, "feeYear": 2020, "currency": "GBP"}]
        before = deepcopy(rows)
        await published.attach(db, rows)
        assert rows == before
        await published.protect_course(db, 51)


@pytest.mark.asyncio
async def test_portal_approval_and_real_http_read_paths_keep_the_mapping(store):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.database import get_db
    from app.routers import courses, search, per_course_resources, scrape
    from app.models.audit import CourseAuditLog
    await merged(store)
    async with store() as db:
        result = await scrape.staged_approve(
            1, db, {"id": 77, "email": "reviewer@example.test"}, {"force": True})
        cid = result["course_id"]
        audit = (await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.action == "manual_approve_intake_fee_schedules"))).scalar_one()
        assert audit.scraped_course_id == 1 and '"feeYear": 2026' in audit.new_value

    app = FastAPI()
    app.include_router(courses.router, prefix="/api")
    app.include_router(search.router, prefix="/api/search")
    app.include_router(per_course_resources.router, prefix="/api")
    async def database():
        async with store() as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/courses/{cid}")
        assert detail.status_code == 200, detail.text
        assert [s["feeYear"] for s in detail.json()["feeSchedules"]] == [2026, 2027]
        fees = await client.get(f"/api/courses/{cid}/fees")
        assert fees.status_code == 200, fees.text
        assert [s["intakeApplicability"]["entryYear"] for s in fees.json()] == [2027, 2027]
        compare = await client.get(f"/api/search/compare?ids={cid}")
        assert compare.status_code == 200, compare.text
        compared = compare.json()["courses"][0]
        assert len(compared["fee_schedules"]) == 2 and compared["fee_year"] is None
        found = await client.get("/api/search/courses")
        assert found.status_code == 200, found.text
        row = next(r for r in found.json()["results"] if r["id"] == cid)
        assert len(row["fee_schedules"]) == 2 and row["international_fee"] is None
        listing = await client.get("/api/courses")
        assert listing.status_code == 200, listing.text
        listed = next(r for r in listing.json()["data"] if r["id"] == cid)
        assert len(listed["feeSchedules"]) == 2 and listed["feeYear"] is None


@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_preserves_legacy_and_refuses_data_loss(store):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    spec = importlib.util.spec_from_file_location(
        "tuition_migration", Path(__file__).parents[1] / "alembic/versions/396_fee_intake_applicability.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    await prepared(store)
    async with store() as db:
        # This fixture owns and later drops a unique assistant_test_ schema.
        assert (await db.scalar(text("SELECT current_schema()"))).startswith("assistant_test_")
        db.add(Course(id=51, university_id=1, name="Legacy"))
        await db.flush()
        db.add(Fee(id=101, course_id=51, international_fee=5000, fee_year=2020))
        await db.flush()
        conn = await db.connection()
        await conn.run_sync(lambda c: FeeIntakeApplicability.__table__.drop(c))
        def invoke(c, fn):
            with Operations.context(MigrationContext.configure(c)):
                fn()
        await conn.run_sync(lambda c: invoke(c, migration.upgrade))
        await conn.run_sync(lambda c: invoke(c, migration.downgrade))
        assert (await db.get(Fee, 101)).international_fee == 5000
        await conn.run_sync(lambda c: invoke(c, migration.upgrade))
        db.add(FeeIntakeApplicability(fee_id=101, entry_year=2027, months=["April"],
                                     source_urls=["https://example.test/course"], source_digest="test"))
        await db.flush()
        with pytest.raises(RuntimeError, match="Cannot downgrade"):
            await conn.run_sync(lambda c: invoke(c, migration.downgrade))
        assert (await db.get(FeeIntakeApplicability, 101)).months == ["April"]

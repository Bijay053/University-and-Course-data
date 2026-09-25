from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_db
from app.models import Course, Fee, ScrapedCourse, University
from app.routers import staged_selected_approval as route
from tests.test_campus_fee_split import row_values, migrate_offerings_in_transaction


@pytest.mark.parametrize("ids", [[], [True], ["1"], [-1], [0], [1] * 2001])
def test_ids_are_strict(ids):
    with pytest.raises(ValidationError):
        route.ApproveSelectedBody(courseIds=ids)


def test_defaults_and_deduplication():
    body = route.ApproveSelectedBody(courseIds=[3, 3, 4])
    assert body.courseIds == [3, 4] and not body.force
    with pytest.raises(ValidationError):
        route.ApproveSelectedBody(courseIds=[3], force="false")


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_http_contract_sanitizes_database_errors_and_rollback(rollback_fails):
    from app.routers.scrape import router
    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("private SQL and secret")),
        rollback=AsyncMock(side_effect=RuntimeError("private rollback") if rollback_fails else None),
    )
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "reviewer", "permissions": ["staged.approve"],
    }
    with TestClient(app) as client:
        response = client.post("/api/scrape/staged/approve-selected", json={"courseIds": [1, 2]})
    assert response.status_code == 200
    assert response.json()["approvedIds"] == []
    assert response.json()["attempted"] == 2
    assert [f["id"] for f in response.json()["failed"]] == [1, 2]
    assert "private" not in response.text and "secret" not in response.text
    assert db.rollback.await_count == (1 if rollback_fails else 2)


@pytest_asyncio.fixture
async def db():
    from app.database import engine as configured_engine
    engine = create_async_engine(configured_engine.url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await migrate_offerings_in_transaction(connection)
        async with AsyncSession(bind=connection, expire_on_commit=False,
                                join_transaction_mode="create_savepoint") as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


async def seed(db, **overrides):
    from app.models.scrape_runtime import ScrapeRuntimeJob
    uni = University(name=f"Selected approval {uuid4()}", country="United Kingdom", city="London")
    db.add(uni)
    await db.flush()
    job_id = str(uuid4())
    db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni.id, job_type="scrape", status="completed"))
    await db.flush()
    row = ScrapedCourse(university_id=uni.id, scrape_job_id=job_id, **(row_values() | overrides))
    db.add(row)
    await db.flush()
    # Establish a durable baseline; per-source rollback must preserve input.
    await db.commit()
    return uni.id, row.id


@pytest.fixture
def campuses(monkeypatch):
    from app.services.scraper.extractors import ulaw_campuses
    resolver = AsyncMock(return_value={"status": "unchanged"})
    monkeypatch.setattr(ulaw_campuses, "enrich_course_campuses", resolver)
    return resolver


@pytest.mark.asyncio
async def test_all_siblings_mixed_ambiguous_and_retry(db, campuses):
    uni, good = await seed(db)
    _, bad = await seed(db, course_location=None)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[good, bad]), db, {"email": "reviewer"})
    assert result["attempted"] == 2
    assert result["approvedCount"] == 4 and result["splitCount"] == 1
    assert result["failed"][0]["id"] == bad
    courses = (await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()
    assert len(courses) == 1
    assert set(courses[0].course_location.split(", ")) == {"London", "Birmingham", "Leeds", "Manchester"}
    fees = (await db.execute(select(Fee).where(Fee.course_id.in_([c.id for c in courses])))).scalars().all()
    assert fees == []
    from app.models import CourseOffering
    offerings = (await db.execute(select(CourseOffering).where(CourseOffering.course_id == courses[0].id))).scalars().all()
    assert {o.fee_amount for o in offerings} == {17500, 19050}
    assert (await db.get(ScrapedCourse, bad)).status == "pending"
    repeated = await route.approve_selected(route.ApproveSelectedBody(courseIds=result["approvedIds"]), db, {"email": "reviewer"})
    assert repeated["approvedIds"] == result["approvedIds"] and repeated["splitCount"] == 0
    assert len((await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_equal_campus_prices_do_not_require_split(db, campuses):
    values = deepcopy(row_values())
    authority = values["extraction_method"]["fee_variants"]
    for option in authority["selected"]:
        option["amount"] = 20600
    authority.update(status="uniform", international_fee=20600)
    values["international_fee"] = 20600
    uni, row_id = await seed(db, **values)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "reviewer"})
    assert result["approvedCount"] == 4 and result["splitCount"] == 1 and not result["failed"]
    campuses.assert_awaited_once()
    courses = (await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()
    assert len(courses) == 1
    assert len(courses[0].course_location.split(", ")) == 4


@pytest.mark.asyncio
async def test_fee_year_rollover_keeps_parent_and_offering_ids_and_rejects_bad_cohorts(db, campuses):
    from app.models import CourseOffering
    from app.services.scraper.campus_fee_split import split_pending_course
    from app.services.scraper.approve_course import approve_scraped_course, ApprovalValidationError
    uni, original = await seed(db)
    first = await route.approve_selected(route.ApproveSelectedBody(courseIds=[original]), db, {"email": "reviewer"})
    assert not first["failed"]
    course = (await db.execute(select(Course).where(Course.university_id == uni))).scalar_one()
    course_id = course.id
    old_ids = {o.id for o in (await db.execute(select(CourseOffering).where(CourseOffering.course_id == course_id))).scalars()}

    async def new_source(year):
        from app.models.scrape_runtime import ScrapeRuntimeJob
        job_id = str(uuid4())
        db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni,
                               job_type="scrape", status="completed"))
        await db.flush()
        values = row_values()
        values["fee_year"] = year
        authority = values["extraction_method"]["fee_variants"]
        authority["fee_year"] = year
        for option in authority["selected"]:
            option["year"] = year
        row = ScrapedCourse(university_id=uni, scrape_job_id=job_id, **values)
        db.add(row)
        await db.flush()
        return row

    fresh = await new_source(2027)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[fresh.id]), db, {"email": "reviewer"})
    assert not result["failed"]
    assert (await db.execute(select(Course.id).where(Course.university_id == uni))).scalars().all() == [course_id]
    offerings = (await db.execute(select(CourseOffering).where(CourseOffering.course_id == course_id)
                                .execution_options(populate_existing=True))).scalars().all()
    assert {o.id for o in offerings} == old_ids
    assert {o.fee_year for o in offerings} == {2027}

    future = await new_source(2028)
    partition = await split_pending_course(db, future)
    children = [await db.get(ScrapedCourse, cid) for cid in partition["courseIds"]]
    # An individual endpoint cannot roll only London into the next fee year.
    with pytest.raises(ApprovalValidationError, match="all published campuses"):
        async with db.begin_nested():
            await approve_scraped_course(db, children[0], actor="reviewer", commit=False)
    children = [await db.get(ScrapedCourse, cid) for cid in partition["courseIds"]]
    with pytest.raises(ApprovalValidationError, match="all published campuses"):
        async with db.begin_nested():
            await approve_scraped_course(db, children[0], actor="reviewer", commit=False,
                                         offering_cohort=[children[0]])
    children = [await db.get(ScrapedCourse, cid) for cid in partition["courseIds"]]
    # Valid individual evidence from contradictory years must not be blended.
    bad = children[-1]
    metadata = deepcopy(bad.extraction_method)
    metadata["fee_variants"]["fee_year"] = 2029
    for option in metadata["fee_variants"]["options"]:
        option["year"] = 2029
    for option in metadata["fee_variants"]["selected"]:
        option["year"] = 2029
    bad.fee_year = 2029
    bad.extraction_method = metadata
    await db.flush()
    await db.commit()
    mixed = await route.approve_selected(route.ApproveSelectedBody(courseIds=[partition["courseIds"][0]]), db, {"email": "reviewer"})
    assert mixed["approvedCount"] == 0
    assert "Mixed fee years" in mixed["failed"][0]["error"]

    stale = await new_source(2026)
    await db.commit()
    rejected = await route.approve_selected(route.ApproveSelectedBody(courseIds=[stale.id]), db, {"email": "reviewer"})
    assert rejected["approvedCount"] == 0
    assert "Stale fee year" in rejected["failed"][0]["error"]
    offerings = (await db.execute(select(CourseOffering).where(CourseOffering.course_id == course_id)
                                .execution_options(populate_existing=True))).scalars().all()
    assert {o.id for o in offerings} == old_ids
    assert {o.fee_year for o in offerings} == {2027}


@pytest.mark.asyncio
async def test_second_sibling_failure_rolls_back_whole_source_and_continues(db, campuses, monkeypatch):
    uni, row_id = await seed(db)
    _, other = await seed(db, extraction_method={}, international_fee=20000)
    real_approve = route.approve_scraped_course
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("private database password and SQL")
        return await real_approve(*args, **kwargs)

    monkeypatch.setattr(route, "approve_scraped_course", fail_second)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id, other]), db, {"email": "reviewer"})
    assert result["approvedIds"] == [other] and result["splitCount"] == 0
    assert result["failed"] == [{"id": row_id, "error": "Course approval failed. Please retry."}]
    rows = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.university_id == uni))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "pending"
    assert rows[0].course_name == row_values()["course_name"]
    assert not (await db.execute(select(Course).where(Course.university_id == uni))).scalars().all()


@pytest.mark.asyncio
async def test_force_cannot_override_failed_campus_evidence(db, campuses):
    _, row_id = await seed(db, course_location="Birmingham")
    campuses.return_value = {"status": "needs_review", "reason": "Verified campus source unavailable."}
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id], force=True), db, {"email": "reviewer"})
    assert not result["approvedIds"]
    assert result["failed"] == [{"id": row_id, "error": "Verified campus source unavailable."}]
    assert (await db.get(ScrapedCourse, row_id)).status == "pending"


@pytest.mark.asyncio
async def test_verified_source_enrichment_precedes_partition(db, campuses):
    _, row_id = await seed(db, course_location="Birmingham")

    async def verified_campuses(session, row):
        assert session is db and row.id == row_id
        row.course_location = row_values()["course_location"]
        return {"status": "resolved"}

    campuses.side_effect = verified_campuses
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "reviewer"})
    assert result["approvedCount"] == 4 and result["splitCount"] == 1
    assert not result["failed"]
    for child_id in result["approvedIds"]:
        child = await db.get(ScrapedCourse, child_id)
        assert child.extraction_method["campus_fee_scope"]["split_actor"] == "reviewer"


@pytest.mark.asyncio
async def test_confidence_confirmation_is_preserved(db, campuses):
    _, row_id = await seed(db, extraction_method={}, international_fee=None, ielts_overall=None,
                           duration=None, intake_months=[], study_mode=None)
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "reviewer"})
    assert "confidence score" in result["failed"][0]["error"]
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[row_id], force=True), db, {"email": "reviewer"})
    assert result["approvedIds"] == [row_id]
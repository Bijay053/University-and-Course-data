from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Course, Fee, ScrapedCourse, ScrapedFieldEvidence, University
from app.services.scraper.campus_fee_split import plan_campus_fees, scope_refresh_payload, split_pending_course
from app.services.scraper.extractors.ulaw_fees import METHOD, validated_fee_variants

URL = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"


def test_campus_migration_replaces_or_creates_missing_prior_index():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/386_campus_fee_scope.py"
    spec = spec_from_file_location("campus_fee_scope_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Mock()
    migration.upgrade()
    migration.op.drop_index.assert_called_once_with(
        "uq_scraped_courses_job_review_url_identity",
        table_name="scraped_courses",
        if_exists=True,
    )
    args, kwargs = migration.op.create_index.call_args
    assert args[0] == "uq_scraped_courses_job_review_url_identity"
    assert "fee_scope_key" in args[2]
    assert kwargs["unique"] is True


def row_values():
    options = [{
        "amount": amount, "currency": "GBP", "year": 2026, "period": "Full Course",
        "campus": campus, "study_variant": "Standard", "source_url": URL,
        "snippet": f"International Students | 2026 | {campus}: £{amount}",
    } for amount, campus in [(19050, "London"), (17500, "Outside London")]]
    authority = {"status": "range", "selected": options, "options": options,
                 "international_fee": None, "currency": "GBP", "fee_year": 2026, "fee_term": "Full Course"}
    return {
        "course_name": "MSc Healthcare Management", "course_website": URL,
        "course_location": "London, Birmingham, Leeds, Manchester",
        "international_fee": None, "currency": "GBP", "fee_year": 2026, "fee_term": "Full Course",
        "extraction_method": {"international_fee": METHOD, "fee_variants": authority},
        "scrape_warnings": ["international_fee_varies_by_campus"],
        "status": "pending", "duration": 1, "duration_term": "Year", "ielts_overall": 6,
        "intake_months": ["February"], "study_mode": "On Campus", "degree_level": "Master",
    }


def test_proven_campus_groups_and_equal_fee():
    row = row_values()
    groups, reason = plan_campus_fees(row)
    assert reason is None
    assert [(g["amount"], g["locations"]) for g in groups] == [
        (19050, ["London"]), (17500, ["Birmingham", "Leeds", "Manchester"]),
    ]
    same = deepcopy(row)
    authority = same["extraction_method"]["fee_variants"]
    for option in authority["selected"]:
        option["amount"] = 20600
    authority.update(status="uniform", international_fee=20600)
    same["international_fee"] = 20600
    assert len(plan_campus_fees(same)[0]) == 1


def test_single_applicable_price_requires_exact_course_campus_authority():
    row = row_values()
    row["course_location"] = "London Bloomsbury"
    assert not plan_campus_fees(row)[0]
    proof = {
        "method": "location.ulaw_course_authority", "source_url": URL,
        "locations": ["London Bloomsbury"], "fee_year": 2026,
        "fee_term": "Full Course", "study_variant": "Standard",
        "snippet": "Course Key Facts: London Bloomsbury and Online",
    }
    row["extraction_method"]["campus_authority"] = proof
    groups, reason = plan_campus_fees(row)
    assert reason is None
    assert len(groups) == 1 and groups[0]["amount"] == 19050
    assert groups[0]["authority"]["status"] == "uniform"
    for field, wrong in [("source_url", URL + "other"), ("locations", ["Birmingham"]),
                         ("fee_year", 2027), ("fee_term", "Annual"), ("snippet", "")]:
        bad = deepcopy(row)
        bad["extraction_method"]["campus_authority"][field] = wrong
        assert not plan_campus_fees(bad)[0]


@pytest.mark.asyncio
async def test_single_course_owned_campus_is_scoped_and_uniform_without_siblings():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.scraper.fee_selection import unresolved_fee_selection
    values = row_values()
    values["course_location"] = "London Bloomsbury"
    values["extraction_method"]["campus_authority"] = {
        "method": "location.ulaw_course_authority", "source_url": URL,
        "locations": ["London Bloomsbury"], "fee_year": 2026,
        "fee_term": "Full Course", "study_variant": "Standard",
        "snippet": "Course Key Facts: London Bloomsbury and Online",
    }
    row = ScrapedCourse(id=123, **values)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: []))),
        flush=AsyncMock(), add=Mock(),
    )
    result = await split_pending_course(db, row, actor="reviewer")
    assert result["status"] == "split" and result["courseIds"] == [123]
    assert row.international_fee == 19050 and row.fee_scope_key
    assert not unresolved_fee_selection(row)
    assert validated_fee_variants(row)["status"] == "uniform"
    db.add.assert_not_called()
    assert (await split_pending_course(db, row))["status"] == "unchanged"


@pytest.mark.parametrize("change", ["missing", "unknown", "mixed_year", "mixed_period", "mixed_route", "conflict", "qualified"])
def test_ambiguous_scopes_fail_closed(change):
    row = row_values()
    authority = row["extraction_method"]["fee_variants"]
    if change == "missing":
        row["course_location"] = None
    if change == "unknown":
        authority["selected"][0]["campus"] = "Atlantis"
    if change == "mixed_year":
        authority["selected"][0]["year"] = 2027
    if change == "mixed_period":
        authority["selected"][0]["period"] = "Annual"
    if change == "mixed_route":
        authority["selected"][0]["study_variant"] = "Professional Practice"
    if change == "conflict":
        authority["selected"][0]["campus"] = "All campuses"
    if change == "qualified":
        authority["selected"][0]["campus"] = "London Bloomsbury"
    groups, reason = plan_campus_fees(row)
    assert groups == []
    assert reason


def test_london_qualifiers_are_london_not_outside():
    row = row_values()
    row["course_location"] = "London Moorgate, Birmingham"
    groups, reason = plan_campus_fees(row)
    assert reason is None
    assert groups[0]["locations"] == ["London Moorgate"]
    assert groups[0]["amount"] == 19050


@pytest.mark.parametrize("ids", [[], [True], ["1"], [-1], [0], [1] * 2001])
def test_batch_validation(ids):
    from app.routers.scrape import _SplitCampusFeesBody
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _SplitCampusFeesBody(ids=ids)


def test_refresh_unrelated_fields_keeps_scope_and_failed_fee_refresh_blocks():
    from app.services.scraper.campus_fee_split import _apply_group
    row = ScrapedCourse(**row_values())
    original_name, original_locations = row.course_name, row.course_location
    groups, _ = plan_campus_fees(row)
    _apply_group(row, groups[0], original_name, original_locations)
    changes = scope_refresh_payload(row, {"ielts_overall": 6.5, "course_location": original_locations,
                                        "course_name": original_name, "extraction_method": {"ielts_overall": "test"}})
    assert "course_name" not in changes and "course_location" not in changes
    assert changes["extraction_method"]["campus_fee_scope"] == row.extraction_method["campus_fee_scope"]
    assert "international_fee" not in changes
    changes = scope_refresh_payload(row, {"international_fee": 9999})
    assert changes["international_fee"] is None
    assert changes["extraction_method"]["fee_variants"]["status"] == "unresolved"


@pytest.mark.asyncio
async def test_uniform_and_published_rows_are_never_split():
    row = ScrapedCourse(**row_values())
    row.id = 123
    row.status = "approved"
    assert (await split_pending_course(None, row))["status"] == "unchanged"
    assert row.course_name == "MSc Healthcare Management"
    row.status = "pending"
    authority = row.extraction_method["fee_variants"]
    for option in authority["selected"]:
        option["amount"] = 20600
    authority.update(status="uniform", international_fee=20600)
    row.international_fee = 20600
    assert (await split_pending_course(None, row))["status"] == "unchanged"
    assert row.course_location == "London, Birmingham, Leeds, Manchester"


@pytest.mark.asyncio
async def test_real_database_split_approve_rescrape_and_idempotency():
    """All writes (including approval commits) are rolled back by the outer tx."""
    from app.services.scraper.approve_course import approve_scraped_course
    from app.services.scraper.stage_course import stage_course
    from app.routers.scrape import _SplitCampusFeesBody, split_campus_fees
    from app.database import engine as configured_engine
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.models.page_snapshot import PageSnapshot
    from app.services.scraper.snapshot_save import persist_staged_row_backup
    engine = create_async_engine(configured_engine.url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
            uni = University(name=f"Campus fee test {uuid4()}", country="United Kingdom", city="London")
            db.add(uni)
            await db.flush()
            values = row_values()
            job_id = str(uuid4())
            db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni.id, job_type="scrape", status="completed"))
            await db.flush()
            row = ScrapedCourse(university_id=uni.id, scrape_job_id=job_id, **values)
            db.add(row)
            await db.flush()
            db.add(ScrapedFieldEvidence(scraped_course_id=row.id, field_key="international_fee",
                                      source_url=URL, snippet="International Students | published alternatives",
                                      extraction_method=METHOD))
            await db.flush()
            await persist_staged_row_backup(db, row)
            await db.flush()
            result = await split_campus_fees(_SplitCampusFeesBody(ids=[row.id]), db, {"email": "test-reviewer"})
            assert result["created"] == 1 and result["split"] == 1
            ids = result["results"][0]["courseIds"]
            children = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.id.in_(ids)).order_by(ScrapedCourse.id))).scalars().all()
            assert len(children) == 2
            assert {c.canonical_course_url for c in children} == {children[0].canonical_course_url}
            assert len({c.fee_scope_key for c in children}) == 2
            assert all(c.status == "pending" and validated_fee_variants(c) for c in children)
            assert all(len(c.extraction_method["fee_variants"]["options"]) == 2 for c in children)
            snapshots = (await db.execute(select(PageSnapshot).where(PageSnapshot.scrape_job_id == job_id))).scalars().all()
            assert {s.original_extraction.get("fee_scope_key") for s in snapshots} == {"", *(c.fee_scope_key for c in children)}
            for child in children:
                from app.services.auto_publish import should_auto_publish
                assert not should_auto_publish(child).auto_publish
                assert (await db.execute(select(ScrapedFieldEvidence).where(ScrapedFieldEvidence.scraped_course_id == child.id))).scalars().all()
                refreshed = scope_refresh_payload(child, row_values())
                assert refreshed["international_fee"] == child.international_fee
                assert "course_location" not in refreshed and "course_name" not in refreshed
            repeated = await split_campus_fees(_SplitCampusFeesBody(ids=ids), db, {"email": "test-reviewer"})
            assert repeated["created"] == repeated["split"] == 0
            live = [await approve_scraped_course(db, child, actor="test-reviewer") for child in children]
            assert len({c["course_id"] for c in live}) == 2
            courses = (await db.execute(select(Course).where(Course.university_id == uni.id))).scalars().all()
            assert {c.course_location for c in courses} == {"London", "Birmingham, Leeds, Manchester"}
            assert len(courses) == 2
            fees = (await db.execute(select(Fee).where(Fee.course_id.in_([c.id for c in courses])))).scalars().all()
            assert {fee.international_fee for fee in fees} == {17500, 19050}
            assert all(fee.currency == "GBP" and fee.fee_year == 2026 and fee.fee_term == "Full Course" for fee in fees)
            payload = {k: v for k, v in row_values().items() if k not in {"course_name", "status"}}
            evidence = [{"field_key": "international_fee", "value": None, "method": METHOD, "source_url": URL, "snippet": "International Students | campus prices"}]
            staged = await stage_course(db, scrape_job_id=str(uuid4()), university_id=uni.id,
                                        course_name=values["course_name"], payload=payload, evidence=evidence, source_url=URL)
            assert staged.saved, staged.reason
            pending = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.university_id == uni.id, ScrapedCourse.status == "pending"))).scalars().all()
            assert len(pending) == 2
            assert {c.course_location for c in pending} == {"London", "Birmingham, Leeds, Manchester"}
            for child in pending:
                await approve_scraped_course(db, child, actor="test-reviewer")
            assert len((await db.execute(select(Course).where(Course.university_id == uni.id))).scalars().all()) == 2
        await transaction.rollback()
    await engine.dispose()
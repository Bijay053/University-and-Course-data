"""Real staging and concurrent commits, confined to a disposable PostgreSQL schema."""
import asyncio
from copy import deepcopy
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base, postgres_tls_connect_args
from app.models import University, ScrapedCourse
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.scraper.pending_imports import ATTEMPT, identity, linked_course_ids
from app.services.scraper.stage_course import stage_course

URL = "https://example.edu/courses/business"


@pytest_asyncio.fixture
async def store():
    if os.environ.get("ALLOW_ISOLATED_PENDING_IMPORT_TESTS") != "1":
        pytest.skip("Requires opt-in to disposable development PostgreSQL schema")
    schema = "test_pending_import_" + uuid.uuid4().hex
    admin = create_async_engine(settings.database_url, poolclass=NullPool,
                                connect_args=postgres_tls_connect_args())
    engine = create_async_engine(settings.database_url, poolclass=NullPool, connect_args={
        **postgres_tls_connect_args(),
        "server_settings": {"search_path": schema, "statement_timeout": "20000",
                            "lock_timeout": "15000"},
    })
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            uni = University(name="Import test", country="United Kingdom", city="London")
            db.add(uni)
            await db.flush()
            uid = uni.id
            for job in ("first", "second", "third"):
                db.add(ScrapeRuntimeJob(runtime_job_id=job, university_id=uid,
                                       job_type="scrape", request_payload={}))
            await db.commit()
        yield sessions, uid
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            assert (await conn.execute(text(
                "SELECT count(*) FROM pg_namespace WHERE nspname = :name"
            ), {"name": schema})).scalar_one() == 0
        await admin.dispose()


async def stage(store, job, **overrides):
    sessions, uid = store
    name = overrides.pop("name", "Bachelor of Business")
    url = overrides.pop("url", URL)
    reuse = overrides.pop("reuse", True)
    payload = {
        "course_website": url, "degree_level": "Bachelor",
        "course_location": "London", "study_mode": "On Campus",
        "international_fee": 20000, "fee_term": "Annual",
        "fee_year": 2026, "currency": "GBP", "duration": 3, "duration_term": "Year",
        **overrides,
    }
    evidence = [{"field_key": key, "value": value, "source_url": url,
                 "snippet": f"{key}: {value}", "confidence": 1, "method": "static"}
                for key, value in payload.items()]
    async with sessions() as db:
        result = await stage_course(db, scrape_job_id=job, university_id=uid,
                                   course_name=name, source_url=url, payload=payload,
                                   evidence=evidence, reuse_pending=reuse,
                                   preserve_existing=True)
        assert result.saved, result.reason
        return result


@pytest.mark.asyncio
async def test_cross_review_retry_and_immutable_staff_values(store):
    sessions, uid = store
    first = await stage(store, "first")
    async with sessions() as db:
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        row.international_fee = 23456
        row.notes = "Staff-provided correction"
        row.extraction_method = {"international_fee": "staff_provided"}
        await db.commit()
        before = {c.key: deepcopy(getattr(row, c.key)) for c in row.__table__.columns}
    second = await stage(store, "second", url=URL + "/#fees")
    retry = await stage(store, "second")
    assert first.scraped_course_id == second.scraped_course_id == retry.scraped_course_id
    async with sessions() as db:
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        assert len(rows) == 1
        assert {c.key: getattr(rows[0], c.key) for c in rows[0].__table__.columns} == before
        attempts = (await db.execute(select(PageSnapshot).where(
            PageSnapshot.snapshot_type == ATTEMPT).order_by(PageSnapshot.id))).scalars().all()
        assert len(attempts) == 3
        assert attempts[0].original_extraction["reused"] == []
        changed = attempts[1].original_extraction["reused"][0]["differences"]
        assert changed["international_fee"] == {"existing": 23456, "incoming": 20000}
        assert await linked_course_ids(db, ["second"], uid) == {first.scraped_course_id}
        from app.services.scraper.replay_extraction import continuation_review_scope
        _, _, ids, _ = await continuation_review_scope(db, "second")
        assert first.scraped_course_id in ids
        from app.services.assistant_recovery_results import review_course_results
        result, complete, _ = await review_course_results(
            db, await db.get(ScrapeRuntimeJob, "second"), [URL], ["pending"])
        assert complete
        assert "international_fee" in result[0]["conflictingFields"]
        assert "not changed" in result[0]["message"]


@pytest.mark.asyncio
async def test_concurrent_cross_review_and_same_job_imports(store):
    results = await asyncio.gather(
        stage(store, "first"), stage(store, "second"), stage(store, "second"))
    assert len({r.scraped_course_id for r in results}) == 1
    sessions, _ = store
    async with sessions() as db:
        assert len((await db.execute(select(ScrapedCourse))).scalars().all()) == 1
        assert len((await db.execute(select(PageSnapshot).where(
            PageSnapshot.snapshot_type == ATTEMPT))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_distinct_awards_campuses_years_and_urls_are_not_merged(store):
    results = [
        await stage(store, "first"),
        await stage(store, "first", name="Master of Business", degree_level="Master"),
        await stage(store, "first", course_location="Manchester"),
        await stage(store, "first", fee_year=2027),
        await stage(store, "first", fee_term="Full Course"),
        await stage(store, "first", url=URL + "?specialisation=accounting"),
    ]
    assert len({r.scraped_course_id for r in results}) == len(results)


@pytest.mark.asyncio
async def test_published_and_other_providers_are_not_reused_or_modified(store):
    sessions, _ = store
    first = await stage(store, "first")
    async with sessions() as db:
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        row.status = "published"
        await db.commit()
        before = {c.key: deepcopy(getattr(row, c.key)) for c in row.__table__.columns}
        other = University(name="Other provider", country="United Kingdom", city="London")
        db.add(other)
        await db.flush()
        db.add(ScrapedCourse(university_id=other.id, scrape_job_id="other",
                             course_name=row.course_name, course_website=URL, status="pending"))
        await db.commit()
    second = await stage(store, "second")
    assert second.scraped_course_id != first.scraped_course_id
    async with sessions() as db:
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        assert {c.key: getattr(row, c.key) for c in row.__table__.columns} == before
        from app.services.assistant_recovery_results import review_course_results
        _, complete, _ = await review_course_results(
            db, await db.get(ScrapeRuntimeJob, "second"), [URL], ["pending"])
        assert complete
        assert len((await db.execute(select(ScrapedCourse))).scalars().all()) == 3


def test_identity_preserves_variant_scope_but_not_conflicting_prices():
    def row(**kw):
        return ScrapedCourse(university_id=1, course_name="Bachelor of Business",
                             course_website=URL, **kw)
    a = row(international_fee=10)
    b = row(international_fee=20)
    assert identity(a) == identity(b)
    assert identity(a) != identity(row(extraction_method={
        "fee_variants": {"selected": [{"study_load": "Part Time"}]}}))
    assert identity(a) != identity(row(extraction_method={
        "fee_variants": {"selected": [{"study_variant": "Accelerated"}]}}))
    assert identity(a) != identity(row(extraction_method={
        "ulaw_qualification_scope": {"award": "PGDip"}}))


def test_reused_attempt_is_not_a_new_quality_payload():
    from app.services.scraper.orchestrator import _record_staged_quality_payload
    from app.services.scraper.stage_course import StageResult
    values = []
    assert not _record_staged_quality_payload(values, {"international_fee": 123},
        StageResult(True, "reused_pending", 1, {"reused_pending": True}), source_url=URL)
    assert values == []


@pytest.mark.asyncio
async def test_split_campuses_reuse_and_partial_new_offerings(store):
    from tests.test_campus_fee_split import row_values
    values = row_values()
    values.pop("status")
    values["name"] = values.pop("course_name")
    values["url"] = values.pop("course_website")
    first = await stage(store, "first", **deepcopy(values))
    assert len(first.extra["staged_course_ids"]) == 4
    second = await stage(store, "second", **deepcopy(values))
    assert set(first.extra["staged_course_ids"]) == set(second.extra["staged_course_ids"])
    sessions, _ = store
    # A reviewer removed one offering; subsequent import must create only that
    # missing campus, retaining every other row and its staff edits.
    from sqlalchemy import delete
    missing = first.extra["staged_course_ids"][-1]
    async with sessions() as db:
        await db.execute(delete(ScrapedCourse).where(ScrapedCourse.id == missing))
        await db.commit()
    third = await stage(store, "third", **deepcopy(values))
    assert len(third.extra["staged_course_ids"]) == 4
    assert len(set(third.extra["staged_course_ids"]) & set(first.extra["staged_course_ids"])) == 3
    # A distinct award on the same page and campuses cannot hit the URL index.
    values["name"] = "Master of Business Administration"
    fourth = await stage(store, "third", **deepcopy(values))
    assert not set(fourth.extra["staged_course_ids"]) & set(third.extra["staged_course_ids"])
    async with sessions() as db:
        assert len((await db.execute(select(ScrapedCourse))).scalars().all()) == 8


@pytest.mark.asyncio
async def test_failed_attempt_evidence_rolls_back_new_row(store, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("attempt evidence unavailable")
    monkeypatch.setattr("app.services.scraper.pending_imports.record_attempt", fail)
    sessions, uid = store
    async with sessions() as db:
        result = await stage_course(
            db, scrape_job_id="first", university_id=uid,
            course_name="Bachelor of Business", source_url=URL,
            payload={"course_website": URL, "degree_level": "Bachelor",
                     "international_fee": 20000},
            evidence=[], reuse_pending=True, preserve_existing=True,
        )
        assert not result.saved
    async with sessions() as db:
        assert not (await db.execute(select(ScrapedCourse))).scalars().all()
        assert not (await db.execute(select(PageSnapshot))).scalars().all()


@pytest.mark.asyncio
async def test_identity_edits_reuse_original_offering_and_keep_new_variants(store):
    first = await stage(store, "first")
    sessions, _ = store
    async with sessions() as db:
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        row.course_name = "Bachelor of Business Administration"
        row.course_location = "London City"
        row.study_mode = "Online"
        row.fee_term = "Full Course"
        row.fee_year = 2027
        row.course_website = URL + "/corrected"
        await db.commit()
        before = {c.key: deepcopy(getattr(row, c.key)) for c in row.__table__.columns}
    second = await stage(store, "second")
    assert second.scraped_course_id == first.scraped_course_id
    distinct = await stage(store, "second", fee_year=2028)
    assert distinct.scraped_course_id != first.scraped_course_id
    async with sessions() as db:
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        assert {c.key: getattr(row, c.key) for c in row.__table__.columns} == before


@pytest.mark.asyncio
async def test_mixed_job_automatic_mutations_only_update_new_pending_rows(store):
    from app.services.scraper.pending_imports import AUTO_MUTATION_SQL, automatic_mutation_clause
    first = await stage(store, "first")
    await stage(store, "first")  # same-job retry is also read-only
    fresh = await stage(store, "first", name="Master of Business", degree_level="Master")
    sessions, _ = store
    async with sessions() as db:
        ids = (await db.execute(select(ScrapedCourse.id).where(
            ScrapedCourse.scrape_job_id == "first", automatic_mutation_clause()))).scalars().all()
        assert ids == [fresh.scraped_course_id]
        changed = (await db.execute(text(
            "UPDATE scraped_courses SET notes = 'automatic' WHERE scrape_job_id = :job"
            + AUTO_MUTATION_SQL + " RETURNING id"), {"job": "first"})).scalars().all()
        assert changed == [fresh.scraped_course_id]
        await db.commit()
        assert (await db.get(ScrapedCourse, first.scraped_course_id)).notes != "automatic"


@pytest.mark.asyncio
async def test_legacy_ordinary_import_identity_survives_staff_edits(store):
    first = await stage(store, "first", reuse=False)
    sessions, _ = store
    async with sessions() as db:
        # Simulate a backup made before explicit row IDs were recorded.
        backups = (await db.execute(select(PageSnapshot))).scalars().all()
        assert all(s.snapshot_type != ATTEMPT for s in backups)
        for backup in backups:
            data = dict(backup.original_extraction or {})
            data.pop("_staged_course_id", None)
            backup.original_extraction = data
        row = await db.get(ScrapedCourse, first.scraped_course_id)
        row.course_name = "Bachelor of Corrected Business"
        row.course_location = "Corrected campus"
        row.study_mode = "Online"
        row.course_website = URL + "/staff-correction"
        row.fee_year = 2027
        row.fee_term = "Full Course"
        row.notes = "Staff correction must remain intact"
        await db.commit()
        before = {c.key: deepcopy(getattr(row, c.key)) for c in row.__table__.columns}
    repeated = await stage(store, "second")
    assert repeated.scraped_course_id == first.scraped_course_id
    async with sessions() as db:
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        assert len(rows) == 1
        assert {c.key: getattr(rows[0], c.key) for c in rows[0].__table__.columns} == before

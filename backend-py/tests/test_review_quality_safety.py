"""Execute the real SQL against an isolated in-memory DB, never production."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.models import ScrapedCourse, ScrapeRuntimeJob, University
from app.models.field_conflict import FieldConflict
from app.models.publishing_ledger import PublishingLedger
from app.services.scraper.review_policy import annotate_review_quality
from app.services.publishing_engine import get_review_queue, run_publishing_pass
from app.routers.scrape import reconcile_review_quality
from tests.test_scrape_payload_compat import client_with_uni


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return "JSON"


class LocalDB:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    async def get(self, model, key, **kwargs):
        return self.session.get(model, key, **kwargs)

    async def commit(self):
        self.session.commit()

    def add(self, row):
        self.session.add(row)


@pytest.fixture
def quality_db():
    engine = create_engine("sqlite://")
    for model in (University, ScrapeRuntimeJob, ScrapedCourse, FieldConflict, PublishingLedger):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(University(id=1, name="Test University", country="UK", city="Test City"))
        session.add(ScrapeRuntimeJob(
            runtime_job_id="new", university_id=1, job_type="single", status="completed",
            request_payload={"fullCatalogueReviewOnly": True},
            gate_skip_counts={"data_quality": {
                "critical_count": 1, "critical_issues": [{"severity": "critical"}],
                "critical_urls": ["https://example.edu/bad"], "affected_course_count": 1,
            }},
        ))
        for id_, job, url in (
            (40883, "old", "bad"), (40884, "new", "bad"), (40885, "new", "good"),
        ):
            session.add(ScrapedCourse(
                id=id_, university_id=1, scrape_job_id=job, course_name=url,
                course_website=f"https://example.edu/{url}", status="pending",
                auto_publish_status="review", pub_decision="needs_review",
                completeness=100, avg_verification_confidence=100,
            ))
        session.commit()
        yield LocalDB(session)
    engine.dispose()


async def test_quality_marks_only_explicit_new_rows_and_excludes_queue(quality_db):
    db = quality_db
    # Even accidentally including an old ID cannot escape the job boundary.
    changed = await annotate_review_quality(
        db, job_id="new", university_id=1, row_ids=[40883, 40884],
        critical_urls=["https://example.edu/bad"],
    )
    await db.commit()
    assert changed == [40884]
    assert (await db.get(ScrapedCourse, 40883)).auto_publish_status == "review"
    assert (await db.get(ScrapedCourse, 40885)).auto_publish_status == "review"
    queue = await get_review_queue(db)
    assert {row["id"] for row in queue} == {40883, 40885}


async def test_exact_ids_required_even_with_matching_job_and_url(quality_db):
    assert await annotate_review_quality(
        quality_db, job_id="new", university_id=1, row_ids=[40885],
        critical_urls=["https://example.edu/bad"],
    ) == []


async def test_later_publishing_pass_preserves_review_only_rows(quality_db, monkeypatch):
    db = quality_db
    old = await db.get(ScrapedCourse, 40883)
    old.status = "rejected"  # Not a candidate in this test.
    await db.commit()
    publish = AsyncMock()
    monkeypatch.setattr("app.services.scraper.approve_course.approve_scraped_course", publish)
    # New session discards in-process scrape context; policy comes from the DB.
    db.session.expunge_all()
    for _ in range(2):
        counts = await run_publishing_pass(db)
        assert counts["auto_published"] == counts["scored"] == 0
    publish.assert_not_awaited()
    assert (await db.get(ScrapedCourse, 40885)).auto_publish_status == "review"


async def test_promotion_boundary_also_checks_durable_policy(quality_db):
    from app.services.scraper.approve_course import approve_scraped_course
    sc = await quality_db.get(ScrapedCourse, 40885)
    with pytest.raises(ValueError, match="Review-only"):
        await approve_scraped_course(quality_db, sc)


async def test_stored_evidence_reconciliation_is_bounded_and_idempotent(quality_db):
    db = quality_db
    old = await db.get(ScrapedCourse, 40883)
    before = {c.name: getattr(old, c.name) for c in old.__table__.columns}
    result = await reconcile_review_quality("new", db, {})
    assert result["scoped_rows"] == 2
    assert result["marked_ids"] == [40884]
    assert (await reconcile_review_quality("new", db, {}))["marked_ids"] == []
    db.session.refresh(old)
    assert before == {c.name: getattr(old, c.name) for c in old.__table__.columns}
    for id_ in (40883, 40884, 40885):
        assert (await db.get(ScrapedCourse, id_)).status == "pending"


async def test_reconciliation_refuses_truncated_evidence(quality_db):
    from fastapi import HTTPException
    job = await quality_db.get(ScrapeRuntimeJob, "new")
    job.gate_skip_counts = {"data_quality": {"critical_count": 12, "critical_issues": []}}
    await quality_db.commit()
    with pytest.raises(HTTPException) as error:
        await reconcile_review_quality("new", quality_db, {})
    assert error.value.status_code == 409


def test_reconciliation_requires_trigger_permission(client_with_uni):
    from app.main import app
    from app.dependencies import get_current_user
    client, db = client_with_uni
    app.dependency_overrides[get_current_user] = lambda: {"sub": "limited", "permissions": []}
    response = client.post("/api/scrape/jobs/new/reconcile-review-quality")
    assert response.status_code == 403
    assert not db.added


async def test_metadata_provenance_survives_missing_request_flag(quality_db, monkeypatch):
    db = quality_db
    job = await db.get(ScrapeRuntimeJob, "new")
    job.request_payload = {}
    job.discovered_config = {"fullCatalogueReviewPolicy": {"review_only": True}}
    (await db.get(ScrapedCourse, 40883)).status = "rejected"
    await db.commit()
    publish = AsyncMock()
    monkeypatch.setattr("app.services.scraper.approve_course.approve_scraped_course", publish)
    assert (await run_publishing_pass(db))["scored"] == 0
    publish.assert_not_awaited()


async def test_normal_job_remains_publishable_but_review_job_does_not(quality_db, monkeypatch):
    publish = AsyncMock()
    monkeypatch.setattr("app.services.scraper.approve_course.approve_scraped_course", publish)
    counts = await run_publishing_pass(quality_db)
    assert counts["scored"] == counts["auto_published"] == 1
    publish.assert_awaited_once()
    assert publish.await_args.args[1].id == 40883


async def test_real_quality_report_marks_the_new_saved_row(quality_db):
    from types import SimpleNamespace
    from app.services.scraper.orchestrator import _record_staged_quality_payload
    from app.services.scraper.data_quality import run_quality_checks
    from tests.test_data_quality import _good_payload
    staged = []
    _record_staged_quality_payload(
        staged, _good_payload(international_fee=None, has_central_fee_page=False),
        SimpleNamespace(saved=True, scraped_course_id=40884),
        source_url="https://example.edu/bad",
    )
    report = await run_quality_checks(staged)
    assert report["critical"] > 0
    changed = await annotate_review_quality(
        quality_db, job_id="new", university_id=1,
        row_ids=[item["scraped_course_id"] for item in staged],
        critical_urls=list(report["critical_urls"]),
    )
    assert changed == [40884]
    assert (await quality_db.get(ScrapedCourse, 40883)).auto_publish_status == "review"
    assert 40884 not in {row["id"] for row in await get_review_queue(quality_db)}
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.course import Course
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.models.scraped_course import ScrapedCourse
from app.services.scraper.replay_extraction import restore_review_rows
from app.services.scraper.snapshot_save import _extraction_fields, staged_row_backup_payload


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, jobs, snapshots, staged=(), published=()):
        self.jobs = jobs
        self.snapshots = list(snapshots)
        self.staged = list(staged)
        self.published = list(published)
        self.added = []
        self.commits = 0

    async def get(self, model, key):
        assert model is ScrapeRuntimeJob
        return self.jobs.get(key)

    async def execute(self, statement, _params=None):
        if not getattr(statement, "column_descriptions", None):
            return _Scalars([])
        entity = statement.column_descriptions[0]["entity"]
        if entity is PageSnapshot:
            return _Scalars(self.snapshots)
        if entity is ScrapedCourse:
            return _Scalars(self.staged)
        if entity is Course:
            return _Scalars(self.published)
        raise AssertionError(f"unexpected query entity: {entity}")

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("rollback was not expected")


def _job(job_id, parent=None):
    return SimpleNamespace(
        runtime_job_id=job_id,
        university_id=42,
        request_payload={"retrySourceJobId": parent} if parent else {},
    )


def _snapshot(
    snapshot_id,
    job_id,
    url,
    name,
    fee,
    *,
    course_website=None,
    snapshot_type="html",
    snapshot_schema=None,
    extra=None,
):
    extraction = {
        "course_name": name,
        "international_fee": fee,
        "degree_level": "Master's",
        "intake_months": ["February", "July"],
    }
    if course_website is not None:
        extraction["course_website"] = course_website
    if snapshot_schema is not None:
        extraction["_snapshot_schema"] = snapshot_schema
    extraction.update(extra or {})
    return SimpleNamespace(
        id=snapshot_id,
        scrape_job_id=job_id,
        course_url=url,
        snapshot_type=snapshot_type,
        fetched_at=datetime(2026, 1, snapshot_id, tzinfo=timezone.utc),
        original_extraction=extraction,
    )


def test_deleted_parent_review_set_is_reconstructed_exactly_and_idempotently():
    jobs = {
        "child": _job("child", "parent"),
        "parent": _job("parent"),
    }
    snapshots = [
        _snapshot(1, "parent", "https://uni.test/a", "Course A", 31000),
        _snapshot(2, "parent", "https://uni.test/b", "Course B", 32000),
        _snapshot(3, "parent", "https://uni.test/c", "Course C", 33000),
    ]
    surviving = SimpleNamespace(
        course_website="https://uni.test/a",
        course_name="Course A",
        status="pending",
    )
    published = SimpleNamespace(
        course_website="https://uni.test/c",
        name="Course C",
    )
    db = _FakeDb(jobs, snapshots, staged=[surviving], published=[published])

    result = asyncio.run(restore_review_rows("child", commit=True, db=db))

    assert result["chain_job_ids"] == ["child", "parent"]
    assert result["restored"] == 1
    assert result["skipped_existing"] == 2
    assert result["legacy_candidates"] == 3
    assert result["full_fidelity"] is False
    assert db.commits == 1
    assert len(db.added) == 1
    restored = db.added[0]
    assert restored.scrape_job_id == "parent"
    assert restored.status == "pending"
    assert restored.reviewed_at is None
    assert restored.course_name == "Course B"
    assert restored.course_website == "https://uni.test/b"
    assert restored.international_fee == 32000
    assert restored.intake_months == ["February", "July"]

    # A second call sees the restored URL and does not create another row.
    db.staged.append(restored)
    second = asyncio.run(restore_review_rows("child", commit=True, db=db))
    assert second["restored"] == 0
    assert len(db.added) == 1


def test_restore_preview_does_not_write():
    jobs = {"parent": _job("parent")}
    db = _FakeDb(
        jobs,
        [_snapshot(1, "parent", "https://uni.test/a", "Course A", 31000)],
    )

    result = asyncio.run(restore_review_rows("parent", commit=False, db=db))

    assert result["restored"] == 1
    assert result["commit"] is False
    assert db.added == []
    assert db.commits == 0


def test_snapshot_fields_unwrap_normal_extractor_envelope():
    stored = _extraction_fields({
        "url": "https://uni.test/a",
        "payload": {
            "course_name": "Course A",
            "international_fee": 31000,
            "pte_writing": 58,
            "duration_term": "years",
        },
        "evidence": [{"field_key": "international_fee"}],
    })

    assert stored == {
        "course_name": "Course A",
        "international_fee": 31000,
        "pte_writing": 58,
        "duration_term": "years",
        "_snapshot_schema": "extractor_payload_v1",
    }


def test_restore_preserves_canonical_course_url_from_original_extraction():
    jobs = {"parent": _job("parent")}
    snapshot = _snapshot(
        1,
        "parent",
        "https://uni.test/fetched-version",
        "Course A",
        31000,
        course_website="https://uni.test/canonical-course",
    )
    db = _FakeDb(jobs, [snapshot])

    result = asyncio.run(restore_review_rows("parent", commit=True, db=db))

    assert result["restored"] == 1
    assert db.added[0].course_website == "https://uni.test/canonical-course"
    assert result["rows"][0]["course_url"] == "https://uni.test/canonical-course"
    assert result["rows"][0]["snapshot_url"] == "https://uni.test/fetched-version"


def test_exact_backup_suppresses_legacy_fetched_url_duplicate():
    jobs = {"parent": _job("parent")}
    # The legacy URL sorts first lexically; exact backups must still win.
    canonical_url = "https://uni.test/z-canonical-course"
    original = ScrapedCourse(
        scrape_job_id="parent",
        university_id=42,
        course_name="Course A",
        course_website=canonical_url,
        international_fee=31000,
        degree_level="Master's",
        status="pending",
    )
    exact = _snapshot(
        2,
        "parent",
        canonical_url,
        "Course A",
        31000,
        snapshot_type="staged_row",
        snapshot_schema="staged_row_v1",
        extra=staged_row_backup_payload(original),
    )
    legacy = _snapshot(
        1,
        "parent",
        "https://uni.test/a-fetched-version",
        "Course A",
        31000,
    )
    db = _FakeDb(jobs, [legacy, exact])

    result = asyncio.run(restore_review_rows("parent", commit=True, db=db))

    assert result["restored"] == 1
    assert result["skipped_existing"] == 1
    assert len(db.added) == 1
    assert db.added[0].course_website == canonical_url

    db.staged.append(db.added[0])
    second = asyncio.run(restore_review_rows("parent", commit=True, db=db))
    assert second["restored"] == 0
    assert len(db.added) == 1


def test_exact_staged_row_backup_round_trips_final_review_values():
    jobs = {"parent": _job("parent")}
    original = ScrapedCourse(
        scrape_job_id="parent",
        university_id=42,
        course_name="Master of Applied Data",
        course_website="https://uni.test/applied-data",
        international_fee=34567,
        degree_level="Master's",
        category="Computer Science & IT",
        sub_category="Data Science",
        intake_months=["March", "October"],
        completeness=87,
        eligibility_status="review",
        eligibility_reason="Missing: location",
        auto_publish_status="review",
        decision_score=72.5,
        avg_verification_confidence=68.25,
        extraction_method={"fee": "table"},
        scrape_warnings=["confidence_low"],
        status="pending",
    )
    backup = staged_row_backup_payload(original)
    snapshot = _snapshot(
        1,
        "parent",
        original.course_website,
        original.course_name,
        original.international_fee,
        snapshot_type="staged_row",
        snapshot_schema="staged_row_v1",
        extra=backup,
    )
    db = _FakeDb(jobs, [snapshot])

    result = asyncio.run(restore_review_rows("parent", commit=True, db=db))

    assert result["restored"] == 1
    assert result["legacy_candidates"] == 0
    assert result["full_fidelity"] is True
    restored = db.added[0]
    for field in (
        "course_name",
        "course_website",
        "international_fee",
        "degree_level",
        "category",
        "sub_category",
        "intake_months",
        "completeness",
        "eligibility_status",
        "eligibility_reason",
        "auto_publish_status",
        "decision_score",
        "avg_verification_confidence",
        "extraction_method",
        "scrape_warnings",
        "status",
    ):
        assert getattr(restored, field) == getattr(original, field), field
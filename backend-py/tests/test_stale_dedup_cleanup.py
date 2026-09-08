"""Regression tests for the orchestrator's stale-dedup cleanup.

Covers the contract added alongside verbose log emissions:

1. ``pending`` rows older than the cutoff ARE deleted (cures "0 staged"
   symptom from prior failed runs).
2. ``pending`` rows newer than the cutoff are KEPT (no mid-flight wipe of
   another active run).
3. ``rejected`` rows are NEVER deleted — they represent reviewer decisions
   that drive Bug #7's ``rejection_block_days`` re-stage block. If this
   guarantee ever regresses, Bug #7 silently breaks.
4. Cleanup is scoped to a single university — rows for other unis are not
   touched.

These run against the same database the rest of the suite uses; we isolate
ourselves with a unique ``scrape_job_id`` prefix and clean up in a
``finally`` block so a failed assertion never leaves rows behind.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.database import AsyncSessionLocal, engine
from app.models import ScrapedCourse, University
from app.services.scraper.orchestrator import _clear_stale_dedup
from app.services.scraper.metrics import get_run_event_metrics, reset_run_event_metrics
from app.services.scraper.stage_course import stage_course


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    """pytest-asyncio creates a fresh event loop per test in 'auto' mode; the
    SQLAlchemy connection pool can otherwise hold connections bound to a
    closed loop. Dispose before each test so every session opens fresh."""
    await engine.dispose()
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def isolated_universities() -> tuple[int, int]:
    """Create dedicated universities so cleanup cannot touch existing rows."""
    token = uuid.uuid4().hex[:10]
    async with AsyncSessionLocal() as db:
        universities = [
            University(name=f"Stale Cleanup A {token}", country="Test", city="Test"),
            University(name=f"Stale Cleanup B {token}", country="Test", city="Test"),
        ]
        db.add_all(universities)
        await db.flush()
        ids = (universities[0].id, universities[1].id)
        await db.commit()
    try:
        yield ids
    finally:
        from app.models.scrape_runtime import ScrapeRuntimeJob

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(ScrapedCourse).where(ScrapedCourse.university_id.in_(ids))
            )
            await db.execute(
                delete(ScrapeRuntimeJob).where(ScrapeRuntimeJob.university_id.in_(ids))
            )
            await db.execute(delete(University).where(University.id.in_(ids)))
            await db.commit()


async def _insert(
    scrape_job_id: str,
    uni_id: int,
    name: str,
    status: str,
    age_min: int,
    *,
    course_website: str | None = None,
) -> int:
    """Insert one scraped_course row backdated by ``age_min`` minutes; return its id."""
    async with AsyncSessionLocal() as db:
        sc = ScrapedCourse(
            scrape_job_id=scrape_job_id,
            university_id=uni_id,
            course_name=name,
            status=status,
            course_website=course_website,
        )
        db.add(sc)
        await db.flush()
        # created_at has server_default=now(); override after insert so we can age the row.
        sc.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_min)
        await db.commit()
        return sc.id


async def _exists(row_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        return (await db.get(ScrapedCourse, row_id)) is not None


async def _cleanup(prefix: str) -> None:
    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as db:
        await db.execute(
            _text("DELETE FROM scraped_courses WHERE scrape_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_clear_stale_dedup_deletes_old_pending_keeps_recent_and_rejected(
    isolated_universities,
):
    uni_a, uni_b = isolated_universities
    prefix = f"test_stale_{uuid.uuid4().hex[:8]}_"
    try:
        # Setup: one of each row category we want to assert on.
        old_pending = await _insert(prefix + "op", uni_a, prefix + "old-pending", "pending", age_min=30)
        new_pending = await _insert(prefix + "np", uni_a, prefix + "new-pending", "pending", age_min=2)
        old_rejected = await _insert(prefix + "or", uni_a, prefix + "old-rejected", "rejected", age_min=30)
        old_pending_other_uni = await _insert(
            prefix + "ou", uni_b, prefix + "old-pending-other-uni", "pending", age_min=30
        )

        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)

        # Only the old pending row for uni_a is gone.
        assert cleared == 1, f"expected 1 deletion, got {cleared}"
        assert not await _exists(old_pending), "old pending row should be deleted"
        assert await _exists(new_pending), "recent pending row must NOT be deleted (mid-flight protection)"
        assert await _exists(old_rejected), (
            "old rejected row MUST NOT be deleted — preserves Bug #7 reviewer-decision lock"
        )
        assert await _exists(old_pending_other_uni), "rows for other universities must not be deleted"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_clear_stale_dedup_returns_zero_when_nothing_stale(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_stale_{uuid.uuid4().hex[:8]}_"
    try:
        # Only fresh rows — nothing should be cleared.
        await _insert(prefix + "f1", uni_a, prefix + "fresh-1", "pending", age_min=1)
        await _insert(prefix + "f2", uni_a, prefix + "fresh-2", "pending", age_min=5)
        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)
        assert cleared == 0
    finally:
        await _cleanup(prefix)


# ──────────────────────────────────────────────────────────────────
# PR-1.5 prod-regression coverage: counter-vs-rows mismatch
# Root cause was _clear_stale_dedup wiping pending rows from a
# previous *completed* job before scrape #2 staged anything.
# Job_440a0e26c6df reported imported=9 with COUNT(*)=0; this test
# locks in the contract that completed-job rows survive cleanup.
# ──────────────────────────────────────────────────────────────────


async def _insert_runtime_job(runtime_job_id: str, uni_id: int, status: str) -> None:
    """Create a row in scrape_runtime_jobs so the EXISTS subquery in
    _clear_stale_dedup can find it. status ∈ {'completed','running','failed'}."""
    from app.models.scrape_runtime import ScrapeRuntimeJob
    async with AsyncSessionLocal() as db:
        rj = ScrapeRuntimeJob(
            runtime_job_id=runtime_job_id,
            university_id=uni_id,
            job_type="full",
            status=status,
        )
        db.add(rj)
        await db.commit()


async def _delete_runtime_job(runtime_job_id: str) -> None:
    from sqlalchemy import text as _text
    async with AsyncSessionLocal() as db:
        await db.execute(
            _text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :rid"),
            {"rid": runtime_job_id},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_clear_stale_dedup_preserves_known_review_rows_and_clears_orphans(
    isolated_universities,
):
    """Age never deletes review owned by a known job; only orphans are stale."""
    uni_a, _ = isolated_universities
    prefix = f"test_completed_{uuid.uuid4().hex[:8]}_"
    completed_job = prefix + "completed_job"
    failed_job = prefix + "failed_job"
    orphan_job = prefix + "orphan_job"
    try:
        # Three old pending rows under different runtime-job statuses.
        await _insert_runtime_job(completed_job, uni_a, "completed")
        await _insert_runtime_job(failed_job, uni_a, "failed")
        # orphan_job intentionally has no scrape_runtime_jobs entry —
        # this is the "scraper crashed before flushing the job row" case.
        from_completed = await _insert(completed_job, uni_a, prefix + "from-completed", "pending", age_min=30)
        from_failed = await _insert(failed_job, uni_a, prefix + "from-failed", "pending", age_min=30)
        from_orphan = await _insert(orphan_job, uni_a, prefix + "from-orphan", "pending", age_min=30)

        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)

        assert cleared == 1, f"expected only the orphan deletion, got {cleared}"
        assert await _exists(from_completed), (
            "completed-job pending review must survive until an operator decides it"
        )
        assert await _exists(from_failed), "recent failed-job row is a resumable checkpoint"
        assert not await _exists(from_orphan), "orphan-job pending row should be cleared"
    finally:
        await _cleanup(prefix)
        for jid in (completed_job, failed_job):
            await _delete_runtime_job(jid)


@pytest.mark.asyncio
async def test_clear_stale_dedup_preserves_rows_from_running_jobs(
    isolated_universities,
):
    """Pending rows whose source job is RUNNING must survive too —
    a concurrent scrape is still actively writing them. Wiping
    them mid-flight would corrupt the in-flight job's output."""
    uni_a, _ = isolated_universities
    prefix = f"test_running_{uuid.uuid4().hex[:8]}_"
    running_job = prefix + "running_job"
    try:
        await _insert_runtime_job(running_job, uni_a, "running")
        # Backdate 30 min — would normally be cleared by the age check.
        from_running = await _insert(running_job, uni_a, prefix + "from-running", "pending", age_min=30)

        async with AsyncSessionLocal() as db:
            cleared = await _clear_stale_dedup(db, uni_a, minutes=10)

        assert cleared == 0, "running-job pending rows MUST NOT be cleared mid-flight"
        assert await _exists(from_running)
    finally:
        await _cleanup(prefix)
        await _delete_runtime_job(running_job)


@pytest.mark.asyncio
async def test_targeted_stage_replaces_selected_url_and_preserves_unrelated_pending_row(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_targeted_{uuid.uuid4().hex[:8]}_"
    selected_url = "https://example.edu/course/selected"
    unrelated_url = "https://example.edu/course/unrelated"
    course_name = f"Bachelor of Example {prefix}"
    old_job = prefix + "old_job"
    targeted_job = prefix + "targeted_job"

    try:
        selected_old = await _insert(
            old_job,
            uni_a,
            course_name,
            "pending",
            age_min=30,
            course_website=selected_url,
        )
        unrelated_pending = await _insert(
            old_job,
            uni_a,
            course_name,
            "pending",
            age_min=30,
            course_website=unrelated_url,
        )
        await _insert_runtime_job(targeted_job, uni_a, "running")

        payload = {
            "course_name": course_name,
            "degree_level": "Bachelor's",
            "study_mode": "On Campus",
            "course_location": "Test Campus",
            "international_fee": 45000,
            "course_website": selected_url,
        }
        evidence = [{
            "field_key": "international_fee",
            "value": 45000,
            "method": "fee:test",
            "confidence": 1.0,
            "source_url": selected_url,
            "snippet": "International tuition: $45,000",
        }]

        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=targeted_job,
                university_id=uni_a,
                course_name=course_name,
                payload=payload,
                evidence=evidence,
                source_url=selected_url,
                targeted_retry=True,
            )

        assert result.saved, result.reason
        assert not await _exists(selected_old), "the selected URL's stale row should be replaced"
        assert await _exists(unrelated_pending), (
            "a one-course retry must preserve pending rows at unrelated URLs"
        )
        async with AsyncSessionLocal() as db:
            replacement_ids = (
                await db.execute(
                    select(ScrapedCourse.id).where(
                        ScrapedCourse.scrape_job_id == targeted_job,
                        ScrapedCourse.course_website == selected_url,
                    )
                )
            ).scalars().all()
        assert replacement_ids == [result.scraped_course_id]
    finally:
        await _cleanup(prefix)
        await _delete_runtime_job(targeted_job)


@pytest.mark.asyncio
async def test_full_stage_preserves_distinct_same_title_course_at_different_url(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_full_distinct_{uuid.uuid4().hex[:8]}_"
    course_name = f"Master of Example {prefix}"
    old_url = "https://example.edu/course/example-city"
    new_url = "https://example.edu/course/example-harbour"
    old_row = await _insert(
        prefix + "old",
        uni_a,
        course_name,
        "pending",
        age_min=30,
        course_website=old_url,
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=prefix + "new",
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Master's",
                    "international_fee": 42000,
                    "course_website": new_url,
                },
                evidence=[],
                source_url=new_url,
            )

        assert result.saved, result.reason
        assert await _exists(old_row), (
            "same-title courses at distinct canonical URLs must both remain reviewable"
        )
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_full_stage_replaces_true_canonical_url_alias(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_full_alias_{uuid.uuid4().hex[:8]}_"
    course_name = f"Bachelor of Alias {prefix}"
    old_url = "http://www.example.edu/course/alias/?utm_source=catalogue"
    new_url = "https://example.edu/course/alias"
    old_row = await _insert(
        prefix + "old",
        uni_a,
        course_name,
        "pending",
        age_min=30,
        course_website=old_url,
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=prefix + "new",
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Bachelor's",
                    "international_fee": 39000,
                    "course_website": new_url,
                },
                evidence=[],
                source_url=new_url,
            )

        assert result.saved, result.reason
        assert not await _exists(old_row), "a true canonical URL alias should be replaced"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_full_stage_keeps_semantic_query_variants_and_reviewed_alias(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_full_semantic_{uuid.uuid4().hex[:8]}_"
    course_name = f"Bachelor of Query {prefix}"
    semantic_row = await _insert(
        prefix + "semantic",
        uni_a,
        course_name,
        "pending",
        age_min=30,
        course_website="https://example.edu/course/query?campus=city",
    )
    approved_alias = await _insert(
        prefix + "approved",
        uni_a,
        course_name,
        "approved",
        age_min=30,
        course_website="http://www.example.edu/course/query/?utm_source=old",
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=prefix + "new",
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Bachelor's",
                    "international_fee": 39000,
                    "course_website": "https://example.edu/course/query",
                },
                evidence=[],
                source_url="https://example.edu/course/query",
            )

        assert result.saved, result.reason
        assert await _exists(semantic_row), "semantic query parameters must remain distinct"
        assert await _exists(approved_alias), "approved aliases must never be deleted"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_within_job_stage_rejects_true_canonical_url_alias(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_same_job_alias_{uuid.uuid4().hex[:8]}_"
    scrape_job_id = prefix + "job"
    course_name = f"Bachelor of Same Job Alias {prefix}"
    first_url = "http://www.example.edu/course/same-job/?utm_source=catalogue"
    alias_url = "https://example.edu/course/same-job"
    try:
        first_row = await _insert(
            scrape_job_id,
            uni_a,
            course_name,
            "pending",
            age_min=0,
            course_website=first_url,
        )
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=scrape_job_id,
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Bachelor's",
                    "international_fee": 39000,
                    "course_website": alias_url,
                },
                evidence=[],
                source_url=alias_url,
            )

        assert not result.saved
        assert result.reason == "rejected: duplicate_url_in_job"
        assert await _exists(first_row), "the first same-job alias must remain"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_within_job_stage_keeps_semantic_query_variants(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_same_job_semantic_{uuid.uuid4().hex[:8]}_"
    scrape_job_id = prefix + "job"
    course_name = f"Bachelor of Same Job Query {prefix}"
    city_url = "https://example.edu/course/same-job-query?campus=city"
    harbour_url = "https://example.edu/course/same-job-query?campus=harbour"
    try:
        first_row = await _insert(
            scrape_job_id,
            uni_a,
            course_name,
            "pending",
            age_min=0,
            course_website=city_url,
        )
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=scrape_job_id,
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Bachelor's",
                    "international_fee": 39000,
                    "course_website": harbour_url,
                },
                evidence=[],
                source_url=harbour_url,
            )

        assert result.saved, result.reason
        assert await _exists(first_row), "semantic same-job variants must remain distinct"
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_concurrent_within_job_aliases_create_one_review_row(
    isolated_universities,
):
    uni_a, _ = isolated_universities
    prefix = f"test_concurrent_alias_{uuid.uuid4().hex[:8]}_"
    scrape_job_id = prefix + "job"
    course_name = f"Bachelor of Concurrent Alias {prefix}"
    urls = (
        "http://www.example.edu/course/concurrent/?utm_source=catalogue",
        "https://example.edu/course/concurrent",
    )
    reset_run_event_metrics()

    async def stage(url: str):
        async with AsyncSessionLocal() as db:
            return await stage_course(
                db,
                scrape_job_id=scrape_job_id,
                university_id=uni_a,
                course_name=course_name,
                payload={
                    "course_name": course_name,
                    "degree_level": "Bachelor's",
                    "international_fee": 39000,
                    "course_website": url,
                },
                evidence=[],
                source_url=url,
            )

    try:
        results = await asyncio.gather(*(stage(url) for url in urls))
        assert sum(result.saved for result in results) == 1
        assert sorted(result.reason for result in results) == [
            "rejected: duplicate_url_in_job",
            "staged",
        ]

        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ScrapedCourse).where(
                        ScrapedCourse.university_id == uni_a,
                        ScrapedCourse.scrape_job_id == scrape_job_id,
                    )
                )
            ).scalars().all()
        assert len(rows) == 1
        assert get_run_event_metrics() == {"duplicate_alias_collisions": 1}
    finally:
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_review_url_unique_index_preserves_cross_job_lookup_prefix():
    async with AsyncSessionLocal() as db:
        index_definition = (
            await db.execute(
                text(
                    """
                    SELECT indexdef
                    FROM pg_indexes
                    WHERE tablename = 'scraped_courses'
                      AND indexname = 'uq_scraped_courses_job_review_url_identity'
                    """
                )
            )
        ).scalar_one()

    normalized = " ".join(index_definition.split())
    assert "UNIQUE INDEX" in normalized
    assert (
        "(university_id, canonical_course_url, scrape_job_id)" in normalized
    ), "canonical URL must precede scrape job so cross-job alias cleanup stays index-backed"
    assert "status <> ALL" in normalized or "status NOT IN" in normalized


def test_alias_dedup_uses_indexed_identity_without_queue_scan():
    source = __import__(
        "inspect"
    ).getsource(__import__(
        "app.services.scraper.stage_course",
        fromlist=["stage_course"],
    ).stage_course)

    assert "ScrapedCourse.canonical_course_url == _source_url_key" in source
    assert "select(ScrapedCourse.id, ScrapedCourse.course_website)" not in source
    assert "canonical_course_url_key(row[1])" not in source

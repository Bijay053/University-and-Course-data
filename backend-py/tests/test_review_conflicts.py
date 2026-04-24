"""Tests for app.services.review.conflicts.

Pattern matches test_stage_evidence_and_review.py — open a real
``AsyncSessionLocal`` against the test DB, seed an isolated staged course
plus its evidence rows, run the detector, then clean up via the cascade
on ``DELETE FROM scraped_courses``. Skips when no university exists in
the DB so the suite stays runnable on a freshly-provisioned instance.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.models import (
    FieldConflict,
    ScrapedCourse,
    ScrapedFieldEvidence,
    University,
)
from app.services.review.conflicts import detect_and_persist_conflicts


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _pick_university() -> int:
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(select(University.id).order_by(University.id).limit(1))
        ).first()
    if not row:
        pytest.skip("need at least one university in the DB to run integration test")
    return row[0]


async def _cleanup(prefix: str) -> None:
    # FieldConflict cascades from scraped_course; deleting the parent is enough.
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scraped_courses WHERE scrape_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


def _ev(
    *,
    sc_id: int,
    field: str,
    value: str,
    method: str = "regex",
    page_type: str = "course_page",
    confidence: float | None = 0.8,
) -> ScrapedFieldEvidence:
    return ScrapedFieldEvidence(
        scraped_course_id=sc_id,
        field_key=field,
        candidate_value=value,
        normalized_value=value,
        page_type=page_type,
        extraction_method=method,
        confidence=confidence,
    )


async def _seed_staged_course(db, job_id: str, uni_id: int) -> int:
    sc = ScrapedCourse(
        scrape_job_id=job_id,
        university_id=uni_id,
        course_name="Master of Test Conflict",
        status="pending",
    )
    db.add(sc)
    await db.flush()
    return sc.id


@pytest.mark.asyncio
async def test_detects_two_course_page_disagreement():
    uni_id = await _pick_university()
    job_id = f"test_conflict_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_id = await _seed_staged_course(db, job_id, uni_id)
            db.add_all(
                [
                    _ev(sc_id=sc_id, field="ielts_overall", value="6.0", method="regex"),
                    _ev(sc_id=sc_id, field="ielts_overall", value="6.5", method="ai"),
                ]
            )
            await db.flush()
            written = await detect_and_persist_conflicts(db, sc_id)
            await db.commit()

        assert written == 1
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(FieldConflict).where(
                        FieldConflict.scraped_course_id == sc_id
                    )
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].field_key == "ielts_overall"
        assert rows[0].conflict_type == "source_mismatch"
        assert {rows[0].value_a, rows[0].value_b} == {"6.0", "6.5"}
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_no_conflict_when_values_match():
    uni_id = await _pick_university()
    job_id = f"test_conflict_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_id = await _seed_staged_course(db, job_id, uni_id)
            db.add_all(
                [
                    _ev(sc_id=sc_id, field="ielts_overall", value="6.5"),
                    _ev(sc_id=sc_id, field="ielts_overall", value="6.5", method="ai"),
                ]
            )
            await db.flush()
            written = await detect_and_persist_conflicts(db, sc_id)
            await db.commit()
        assert written == 0
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_no_conflict_across_tiers():
    # course_page (primary) vs uni_pdf (fallback) — different tiers.
    uni_id = await _pick_university()
    job_id = f"test_conflict_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_id = await _seed_staged_course(db, job_id, uni_id)
            db.add_all(
                [
                    _ev(sc_id=sc_id, field="international_fee",
                        value="35000", page_type="course_page"),
                    _ev(sc_id=sc_id, field="international_fee",
                        value="42000", page_type="uni_pdf"),
                ]
            )
            await db.flush()
            written = await detect_and_persist_conflicts(db, sc_id)
            await db.commit()
        assert written == 0
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_dedupes_repeated_value_pairs():
    # Five evidence rows with only two distinct values → exactly 1 conflict.
    uni_id = await _pick_university()
    job_id = f"test_conflict_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_id = await _seed_staged_course(db, job_id, uni_id)
            db.add_all(
                [
                    _ev(sc_id=sc_id, field="duration", value="12 months", confidence=0.9),
                    _ev(sc_id=sc_id, field="duration", value="12 months", confidence=0.7),
                    _ev(sc_id=sc_id, field="duration", value="24 months", confidence=0.8),
                    _ev(sc_id=sc_id, field="duration", value="24 months", confidence=0.6),
                    _ev(sc_id=sc_id, field="duration", value="12 months", confidence=0.5),
                ]
            )
            await db.flush()
            written = await detect_and_persist_conflicts(db, sc_id)
            await db.commit()
        assert written == 1
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_idempotent_rerun_clears_open_conflicts():
    uni_id = await _pick_university()
    job_id = f"test_conflict_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_id = await _seed_staged_course(db, job_id, uni_id)
            db.add_all(
                [
                    _ev(sc_id=sc_id, field="ielts_overall", value="6.0"),
                    _ev(sc_id=sc_id, field="ielts_overall", value="7.0", method="ai"),
                ]
            )
            await db.flush()
            await detect_and_persist_conflicts(db, sc_id)
            await detect_and_persist_conflicts(db, sc_id)
            await db.commit()

        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(FieldConflict).where(
                        FieldConflict.scraped_course_id == sc_id,
                        FieldConflict.status == "open",
                    )
                )
            ).scalars().all()
        # Still exactly one — second call deleted the first row before re-writing.
        assert len(rows) == 1
    finally:
        await _cleanup(job_id)

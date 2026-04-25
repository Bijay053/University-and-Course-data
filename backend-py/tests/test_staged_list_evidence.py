"""Tests for `_attach_evidence_bulk` — the helper that surfaces
per-course evidence rows on the staged-list endpoints so the React
EvidencePanel can render finding sources.

Bug: the Python staged-list endpoints returned course rows without
``evidence``, so the UI's `course.evidence?.length` was always 0 and
the "Sources" toggle stayed disabled. Node had this; Python rewrite
missed it.

Pattern mirrors test_review_conflicts.py — open AsyncSessionLocal
against the test DB, seed an isolated staged course with evidence,
call the helper, assert shape, clean up via cascade on
``DELETE FROM scraped_courses``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.models import ScrapedCourse, ScrapedFieldEvidence, University
from app.routers.scrape import _attach_evidence_bulk, _staged_row_to_dict


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
        pytest.skip("need at least one university in the DB")
    return row[0]


async def _cleanup(prefix: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scraped_courses WHERE scrape_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_attach_evidence_bulk_populates_camelcase_fields():
    uni_id = await _pick_university()
    job_id = f"test_evbulk_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc = ScrapedCourse(
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Bachelor of Evidence Bulk",
                status="pending",
            )
            db.add(sc)
            await db.flush()
            db.add_all(
                [
                    ScrapedFieldEvidence(
                        scraped_course_id=sc.id,
                        field_key="ielts_overall",
                        candidate_value="6.5",
                        normalized_value="6.5",
                        page_type="course_page",
                        extraction_method="regex",
                        source_url="https://example.edu/course",
                        snippet="IELTS overall 6.5",
                        confidence=0.9,
                        decision_score=0.85,
                        validation_status="ok",
                        decision_status="selected",
                        selected=True,
                    ),
                    ScrapedFieldEvidence(
                        scraped_course_id=sc.id,
                        field_key="ielts_overall",
                        candidate_value="6.0",
                        page_type="uni_pdf",
                        extraction_method="ai",
                        confidence=0.55,
                    ),
                ]
            )
            await db.commit()

            row = (await db.execute(
                select(ScrapedCourse).where(ScrapedCourse.id == sc.id)
            )).scalars().one()
            dicts = [_staged_row_to_dict(row)]

            assert dicts[0]["evidence"] == []  # default before bulk-load

            await _attach_evidence_bulk(db, dicts)

        evidence = dicts[0]["evidence"]
        assert len(evidence) == 2

        # Highest confidence first per the ORDER BY clause.
        first = evidence[0]
        assert first["fieldKey"] == "ielts_overall"
        assert first["candidateValue"] == "6.5"
        assert first["normalizedValue"] == "6.5"
        assert first["sourceUrl"] == "https://example.edu/course"
        assert first["pageType"] == "course_page"
        assert first["extractionMethod"] == "regex"
        assert first["confidence"] == pytest.approx(0.9)
        assert first["decisionScore"] == pytest.approx(0.85)
        assert first["validationStatus"] == "ok"
        assert first["decisionStatus"] == "selected"
        assert first["selected"] is True
        # snake_case kept for any Python consumer that wants both shapes.
        assert first["field_key"] == "ielts_overall"
        # ISO-stringified timestamp so JSON encoding never breaks.
        assert isinstance(first["created_at"], str)

        second = evidence[1]
        assert second["candidateValue"] == "6.0"
        assert second["pageType"] == "uni_pdf"
        assert second["sourceUrl"] is None  # nullable column survives
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_attach_evidence_bulk_isolates_per_course():
    """Two staged courses, only one has evidence — the other must
    receive an empty list, not the sibling's rows."""
    uni_id = await _pick_university()
    job_id = f"test_evbulk_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc_a = ScrapedCourse(
                scrape_job_id=job_id, university_id=uni_id,
                course_name="A", status="pending",
            )
            sc_b = ScrapedCourse(
                scrape_job_id=job_id, university_id=uni_id,
                course_name="B", status="pending",
            )
            db.add_all([sc_a, sc_b])
            await db.flush()
            db.add(ScrapedFieldEvidence(
                scraped_course_id=sc_a.id,
                field_key="pte_overall",
                candidate_value="58",
                page_type="course_page",
                extraction_method="regex",
                confidence=0.8,
            ))
            await db.commit()

            rows = (await db.execute(
                select(ScrapedCourse)
                .where(ScrapedCourse.scrape_job_id == job_id)
                .order_by(ScrapedCourse.id)
            )).scalars().all()
            dicts = [_staged_row_to_dict(r) for r in rows]
            await _attach_evidence_bulk(db, dicts)

        by_name = {d["courseName"]: d for d in dicts}
        assert len(by_name["A"]["evidence"]) == 1
        assert by_name["A"]["evidence"][0]["fieldKey"] == "pte_overall"
        assert by_name["B"]["evidence"] == []
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_attach_evidence_bulk_empty_input_noop():
    async with AsyncSessionLocal() as db:
        # Both branches are no-ops: empty list, and dicts with no `id`.
        await _attach_evidence_bulk(db, [])
        bare = [{"foo": "bar"}]
        await _attach_evidence_bulk(db, bare)
        assert bare == [{"foo": "bar"}]


@pytest.mark.asyncio
async def test_staged_row_to_dict_seeds_empty_evidence_field():
    """Default `evidence: []` so UI's optional chain returns 0 not undefined,
    even when the bulk loader is skipped (e.g. by a future caller)."""
    uni_id = await _pick_university()
    job_id = f"test_evbulk_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            sc = ScrapedCourse(
                scrape_job_id=job_id, university_id=uni_id,
                course_name="Default Evidence", status="pending",
            )
            db.add(sc)
            await db.commit()
            row = (await db.execute(
                select(ScrapedCourse).where(ScrapedCourse.id == sc.id)
            )).scalars().one()
        d = _staged_row_to_dict(row)
        assert d["evidence"] == []
        assert isinstance(d["evidence"], list)
    finally:
        await _cleanup(job_id)

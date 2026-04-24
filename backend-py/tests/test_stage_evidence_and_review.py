"""Bug C + Bug D integration test.

Verifies, end-to-end against the real DB:
  * stage_course writes per-field evidence rows (Bug D root cause).
  * stage_course populates completeness, eligibility_status,
    eligibility_reason, auto_publish_status, decision_score
    (Bug C root cause).
  * The /staged/{id}/review endpoint returns those evidence rows so the
    Evidence Review modal renders them.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models import ScrapedCourse, ScrapedFieldEvidence, University
from app.services.scraper.stage_course import stage_course


@pytest.fixture(autouse=True)
async def _dispose_engine_per_test():
    await engine.dispose()
    yield
    await engine.dispose()


async def _pick_university() -> int:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(University.id).order_by(University.id).limit(1))).first()
    if not row:
        pytest.skip("need at least one university in the DB to run integration test")
    return row[0]


async def _cleanup(prefix: str) -> None:
    async with AsyncSessionLocal() as db:
        # Evidence rows cascade-delete with the parent scraped_course.
        await db.execute(
            text("DELETE FROM scraped_courses WHERE scrape_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_stage_course_persists_completeness_and_evidence():
    uni_id = await _pick_university()
    job_id = f"test_bugcd_{uuid.uuid4().hex[:10]}"
    try:
        evidence = [
            {
                "field_key": "course_name",
                "value": "Bachelor of Computer Science",
                "method": "course_name:h1",
                "confidence": 0.95,
                "snippet": "<h1>Bachelor of Computer Science</h1>",
            },
            {
                "field_key": "degree_level",
                "value": "Bachelor's",
                "normalized": {"degree_level": "Bachelor's"},
                "method": "degree_level:name",
                "confidence": 0.9,
            },
            {
                "field_key": "study_mode",
                "value": "On Campus",
                "method": "study_mode:rule",
                "confidence": 0.7,
            },
        ]
        payload = {
            "course_name": "Bachelor of Computer Science",
            "degree_level": "Bachelor's",
            "category": "Computer Science & IT",
            "study_mode": "On Campus",
            "course_location": "Sydney",
            "duration": 3.0,  # FLOAT column — years as numeric
            "intake_months": ["February", "July"],
            "international_fee": 45000,
            "description": "A great course.",
            "academic_level": "Year 12",
            "academic_score": 85,
            "ielts_overall": 6.5,
            "other_requirement": "Personal statement",
            "course_website": "https://example.edu/cs",
        }
        async with AsyncSessionLocal() as db:
            res = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name=payload["course_name"],
                payload=payload,
                evidence=evidence,
                source_url=payload["course_website"],
            )
        assert res.saved, res.reason
        sc_id = res.scraped_course_id
        assert sc_id is not None

        # ----- Bug C assertions: scoring + auto_publish populated -----
        async with AsyncSessionLocal() as db:
            sc = await db.get(ScrapedCourse, sc_id)
            assert sc is not None
            assert sc.completeness == 100
            assert sc.degree_level == "Bachelor's"
            assert sc.study_mode == "On Campus"
            assert sc.category == "Computer Science & IT"
            assert sc.eligibility_status == "ready"
            assert sc.auto_publish_status == "ready"

        # ----- Bug D assertions: evidence rows exist -----
        async with AsyncSessionLocal() as db:
            ev_rows = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id
                    )
                )
            ).scalars().all()
            assert len(ev_rows) == 3
            keys = {r.field_key for r in ev_rows}
            assert keys == {"course_name", "degree_level", "study_mode"}
            for r in ev_rows:
                # Defaults must land for the operator-decision columns.
                assert r.validation_status == "pending"
                assert r.decision_status == "needs_review"
                assert r.selected is False
                assert r.source_url == "https://example.edu/cs"

        # ----- /staged/{id}/review returns evidence + eligibility -----
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/scrape/staged/{sc_id}/review")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["completeness"] == 100
        assert body["eligibilityStatus"] == "ready"
        assert body["autoPublishStatus"] == "ready"
        assert isinstance(body["evidence"], list)
        assert len(body["evidence"]) == 3
        # Per-field grouping must include each key we wrote.
        assert set(body["evidenceByField"].keys()) == {
            "course_name",
            "degree_level",
            "study_mode",
        }
        # camelCase aliases the React UI expects.
        sample = body["evidence"][0]
        for k in ("fieldKey", "candidateValue", "extractionMethod", "sourceUrl"):
            assert k in sample
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_stage_course_review_status_when_blockers_present():
    """A course missing degree_level + english test must land as 'review'
    with auto_publish_status='review' and a human-readable reason."""
    uni_id = await _pick_university()
    job_id = f"test_bugcd_blk_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            res = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Foundation Pathway Program",
                payload={"course_name": "Foundation Pathway Program"},
                evidence=[],
            )
        assert res.saved
        async with AsyncSessionLocal() as db:
            sc = await db.get(ScrapedCourse, res.scraped_course_id)
            assert sc.eligibility_status == "review"
            assert sc.auto_publish_status == "review"
            assert sc.eligibility_reason and "Needs review" in sc.eligibility_reason
            # Both hard blockers should be named so the modal can show them.
            assert "degreeLevel" in sc.eligibility_reason
            assert "englishTest" in sc.eligibility_reason
    finally:
        await _cleanup(job_id)

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
from sqlalchemy import delete, select, text

from app.database import AsyncSessionLocal, engine
from app.main import app
from app.models import ScrapedCourse, ScrapedFieldEvidence, University
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.scraper.snapshot_save import staged_row_backup_payload
from app.services.scraper.stage_course import stage_course
from app.services.scraper.replay_extraction import restore_review_rows


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
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        await db.commit()


@pytest.mark.asyncio
async def test_stage_course_persists_completeness_and_evidence():
    uni_id = await _pick_university()
    job_id = f"test_bugcd_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=job_id,
                    university_id=uni_id,
                    university_name="Snapshot Integration Test",
                    url="https://example.edu/cs",
                    job_type="scrape",
                    status="running",
                    request_payload={},
                )
            )
            await db.commit()
        evidence = [
            {
                "field_key": "course_name",
                "value": "Bachelor of Computer Science",
                "method": "course_name:h1",
                "confidence": 0.95,
                "source_url": "https://example.edu/cs",
                "snippet": "<h1>Bachelor of Computer Science</h1>",
            },
            {
                "field_key": "degree_level",
                "value": "Bachelor's",
                "normalized": {"degree_level": "Bachelor's"},
                "method": "degree_level:name",
                "confidence": 0.9,
                "source_url": "https://example.edu/cs",
                "snippet": "Bachelor of Computer Science",
            },
            {
                "field_key": "study_mode",
                "value": "On Campus",
                "method": "study_mode:rule",
                "confidence": 0.7,
                "source_url": "https://example.edu/cs",
                "snippet": "Delivery: On Campus",
            },
            {
                "field_key": "international_fee",
                "value": 45000,
                "method": "fee:table",
                "confidence": 0.85,
                "source_url": "https://example.edu/cs",
                "snippet": "International tuition: A$45,000",
            },
            {
                "field_key": "ielts_overall",
                "value": 6.5,
                "method": "english:table",
                "confidence": 0.9,
                "source_url": "https://example.edu/cs",
                "snippet": "IELTS overall: 6.5",
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
            assert sc.sub_category == "Computer Science"
            assert sc.eligibility_status == "ready"
            assert sc.auto_publish_status == "ready"
            snapshot = (
                await db.execute(
                    select(PageSnapshot).where(
                        PageSnapshot.scrape_job_id == job_id,
                        PageSnapshot.snapshot_type == "staged_row",
                    )
                )
            ).scalar_one()
            assert snapshot.storage_path is None
            expected_backup = staged_row_backup_payload(sc)
            assert snapshot.original_extraction == expected_backup

        # ----- Bug D assertions: evidence rows exist -----
        async with AsyncSessionLocal() as db:
            ev_rows = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id
                    )
                )
            ).scalars().all()
            assert len(ev_rows) == 6
            keys = {r.field_key for r in ev_rows}
            assert keys == {
                "course_name",
                "degree_level",
                "study_mode",
                "international_fee",
                "ielts_overall",
                "sub_category",
            }
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
        assert len(body["evidence"]) == 6
        # Per-field grouping must include each key we wrote.
        assert set(body["evidenceByField"].keys()) == {
            "course_name",
            "degree_level",
            "study_mode",
            "international_fee",
            "ielts_overall",
            "sub_category",
        }
        # camelCase aliases the React UI expects.
        sample = body["evidence"][0]
        for k in ("fieldKey", "candidateValue", "extractionMethod", "sourceUrl"):
            assert k in sample

        # Bug F: the modal destructures `course` (camelCase StagedCourse
        # shape) and `conflicts` (array). When either is undefined the
        # React tree throws on `reviewDetail.conflicts.length`. Pin both.
        assert isinstance(body.get("conflicts"), list)
        assert "course" in body and isinstance(body["course"], dict)
        course = body["course"]
        assert course["courseName"] == "Bachelor of Computer Science"
        # Spot-check that camelCase, not snake_case, made it into `course`.
        assert "internationalFee" in course
        assert "ieltsOverall" in course
        assert "autoPublishStatus" in course
        # Snake_case keys must NOT leak into `course`.
        assert "course_name" not in course
        assert "auto_publish_status" not in course

        # Delete the live review row, then reconstruct it exclusively from the
        # DB-only final staged-row backup. The restored persisted values and
        # source-job linkage must match the original row exactly.
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ScrapedCourse).where(ScrapedCourse.id == sc_id))
            await db.commit()
            restore_result = await restore_review_rows(job_id, commit=True, db=db)
            assert restore_result["restored"] == 1
            restored = (
                await db.execute(
                    select(ScrapedCourse).where(
                        ScrapedCourse.scrape_job_id == job_id,
                        ScrapedCourse.course_website == payload["course_website"],
                    )
                )
            ).scalar_one()
            assert restored.status == "pending"
            assert staged_row_backup_payload(restored) == expected_backup
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_stage_course_review_status_when_blockers_present():
    """A course missing its English test must land as 'review'
    with auto_publish_status='review' and a human-readable reason.

    The staging gate requires (a) a degree-qualified name and (b) an
    international_fee before a row can be staged.  Both are supplied here
    so the gate passes and the completeness / eligibility step runs.
    Degree level is derived from the qualified course name, while the missing
    English-test field remains a hard blocker and forces review status.
    """
    uni_id = await _pick_university()
    job_id = f"test_bugcd_blk_{uuid.uuid4().hex[:10]}"
    try:
        async with AsyncSessionLocal() as db:
            res = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                # "Master of Science" passes the degree-qualifier name gate.
                # Degree level is derived; no English-test field → blocker fires.
                course_name="Master of Science",
                payload={
                    "course_name": "Master of Science",
                    "international_fee": 25000,   # satisfies the fee gate
                },
                evidence=[],
            )
        assert res.saved
        async with AsyncSessionLocal() as db:
            sc = await db.get(ScrapedCourse, res.scraped_course_id)
            assert sc.eligibility_status == "review"
            assert sc.auto_publish_status == "review"
            # T205: reason follows Node's buildReviewNotes shape:
            #   "Publish blocked: <blockers> | Missing: <missing>
            #    | Warnings: <warnings>"
            assert sc.eligibility_reason and sc.eligibility_reason.startswith(
                "Publish blocked: "
            )
            assert "degreeLevel" not in sc.eligibility_reason
            assert "englishTest" in sc.eligibility_reason
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_re_extract_staged_refreshes_changed_fee_evidence(monkeypatch):
    uni_id = await _pick_university()
    job_id = f"test_reextract_ev_{uuid.uuid4().hex[:10]}"
    old_url = "https://example.edu/courses/2025/computer-science"
    new_url = "https://example.edu/courses/computer-science?year=2026"
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=job_id,
                    university_id=uni_id,
                    university_name="Re-extract Evidence Test",
                    url=old_url,
                    job_type="scrape",
                    status="running",
                    request_payload={},
                )
            )
            await db.commit()
            staged = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Bachelor of Computer Science",
                payload={
                    "course_name": "Bachelor of Computer Science",
                    "international_fee": 41000,
                    "fee_year": 2025,
                    "course_website": old_url,
                },
                evidence=[
                    {
                        "field_key": "international_fee",
                        "value": 41000,
                        "method": "fee:table",
                        "source_url": old_url,
                        "snippet": "2025 international fee: A$41,000",
                        "decision_status": "selected",
                    },
                    {
                        "field_key": "fee_year",
                        "value": 2025,
                        "method": "fee:table",
                        "source_url": old_url,
                        "snippet": "Fees for 2025",
                        "decision_status": "selected",
                    },
                ],
                source_url=old_url,
            )
        assert staged.saved
        sc_id = staged.scraped_course_id
        assert sc_id is not None

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": new_url,
                "payload": {
                    "international_fee": 45000,
                    "fee_year": 2026,
                    "course_website": new_url,
                },
                "evidence": [
                    {
                        "field_key": "international_fee",
                        "value": 45000,
                        "normalized": 45000,
                        "method": "fee:canonical-table",
                        "source_url": new_url,
                        "snippet": "2026 international tuition fee: A$45,000",
                        "decision_status": "selected",
                    },
                    {
                        "field_key": "fee_year",
                        "value": 2026,
                        "normalized": 2026,
                        "method": "fee:canonical-table",
                        "source_url": new_url,
                        "snippet": "Fees shown are for 2026",
                        "decision_status": "selected",
                    },
                ],
            }

        monkeypatch.setattr(
            "app.services.scraper.orchestrator._extract_only",
            _fake_extract_only,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/scrape/staged/re-extract",
                json={"ids": [sc_id], "universityId": uni_id},
            )
        assert response.status_code == 200, response.text
        assert response.json()["updated"] == 1

        async with AsyncSessionLocal() as db:
            course = await db.get(ScrapedCourse, sc_id)
            assert course is not None
            assert course.international_fee == 45000
            assert course.fee_year == 2026
            assert course.course_website == new_url
            fee_evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                        ScrapedFieldEvidence.field_key.in_(
                            {"international_fee", "fee_year"}
                        ),
                    )
                )
            ).scalars().all()

        assert len(fee_evidence) == 2
        assert all(ev.selected for ev in fee_evidence)
        assert all(ev.decision_status == "selected" for ev in fee_evidence)
        assert all(ev.source_url == new_url for ev in fee_evidence)
        by_field = {ev.field_key: ev for ev in fee_evidence}
        assert by_field["international_fee"].candidate_value == "45000"
        assert by_field["international_fee"].snippet == (
            "2026 international tuition fee: A$45,000"
        )
        assert by_field["fee_year"].candidate_value == "2026"
        assert by_field["fee_year"].snippet == "Fees shown are for 2026"
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_re_extract_refreshes_unchanged_fee_from_newer_canonical_page(monkeypatch):
    uni_id = await _pick_university()
    job_id = f"test_reextract_same_ev_{uuid.uuid4().hex[:10]}"
    old_url = "https://example.edu/courses/2025/computer-science"
    new_url = "https://example.edu/courses/2026/computer-science"
    try:
        async with AsyncSessionLocal() as db:
            staged = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Bachelor of Computer Science",
                payload={
                    "course_name": "Bachelor of Computer Science",
                    "international_fee": 41000,
                    "fee_year": 2026,
                    "course_website": old_url,
                },
                evidence=[{
                    "field_key": "international_fee",
                    "value": 41000,
                    "normalized": 41000,
                    "method": "fee:table",
                    "source_url": old_url,
                    "snippet": "2025 international fee: A$41,000",
                    "decision_status": "selected",
                }],
                source_url=old_url,
            )
            assert staged.saved
            sc_id = staged.scraped_course_id
            old_evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                        ScrapedFieldEvidence.field_key == "international_fee",
                    )
                )
            ).scalar_one()
            old_evidence.validation_status = "ok"
            await db.commit()

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": new_url,
                "payload": {"international_fee": 41000},
                "evidence": [{
                    "field_key": "international_fee",
                    "value": 41000,
                    "normalized": 41000,
                    "method": "fee:canonical-table",
                    "source_url": new_url,
                    "snippet": "2026 international fee remains A$41,000",
                    "decision_status": "selected",
                }],
            }

        monkeypatch.setattr(
            "app.services.scraper.orchestrator._extract_only",
            _fake_extract_only,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/scrape/staged/re-extract",
                json={"ids": [sc_id], "universityId": uni_id},
            )

        assert response.status_code == 200, response.text
        result = response.json()["results"][0]
        assert result["updated_fields"] == []
        assert result["refreshed_evidence_fields"] == ["international_fee"]

        async with AsyncSessionLocal() as db:
            course = await db.get(ScrapedCourse, sc_id)
            assert course.international_fee == 41000
            fee_evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                        ScrapedFieldEvidence.field_key == "international_fee",
                    )
                )
            ).scalar_one()

        assert fee_evidence.source_url == new_url
        assert fee_evidence.extraction_method == "fee:canonical-table"
        assert fee_evidence.snippet == "2026 international fee remains A$41,000"
        assert fee_evidence.validation_status == "ok"
        assert fee_evidence.selected is True
    finally:
        await _cleanup(job_id)

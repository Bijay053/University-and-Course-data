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
from app.routers.scrape import _filter_resolved_reextract_warnings
from app.services.scraper.snapshot_save import staged_row_backup_payload
from app.services.scraper.stage_course import stage_course
from app.services.scraper.replay_extraction import restore_review_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
async def test_ulaw_normal_targeted_reextract_persists_course_owned_range(monkeypatch, smart):
    """Run the real extractor, staging and normal re-extract persistence path."""
    import os
    from pathlib import Path
    from app.routers.scrape import ReExtractBody, re_extract_staged
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    uni_id = await _pick_university()
    job_id = f"test_ulaw_range_{uuid.uuid4().hex[:10]}"
    url = "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/"
    html = """<html><main><h1>MSc Healthcare Management</h1>
    <a role="tab" href="#f">International Students</a><div id="f"><table>
    <tr><td>2026/27 Course Fees</td><td></td></tr>
    <tr><td>London</td><td>£19,050 (or £16,050 including a £3,000 bursary)</td></tr>
    <tr><td>Outside London</td><td>£17,500</td></tr></table></div></main></html>"""
    # Optional replay of an unmodified current official Healthcare page. The
    # ordinary suite remains deterministic/offline; acceptance uses the exact
    # same full pipeline and DB assertions, not a separate parser-only harness.
    if live_html_path := os.environ.get("ULAW_LIVE_HTML_FILE"):
        html = Path(live_html_path).read_text()
    cfg = load_uni_config(slug="law_1902", name="University of Law",
                          scrape_url="https://www.law.ac.uk/study/", create_missing_stub=False)

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    async def full_extract(link, **kwargs):
        token = current_uni_config.set(cfg)
        try:
            return await extract_course(link["url"], html=html, country="United Kingdom", use_ai_fallback=False)
        finally:
            current_uni_config.reset(token)

    async def no_central(*args, **kwargs):
        return {}

    async def no_fee_shortcut(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.scraper.extractors.ulaw_fees.recover_course_fee_only", no_fee_shortcut)
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    monkeypatch.setattr("app.services.scraper.orchestrator._extract_only", full_extract)
    monkeypatch.setattr("app.services.scraper.central_pages.prefetch_central_pages", no_central)
    try:
        fresh = await full_extract({"url": url})
        async with AsyncSessionLocal() as db:
            db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni_id, job_type="scrape", status="completed"))
            await db.flush()
            token = current_uni_config.set(cfg)
            try:
                staged = await stage_course(
                    db, scrape_job_id=job_id, university_id=uni_id,
                    course_name="MSc Healthcare Management", source_url=url,
                    payload=fresh["payload"], evidence=fresh["evidence"],
                )
            finally:
                current_uni_config.reset(token)
            assert staged.saved, staged.reason
            await db.commit()
            row = await db.get(ScrapedCourse, staged.scraped_course_id)
            assert row.extraction_method["fee_variants"]["status"] == "range"
            # Legacy contaminated scalar and companions must be removed, while
            # a targeted fee request cannot modify unrelated reviewer values.
            row.international_fee = 16900
            row.fee_term = "Annual"
            row.fee_year = 2027
            row.currency = "AUD"
            row.course_location = "Reviewer verified campus"
            row.ielts_overall = 7.5
            row.extraction_method = {"ielts_overall": "manual"}
            await db.commit()
            result = await re_extract_staged(ReExtractBody(
                ids=[row.id], universityId=uni_id,
                smart=smart,
                targetFields=["international_fee"], forceFields=["international_fee"],
                forceReasons={"international_fee": "Official course international tab contradicts legacy domestic amount"},
            ), db)
            assert result["errors"] == 0, result
            await db.refresh(row)
            assert row.international_fee is None
            assert row.fee_term == "Full Course"
            assert row.fee_year == 2026
            assert row.currency == "GBP"
            assert row.extraction_method["fee_variants"]["status"] == "range"
            assert row.extraction_method["ielts_overall"] == "manual"
            assert row.course_location == "Reviewer verified campus"
            assert row.ielts_overall == 7.5
            assert row.status not in {"approved", "published"}
            assert result["results"][0]["made_progress"] is True
            evidence = (await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == row.id,
                ScrapedFieldEvidence.field_key == "international_fee",
            ))).scalars().all()
            assert any("19,050" in (item.snippet or "") for item in evidence)
            from app.routers.scrape import analyze_staged, _staged_row_to_dict
            from app.services.auto_publish import should_auto_publish
            from app.services.scraper.data_quality import _check_course
            from app.services.scraper.smart_fix import run_smart_batch

            analysis = await analyze_staged(ReExtractBody(ids=[row.id], universityId=uni_id), db)
            assert not any(item["field"] == "international_fee" for item in analysis["issues"])
            serialized = _staged_row_to_dict(row)
            assert serialized["extractionMethod"]["fee_variants"]["status"] == "range"
            assert serialized["extraction_method"] == serialized["extractionMethod"]
            quality = _check_course(serialized, url)
            codes = {item.code for item in quality}
            assert "international_fee_campus_review" in codes
            assert "missing_international_fee" not in codes
            row.completeness = 100
            decision = should_auto_publish(row)
            assert not decision.auto_publish
            assert "campus" in decision.reason.lower()

            # The actual SmartFix analyzer→normal recovery→analyzer round-trip,
            # not merely the endpoint's internal made_progress flag.
            row.extraction_method = {"ielts_overall": "manual"}
            await db.commit()
            smart_result = await run_smart_batch(ReExtractBody(
                ids=[row.id], universityId=uni_id, smart=True,
                targetFields=["international_fee"],
            ), db)
            item = smart_result["results"][0]
            assert item["resolved_fields"] == ["international_fee"], item
            assert item["unresolved_fields"] == []
            assert item["made_progress"] is True
            assert item["reason_code"] == "issues_resolved"
            again = await run_smart_batch(ReExtractBody(
                ids=[row.id], universityId=uni_id, smart=True,
                targetFields=["international_fee"],
            ), db)
            assert again["results"][0]["attempted"] is False
            row.international_fee = 16900
            row.extraction_method = {"ielts_overall": "manual"}
            await db.commit()
            correction = await run_smart_batch(ReExtractBody(
                ids=[row.id], universityId=uni_id, smart=True,
                targetFields=["international_fee"], forceFields=["international_fee"],
                forceReasons={"international_fee": "The stored domestic scalar contradicts the official international course fee"},
            ), db)
            assert correction["results"][0]["resolved_fields"] == ["international_fee"]
            assert correction["results"][0]["made_progress"] is True
            assert row.auto_publish_status == "review"
    finally:
        await _cleanup(job_id)


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
async def test_stage_course_converts_londonmet_numeric_intake_to_january():
    uni_id = await _pick_university()
    job_id = f"test_londonmet_january_{uuid.uuid4().hex[:10]}"
    url = (
        "https://www.londonmet.ac.uk/courses/postgraduate/"
        "applied-cyber-security-and-cloud-technology---msc/"
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Applied Cyber Security and Cloud Technology MSc",
                payload={
                    "course_name": (
                        "Applied Cyber Security and Cloud Technology MSc"
                    ),
                    "degree_level": "Master",
                    "study_load": "Full Time",
                    "international_fee": 20000,
                    "currency": "GBP",
                    "fee_term": "Annual",
                    "duration": 1,
                    "duration_term": "Year",
                    "intake_months": [1],
                    "course_location": "Holloway",
                },
                evidence=[{
                    "field_key": "intake_months",
                    "value": [1],
                    "method": "londonmet_chrome_scrub:data_cost_attr",
                    "source_url": url,
                    "snippet": (
                        "London Met Overseas full-time entry-point option, "
                        "cohort year 2027: January 2027"
                    ),
                }],
                source_url=url,
            )
            assert result.saved, result.reason
            await db.commit()

            stored = await db.get(ScrapedCourse, result.scraped_course_id)
            assert stored is not None
            assert stored.intake_months == ["January"]
            intake_evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == stored.id,
                        ScrapedFieldEvidence.field_key == "intake_months",
                    )
                )
            ).scalars().all()
            assert len(intake_evidence) == 1
            assert intake_evidence[0].extraction_method == (
                "londonmet_chrome_scrub:data_cost_attr"
            )
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_otago_metadata_fee_is_saved_for_review_with_selected_evidence():
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    uni_id = await _pick_university()
    prefix = f"test_otago_meta_{uuid.uuid4().hex[:10]}"
    url = "https://www.otago.ac.nz/study/qualifications/master-of-music-coursework"
    config = load_uni_config(
        slug="otago",
        university_id=2189,
        scrape_url="https://www.otago.ac.nz/study/qualifications",
        name="University of Otago",
    )
    token = current_uni_config.set(config)
    html = """
      <html><head>
        <meta name="internationalFeesMin" content="66255">
        <meta name="internationalFeesYear" content="2027">
      </head><body><main>
        <h1>Master of Music (Coursework) (MMus(Coursework))</h1>
        <p>Domestic fee 2026: NZ $13,000 – NZ $15,500</p>
        <p>Duration: 1 year full-time.</p>
        <p>Intake: February.</p>
        <p>Location: Dunedin.</p>
        <p>Study mode: On Campus.</p>
        <p>IELTS overall score of 6.5 with no band below 6.0.</p>
      </main></body></html>
    """
    try:
        extracted = await extract_course(
            url,
            country="New Zealand",
            html=html,
            use_ai_fallback=False,
        )
        async with AsyncSessionLocal() as db:
            staged = await stage_course(
                db,
                scrape_job_id=prefix,
                university_id=uni_id,
                course_name=extracted["payload"]["course_name"],
                payload=extracted["payload"],
                evidence=extracted["evidence"],
                source_url=url,
            )
            assert staged.saved
            fresh = await db.get(ScrapedCourse, staged.scraped_course_id)
            assert fresh.international_fee == 66255
            assert fresh.currency == "NZD"
            assert fresh.fee_year == 2027
            selected = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == fresh.id,
                        ScrapedFieldEvidence.field_key == "international_fee",
                        ScrapedFieldEvidence.selected.is_(True),
                    )
                )
            ).scalars().all()
            assert any(
                row.extraction_method == "fee.explicit_international_meta"
                and row.normalized_value == "66255"
                for row in selected
            )
    finally:
        current_uni_config.reset(token)
        await _cleanup(prefix)


@pytest.mark.asyncio
async def test_bond_source_empty_fee_is_not_inherited_from_approved_row():
    uni_id = await _pick_university()
    prefix = f"test_bond_empty_{uuid.uuid4().hex[:10]}"
    name = f"Master of Source Omission {uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            approved = ScrapedCourse(
                scrape_job_id=f"{prefix}_old",
                university_id=uni_id,
                course_name=name,
                course_website="https://bond.edu.au/program/old-version",
                status="approved",
                international_fee=75000,
                fee_term="Annual",
            )
            db.add(approved)
            await db.commit()

        async with AsyncSessionLocal() as db:
            staged = await stage_course(
                db,
                scrape_job_id=f"{prefix}_new",
                university_id=uni_id,
                course_name=name,
                payload={
                    "course_name": name,
                    "course_website": "https://bond.edu.au/program/current-version",
                    "international_fee": None,
                    "fee_term": None,
                    "has_central_fee_page": True,
                    "course_location": "Gold Coast, Queensland",
                    "scrape_warnings": ["bond_fee_source_empty"],
                },
                evidence=[],
                source_url="https://bond.edu.au/program/current-version",
            )
            assert staged.saved
            fresh = await db.get(ScrapedCourse, staged.scraped_course_id)
            assert fresh.international_fee is None
            assert fresh.fee_term is None
            inherited = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == fresh.id,
                        ScrapedFieldEvidence.extraction_method == "approved_row:inherited",
                        ScrapedFieldEvidence.field_key.in_(
                            ["international_fee", "fee_term"]
                        ),
                    )
                )
            ).scalars().all()
            assert inherited == []
    finally:
        await _cleanup(prefix)


def test_reextract_warning_cleanup_is_condition_specific():
    warnings = _filter_resolved_reextract_warnings(
        [
            "fee_section_detected_fee_blank",
            "suspicious_duration",
            "confidence_low:55",
            "confidence_warn:75",
            "manual_operator_review",
        ],
        fresh_payload={
            "international_fee": 17420,
            "duration": 1.5,
        },
        current_payload={
            "international_fee": 17420,
            "duration": 1.5,
            "ielts_overall": 6.0,
            "intake_months": ["February"],
            "study_mode": "Full Time",
        },
    )

    assert warnings == ["manual_operator_review"]


@pytest.mark.asyncio
async def test_targeted_fee_fix_does_not_persist_unrelated_extraction(monkeypatch):
    uni_id = await _pick_university()
    job_id = f"test_targeted_fee_{uuid.uuid4().hex[:10]}"
    course_url = "https://example.edu/courses/targeted-fee"
    try:
        async with AsyncSessionLocal() as db:
            row = ScrapedCourse(
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Bachelor of Targeted Repair",
                course_website=course_url,
                status="pending",
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            sc_id = row.id

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": course_url,
                "payload": {
                    "international_fee": 32000,
                    "fee_term": "Annual",
                    "fee_year": 2026,
                    "currency": "AUD",
                    "other_requirement": "Unrelated AI entry requirement",
                    "category": "Unrelated AI category",
                },
                "evidence": [
                    {
                        "field_key": "international_fee",
                        "value": 32000,
                        "normalized": 32000,
                        "method": "fee:table",
                        "source_url": course_url,
                        "snippet": "International tuition A$32,000",
                        "decision_status": "selected",
                    },
                    {
                        "field_key": "other_requirement",
                        "value": "Unrelated AI entry requirement",
                        "normalized": "Unrelated AI entry requirement",
                        "method": "openai_primary",
                        "source_url": course_url,
                        "snippet": "Entry requirements",
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
                json={
                    "ids": [sc_id],
                    "universityId": uni_id,
                    "targetFields": ["international_fee"],
                },
            )

        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["updated_fields"] == [
            "international_fee",
            "fee_term",
            "fee_year",
            "currency",
        ]
        async with AsyncSessionLocal() as db:
            course = await db.get(ScrapedCourse, sc_id)
            assert course is not None
            assert course.international_fee == 32000
            assert course.fee_term == "Annual"
            assert course.fee_year == 2026
            assert course.currency == "AUD"
            assert course.other_requirement is None
            assert course.category is None
            evidence_fields = set(
                (
                    await db.execute(
                        select(ScrapedFieldEvidence.field_key).where(
                            ScrapedFieldEvidence.scraped_course_id == sc_id
                        )
                    )
                ).scalars()
            )
            assert evidence_fields == {"international_fee"}
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_stage_course_persists_only_calendar_intake_months():
    uni_id = await _pick_university()
    job_id = f"test_intake_guard_{uuid.uuid4().hex[:10]}"
    url = "https://example.edu/courses/master-of-computing"
    try:
        async with AsyncSessionLocal() as db:
            result = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Master of Computing",
                payload={
                    "course_name": "Master of Computing",
                    "degree_level": "Master's",
                    "course_website": url,
                    "international_fee": 42000,
                    "intake_months": [
                        "Rolling",
                        "February",
                        "Research Term 1",
                        "July",
                    ],
                },
                evidence=[],
                source_url=url,
            )
            assert result.saved, result.reason

        async with AsyncSessionLocal() as db:
            staged = await db.get(ScrapedCourse, result.scraped_course_id)
            assert staged is not None
            assert staged.intake_months == ["February", "July"]
    finally:
        await _cleanup(job_id)


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
                *[
                    {
                        "field_key": field,
                        "value": 6.0,
                        "method": "english:table",
                        "confidence": 0.9,
                        "source_url": "https://example.edu/cs",
                        "snippet": "Minimum 6.0 in each IELTS component",
                    }
                    for field in (
                        "ielts_listening",
                        "ielts_speaking",
                        "ielts_writing",
                        "ielts_reading",
                    )
                ],
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
                "ielts_listening": 6.0,
                "ielts_speaking": 6.0,
                "ielts_writing": 6.0,
                "ielts_reading": 6.0,
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
            assert len(ev_rows) == 10
            keys = {r.field_key for r in ev_rows}
            assert keys == {
                "course_name",
                "degree_level",
                "study_mode",
                "international_fee",
                "ielts_overall",
                "ielts_listening",
                "ielts_speaking",
                "ielts_writing",
                "ielts_reading",
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
        assert len(body["evidence"]) == 10
        # Per-field grouping must include each key we wrote.
        assert set(body["evidenceByField"].keys()) == {
            "course_name",
            "degree_level",
            "study_mode",
            "international_fee",
            "ielts_overall",
            "ielts_listening",
            "ielts_speaking",
            "ielts_writing",
            "ielts_reading",
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
    extract_calls: list[dict] = []
    expected_central_data = {
        "fees": [{"program": "Bachelor of Computer Science", "fee": 45000}],
        "english": {},
        "fee_page_url": "https://example.edu/fees",
        "english_page_url": None,
    }
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
                    "scrape_warnings": [
                        "suspicious_duration",
                        "fee_section_detected_fee_blank",
                    ],
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

        async def _fake_extract_only(*_args, **kwargs):
            extract_calls.append(kwargs)
            is_retry = len(extract_calls) == 2
            return {
                "url": new_url,
                "payload": {
                    "international_fee": 45000,
                    "fee_year": 2026,
                    "course_website": new_url,
                    "duration": 3,
                    "duration_term": "Year",
                    "category": "Business" if is_retry else "Science",
                    **({"course_location": "Sydney"} if is_retry else {}),
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
                    {
                        "field_key": "category",
                        "value": "Business" if is_retry else "Science",
                        "normalized": "Business" if is_retry else "Science",
                        "method": "openai_primary",
                        "source_url": new_url,
                        "snippet": "Course discipline",
                        "decision_status": "selected",
                    },
                ],
            }

        async def _fake_prefetch_central_pages(*_args, **_kwargs):
            return expected_central_data

        monkeypatch.setattr(
            "app.services.scraper.orchestrator._extract_only",
            _fake_extract_only,
        )
        monkeypatch.setattr(
            "app.services.scraper.central_pages.prefetch_central_pages",
            _fake_prefetch_central_pages,
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
        assert len(extract_calls) == 2
        assert all(call["ai_provider"] == "openai" for call in extract_calls)
        assert all(
            call["central_data"] == expected_central_data
            for call in extract_calls
        )
        assert response.json()["results"][0]["extraction_passes"] == 2
        assert response.json()["results"][0]["ai_provider"] == "openai"

        async with AsyncSessionLocal() as db:
            course = await db.get(ScrapedCourse, sc_id)
            assert course is not None
            assert course.international_fee == 45000
            assert course.fee_year == 2026
            assert course.course_website == new_url
            assert course.category == "Science"
            assert course.course_location == "Sydney"
            assert "suspicious_duration" not in course.scrape_warnings
            assert "fee_section_detected_fee_blank" not in course.scrape_warnings
            assert "confidence_low" in course.scrape_warnings
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
async def test_winchester_analyze_targets_and_reextract_persists_owned_fields(
    monkeypatch,
):
    """Portal Fix path updates name/location/mode and nothing unrelated."""
    uni_id = await _pick_university()
    job_id = f"test_winchester_fix_{uuid.uuid4().hex[:10]}"
    url = (
        "https://www.winchester.ac.uk/study/Postgraduate/Courses/"
        "MA-Politics-and-International-Relations-2025/"
    )
    try:
        async with AsyncSessionLocal() as db:
            staged = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="MA Politics and International Relations (2025)",
                payload={
                    "course_name": "MA Politics and International Relations (2025)",
                    "degree_level": "Master",
                    "category": "Arts, Humanities & Social Sciences",
                    "international_fee": 17450,
                    "currency": "GBP",
                    "fee_term": "Annual",
                    "ielts_overall": 6.0,
                    "pte_overall": 58,
                    "duration": 1,
                    "duration_term": "Year",
                    "intake_months": ["September"],
                    "course_location": (
                        "Blended learning in school and on campus in Winchester"
                    ),
                    "study_mode": "On Campus",
                    "course_website": url,
                    "scrape_warnings": ["dated_catalogue_page_review"],
                },
                evidence=[],
                source_url=url,
            )
        assert staged.saved, staged.reason
        sc_id = staged.scraped_course_id
        assert sc_id is not None

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            analysis_response = await client.post(
                "/api/scrape/staged/analyze",
                json={"ids": [sc_id], "universityId": uni_id},
            )
        assert analysis_response.status_code == 200, analysis_response.text
        target_fields = [
            issue["field"] for issue in analysis_response.json()["issues"]
            if issue["field"] in {"course_name", "course_location"}
        ]
        assert set(target_fields) == {"course_name", "course_location"}

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": url,
                "payload": {
                    "course_name": "MA Politics and International Relations",
                    "course_location": "Winchester",
                    "study_mode": "Blended",
                    "category": "Business & Management",
                },
                "evidence": [
                    {
                        "field_key": field,
                        "value": value,
                        "normalized": value,
                        "method": "winchester:course_owned_fact",
                        "source_url": url,
                        "snippet": "Official Winchester course-owned fact",
                        "decision_status": "selected",
                    }
                    for field, value in (
                        ("course_name", "MA Politics and International Relations"),
                        ("course_location", "Winchester"),
                        ("study_mode", "Blended"),
                    )
                ],
            }

        async def _fake_prefetch(*_args, **_kwargs):
            return {}

        monkeypatch.setattr(
            "app.services.scraper.orchestrator._extract_only",
            _fake_extract_only,
        )
        monkeypatch.setattr(
            "app.services.scraper.central_pages.prefetch_central_pages",
            _fake_prefetch,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            fix_response = await client.post(
                "/api/scrape/staged/re-extract",
                json={
                    "ids": [sc_id],
                    "universityId": uni_id,
                    "targetFields": target_fields,
                },
            )
        assert fix_response.status_code == 200, fix_response.text
        assert fix_response.json()["updated"] == 1

        async with AsyncSessionLocal() as db:
            stored = await db.get(ScrapedCourse, sc_id)
            assert stored is not None
            assert stored.course_name == "MA Politics and International Relations"
            assert stored.course_location == "Winchester"
            assert stored.study_mode == "Blended"
            assert stored.category == "Arts, Humanities & Social Sciences"
            assert "dated_catalogue_page_review" in (stored.scrape_warnings or [])
            assert stored.auto_publish_status == "review"

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            after_response = await client.post(
                "/api/scrape/staged/analyze",
                json={"ids": [sc_id], "universityId": uni_id},
            )
        assert after_response.status_code == 200, after_response.text
        remaining = {issue["field"] for issue in after_response.json()["issues"]}
        assert "course_name" not in remaining
        assert "course_location" not in remaining
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_re_extract_clears_legacy_rolling_intake_without_touching_other_fields(
    monkeypatch,
):
    uni_id = await _pick_university()
    job_id = f"test_reextract_intake_{uuid.uuid4().hex[:10]}"
    url = "https://example.edu/courses/master-of-computing"
    try:
        async with AsyncSessionLocal() as db:
            row = ScrapedCourse(
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Master of Computing",
                course_website=url,
                status="pending",
                category="Computer Science & IT",
                intake_months=["Rolling"],
                international_fee=42000,
            )
            db.add(row)
            await db.flush()
            sc_id = row.id
            await db.commit()

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": url,
                "payload": {
                    "intake_months": ["Research Term 1"],
                    "category": "Computing",
                },
                "evidence": [
                    {
                        "field_key": "intake_months",
                        "value": ["Research Term 1"],
                        "method": "test:research-term",
                        "source_url": url,
                        "snippet": "Research Term 1",
                        "decision_status": "selected",
                    },
                    {
                        "field_key": "category",
                        "value": "Computing",
                        "method": "test:category",
                        "source_url": url,
                        "snippet": "Computing",
                        "decision_status": "selected",
                    },
                ],
            }

        async def _fake_prefetch_central_pages(*_args, **_kwargs):
            return None

        monkeypatch.setattr(
            "app.services.scraper.orchestrator._extract_only",
            _fake_extract_only,
        )
        monkeypatch.setattr(
            "app.services.scraper.central_pages.prefetch_central_pages",
            _fake_prefetch_central_pages,
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
            refreshed = await db.get(ScrapedCourse, sc_id)
            assert refreshed is not None
            assert refreshed.intake_months is None
            assert refreshed.category == "Computing"
            assert refreshed.international_fee == 42000
            evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                    )
                )
            ).scalars().all()
            assert {item.field_key for item in evidence} == {"category"}
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_re_extract_refreshes_unchanged_fee_from_newer_canonical_page(monkeypatch):
    uni_id = await _pick_university()
    job_id = f"test_reextract_same_ev_{uuid.uuid4().hex[:10]}"
    old_url = f"https://example.edu/courses/2025/{job_id}"
    new_url = f"https://example.edu/courses/2026/{job_id}"
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

        async def _fake_prefetch_central_pages(*_args, **_kwargs):
            return {}

        monkeypatch.setattr(
            "app.services.scraper.central_pages.prefetch_central_pages",
            _fake_prefetch_central_pages,
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
        assert result["made_progress"] is True
        assert response.json()["updated"] == 1

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

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            unchanged_response = await client.post(
                "/api/scrape/staged/re-extract",
                json={
                    "ids": [sc_id],
                    "universityId": uni_id,
                    "targetFields": ["international_fee"],
                },
            )

        assert unchanged_response.status_code == 200, unchanged_response.text
        unchanged_body = unchanged_response.json()
        assert unchanged_body["updated"] == 0
        unchanged_result = unchanged_body["results"][0]
        assert unchanged_result["updated_fields"] == []
        assert unchanged_result["refreshed_evidence_fields"] == []
        assert unchanged_result["made_progress"] is False
        assert unchanged_result["outcome"] == "no_progress"
        assert unchanged_result["reason"] == (
            "Requested target fields and selected evidence were unchanged"
        )
    finally:
        await _cleanup(job_id)


@pytest.mark.asyncio
async def test_re_extract_refreshes_when_equal_value_gains_selected_evidence(
    monkeypatch,
):
    uni_id = await _pick_university()
    job_id = f"test_reextract_select_ev_{uuid.uuid4().hex[:10]}"
    url = "https://example.edu/courses/master-of-engineering"
    try:
        async with AsyncSessionLocal() as db:
            staged = await stage_course(
                db,
                scrape_job_id=job_id,
                university_id=uni_id,
                course_name="Master of Engineering",
                payload={
                    "course_name": "Master of Engineering",
                    "international_fee": 41000,
                    "ielts_listening": 5.5,
                    "course_website": url,
                },
                evidence=[
                    {
                        "field_key": "international_fee",
                        "value": 41000,
                        "normalized": 41000,
                        "method": "fee:table",
                        "source_url": url,
                        "snippet": "International tuition fee A$41,000",
                        "decision_status": "selected",
                    },
                    {
                        "field_key": "ielts_listening",
                        "value": 5.5,
                        "normalized": 5.5,
                        "method": "approved_row:inherited",
                        "source_url": url,
                        "snippet": "Inherited from an approved row",
                        "decision_status": "needs_review",
                    },
                ],
                source_url=url,
            )
            assert staged.saved
            sc_id = staged.scraped_course_id
            old_evidence = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                        ScrapedFieldEvidence.field_key == "ielts_listening",
                    )
                )
            ).scalar_one()
            assert old_evidence.selected is False

        async def _fake_extract_only(*_args, **_kwargs):
            return {
                "url": url,
                "payload": {"ielts_listening": 5.5},
                "evidence": [{
                    "field_key": "ielts_listening",
                    "value": 5.5,
                    "normalized": 5.5,
                    "method": "cqu_json:requisite_conditions_text",
                    "source_url": url,
                    "snippet": "IELTS minimum 5.5 in each component",
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
        assert result["refreshed_evidence_fields"] == ["ielts_listening"]
        assert result["made_progress"] is True
        assert response.json()["updated"] == 1

        async with AsyncSessionLocal() as db:
            refreshed = (
                await db.execute(
                    select(ScrapedFieldEvidence).where(
                        ScrapedFieldEvidence.scraped_course_id == sc_id,
                        ScrapedFieldEvidence.field_key == "ielts_listening",
                    )
                )
            ).scalar_one()

        assert refreshed.extraction_method == (
            "cqu_json:requisite_conditions_text"
        )
        assert refreshed.selected is True
        assert refreshed.decision_status == "selected"
    finally:
        await _cleanup(job_id)

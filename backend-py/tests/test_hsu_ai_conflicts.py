"""Adversarial remote responses must never replace HSU's course-owned facts."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from tests.test_hsu_facts import hsu_database, hsu_postgres, no_live_gemini

CASES = [
    pytest.param("<h3>Tuition Fees 2026/27</h3><p>UK/ROI/Channel Islands and Isle of Man "
                 "students – £9,790 International – £17, 684</p>",
                 17684, "Annual", 2026, 9790, "Full Course", id="domestic-inline"),
    pytest.param("<h3>Tuition Fees 2026/27</h3><h4>International</h4>"
                 "<p>£17,520 total course fee</p>",
                 17520, "Full Course", 2026, 8760, "Annual", id="full-course-not-annual"),
    pytest.param("<h3>Tuition Fees 2026/27</h3><h4>International</h4>"
                 "<p>£17,046 per annum (Full time)</p>",
                 17046, "Annual", 2026, 51138, "Full Course", id="annual-not-total"),
    pytest.param("<h3>Tuition Fees</h3><p>International – £15,500.</p>",
                 15500, None, None, 13000, "Annual", id="undated-mba"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_stage", ["primary", "fallback"])
@pytest.mark.parametrize("fees,amount,term,year,bad_amount,bad_term", CASES)
async def test_hsu_official_facts_survive_ai_and_persistence(
    hsu_database, monkeypatch, ai_stage, fees, amount, term, year, bad_amount, bad_term,
):
    import httpx
    from app.models import University, ScrapedCourse, ScrapedFieldEvidence
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.models.page_snapshot import PageSnapshot
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.stage_course import stage_course
    from app.services.scraper.snapshot_save import staged_row_backup_payload

    # Fail closed on unexpected HTTP even when the pipeline catches errors.
    network_attempts = []
    async def forbidden_http(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("No live network permitted in adversarial acceptance")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_http)
    def forbidden_sync_http(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("No live network permitted in adversarial acceptance")
    monkeypatch.setattr(httpx.Client, "send", forbidden_sync_http)
    bad = {
        "international_fee": bad_amount, "currency": "AUD", "fee_currency": "AUD",
        "fee_term": bad_term, "fee_year": 2030, "ielts_overall": 8.0,
        **{f"ielts_{p}": 7.5 for p in ("reading", "writing", "listening", "speaking")},
    }
    primary = AsyncMock(return_value=(
        deepcopy(bad) if ai_stage == "primary" else {}, 0.0, 0, 0,
        {"skipped": ai_stage != "primary"},
    ))
    fallback = AsyncMock(return_value=deepcopy(bad) if ai_stage == "fallback" else {})
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", primary)
    monkeypatch.setattr("app.services.scraper.extractors.ai_fallback.fill_missing", fallback)
    config = load_uni_config(slug="hsu", name="Health Sciences University", university_id=101,
                             scrape_url="https://www.hsu.ac.uk", create_missing_stub=False)
    config.extraction.skip_remote_ai_enrichment = False
    config.extraction.skip_per_course_browser = True
    config.extraction.skip_browser_rescue = True
    title = "MBA Management with Health Data Analytics and AI" if year is None else "Master of Chiropractic"
    slug = "mba-management-with-health-data-analytics-and-ai" if year is None else "master-of-chiropractic"
    url = f"https://www.hsu.ac.uk/course/{slug}/"
    html = (f"<main><h1>{title}</h1><p>Full-time on campus.</p>"
            "<details><summary>International Students</summary>"
            "<p>Undergraduate standard: 6.0 overall with no less than 5.5 "
            "in each component.</p></details>" + fees + "</main>")
    token = current_uni_config.set(config)
    try:
        fresh = await extract_course(url, html=html, country="United Kingdom",
                                     use_ai_fallback=True)
        # Do not let a catch-and-continue branch make an unexercised mock pass.
        assert primary.await_count == 1
        if ai_stage == "fallback":
            assert fallback.await_count == 1
        else:
            # The primary response really entered the evidence stream; passing
            # cannot be explained by accidentally bypassing the adversarial AI.
            assert any(e["field_key"] == "fee_currency" and e.get("value") == "AUD"
                       and e.get("decision_status") != "selected"
                       for e in fresh["evidence"])
        assert not network_attempts
        expected = {"international_fee": amount, "currency": "GBP",
                    "fee_term": term, "fee_year": year, "ielts_overall": 6.0,
                    **{f"ielts_{p}": 5.5 for p in ("reading","writing","listening","speaking")}}
        for field, value in expected.items():
            assert fresh["payload"].get(field) == value, field
        job_id = f"hsu-ai-conflict-{uuid4()}"
        async with hsu_database() as db:
            uni = University(name=job_id, country="United Kingdom", city="Bournemouth")
            db.add(uni)
            await db.flush()
            uni_id = uni.id
            db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni_id,
                                   job_type="scrape", status="completed"))
            await db.commit()
            staged = await stage_course(db, university_id=uni_id, scrape_job_id=job_id,
                                        course_name=title, source_url=url,
                                        payload=fresh["payload"], evidence=fresh["evidence"])
            assert staged.saved, staged.reason
            row_id = staged.scraped_course_id
        async with hsu_database() as db:
            row = await db.get(ScrapedCourse, row_id)
            assert row.status == "pending"
            for field, value in expected.items():
                assert getattr(row, field) == value, field
            evidence = (await db.scalars(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == row_id,
                ScrapedFieldEvidence.selected.is_(True)))).all()
            for item in evidence:
                if item.field_key in ("currency", "fee_currency", "fee_term", "fee_year"):
                    value = expected["currency" if item.field_key == "fee_currency" else item.field_key]
                    assert value is not None and str(item.normalized_value) == str(value), item.field_key
            for field in ("international_fee", "ielts_overall", "ielts_reading",
                          "ielts_writing", "ielts_listening", "ielts_speaking"):
                winners = [e for e in evidence if e.field_key == field]
                assert len(winners) == 1, field
                winner = winners[0]
                assert float(winner.normalized_value) == expected[field]
                assert winner.source_url == url and winner.snippet
                assert "ai" not in winner.extraction_method.lower()
                assert "gemini" not in winner.extraction_method.lower()
            snapshot = (await db.scalars(select(PageSnapshot).where(
                PageSnapshot.scrape_job_id == job_id,
                PageSnapshot.snapshot_type == "staged_row"))).one()
            assert snapshot.original_extraction == staged_row_backup_payload(row)
        assert not network_attempts
    finally:
        current_uni_config.reset(token)

"""Opt-in fresh-source acceptance; persistence uses disposable PostgreSQL only."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_hsu_facts import hsu_database, hsu_postgres, no_live_gemini

CASES = [
    ("master-of-chiropractic", 17684, "Annual", 2026, 6.0, 5.5),
    ("msc-dietetics-pre-registration", 17684, "Annual", 2026, 7.0, 6.5),
    ("research-degree-phd", 17046, "Annual", 2026, 6.5, 6.0),
    ("msc-sport-and-exercise-rehabilitation", 17520, "Full Course", 2026, 6.0, 5.5),
    ("mba-management-with-health-data-analytics-and-ai", 15500, None, None, None, None),
    ("msc-global-healthcare-management", 15000, None, None, None, None),
    ("bsc-occupational-therapy", None, None, None, None, None),
    ("msc-dietetics-integrated-degree-apprenticeship", None, None, None, 7.0, 6.5),
]


@pytest.mark.skipif(os.getenv("RUN_HSU_FRESH_ACCEPTANCE") != "1",
                    reason="Explicit opt-in required for bounded paid live fetches")
@pytest.mark.asyncio
async def test_fresh_hsu_catalogue_persistence(hsu_database, monkeypatch):
    import asyncio
    from app.models.university import University
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.models.scraped_course import ScrapedCourse
    from app.models.evidence import ScrapedFieldEvidence
    from app.services.scraper.http_fetcher import fetch_html_scrape_do
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.stage_course import stage_course
    from app.services.scraper.snapshot_save import staged_row_backup_payload
    from app.models.page_snapshot import PageSnapshot
    from bs4 import BeautifulSoup

    async def no_ai(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", no_ai)
    config = load_uni_config(slug="hsu", name="Health Sciences University",
                             university_id=101, scrape_url="https://www.hsu.ac.uk",
                             create_missing_stub=False)
    config.extraction.skip_per_course_browser = True
    config.extraction.skip_browser_rescue = True
    config.extraction.skip_remote_ai_enrichment = True
    output = (Path(__file__).resolve().parents[2] / "generated-artifacts/hsu-fresh-acceptance"
              / str(uuid4()))
    output.mkdir(parents=True, exist_ok=True)
    receipts = {"started_at": datetime.now(timezone.utc).isoformat(),
                "isolation": "disposable PostgreSQL; unique university/job; rollback teardown",
                "cases": []}
    for relative in ("app/services/scraper/extractors/hsu_facts.py",
                     "app/services/scraper/pipelines/single_course.py",
                     "app/services/scraper/stage_course.py"):
        receipts.setdefault("code_sha256", {})[relative] = hashlib.sha256(
            (Path(__file__).resolve().parents[1] / relative).read_bytes()).hexdigest()
    job_id = "hsu-fresh-acceptance-" + str(uuid4())
    async with hsu_database() as db:
        uni = University(name=job_id, country="United Kingdom", city="Bournemouth")
        db.add(uni)
        await db.flush()
        uni_id = uni.id
        db.add(ScrapeRuntimeJob(runtime_job_id=job_id, university_id=uni_id,
                               job_type="scrape", status="completed"))
        await db.commit()
    token = current_uni_config.set(config)
    try:
        requested = set(filter(None, os.getenv("HSU_ACCEPTANCE_SLUGS", "").split(",")))
        assert requested <= {c[0] for c in CASES}, "Unknown continuation case"
        cases = [c for c in CASES if not requested or c[0] in requested]
        for slug, fee, term, year, overall, floor in cases:
            url = f"https://www.hsu.ac.uk/course/{slug}/"
            # Fresh transport only: no archive, discovery cache, saved HTML,
            # retries, or fallback to older receipts.
            html = await asyncio.wait_for(fetch_html_scrape_do(
                url, render=True, max_retries=0, request_timeout_seconds=40), 55)
            assert html and len(html) > 10000, f"Fresh source unavailable: {slug}"
            soup = BeautifulSoup(html, "html.parser")
            assert soup.find("h1"), f"No course title: {slug}"
            source_text = soup.get_text(" ", strip=True)
            if fee is not None:
                assert f"£{fee}" in re.sub(r"[\s,]", "", source_text), slug
            elif slug == "bsc-occupational-therapy":
                assert "fees for overseas students are still to be confirmed" in source_text.lower()
            else:
                assert "apprenticeship funding band" in source_text.lower()
            (output / f"{slug}.html").write_text(html)
            receipt = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                       "sha256": hashlib.sha256(html.encode()).hexdigest()}
            receipts["cases"].append(receipt)
            (output / "receipt.json").write_text(json.dumps(receipts, indent=2))
            result = await asyncio.wait_for(extract_course(
                url, html=html, country="United Kingdom", use_ai_fallback=False), 90)
            payload = result["payload"]
            assert (payload.get("international_fee"), payload.get("fee_term"),
                    payload.get("fee_year")) == (fee, term, year), slug
            if overall is not None:
                assert payload["ielts_overall"] == overall, slug
                for part in ("reading", "writing", "listening", "speaking"):
                    assert payload[f"ielts_{part}"] == floor, slug
            async with hsu_database() as db:
                staged = await stage_course(
                    db, university_id=uni_id, scrape_job_id=job_id,
                    course_name=soup.find("h1").get_text(" ", strip=True),
                    source_url=url, payload=payload, evidence=result["evidence"])
                assert staged.saved, (slug, staged.reason)
                row_id = staged.scraped_course_id
            # Separate session verifies the final database representation,
            # evidence selection and durable snapshot, not the input payload.
            async with hsu_database() as db:
                row = await db.get(ScrapedCourse, row_id)
                assert row.status == "pending"
                assert (row.international_fee, row.fee_term, row.fee_year) == (fee, term, year)
                evidence = (await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id == row_id,
                    ScrapedFieldEvidence.selected == True))).scalars().all()
                fields = {"international_fee": fee} if fee is not None else {}
                if overall is not None:
                    fields["ielts_overall"] = overall
                    fields.update({f"ielts_{p}": floor for p in ("reading","writing","listening","speaking")})
                for field, value in fields.items():
                    assert getattr(row, field) == value
                    assert any(e.field_key == field and float(e.normalized_value) == value
                               and e.source_url == url and e.snippet for e in evidence), (slug, field)
                if fee is None:
                    assert not any(e.field_key == "international_fee" for e in evidence)
                snapshot = (await db.execute(select(PageSnapshot).where(
                    PageSnapshot.scrape_job_id == job_id, PageSnapshot.course_url == url,
                    PageSnapshot.snapshot_type == "staged_row"))).scalar_one()
                assert snapshot.original_extraction == staged_row_backup_payload(row)
                receipt.update(status="passed", persisted=staged_row_backup_payload(row),
                               selected_evidence=[{"field": e.field_key, "value": e.normalized_value,
                                                   "source": e.source_url, "snippet": e.snippet}
                                                  for e in evidence if e.field_key in fields])
                (output / "receipt.json").write_text(json.dumps(receipts, indent=2))
    finally:
        current_uni_config.reset(token)
    receipts["completed_at"] = datetime.now(timezone.utc).isoformat()
    for relative, digest in receipts["code_sha256"].items():
        assert hashlib.sha256((Path(__file__).resolve().parents[1] / relative).read_bytes()).hexdigest() == digest
    (output / "receipt.json").write_text(json.dumps(receipts, indent=2))

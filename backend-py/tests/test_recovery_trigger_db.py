"""Integration tests for the single-course recovery trigger.

These tests complement the unit tests in test_recovery_pdf.py.  Where those
tests mock _write_recovery_results, these tests use a real AsyncSessionLocal and
only mock the HTTP I/O layer (search_candidate_pages + extract_from_url).

The goal is to verify the full chain:

  search_candidate_pages (via_broad_scorer=True)
    → extract_from_url returns source_type='pdf'
      → orchestrator retags to source_type='pdf_broad'
        → map_results_to_course preserves source_type
          → _write_recovery_results writes 'pdf_broad' to DB
            → GET /api/scrape/recovery/{sc_id} returns sourceType='pdf_broad'
            → GET /api/scrape/recovery/summary/{run_id} counts pdfBroadSources

Done criteria from task 162:
  1. agent_recovery_results rows appear with source_type='pdf_broad'
  2. Those rows display correctly in the recovery review panel UI
     (verified here by confirming the API response carries sourceType='pdf_broad')
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.main import app

_TRANSPORT = httpx.ASGITransport(app=app)
_BASE = "http://testserver"

_RUN_ID = "test-trigger-db-pdf-broad-run-001"
_PDF_URL = "https://uni.edu.au/download/2024-international-prospectus.pdf"
_SEED_URL = "https://uni.edu.au"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _seed() -> dict:
    """Seed a university with a scrape_url and a staged course missing fees."""
    async with AsyncSessionLocal() as db:
        uni_id = (await db.execute(
            text(
                "INSERT INTO universities (name, country, city, scrape_url) "
                "VALUES (:n, 'Australia', 'Sydney', :u) RETURNING id"
            ),
            {"n": "TRIGGER_DB_TEST_UNI_BROAD_PDF", "u": _SEED_URL},
        )).scalar_one()

        sc_id = (await db.execute(
            text(
                "INSERT INTO scraped_courses "
                "(scrape_job_id, university_id, course_name, degree_level, status, "
                " ielts_overall, intake_months, course_location) "
                "VALUES (:run, :u, 'Bachelor of Commerce — DB Integration Test', "
                "        'Bachelor''s', 'pending', 6.5, :intakes, 'Sydney') "
                "RETURNING id"
            ),
            {
                "run": _RUN_ID,
                "u": uni_id,
                "intakes": '["February", "July"]',
            },
        )).scalar_one()

        await db.commit()
        return {"uni_id": uni_id, "sc_id": sc_id}


async def _teardown(ids: dict) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM agent_recovery_results WHERE scrape_run_id = :r"),
            {"r": _RUN_ID},
        )
        await db.execute(
            text("DELETE FROM scraped_courses WHERE id = :i"),
            {"i": ids["sc_id"]},
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"),
            {"i": ids["uni_id"]},
        )
        await db.commit()


def _broad_candidate() -> dict:
    """A PDF candidate surfaced only by the broad-keyword fallback scorer."""
    return {
        "url": _PDF_URL,
        "category": "fees",
        "score": 3,
        "path_score": 1,
        "matched_keyword": "international",
        "via_broad_scorer": True,
    }


def _fee_result() -> dict:
    """A fee extraction result as returned by extract_from_url / _extract_from_pdf.

    source_type='pdf' — the orchestrator retags this to 'pdf_broad' because the
    URL is in url_is_broad.
    """
    return {
        "field": "international_fee",
        "value": 28000.0,
        "normalized": 28000.0,
        "confidence": 0.85,
        "snippet": "International tuition fee: AUD 28,000 per year",
        "method": "table",
        "source_url": _PDF_URL,
        "source_type": "pdf",
    }


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

async def test_trigger_writes_pdf_broad_to_db():
    """run_single_course_recovery writes source_type='pdf_broad' to
    agent_recovery_results when the candidate came from the broad-keyword
    fallback scorer.

    This is the core DB-persistence check that the unit tests skip by
    mocking _write_recovery_results.
    """
    ids = await _seed()
    try:
        async with AsyncSessionLocal() as db:
            with ExitStack() as stack:
                stack.enter_context(patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[_broad_candidate()]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[_fee_result()]),
                ))
                from app.services.scraper.recovery.run_recovery import (
                    run_single_course_recovery,
                )
                results = await run_single_course_recovery(ids["sc_id"], db)

        assert results, (
            "run_single_course_recovery returned an empty list — "
            "expected at least one recovery result for international_fee"
        )

        # Verify DB row
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                text(
                    "SELECT field, source_type, status, recovered_value "
                    "FROM agent_recovery_results "
                    "WHERE scraped_course_id = :sc_id "
                    "  AND status = 'pending'"
                ),
                {"sc_id": ids["sc_id"]},
            )).all()

        assert rows, (
            "No pending rows found in agent_recovery_results for course %s. "
            "Expected a row with source_type='pdf_broad'." % ids["sc_id"]
        )

        fee_row = next((r for r in rows if r.field == "international_fee"), None)
        assert fee_row is not None, (
            "No row for field='international_fee' in agent_recovery_results; "
            "rows found: %r" % [(r.field, r.source_type, r.status) for r in rows]
        )
        assert fee_row.source_type == "pdf_broad", (
            "Expected source_type='pdf_broad' for a result from a broad-scorer PDF; "
            "got source_type=%r.  The orchestrator must retag source_type='pdf' → "
            "'pdf_broad' BEFORE calling _write_recovery_results." % fee_row.source_type
        )
        assert fee_row.recovered_value is not None, (
            "recovered_value must not be NULL for a pending row; "
            "got %r" % fee_row.recovered_value
        )

    finally:
        await _teardown(ids)


async def test_trigger_pdf_broad_row_returned_in_api_get():
    """GET /api/scrape/recovery/{sc_id} must return the pdf_broad row with
    sourceType='pdf_broad', proving the row is visible in the review panel.

    This covers the second done criterion: 'those rows display correctly in
    the recovery review panel UI' — the UI reads from this endpoint.
    """
    ids = await _seed()
    try:
        async with AsyncSessionLocal() as db:
            with ExitStack() as stack:
                stack.enter_context(patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[_broad_candidate()]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[_fee_result()]),
                ))
                from app.services.scraper.recovery.run_recovery import (
                    run_single_course_recovery,
                )
                await run_single_course_recovery(ids["sc_id"], db)

        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/{ids['sc_id']}")

        assert r.status_code == 200, (
            "GET /api/scrape/recovery/{sc_id} returned HTTP %d" % r.status_code
        )
        body = r.json()
        assert body["total"] >= 1, (
            "Expected at least 1 result row; got total=%d" % body["total"]
        )

        fee_row = next(
            (row for row in body["results"] if row["field"] == "international_fee"),
            None,
        )
        assert fee_row is not None, (
            "No row for field='international_fee' in API response; "
            "rows: %r" % [(r["field"], r["sourceType"]) for r in body["results"]]
        )
        assert fee_row["sourceType"] == "pdf_broad", (
            "API must return sourceType='pdf_broad' for a broad-scorer PDF result; "
            "got sourceType=%r.  The review panel uses this value to decide how to "
            "label the source badge." % fee_row["sourceType"]
        )
        assert fee_row["status"] == "pending", (
            "Row must start as 'pending' so the operator can apply or reject it; "
            "got status=%r" % fee_row["status"]
        )
        assert fee_row["sourceUrl"] == _PDF_URL, (
            "sourceUrl must be the broad-scorer PDF URL; got %r" % fee_row["sourceUrl"]
        )

    finally:
        await _teardown(ids)


async def test_trigger_summary_counts_pdf_broad_source():
    """GET /api/scrape/recovery/summary/{run_id} must count the broad-scorer
    PDF URL in pdfBroadSources >= 1 after the trigger writes a pdf_broad row.
    """
    ids = await _seed()
    try:
        async with AsyncSessionLocal() as db:
            with ExitStack() as stack:
                stack.enter_context(patch(
                    "app.services.scraper.recovery.searcher.search_candidate_pages",
                    new=AsyncMock(return_value=[_broad_candidate()]),
                ))
                stack.enter_context(patch(
                    "app.services.scraper.recovery.extractor.extract_from_url",
                    new=AsyncMock(return_value=[_fee_result()]),
                ))
                from app.services.scraper.recovery.run_recovery import (
                    run_single_course_recovery,
                )
                await run_single_course_recovery(ids["sc_id"], db)

        async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
            r = await ac.get(f"/api/scrape/recovery/summary/{_RUN_ID}")

        assert r.status_code == 200
        body = r.json()

        assert "pdfBroadSources" in body, (
            "Summary response must include 'pdfBroadSources' key; "
            "got keys: %s" % list(body.keys())
        )
        assert "pdfSources" in body, (
            "Summary response must include 'pdfSources' key; "
            "got keys: %s" % list(body.keys())
        )
        assert body["pdfBroadSources"] >= 1, (
            "pdfBroadSources must be >= 1 after a broad-scorer PDF result was "
            "written; got pdfBroadSources=%d" % body["pdfBroadSources"]
        )
        assert body["pdfSources"] >= 1, (
            "pdfSources (total PDF count) must be >= 1 since pdf_broad is a "
            "subset of PDF sources; got pdfSources=%d" % body["pdfSources"]
        )
        assert body["pending"] >= 1, (
            "pending count must be >= 1; got pending=%d" % body["pending"]
        )

    finally:
        await _teardown(ids)


async def test_trigger_api_endpoint_preserves_pdf_broad():
    """POST /api/scrape/recovery/trigger (the real endpoint, not the unit-mocked
    version in test_recovery_api.py) must return results with sourceType='pdf_broad'
    when the underlying run_single_course_recovery produces broad-scorer rows.

    This confirms the router correctly passes through the sourceType returned by
    run_single_course_recovery → _fetch_all_rows_for_course.
    """
    ids = await _seed()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch(
                "app.services.scraper.recovery.searcher.search_candidate_pages",
                new=AsyncMock(return_value=[_broad_candidate()]),
            ))
            stack.enter_context(patch(
                "app.services.scraper.recovery.extractor.extract_from_url",
                new=AsyncMock(return_value=[_fee_result()]),
            ))
            async with httpx.AsyncClient(transport=_TRANSPORT, base_url=_BASE) as ac:
                r = await ac.post(
                    "/api/scrape/recovery/trigger",
                    json={"scraped_course_id": ids["sc_id"]},
                )

        assert r.status_code == 200, (
            "POST /api/scrape/recovery/trigger returned HTTP %d: %s" % (
                r.status_code, r.text
            )
        )
        body = r.json()
        assert body["ok"] is True
        assert body["scrapedCourseId"] == ids["sc_id"]
        assert isinstance(body["results"], list)

        fee_row = next(
            (row for row in body["results"] if row.get("field") == "international_fee"),
            None,
        )
        assert fee_row is not None, (
            "Expected an international_fee result in trigger response; "
            "got results=%r" % body["results"]
        )
        assert fee_row["sourceType"] == "pdf_broad", (
            "Trigger API response must include sourceType='pdf_broad' for a broad-scorer "
            "PDF result; got sourceType=%r" % fee_row["sourceType"]
        )

    finally:
        await _teardown(ids)

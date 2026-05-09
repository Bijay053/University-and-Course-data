"""Tests for ``app.services.scraper.run_summary.compute_run_summary``.

Week 2 Prompt 1 — wide one-row-per-run summary table.

Pattern matches ``test_review_conflicts.py``: open a real ``AsyncSessionLocal``
against the test DB, seed an isolated runtime job + staged courses + evidence
+ one Gemini call log, run the summary computation, then clean up via the
cascades on ``DELETE FROM scrape_runtime_jobs``. Skips when no university
exists in the DB so the suite stays runnable on a freshly-provisioned
instance.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.models import (
    GeminiCallLog,
    ScrapedCourse,
    ScrapedFieldEvidence,
    ScrapeRunSummary,
    ScrapeRuntimeJob,
    University,
)
from app.services.scraper.run_summary import (
    bucket_skip_reasons,
    compute_run_summary,
)


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


async def _cleanup(runtime_job_id: str) -> None:
    # scraped_courses, scrape_run_summary and gemini_call_log all cascade
    # from scrape_runtime_jobs via FK — but scraped_courses uses
    # scrape_job_id (TEXT, no FK), so delete it explicitly first.
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scraped_courses WHERE scrape_job_id = :j"),
            {"j": runtime_job_id},
        )
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
            {"j": runtime_job_id},
        )
        await db.commit()


def _ev(*, sc_id: int, field: str, value: str, method: str = "regex") -> ScrapedFieldEvidence:
    return ScrapedFieldEvidence(
        scraped_course_id=sc_id,
        field_key=field,
        candidate_value=value,
        normalized_value=value,
        page_type="course_page",
        extraction_method=method,
        confidence=0.9,
        selected=True,
    )


# ───────────────────────── Pure-function tests ─────────────────────────

def test_bucket_skip_reasons_maps_known_tokens():
    raw = {
        "domestic_only": 3,
        "online_only_program": 2,
        "no_international_fee": 5,
        "category_landing_page": 1,
        "generic_category_page": 4,
        "fetch_failed_500": 7,
        "timeout_after_30s": 1,
        "weird_unknown_reason": 9,
    }
    out = bucket_skip_reasons(raw)
    assert out["domestic_only"] == 3
    assert out["online_only"] == 2
    assert out["no_international_fee"] == 5
    assert out["category_landing_page"] == 1
    assert out["generic_category_page"] == 4
    # fetch_failed_500 + timeout_after_30s both bucket to fetch_failed.
    assert out["fetch_failed"] == 8
    assert out["other"] == 9


def test_bucket_skip_reasons_handles_empty():
    out = bucket_skip_reasons({})
    assert sum(out.values()) == 0
    assert set(out.keys()) == {
        "domestic_only", "online_only", "no_international_fee",
        "category_landing_page", "generic_category_page",
        "fetch_failed", "other",
    }


# ───────────────────────── Integration test ────────────────────────────

@pytest.mark.asyncio
async def test_compute_run_summary_writes_one_wide_row():
    """End-to-end: seed 4 staged courses + evidence + a Gemini call log,
    call compute_run_summary, assert the wide row reflects fill rates,
    skip buckets, method distribution and cost.
    """
    uni_id = await _pick_university()
    runtime_job_id = f"test_summary_{uuid.uuid4().hex[:10]}"

    started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    finished_at = datetime.now(timezone.utc)

    try:
        # Seed runtime job + 4 staged courses + evidence + a Gemini call.
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=runtime_job_id,
                    university_id=uni_id,
                    job_type="university_full",
                    status="completed",
                    started_at=started_at,
                    completed_at=finished_at,
                    total_found=10,
                    imported=4,
                    skipped=6,
                    errors=0,
                )
            )
            await db.flush()

            sc_ids: list[int] = []
            for i in range(4):
                sc = ScrapedCourse(
                    scrape_job_id=runtime_job_id,
                    university_id=uni_id,
                    course_name=f"Master of Test Summary {i}",
                    status="pending",
                )
                db.add(sc)
                await db.flush()
                sc_ids.append(sc.id)

            # International fee: 4/4 (100%) — all regex
            # IELTS overall: 3/4 (75%) — 2 regex + 1 ai_fallback, 1 missing
            # PTE overall: 0/4 (0%) — none
            # Duration: 2/4 (50%) — both regex
            evidence: list[ScrapedFieldEvidence] = []
            for sc_id in sc_ids:
                evidence.append(
                    _ev(sc_id=sc_id, field="international_fee", value="35000")
                )
            for sc_id in sc_ids[:2]:
                evidence.append(_ev(sc_id=sc_id, field="ielts_overall", value="6.5"))
            evidence.append(
                _ev(sc_id=sc_ids[2], field="ielts_overall",
                    value="6.0", method="ai_fallback")
            )
            for sc_id in sc_ids[:2]:
                evidence.append(_ev(sc_id=sc_id, field="duration", value="2 years"))
            db.add_all(evidence)

            # One Gemini call.
            db.add(
                GeminiCallLog(
                    scrape_run_id=runtime_job_id,
                    university_id=uni_id,
                    call_type="primary_full",
                    model="gemini-2.5-flash-lite",
                    input_tokens=1000,
                    output_tokens=200,
                    cost_usd=0.0125,
                )
            )
            await db.commit()

        # ── Act ───────────────────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            row = await compute_run_summary(
                db, runtime_job_id, uni_id,
                summary={"discovered": 10, "staged": 4, "skipped": 6,
                         "errors": 0, "fetch_failed": 1},
                skip_reasons={"domestic_only": 3, "fetch_failed_500": 1,
                              "no_international_fee": 2},
            )
            assert row is not None

        # ── Assert via fresh read ─────────────────────────────────────
        async with AsyncSessionLocal() as db:
            persisted = (
                await db.execute(
                    select(ScrapeRunSummary).where(
                        ScrapeRunSummary.scrape_run_id == runtime_job_id
                    )
                )
            ).scalar_one()

        assert persisted.university_id == uni_id
        assert persisted.candidates_discovered == 10
        assert persisted.candidates_staged == 4
        assert persisted.candidates_skipped == 6
        assert persisted.fetch_errors == 1

        # Skip buckets. The fetch_failed bucket reconciles against
        # summary["fetch_failed"] — token-bucketed value was 1, summary
        # value was 1, so result is max(1, 1) == 1.
        assert persisted.skipped_domestic_only == 3
        assert persisted.skipped_fetch_failed == 1
        assert persisted.skipped_no_international_fee == 2
        assert persisted.skipped_other == 0

        # Fill rates
        assert persisted.fill_rate_international_fee == Decimal("1.000")
        assert persisted.fill_rate_ielts_overall == Decimal("0.750")
        assert persisted.fill_rate_pte_overall == Decimal("0.000")
        assert persisted.fill_rate_duration == Decimal("0.500")

        # Method distribution — JSONB; presence + shape, not strict equality.
        md = persisted.method_distribution or {}
        assert md.get("international_fee", {}).get("regex") == 4
        assert md["international_fee"]["null"] == 0
        assert md["ielts_overall"]["regex"] == 2
        assert md["ielts_overall"]["ai_fallback"] == 1
        assert md["ielts_overall"]["null"] == 1

        # Cost
        assert persisted.gemini_calls == 1
        assert persisted.gemini_cost_usd == Decimal("0.012500")
        # Generated column: 0.0125 / 4 = 0.003125
        assert persisted.avg_cost_per_course == Decimal("0.003125")

        # Generated duration column: started_at = now-120s, finished = now → ~120s.
        assert persisted.run_duration_seconds is not None
        assert 110 <= persisted.run_duration_seconds <= 130

    finally:
        await _cleanup(runtime_job_id)


@pytest.mark.asyncio
async def test_compute_run_summary_is_idempotent_on_retry():
    """Calling compute_run_summary twice for the same run produces a
    single wide row (UNIQUE constraint + DELETE-before-INSERT)."""
    uni_id = await _pick_university()
    runtime_job_id = f"test_summary_idem_{uuid.uuid4().hex[:8]}"

    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=runtime_job_id,
                    university_id=uni_id,
                    job_type="university_full",
                    status="completed",
                    started_at=datetime.now(timezone.utc) - timedelta(seconds=30),
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        async with AsyncSessionLocal() as db:
            await compute_run_summary(db, runtime_job_id, uni_id)
        async with AsyncSessionLocal() as db:
            await compute_run_summary(db, runtime_job_id, uni_id)

        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ScrapeRunSummary).where(
                        ScrapeRunSummary.scrape_run_id == runtime_job_id
                    )
                )
            ).scalars().all()
        assert len(rows) == 1

    finally:
        await _cleanup(runtime_job_id)


@pytest.mark.asyncio
async def test_fetch_failed_propagates_from_summary_dict():
    """Orchestrator counts fetch failures in ``summary["fetch_failed"]`` but
    does not push them into ``skip_reasons``. The summary must still
    surface them in the ``skipped_fetch_failed`` column."""
    uni_id = await _pick_university()
    runtime_job_id = f"test_summary_fetch_{uuid.uuid4().hex[:8]}"

    try:
        async with AsyncSessionLocal() as db:
            db.add(
                ScrapeRuntimeJob(
                    runtime_job_id=runtime_job_id,
                    university_id=uni_id,
                    job_type="university_full",
                    status="completed",
                    started_at=datetime.now(timezone.utc) - timedelta(seconds=10),
                    completed_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        async with AsyncSessionLocal() as db:
            row = await compute_run_summary(
                db, runtime_job_id, uni_id,
                summary={"discovered": 20, "staged": 0, "skipped": 0,
                         "errors": 7, "fetch_failed": 7},
                # Note: no fetch_failed token in skip_reasons — the
                # orchestrator never adds one.
                skip_reasons={"domestic_only": 0},
            )
            assert row is not None

        async with AsyncSessionLocal() as db:
            persisted = (
                await db.execute(
                    select(ScrapeRunSummary).where(
                        ScrapeRunSummary.scrape_run_id == runtime_job_id
                    )
                )
            ).scalar_one()
        assert persisted.fetch_errors == 7
        # The fix: summary["fetch_failed"] is reconciled into the bucket
        # column even though no token bucketed to fetch_failed.
        assert persisted.skipped_fetch_failed == 7

    finally:
        await _cleanup(runtime_job_id)


@pytest.mark.asyncio
async def test_compute_run_summary_returns_none_for_missing_job():
    """If runtime job doesn't exist, the function logs and returns None."""
    async with AsyncSessionLocal() as db:
        out = await compute_run_summary(db, "definitely_does_not_exist", 1)
    assert out is None

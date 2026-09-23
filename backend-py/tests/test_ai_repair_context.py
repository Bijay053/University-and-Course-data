"""Defensive tests for AI repair agent context loading.

Covers: schema-safe column access (total_errors alias), NULL coercion,
and graceful handling when runtime stats are missing or zero.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MISSING = object()  # sentinel distinct from None and False


def _make_db(job_row_data, quality_row_data: dict | None = None):
    """
    Return a mock async DB session whose .execute() alternates responses:
      call 0 → job/university row  (row query)
      call 1 → quality stats row   (scraped_courses query)
    """
    call_index = {"n": 0}

    job_defaults = {
        "university_id":     42,
        "total_found":       10,
        "imported":          5,
        "total_errors":      None,
        "status":            "completed",
        "error_message":     None,
        "discovered_config": None,
        "gate_skip_counts":  None,
        "uni_name":          "Test University",
        "scrape_url":        "https://example.com/courses",
        "scrape_config_raw": None,
    }
    if job_row_data is not _MISSING and job_row_data is not None and job_row_data is not False:
        job_defaults.update(job_row_data)

    quality_defaults = {
        "total": 0, "has_fee": 0, "has_ielts": 0, "has_intakes": 0,
        "has_location": 0, "has_degree_level": 0, "has_mode": 0,
        "has_duration": 0, "has_academic_level": 0,
        "critical_quality_count": 0, "critical_quality_rows": None,
        "sample_locations": None, "sample_degree_levels": None, "sample_modes": None,
        "staged_course_urls": None,
    }
    if quality_row_data is not None:
        quality_defaults.update(quality_row_data)

    def _row(data):
        if data is None:
            return None
        r = MagicMock()
        r.__getitem__ = lambda self, k: data[k]
        r.__bool__ = lambda self: True
        return r

    async def fake_execute(*a, **kw):
        n = call_index["n"]
        call_index["n"] += 1
        result = MagicMock()
        if n == 0:
            result.mappings.return_value.first.return_value = None if job_row_data is False else _row(job_defaults)
        else:
            result.mappings.return_value.first.return_value = _row(quality_defaults)
            result.mappings.return_value.all.return_value = []
        return result

    db = MagicMock()
    db.execute = fake_execute
    return db


# ---------------------------------------------------------------------------
# 1. SQL alias guard — must use `errors AS total_errors`, never bare
#    `srj.total_errors` which does not exist in the table.
# ---------------------------------------------------------------------------

def test_gather_context_sql_uses_correct_column_alias():
    """scrape_runtime_jobs.errors must be aliased; bare srj.total_errors must not appear."""
    import app.services.scraper.ai_repair_agent as mod
    source = inspect.getsource(mod._gather_context)
    assert "srj.total_errors" not in source, (
        "Found bare `srj.total_errors` in _gather_context — "
        "the column is `errors`; it must be aliased: `srj.errors AS total_errors`"
    )
    assert "errors" in source and "AS total_errors" in source, (
        "_gather_context must alias srj.errors AS total_errors"
    )


# ---------------------------------------------------------------------------
# 2. NULL / 0 coercion
# ---------------------------------------------------------------------------

import pytest

@pytest.mark.asyncio
async def test_total_errors_defaults_to_zero_when_null():
    """NULL errors in DB → total_errors=0 in context dict (not TypeError)."""
    from app.services.scraper.ai_repair_agent import _gather_context

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("job_null_errors", _make_db({"total_errors": None}))

    assert ctx.get("total_errors") == 0, (
        f"Expected 0 for NULL errors, got {ctx.get('total_errors')!r}"
    )


@pytest.mark.asyncio
async def test_total_errors_preserved_when_nonzero():
    """Non-zero errors must be passed through unchanged."""
    from app.services.scraper.ai_repair_agent import _gather_context

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("job_errors_17", _make_db({"total_errors": 17}))

    assert ctx.get("total_errors") == 17


@pytest.mark.asyncio
async def test_gather_context_returns_empty_dict_for_unknown_job():
    """Missing job row → empty dict, not an exception."""
    from app.services.scraper.ai_repair_agent import _gather_context

    # job_row_data=False signals _make_db to return None for the first query
    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("nonexistent_job", _make_db(False))

    assert ctx == {}


@pytest.mark.asyncio
async def test_gather_context_zero_raw_discovered_no_divzero():
    """drop_rate must be 0 when total_found=0, not ZeroDivisionError."""
    from app.services.scraper.ai_repair_agent import _gather_context

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("job_zero_found", _make_db({"total_found": 0, "imported": 0}))

    assert ctx.get("drop_rate") == 0


@pytest.mark.asyncio
async def test_legacy_job_does_not_treat_staged_count_as_after_filter_count():
    """Legacy 159-found/20-staged evidence has unknown, not 87%, filter loss."""
    from app.services.scraper.ai_repair_agent import _gather_context

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context(
            "legacy_159_found_20_staged",
            _make_db({
                "total_found": 159,
                "imported": 20,
                "discovered_config": None,
            }),
        )

    assert ctx["raw_discovered"] == 159
    assert ctx["after_filter"] == 159
    assert ctx["imported"] == 20
    assert ctx["drop_rate"] == 0


@pytest.mark.asyncio
async def test_gather_context_always_seeds_live_probe_from_latest_staged_course_urls():
    """Even a historical rejection-storm job must probe the latest known staged courses."""
    from app.services.scraper.ai_repair_agent import _gather_context

    db = _make_db(
        {
            "total_found": 159,
            "imported": 20,
            "discovered_config": {
                "pipeline_stats": {
                    "raw_discovered": 159,
                    "after_filter": 20,
                    "passed_sample": [
                        "https://example.com/study/subject/business",
                    ],
                },
            },
        },
        {
            "total": 20,
            "staged_course_urls": [
                "https://example.com/study/course/master-of-teaching",
                "https://example.com/study/course/bachelor-of-business",
                "https://example.com/study/course/master-of-teaching",
            ],
        },
    )

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("job_rejection_storm", db)

    assert set(ctx["repair_course_url_sample"]) == {
        "https://example.com/study/course/master-of-teaching",
        "https://example.com/study/course/bachelor-of-business",
    }
    assert ctx["repair_url_sample"][:2] == ctx["repair_course_url_sample"]


@pytest.mark.asyncio
async def test_rejection_storm_context_preserves_stage_and_critical_quality_evidence():
    """159 extractable pages / 20 staged must not look like an allow-list failure."""
    from app.services.scraper.ai_repair_agent import _gather_context

    critical_rows = [{
        "url": "https://example.com/study/course/master-applied-science",
        "course_name": "Master of Applied Science",
        "international_fee": 12991,
        "fee_term": "Annual",
        "currency": "NZD",
        "ielts_overall": 6.5,
        "course_location": "Rotorua",
    }]
    db = _make_db(
        {
            "total_found": 159,
            "imported": 20,
            "discovered_config": {
                "pipeline_stats": {
                    "raw_discovered": 162,
                    "after_filter": 159,
                    "filter_drop_count": 3,
                    "filter_drop_pct": 1.9,
                },
            },
            "gate_skip_counts": {
                "category_landing_page_missing_degree_qua": 129,
                "data_quality": {
                    "critical_count": 16,
                    "affected_course_count": 16,
                    "critical_urls": [critical_rows[0]["url"]],
                    "critical_issues": [{
                        "severity": "critical",
                        "code": "annual_fee_too_low_critical",
                        "message": "Likely domestic or partial fee",
                        "url": critical_rows[0]["url"],
                    }],
                },
            },
        },
        {
            "total": 20,
            "has_fee": 20,
            "has_ielts": 19,
            "has_intakes": 20,
            "has_location": 20,
            "has_degree_level": 20,
            "has_mode": 20,
            "has_duration": 20,
            "has_academic_level": 20,
            "critical_quality_count": 16,
            "critical_quality_rows": critical_rows,
        },
    )

    with patch("pathlib.Path.glob", return_value=[]):
        ctx = await _gather_context("job_rejection_storm", db)

    assert ctx["drop_rate"] == 2
    assert ctx["staging_rejections"]["reasons"] == {
        "category_landing_page_missing_degree_qua": 129,
    }
    assert ctx["critical_quality"]["affected_course_count"] == 16
    assert ctx["critical_quality"]["critical_issues"][0]["code"] == "annual_fee_too_low_critical"
    assert ctx["quality"]["fee_pct"] == 100
    assert ctx["quality"]["critical_quality_count"] == 16

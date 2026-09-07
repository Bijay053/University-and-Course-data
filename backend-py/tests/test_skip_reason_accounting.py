"""Regression tests for completed scrape skip/reason counter conservation."""

from app.services.scraper.orchestrator import _record_skip


def test_non_staging_skip_paths_each_record_one_stable_reason() -> None:
    summary = {"skipped": 0}
    reasons: dict[str, int] = {}

    _record_skip(summary, reasons, "duplicate_name_deduplicated")
    _record_skip(summary, reasons, "cpd_short_course")
    _record_skip(summary, reasons, "parser_error")

    assert summary["skipped"] == 3
    assert reasons == {
        "duplicate_name_deduplicated": 1,
        "cpd_short_course": 1,
        "parser_error": 1,
    }
    assert sum(reasons.values()) == summary["skipped"]


def test_recovery_sweep_stage_rejection_uses_normalized_reason_bucket() -> None:
    summary = {"skipped": 4}
    reasons = {"online_only": 4}

    key = _record_skip(summary, reasons, "rejected: part_time_only")

    assert key == "part_time_only"
    assert summary["skipped"] == 5
    assert reasons == {"online_only": 4, "part_time_only": 1}
    assert sum(reasons.values()) == summary["skipped"]


def test_missing_reason_is_still_accounted_for() -> None:
    summary: dict[str, int] = {}
    reasons: dict[str, int] = {}

    key = _record_skip(summary, reasons, None)

    assert key == "unknown"
    assert summary["skipped"] == 1
    assert reasons == {"unknown": 1}
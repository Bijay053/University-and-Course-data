"""Terminal status regressions for catalogue discovery/filter collapse."""

import inspect

from app.services.scraper.orchestrator import _catalogue_floor_guard


def test_unsw_style_filter_collapse_cannot_report_success():
    guard = _catalogue_floor_guard(
        raw_discovered=236,
        extractable=0,
        staged=0,
        expected_min_courses=200,
    )

    assert guard is not None
    assert guard["status"] == "failed_degraded"
    assert guard["kind"] == "discovery_filter_collapse"
    assert guard["level"] == "error"
    assert "236 raw candidates became 0 extractable URLs" in guard["message"]
    assert "0 staged courses" in guard["message"]


def test_partial_catalogue_finishes_with_warning_not_success():
    guard = _catalogue_floor_guard(
        raw_discovered=236,
        extractable=150,
        staged=140,
        expected_min_courses=200,
    )

    assert guard is not None
    assert guard["status"] == "completed_with_warnings"
    assert guard["kind"] == "catalogue_below_expected_min"


def test_catalogue_at_floor_is_not_flagged():
    assert _catalogue_floor_guard(
        raw_discovered=236,
        extractable=200,
        staged=180,
        expected_min_courses=200,
    ) is None


def test_zero_staged_is_degraded_even_when_url_floor_is_met():
    guard = _catalogue_floor_guard(
        raw_discovered=236,
        extractable=220,
        staged=0,
        expected_min_courses=200,
    )

    assert guard is not None
    assert guard["status"] == "failed_degraded"
    assert guard["kind"] == "discovery_filter_collapse"
    assert "220 extractable URLs and 0 staged courses" in guard["message"]


def test_authoritative_zero_row_reconciliation_drives_terminal_status():
    from app.services.scraper.orchestrator import run_scrape

    source = inspect.getsource(run_scrape)
    reconciled = source.index('summary["staged"] = actual_staged')
    final_guard = source.index("_reconciled_catalogue_guard = _catalogue_floor_guard")
    forced_status = source.index('job.status = _forced_status')

    assert reconciled < final_guard < forced_status
    assert '_catalogue_guard["status"] == "failed_degraded"' in source


def test_targeted_retry_is_not_compared_with_full_catalogue_floor():
    assert _catalogue_floor_guard(
        raw_discovered=3,
        extractable=3,
        staged=3,
        expected_min_courses=200,
        targeted_retry=True,
    ) is None
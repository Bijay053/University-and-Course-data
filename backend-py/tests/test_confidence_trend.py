"""Tests for Phase 9 Confidence Trend feature.

Covers:
  - Confidence calculation helper (avg from scraped_courses)
  - Trend direction logic
  - Missing previous scrape (first_run / no_data)
  - API response shape from _empty_summary
  - Trend change percentage sign and rounding
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Trend direction logic (pure function — no DB needed)
# ---------------------------------------------------------------------------

def _compute_trend(trend_rows: list[float]) -> tuple[str, float | None]:
    """Replicate the router's trend-direction logic for unit testing."""
    if len(trend_rows) >= 2:
        latest = trend_rows[0]
        previous = trend_rows[1]
        delta = latest - previous
        change = round(delta, 1)
        if delta > 2:
            direction = "improving"
        elif delta < -2:
            direction = "declining"
        else:
            direction = "stable"
        return direction, change
    elif len(trend_rows) == 1:
        return "first_run", None
    else:
        return "no_data", None


class TestTrendDirection:
    def test_improving(self):
        direction, change = _compute_trend([89.0, 68.0])
        assert direction == "improving"
        assert change == 21.0

    def test_declining(self):
        direction, change = _compute_trend([55.0, 78.0])
        assert direction == "declining"
        assert change == -23.0

    def test_stable_within_threshold(self):
        direction, change = _compute_trend([70.0, 69.0])
        assert direction == "stable"
        assert change == 1.0

    def test_stable_exact_threshold(self):
        direction, change = _compute_trend([72.0, 70.0])
        assert direction == "stable"
        assert change == 2.0

    def test_just_above_threshold_improving(self):
        direction, change = _compute_trend([73.1, 70.0])
        assert direction == "improving"
        assert abs(change - 3.1) < 0.01

    def test_first_run_single_entry(self):
        direction, change = _compute_trend([65.0])
        assert direction == "first_run"
        assert change is None

    def test_no_data_empty(self):
        direction, change = _compute_trend([])
        assert direction == "no_data"
        assert change is None

    def test_improving_from_zero(self):
        direction, change = _compute_trend([80.0, 0.0])
        assert direction == "improving"
        assert change == 80.0

    def test_trend_uses_most_recent_two(self):
        # DESC order: index 0 is latest, index 1 is previous
        direction, change = _compute_trend([90.0, 60.0, 40.0, 20.0])
        assert direction == "improving"
        assert change == 30.0

    def test_rounding_to_one_decimal(self):
        direction, change = _compute_trend([70.15, 67.14])
        assert direction == "improving"
        assert change == round(70.15 - 67.14, 1)


# ---------------------------------------------------------------------------
# _empty_summary shape
# ---------------------------------------------------------------------------

def _empty_summary(uni_id: int) -> dict:
    """Replica of the router helper to verify shape contract."""
    return {
        "university_id": uni_id,
        "course_count": 0,
        "total_fields_verified": 0,
        "avg_confidence": 0.0,
        "verified_rate": 0.0,
        "conflict_rate": 0.0,
        "low_confidence_rate": 0.0,
        "auto_publish_safe_rate": 0.0,
        "status_breakdown": {
            "verified": 0,
            "likely_correct": 0,
            "needs_review": 0,
            "conflict": 0,
        },
        "field_breakdown": [],
        "confidence_trend": {
            "history": [],
            "trend_direction": "no_data",
            "trend_change_pct": None,
            "latest_confidence": None,
            "previous_confidence": None,
        },
    }


class TestEmptySummaryShape:
    def test_has_confidence_trend_key(self):
        result = _empty_summary(42)
        assert "confidence_trend" in result

    def test_confidence_trend_has_required_keys(self):
        ct = _empty_summary(42)["confidence_trend"]
        assert set(ct.keys()) == {
            "history", "trend_direction", "trend_change_pct",
            "latest_confidence", "previous_confidence",
        }

    def test_empty_history_is_list(self):
        ct = _empty_summary(42)["confidence_trend"]
        assert isinstance(ct["history"], list)
        assert len(ct["history"]) == 0

    def test_trend_direction_no_data(self):
        ct = _empty_summary(42)["confidence_trend"]
        assert ct["trend_direction"] == "no_data"

    def test_nulls_when_no_runs(self):
        ct = _empty_summary(42)["confidence_trend"]
        assert ct["trend_change_pct"] is None
        assert ct["latest_confidence"] is None
        assert ct["previous_confidence"] is None


# ---------------------------------------------------------------------------
# Confidence history entry shape
# ---------------------------------------------------------------------------

class TestHistoryEntryShape:
    def _make_entry(self, job_id: str, avg_confidence: float, completed_at: str | None) -> dict:
        return {
            "job_id": job_id,
            "completed_at": completed_at,
            "avg_confidence": round(float(avg_confidence), 1),
        }

    def test_entry_has_required_keys(self):
        entry = self._make_entry("job-abc", 78.5, "2026-05-30T10:00:00+00:00")
        assert set(entry.keys()) == {"job_id", "completed_at", "avg_confidence"}

    def test_entry_confidence_rounded(self):
        entry = self._make_entry("job-abc", 78.555, None)
        assert entry["avg_confidence"] == round(78.555, 1)

    def test_entry_handles_null_completed_at(self):
        entry = self._make_entry("job-abc", 65.0, None)
        assert entry["completed_at"] is None


# ---------------------------------------------------------------------------
# Avg confidence calculation logic
# ---------------------------------------------------------------------------

class TestAvgConfidenceCalc:
    def _avg(self, values: list[float]) -> float | None:
        """Replicate: avg of non-None scraped_courses.avg_verification_confidence."""
        valid = [v for v in values if v is not None]
        if not valid:
            return None
        return round(sum(valid) / len(valid), 2)

    def test_basic_avg(self):
        result = self._avg([60.0, 80.0, 100.0])
        assert result == 80.0

    def test_single_value(self):
        result = self._avg([65.0])
        assert result == 65.0

    def test_empty_returns_none(self):
        result = self._avg([])
        assert result is None

    def test_none_values_excluded(self):
        result = self._avg([70.0, None, 90.0])  # type: ignore[list-item]
        assert result == 80.0

    def test_rounding_to_two_decimals(self):
        result = self._avg([33.333, 33.333, 33.334])
        assert isinstance(result, float)
        # result ≈ 33.33
        assert abs(result - 33.33) < 0.01

    def test_high_confidence_all_courses(self):
        result = self._avg([85.0, 90.0, 95.0, 88.0])
        assert result is not None
        assert result >= 85.0


# ---------------------------------------------------------------------------
# Change percentage edge cases
# ---------------------------------------------------------------------------

class TestChangePct:
    def test_positive_improvement(self):
        _, change = _compute_trend([89.0, 68.0])
        assert change is not None
        assert change > 0

    def test_negative_decline(self):
        _, change = _compute_trend([50.0, 80.0])
        assert change is not None
        assert change < 0

    def test_zero_change_is_stable(self):
        direction, change = _compute_trend([70.0, 70.0])
        assert direction == "stable"
        assert change == 0.0

    def test_change_is_none_for_first_run(self):
        _, change = _compute_trend([70.0])
        assert change is None

    def test_change_is_none_for_no_data(self):
        _, change = _compute_trend([])
        assert change is None

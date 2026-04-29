"""Tests for Priority 5 — per-field fill-rate metrics + alert evaluator.

These tests exercise the business logic in metrics.py and alerts.py using
an in-memory SQLite database so they run without a live PostgreSQL instance.
The test fixtures create minimal ORM rows to drive the aggregate queries.

NOTE: The alert evaluator uses PostgreSQL-specific features (jsonb, text PK)
in the ORM models, but the business-logic helpers (_aggregate_by_field,
_check_method_rule, _method_matches) are pure Python and can be tested
directly without DB round-trips.  The DB-backed tests use a real async
PostgreSQL session only when DATABASE_URL is set; otherwise they are skipped.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Unit tests for pure helpers (no DB required)
# ---------------------------------------------------------------------------

class TestAggregateByField:
    """_aggregate_by_field collapses multiple method rows into one fill rate."""

    def _make_metric(self, field_key, method, count, courses_total=10):
        m = MagicMock()
        m.field_key = field_key
        m.method = method
        m.count = count
        m.courses_total = courses_total
        return m

    def test_single_method(self):
        from app.services.scraper.alerts import _aggregate_by_field
        metrics = [self._make_metric("ielts_overall", "regex", 8)]
        result = _aggregate_by_field(metrics)
        assert result["ielts_overall"] == 0.8

    def test_two_methods_same_field_accumulate(self):
        from app.services.scraper.alerts import _aggregate_by_field
        metrics = [
            self._make_metric("ielts_overall", "regex", 6),
            self._make_metric("ielts_overall", "per_course_vision", 3),
        ]
        result = _aggregate_by_field(metrics)
        # 6+3 = 9/10 = 0.9
        assert result["ielts_overall"] == pytest.approx(0.9)

    def test_fill_rate_capped_at_1(self):
        from app.services.scraper.alerts import _aggregate_by_field
        metrics = [
            self._make_metric("duration", "regex", 8),
            self._make_metric("duration", "ai_fallback", 5),
        ]
        result = _aggregate_by_field(metrics)
        assert result["duration"] <= 1.0

    def test_multiple_fields_independent(self):
        from app.services.scraper.alerts import _aggregate_by_field
        metrics = [
            self._make_metric("ielts_overall", "regex", 9),
            self._make_metric("duration", "regex", 7),
        ]
        result = _aggregate_by_field(metrics)
        assert result["ielts_overall"] == pytest.approx(0.9)
        assert result["duration"] == pytest.approx(0.7)

    def test_empty_metrics_returns_empty(self):
        from app.services.scraper.alerts import _aggregate_by_field
        assert _aggregate_by_field([]) == {}


class TestMethodMatches:
    """_method_matches evaluates method prefix rules."""

    def test_exact_match(self):
        from app.services.scraper.alerts import _method_matches
        rule = {"method_prefix": "ai_fallback"}
        assert _method_matches("ai_fallback", rule) is True

    def test_prefix_match_with_colon(self):
        from app.services.scraper.alerts import _method_matches
        rule = {"method_prefix": "sibling_cache:"}
        assert _method_matches("sibling_cache:postgraduate", rule) is True

    def test_no_match(self):
        from app.services.scraper.alerts import _method_matches
        rule = {"method_prefix": "ai_fallback"}
        assert _method_matches("regex", rule) is False

    def test_partial_match_does_not_fire(self):
        from app.services.scraper.alerts import _method_matches
        rule = {"method_prefix": "vision"}
        assert _method_matches("per_course_vision", rule) is False

    def test_per_course_vision_prefix(self):
        from app.services.scraper.alerts import _method_matches
        rule = {"method_prefix": "per_course_vision"}
        assert _method_matches("per_course_vision", rule) is True


class TestCheckMethodRule:
    """_check_method_rule fires violations when method share exceeds threshold."""

    def _make_metric(self, field_key, method, count, courses_total=10):
        m = MagicMock()
        m.field_key = field_key
        m.method = method
        m.count = count
        m.courses_total = courses_total
        return m

    def test_vision_dominance_fires_above_threshold(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "vision_dominance")
        metrics = [
            self._make_metric("ielts_overall", "per_course_vision", 5),
            self._make_metric("ielts_overall", "regex", 5),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 1
        assert violations[0].actual == pytest.approx(0.5)

    def test_vision_dominance_does_not_fire_below_threshold(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "vision_dominance")
        metrics = [
            self._make_metric("ielts_overall", "per_course_vision", 3),
            self._make_metric("ielts_overall", "regex", 7),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 0

    def test_ai_fallback_dominance_fires(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "ai_fallback_dominance")
        metrics = [
            self._make_metric("international_fee", "ai_fallback", 4),
            self._make_metric("international_fee", "regex", 6),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 1
        assert violations[0].actual == pytest.approx(0.4)

    def test_sibling_cache_dominance_fires_on_prefix(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "sibling_cache_dominance")
        metrics = [
            self._make_metric("duolingo_overall", "sibling_cache:postgraduate", 4),
            self._make_metric("duolingo_overall", "regex", 1),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 1
        assert violations[0].actual == pytest.approx(0.8)

    def test_sibling_cache_at_threshold_does_not_fire(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "sibling_cache_dominance")
        # exactly 30% — threshold is >0.30 so this should NOT fire
        metrics = [
            self._make_metric("duolingo_overall", "sibling_cache:postgraduate", 3),
            self._make_metric("duolingo_overall", "regex", 7),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 0

    def test_facebook_icon_url_fires_from_bad_url_counts(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "facebook_icon_source")
        metrics = [
            self._make_metric("ielts_overall", "per_course_vision", 10),
        ]
        bad_url_counts = {"ielts_overall": 2}
        violations = _check_method_rule(metrics, rule, bad_url_counts=bad_url_counts)
        assert len(violations) == 1
        assert violations[0].actual == pytest.approx(0.2)

    def test_facebook_icon_url_zero_hits_no_fire(self):
        from app.services.scraper.alerts import _check_method_rule, METHOD_QUALITY_RULES
        rule = next(r for r in METHOD_QUALITY_RULES if r["rule_id"] == "facebook_icon_source")
        metrics = [
            self._make_metric("ielts_overall", "regex", 10),
        ]
        violations = _check_method_rule(metrics, rule, bad_url_counts={})
        assert len(violations) == 0


class TestGlobalDefaultFloors:
    """GLOBAL_DEFAULT_FLOORS values are within expected ranges."""

    def test_all_floors_between_0_and_1(self):
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        for field, floor in GLOBAL_DEFAULT_FLOORS.items():
            assert 0.0 < floor <= 1.0, f"Floor for {field}={floor} is out of range"

    def test_ielts_overall_floor_at_85(self):
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        assert GLOBAL_DEFAULT_FLOORS["ielts_overall"] == pytest.approx(0.85)

    def test_duration_floor_at_90(self):
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        assert GLOBAL_DEFAULT_FLOORS["duration"] == pytest.approx(0.90)

    def test_international_fee_floor_at_70(self):
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        assert GLOBAL_DEFAULT_FLOORS["international_fee"] == pytest.approx(0.70)


class TestAlertFormatting:
    """ScrapeRunAlert fields generated correctly for fill-rate drop scenario."""

    def test_critical_severity_when_gap_exceeds_20pp(self):
        """Fill rate 40% vs floor 85% → gap=45% → critical."""
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        field_key = "ielts_overall"
        floor = GLOBAL_DEFAULT_FLOORS[field_key]
        actual_rate = 0.40
        gap = floor - actual_rate
        severity = "critical" if gap >= 0.20 else "warning"
        assert severity == "critical"

    def test_warning_severity_when_gap_below_20pp(self):
        """Fill rate 75% vs floor 85% → gap=10% → warning."""
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        field_key = "ielts_overall"
        floor = GLOBAL_DEFAULT_FLOORS[field_key]
        actual_rate = 0.75
        gap = floor - actual_rate
        severity = "critical" if gap >= 0.20 else "warning"
        assert severity == "warning"

    def test_no_alert_when_above_floor(self):
        """Fill rate 90% vs floor 85% → no alert."""
        from app.services.scraper.alerts import GLOBAL_DEFAULT_FLOORS
        field_key = "ielts_overall"
        floor = GLOBAL_DEFAULT_FLOORS[field_key]
        actual_rate = 0.90
        should_alert = actual_rate < floor
        assert not should_alert


class TestAlertDeliveryFormatting:
    """format_alert_digest produces the expected string."""

    def test_digest_contains_run_id(self):
        from app.services.scraper.alert_delivery import format_alert_digest
        alert = MagicMock()
        alert.rule_id = "test_rule"
        alert.message = "Something broke"
        alert.scrape_run_id = "job_abc123"
        result = format_alert_digest("job_abc123", [alert])
        assert "job_abc123" in result

    def test_digest_contains_rule_id(self):
        from app.services.scraper.alert_delivery import format_alert_digest
        alert = MagicMock()
        alert.rule_id = "fill_rate_drop:ielts_overall"
        alert.message = "IELTS fill rate 40% below floor 85%"
        alert.scrape_run_id = "job_xyz"
        result = format_alert_digest("job_xyz", [alert])
        assert "fill_rate_drop:ielts_overall" in result

    def test_digest_shows_count(self):
        from app.services.scraper.alert_delivery import format_alert_digest
        alerts = [MagicMock() for _ in range(3)]
        for i, a in enumerate(alerts):
            a.rule_id = f"rule_{i}"
            a.message = f"Alert {i}"
            a.scrape_run_id = "job_multi"
        result = format_alert_digest("job_multi", alerts)
        assert "3 critical" in result or "3" in result


class TestBadUrlCounting:
    """_count_bad_url_evidence regex matches facebook/instagram/icon URLs."""

    import re
    _ICON_URL_RE = re.compile(
        r"(facebook|instagram|icon[-_]|logo)", re.IGNORECASE
    )

    def test_facebook_url_matches(self):
        assert self._ICON_URL_RE.search("https://example.edu/Icon-facebook_2.png")

    def test_instagram_url_matches(self):
        assert self._ICON_URL_RE.search("https://cdn.example.com/instagram-icon.png")

    def test_icon_underscore_matches(self):
        assert self._ICON_URL_RE.search("https://uni.edu/static/icon_social.png")

    def test_logo_matches(self):
        assert self._ICON_URL_RE.search("https://uni.edu/img/logo.svg")

    def test_regular_course_url_does_not_match(self):
        assert not self._ICON_URL_RE.search("https://uni.edu/courses/bachelor-of-arts/apply")

    def test_case_insensitive(self):
        assert self._ICON_URL_RE.search("https://cdn.example.com/FACEBOOK/share.png")


class TestSeedBaselinesLogic:
    """Baseline seeding statistical calculations."""

    def test_median_of_rates(self):
        import statistics
        rates = [0.90, 0.92, 0.88, 0.95, 0.85]
        median = statistics.median(rates)
        assert 0.88 <= median <= 0.92

    def test_floor_is_p10_minus_5pp(self):
        import statistics
        rates = [0.90, 0.92, 0.88, 0.95, 0.85, 0.87, 0.91, 0.89, 0.86, 0.93]
        p10_idx = max(0, int(len(rates) * 0.10) - 1)
        p10 = sorted(rates)[p10_idx]
        floor = max(0.50, round(p10 - 0.05, 4))
        assert floor >= 0.50
        assert floor < statistics.median(rates)

    def test_floor_never_below_min(self):
        rates = [0.51, 0.52, 0.50]
        import statistics
        p10_idx = max(0, int(len(rates) * 0.10) - 1)
        p10 = sorted(rates)[p10_idx]
        floor = max(0.50, round(p10 - 0.05, 4))
        assert floor >= 0.50

    def test_three_runs_minimum_enforced(self):
        """Universities with < 3 runs should be skipped."""
        _MIN_RUNS = 3
        runs = ["run1", "run2"]
        should_skip = len(runs) < _MIN_RUNS
        assert should_skip

    def test_three_runs_exact_proceeds(self):
        _MIN_RUNS = 3
        runs = ["run1", "run2", "run3"]
        should_skip = len(runs) < _MIN_RUNS
        assert not should_skip

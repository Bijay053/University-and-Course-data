"""Phase 13: Autonomous Monitoring Engine — unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.monitoring_engine import (
    _ema,
    _sha256,
    apply_probe_result,
    compute_next_check_at,
    detect_change,
    get_monitoring_stats,
    get_or_create_watcher,
    next_check_interval_hours,
)


def _make_watcher(**kwargs) -> MagicMock:
    w = MagicMock()
    w.id = 1
    w.university_id = 42
    w.monitoring_strategy = kwargs.get("monitoring_strategy", "passive")
    w.probe_url = kwargs.get("probe_url", "https://example.edu/courses")
    w.etag = kwargs.get("etag", None)
    w.page_hash = kwargs.get("page_hash", None)
    w.sitemap_hash = kwargs.get("sitemap_hash", None)
    w.consecutive_unchanged = kwargs.get("consecutive_unchanged", 0)
    w.total_checks = kwargs.get("total_checks", 0)
    w.total_changes_detected = kwargs.get("total_changes_detected", 0)
    w.total_scrapes_triggered = kwargs.get("total_scrapes_triggered", 0)
    w.change_frequency_days = kwargs.get("change_frequency_days", None)
    w.last_checked_at = kwargs.get("last_checked_at", None)
    w.last_changed_at = kwargs.get("last_changed_at", None)
    w.last_triggered_at = kwargs.get("last_triggered_at", None)
    w.last_scrape_job_id = kwargs.get("last_scrape_job_id", None)
    w.last_probe_result = kwargs.get("last_probe_result", None)
    w.last_probe_status_code = kwargs.get("last_probe_status_code", None)
    w.last_probe_error = kwargs.get("last_probe_error", None)
    w.next_check_at = kwargs.get("next_check_at", None)
    w.enabled = kwargs.get("enabled", True)
    return w


# ── next_check_interval_hours ─────────────────────────────────────────────────

class TestNextCheckInterval:
    def test_none_returns_90_days(self):
        # Default is 90 days — realistic for annual-cycle universities
        assert next_check_interval_hours(None) == 90 * 24.0

    def test_very_frequent_under_14d_returns_3d(self):
        assert next_check_interval_hours(7.0) == 72.0

    def test_frequent_under_30d_returns_7d(self):
        assert next_check_interval_hours(20.0) == 168.0

    def test_moderate_under_60d_returns_14d(self):
        assert next_check_interval_hours(45.0) == 336.0

    def test_slow_under_120d_returns_30d(self):
        assert next_check_interval_hours(90.0) == 720.0

    def test_stable_over_120d_returns_60d(self):
        assert next_check_interval_hours(200.0) == 1440.0

    def test_very_fast_under_14d_boundary(self):
        assert next_check_interval_hours(1.0) == 72.0

    def test_boundary_exactly_14d_returns_weekly(self):
        # < 14 → 3 days; ≥ 14 → 7 days
        assert next_check_interval_hours(14.0) == 168.0


# ── compute_next_check_at ─────────────────────────────────────────────────────

class TestComputeNextCheckAt:
    def test_returns_future_datetime(self):
        result = compute_next_check_at(None)
        assert result > datetime.now(timezone.utc)

    def test_default_is_90_days_from_now(self):
        result = compute_next_check_at(None)
        diff_days = (result - datetime.now(timezone.utc)).total_seconds() / 86400
        assert abs(diff_days - 90) < 0.01

    def test_fast_change_sooner_than_slow(self):
        fast = compute_next_check_at(7.0)
        slow = compute_next_check_at(200.0)
        assert fast < slow

    def test_from_dt_anchors_to_past_date(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = compute_next_check_at(None, from_dt=base)
        expected = base + timedelta(days=90)
        assert result == expected

    def test_from_dt_last_scrape_schedules_correctly(self):
        # A university scraped 30 days ago → next probe in 60 more days (~90d from scrape)
        last_scrape = datetime.now(timezone.utc) - timedelta(days=30)
        result = compute_next_check_at(None, from_dt=last_scrape)
        diff_days = (result - datetime.now(timezone.utc)).total_seconds() / 86400
        assert abs(diff_days - 60) < 0.1


# ── _sha256 ───────────────────────────────────────────────────────────────────

class TestSha256:
    def test_deterministic(self):
        assert _sha256(b"hello") == _sha256(b"hello")

    def test_different_content(self):
        assert _sha256(b"hello") != _sha256(b"world")

    def test_returns_hex_string(self):
        result = _sha256(b"test")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


# ── _ema ──────────────────────────────────────────────────────────────────────

class TestEma:
    def test_none_prev_returns_new(self):
        assert _ema(None, 7.0) == 7.0

    def test_ema_blends_values(self):
        result = _ema(10.0, 20.0, alpha=0.5)
        assert result == 15.0

    def test_low_alpha_weights_prev(self):
        result = _ema(10.0, 20.0, alpha=0.1)
        assert abs(result - 11.0) < 0.01

    def test_high_alpha_weights_new(self):
        result = _ema(10.0, 20.0, alpha=0.9)
        assert abs(result - 19.0) < 0.01


# ── detect_change ─────────────────────────────────────────────────────────────

class TestDetectChange:
    def test_no_fingerprint_and_error_is_not_change(self):
        w = _make_watcher()
        assert not detect_change(w, {"error": "timeout"})

    def test_etag_mismatch_is_change(self):
        w = _make_watcher(etag='"abc123"')
        assert detect_change(w, {"etag": '"def456"', "page_hash": None, "sitemap_hash": None})

    def test_etag_match_is_not_change(self):
        w = _make_watcher(etag='"abc123"')
        assert not detect_change(w, {"etag": '"abc123"', "page_hash": None, "sitemap_hash": None})

    def test_page_hash_mismatch_is_change(self):
        w = _make_watcher(page_hash="aaaa", etag=None)
        assert detect_change(w, {"page_hash": "bbbb", "etag": None, "sitemap_hash": None})

    def test_page_hash_match_is_not_change(self):
        w = _make_watcher(page_hash="aaaa", etag=None)
        assert not detect_change(w, {"page_hash": "aaaa", "etag": None, "sitemap_hash": None})

    def test_sitemap_hash_mismatch_is_change(self):
        w = _make_watcher(sitemap_hash="s1", etag=None, page_hash=None)
        assert detect_change(w, {"sitemap_hash": "s2", "etag": None, "page_hash": None})

    def test_first_ever_probe_no_baseline_not_change(self):
        w = _make_watcher(etag=None, page_hash=None, sitemap_hash=None)
        assert not detect_change(w, {"etag": '"first"', "page_hash": None, "sitemap_hash": None})

    def test_error_probe_is_not_change(self):
        w = _make_watcher(etag='"abc"')
        assert not detect_change(w, {"error": "connection refused", "etag": None, "page_hash": None})


# ── apply_probe_result ────────────────────────────────────────────────────────

class TestApplyProbeResult:
    async def test_error_sets_error_result(self):
        w = _make_watcher()
        db = AsyncMock()
        await apply_probe_result(w, {"error": "timeout", "status_code": None, "etag": None, "page_hash": None, "sitemap_hash": None}, False, db)
        assert w.last_probe_result == "error"
        assert w.total_checks == 1

    async def test_unchanged_increments_consecutive(self):
        w = _make_watcher(consecutive_unchanged=3)
        db = AsyncMock()
        await apply_probe_result(w, {"status_code": 200, "etag": None, "page_hash": None, "sitemap_hash": None}, False, db)
        assert w.last_probe_result == "unchanged"
        assert w.consecutive_unchanged == 4

    async def test_changed_resets_consecutive(self):
        w = _make_watcher(consecutive_unchanged=5, last_changed_at=None)
        db = AsyncMock()
        await apply_probe_result(w, {"status_code": 200, "etag": '"new"', "page_hash": None, "sitemap_hash": None}, True, db)
        assert w.last_probe_result == "changed"
        assert w.consecutive_unchanged == 0
        assert w.total_changes_detected == 1

    async def test_new_etag_stored(self):
        w = _make_watcher(etag='"old"')
        db = AsyncMock()
        await apply_probe_result(w, {"status_code": 200, "etag": '"new"', "page_hash": None, "sitemap_hash": None}, False, db)
        assert w.etag == '"new"'

    async def test_new_page_hash_stored(self):
        w = _make_watcher()
        db = AsyncMock()
        await apply_probe_result(w, {"status_code": 200, "etag": None, "page_hash": "abc123", "sitemap_hash": None}, False, db)
        assert w.page_hash == "abc123"

    async def test_next_check_at_updated(self):
        w = _make_watcher()
        db = AsyncMock()
        await apply_probe_result(w, {"status_code": 200, "etag": None, "page_hash": None, "sitemap_hash": None}, False, db)
        assert w.next_check_at is not None
        assert w.next_check_at > datetime.now(timezone.utc)


# ── get_or_create_watcher ─────────────────────────────────────────────────────

class TestGetOrCreateWatcher:
    async def test_returns_existing(self):
        existing = _make_watcher()
        db = AsyncMock()
        r = MagicMock()
        r.scalar_one_or_none.return_value = existing
        db.execute.return_value = r
        result = await get_or_create_watcher(42, db)
        assert result is existing

    async def test_creates_new_when_missing(self):
        db = AsyncMock()
        r1 = MagicMock(); r1.scalar_one_or_none.return_value = None  # watcher lookup
        uni = MagicMock()
        uni.scrape_url = "https://example.edu/courses"
        uni.website = ""
        r2 = MagicMock(); r2.scalar_one_or_none.return_value = uni   # university lookup
        r3 = MagicMock(); r3.scalar_one_or_none.return_value = None  # last scrape lookup
        db.execute.side_effect = [r1, r2, r3]

        from app.models.university_watcher import UniversityWatcher
        created = []
        def capture_add(obj):
            created.append(obj)
        db.add = capture_add

        result = await get_or_create_watcher(42, db)
        assert len(created) == 1
        assert isinstance(created[0], UniversityWatcher)
        assert created[0].probe_url == "https://example.edu/courses"


# ── monitoring_stats ──────────────────────────────────────────────────────────

class TestGetMonitoringStats:
    def _make_db_with_counts(self, counts: list) -> AsyncMock:
        db = AsyncMock()
        results = []
        for c in counts:
            r = MagicMock()
            r.scalar.return_value = c
            results.append(r)
        db.execute.side_effect = results
        return db

    async def test_returns_dict_with_all_keys(self):
        # 6 db.execute calls: total, enabled, changed_today, triggered_today, due_now, avg_freq
        db = self._make_db_with_counts([10, 8, 2, 3, 1, 5.5])
        result = await get_monitoring_stats(db)
        assert "total_watchers" in result
        assert "enabled" in result
        assert "changed_today" in result
        assert "scrapes_triggered_today" in result
        assert "due_for_check" in result

    async def test_disabled_computed_from_total_minus_enabled(self):
        db = self._make_db_with_counts([10, 7, 0, 0, 0, None])
        result = await get_monitoring_stats(db)
        assert result["disabled"] == 3

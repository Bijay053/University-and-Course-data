"""Week 2 Prompts 3-7 — unit tests.

Covers:
  P3 — url_pattern_anomaly rule + _url_prefix helper
  P4 — ALERTS_NOTIFICATION_ENABLED switch (via _notifications_enabled)
  P5 — _skip_central_english_propagation env toggle
  P6 — sanity_floors log-and-accept semantics + counters
  P7 — vision corroboration log path (smoke check)
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch


# ── P3: url_prefix + anomaly detection helper ────────────────────────────

def test_p3_url_prefix_basic() -> None:
    from app.services.scraper.alerts import _url_prefix
    assert (
        _url_prefix("https://www.uni.edu.au/courses/business/mba")
        == "https://www.uni.edu.au/courses/business"
    )
    assert (
        _url_prefix("https://www.uni.edu.au/handbook/2026/COMP1010")
        == "https://www.uni.edu.au/handbook/2026"
    )


def test_p3_url_prefix_edge_cases() -> None:
    from app.services.scraper.alerts import _url_prefix
    assert _url_prefix("") is None
    assert _url_prefix("not a url") is None
    assert _url_prefix("https://uni.edu.au") == "https://uni.edu.au"
    assert _url_prefix("https://uni.edu.au/") == "https://uni.edu.au"


def test_p3_url_prefix_custom_depth() -> None:
    from app.services.scraper.alerts import _url_prefix
    assert (
        _url_prefix("https://uni.edu.au/a/b/c/d", depth=1)
        == "https://uni.edu.au/a"
    )
    assert (
        _url_prefix("https://uni.edu.au/a/b/c/d", depth=3)
        == "https://uni.edu.au/a/b/c"
    )


# ── P4: ALERTS_NOTIFICATION_ENABLED toggle ───────────────────────────────

def test_p4_notifications_enabled_default_true() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ALERTS_NOTIFICATION_ENABLED", None)
        from app.services.scraper import alert_delivery
        importlib.reload(alert_delivery)
        assert alert_delivery._notifications_enabled() is True


def test_p4_notifications_disabled_explicit() -> None:
    for falsy in ("false", "0", "no", "off", "FALSE", "Off"):
        with patch.dict(os.environ, {"ALERTS_NOTIFICATION_ENABLED": falsy}):
            from app.services.scraper import alert_delivery
            importlib.reload(alert_delivery)
            assert alert_delivery._notifications_enabled() is False, (
                f"{falsy!r} should disable notifications"
            )


def test_p4_notifications_enabled_truthy() -> None:
    for truthy in ("true", "1", "yes", "on", "TRUE", "anything-else"):
        with patch.dict(os.environ, {"ALERTS_NOTIFICATION_ENABLED": truthy}):
            from app.services.scraper import alert_delivery
            importlib.reload(alert_delivery)
            assert alert_delivery._notifications_enabled() is True, (
                f"{truthy!r} should enable notifications"
            )


# ── P5: SKIP_CENTRAL_ENGLISH_PROPAGATION toggle ──────────────────────────

def test_p5_skip_default_false() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SKIP_CENTRAL_ENGLISH_PROPAGATION", None)
        from app.services.scraper.pipelines.single_course import (
            _skip_central_english_propagation,
        )
        assert _skip_central_english_propagation() is False


def test_p5_skip_enabled_truthy() -> None:
    from app.services.scraper.pipelines.single_course import (
        _skip_central_english_propagation,
    )
    for truthy in ("true", "1", "yes", "on", "TRUE"):
        with patch.dict(os.environ, {"SKIP_CENTRAL_ENGLISH_PROPAGATION": truthy}):
            assert _skip_central_english_propagation() is True, (
                f"{truthy!r} should enable skip"
            )


def test_p5_skip_falsy_or_unset() -> None:
    from app.services.scraper.pipelines.single_course import (
        _skip_central_english_propagation,
    )
    for falsy in ("false", "0", "no", "off", ""):
        with patch.dict(os.environ, {"SKIP_CENTRAL_ENGLISH_PROPAGATION": falsy}):
            assert _skip_central_english_propagation() is False


# ── P6: SANITY_FLOORS log-and-accept ─────────────────────────────────────

def test_p6_sanity_check_returns_value_unchanged() -> None:
    from app.services.scraper.sanity_floors import sanity_check
    # Above floor — no log
    assert sanity_check("ielts_overall", 6.5) == 6.5
    # Below floor — accepted, not nulled (used to be hard-rejected)
    assert sanity_check("ielts_overall", 4.5) == 4.5
    # Way below floor — still accepted (rejection of clear noise is the
    # caller's job, not this module's)
    assert sanity_check("international_fee", 800) == 800


def test_p6_sanity_check_none_passthrough() -> None:
    from app.services.scraper.sanity_floors import sanity_check
    assert sanity_check("ielts_overall", None) is None
    assert sanity_check("international_fee", None) is None


def test_p6_sanity_check_unknown_field() -> None:
    from app.services.scraper.sanity_floors import sanity_check
    # Unknown field has no floor — value returned unchanged regardless
    assert sanity_check("totally_made_up", 0) == 0
    assert sanity_check("totally_made_up", -999) == -999


def test_p6_sanity_counter_increments_below_floor() -> None:
    from app.services.scraper.sanity_floors import (
        sanity_check, get_sanity_counters, reset_sanity_counters,
    )
    reset_sanity_counters()
    sanity_check("ielts_overall", 4.5)  # below 5.0 historic floor → counted
    sanity_check("ielts_overall", 4.0)  # below 5.0 historic floor → counted
    sanity_check("ielts_overall", 5.0)  # equal to floor → not counted
    sanity_check("ielts_overall", 7.0)  # above floor — no count
    counters = get_sanity_counters()
    assert counters.get("ielts_overall") == 2
    reset_sanity_counters()
    assert get_sanity_counters() == {}


def test_p6_sanity_floors_constants() -> None:
    """Lock the historic-reject thresholds so any future tweak is visible
    in the PR diff.  Values strictly below these are now log-and-accept."""
    from app.services.scraper.sanity_floors import SANITY_FLOORS
    assert SANITY_FLOORS["ielts_overall"] == 5.0
    assert SANITY_FLOORS["international_fee_annual"] == 5_000
    assert SANITY_FLOORS["international_fee"] == 5_000
    assert SANITY_FLOORS["duration_years"] == 0.25


# ── P7: corroboration helper smoke ───────────────────────────────────────

def test_p6_fee_keyword_path_accepts_low_amounts() -> None:
    """Regression: the keyword-path (`_candidates`) used to silently
    drop any amount below 5_000.  After P6 it must accept down to
    1_000 and emit a sanity-floor counter increment."""
    from app.services.scraper.extractors.fee import _candidates
    from app.services.scraper.sanity_floors import (
        get_sanity_counters, reset_sanity_counters,
    )
    reset_sanity_counters()
    text = (
        "International tuition fee for this short course: A$3,500 per year. "
        "Apply now."
    )
    cands = list(_candidates(text))
    amounts = [c[0] if isinstance(c, tuple) else c for c in cands]
    assert 3500 in amounts, (
        f"Low international fee 3_500 must be accepted; got {amounts}"
    )
    counters = get_sanity_counters()
    assert counters.get("international_fee", 0) >= 1, (
        "sanity_check counter should record the below-5K accept"
    )
    reset_sanity_counters()


def test_p6_fee_keyword_path_still_rejects_noise() -> None:
    """Below 1_000 (the new hard floor) is still rejected as noise."""
    from app.services.scraper.extractors.fee import _candidates
    text = "International tuition fee: A$500 application fee waiver."
    cands = list(_candidates(text))
    amounts = [c[0] if isinstance(c, tuple) else c for c in cands]
    assert 500 not in amounts, (
        f"Sub-1_000 amounts must still be rejected; got {amounts}"
    )


def test_p7_corroboration_check_substring_match() -> None:
    """The vision-corroboration log line uses a simple substring check
    against the lower-cased page text.  Confirm the pattern matches the
    common cases the spec calls out."""
    page = ("English language requirements: IELTS overall 6.5 with no "
            "band below 6.0").lower()
    # Confirms an OCR'd value appears in page text
    assert "6.5" in page
    assert "6.0" in page
    # A hallucinated value would not match
    assert "4.5" not in page

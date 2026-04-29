"""Tests for the Gemini quota circuit breaker (Component 2).

Verifies:
  - Circuit opens after N quota failures within the time window
  - Circuit only trips on quota-type errors (429/503/keyword)
  - Circuit resets after the cool-down period
  - Non-quota errors (e.g. 500) do not trip the circuit
"""
from __future__ import annotations

import time

import pytest

from app.services.ai.gemini_client import GeminiQuotaTracker


def _make_tracker(**kw) -> GeminiQuotaTracker:
    defaults = dict(failure_threshold=5, window_seconds=60, cool_down_seconds=300)
    defaults.update(kw)
    return GeminiQuotaTracker(**defaults)


# ---------------------------------------------------------------------------

def test_circuit_opens_after_threshold():
    tracker = _make_tracker()
    for _ in range(5):
        tracker.record_failure(429, "Quota exceeded")
    assert tracker.is_circuit_open() is True


def test_circuit_only_counts_quota_errors():
    tracker = _make_tracker()
    for _ in range(10):
        tracker.record_failure(500, "Internal server error")
    assert tracker.is_circuit_open() is False


def test_circuit_does_not_open_below_threshold():
    tracker = _make_tracker()
    for _ in range(4):
        tracker.record_failure(429, "Quota exceeded")
    assert tracker.is_circuit_open() is False


def test_circuit_resets_after_cooldown():
    tracker = _make_tracker(cool_down_seconds=0.1)
    for _ in range(5):
        tracker.record_failure(429, "Quota exceeded")
    assert tracker.is_circuit_open() is True
    time.sleep(0.15)
    assert tracker.is_circuit_open() is False


def test_503_trips_circuit():
    tracker = _make_tracker()
    for _ in range(5):
        tracker.record_failure(503, "Service unavailable")
    assert tracker.is_circuit_open() is True


def test_keyword_rate_limit_trips_circuit():
    tracker = _make_tracker()
    for _ in range(5):
        tracker.record_failure(None, "resource_exhausted: rate limit exceeded")
    assert tracker.is_circuit_open() is True


def test_keyword_quota_message_trips_circuit():
    tracker = _make_tracker()
    for _ in range(5):
        tracker.record_failure(None, "quota exceeded for project abc123")
    assert tracker.is_circuit_open() is True


def test_circuit_not_open_initially():
    tracker = _make_tracker()
    assert tracker.is_circuit_open() is False


def test_time_until_close_positive_when_open():
    tracker = _make_tracker(cool_down_seconds=300)
    for _ in range(5):
        tracker.record_failure(429, "quota exceeded")
    t = tracker.time_until_circuit_close()
    assert t > 0.0
    assert t <= 300.0


def test_time_until_close_zero_when_closed():
    tracker = _make_tracker()
    assert tracker.time_until_circuit_close() == 0.0


def test_mixed_errors_only_quota_count():
    tracker = _make_tracker(failure_threshold=3)
    tracker.record_failure(500, "Internal error")   # doesn't count
    tracker.record_failure(429, "quota exceeded")   # counts (1)
    tracker.record_failure(500, "Internal error")   # doesn't count
    tracker.record_failure(429, "quota exceeded")   # counts (2)
    assert tracker.is_circuit_open() is False
    tracker.record_failure(429, "quota exceeded")   # counts (3) → open
    assert tracker.is_circuit_open() is True

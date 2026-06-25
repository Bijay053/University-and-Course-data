"""Tests for the per-run skip-gate counter module (Task #235).

Pure imports — only app.services.skip_counters (stdlib-only) is imported,
so this file can be collected and run in isolation under any CPU load,
without triggering the pydantic / google.genai import chain.
"""
from __future__ import annotations

import pytest

from app.services.skip_counters import (
    _COUNTER_KEYS,
    _skip_counts,
    get_skip_counts,
    note_skip,
    reset_skip_counters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset():
    """Hard-reset the ContextVar to None (pre-init state)."""
    _skip_counts.set(None)


# ---------------------------------------------------------------------------
# TestResetSkipCounters
# ---------------------------------------------------------------------------


class TestResetSkipCounters:
    def setup_method(self):
        _reset()

    def test_initialises_all_keys_to_zero(self):
        reset_skip_counters()
        counts = get_skip_counts()
        assert set(counts.keys()) == set(_COUNTER_KEYS)
        assert all(v == 0 for v in counts.values())

    def test_reset_clears_previous_increments(self):
        reset_skip_counters()
        note_skip("gemini_timeout")
        note_skip("gemini_timeout")
        assert get_skip_counts()["gemini_timeout"] == 2
        reset_skip_counters()
        assert get_skip_counts()["gemini_timeout"] == 0

    def test_double_reset_is_idempotent(self):
        reset_skip_counters()
        reset_skip_counters()
        assert all(v == 0 for v in get_skip_counts().values())


# ---------------------------------------------------------------------------
# TestNoteSkip
# ---------------------------------------------------------------------------


class TestNoteSkip:
    def setup_method(self):
        _reset()

    def test_noop_when_not_initialised(self):
        note_skip("gemini_timeout")
        assert get_skip_counts() == {}

    def test_all_five_counter_keys(self):
        reset_skip_counters()
        for key in _COUNTER_KEYS:
            note_skip(key)
        counts = get_skip_counts()
        for key in _COUNTER_KEYS:
            assert counts[key] == 1, f"{key} should be 1"

    def test_increments_accumulate(self):
        reset_skip_counters()
        for _ in range(7):
            note_skip("vision_early_exit")
        assert get_skip_counts()["vision_early_exit"] == 7

    def test_unknown_key_silently_ignored(self):
        reset_skip_counters()
        note_skip("nonexistent_gate")
        counts = get_skip_counts()
        assert "nonexistent_gate" not in counts
        assert all(v == 0 for v in counts.values())

    def test_multiple_counters_independent(self):
        reset_skip_counters()
        note_skip("gemini_timeout")
        note_skip("gemini_timeout")
        note_skip("gemini_circuit_open")
        note_skip("challenge_shell")
        note_skip("challenge_shell")
        note_skip("challenge_shell")
        counts = get_skip_counts()
        assert counts["gemini_timeout"] == 2
        assert counts["gemini_circuit_open"] == 1
        assert counts["vision_early_exit"] == 0
        assert counts["browser_http_skipped"] == 0
        assert counts["challenge_shell"] == 3

    def test_gemini_timeout(self):
        reset_skip_counters()
        note_skip("gemini_timeout")
        assert get_skip_counts()["gemini_timeout"] == 1

    def test_gemini_circuit_open(self):
        reset_skip_counters()
        note_skip("gemini_circuit_open")
        assert get_skip_counts()["gemini_circuit_open"] == 1

    def test_vision_early_exit(self):
        reset_skip_counters()
        note_skip("vision_early_exit")
        assert get_skip_counts()["vision_early_exit"] == 1

    def test_browser_http_skipped(self):
        reset_skip_counters()
        note_skip("browser_http_skipped")
        assert get_skip_counts()["browser_http_skipped"] == 1

    def test_challenge_shell(self):
        reset_skip_counters()
        note_skip("challenge_shell")
        assert get_skip_counts()["challenge_shell"] == 1


# ---------------------------------------------------------------------------
# TestGetSkipCounts
# ---------------------------------------------------------------------------


class TestGetSkipCounts:
    def setup_method(self):
        _reset()

    def test_returns_empty_dict_when_not_initialised(self):
        assert get_skip_counts() == {}

    def test_returns_snapshot_not_live_dict(self):
        reset_skip_counters()
        snapshot = get_skip_counts()
        note_skip("gemini_timeout")
        assert snapshot["gemini_timeout"] == 0
        assert get_skip_counts()["gemini_timeout"] == 1

    def test_nonzero_filter_idiom(self):
        reset_skip_counters()
        note_skip("gemini_circuit_open")
        note_skip("challenge_shell")
        note_skip("challenge_shell")
        nonzero = {k: v for k, v in get_skip_counts().items() if v}
        assert nonzero == {"gemini_circuit_open": 1, "challenge_shell": 2}

    def test_all_zeros_after_reset(self):
        reset_skip_counters()
        counts = get_skip_counts()
        assert counts
        assert all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# TestCounterKeys
# ---------------------------------------------------------------------------


class TestCounterKeys:
    def test_all_expected_keys_present(self):
        expected = {
            "gemini_timeout",
            "gemini_circuit_open",
            "vision_early_exit",
            "browser_http_skipped",
            "challenge_shell",
        }
        assert set(_COUNTER_KEYS) == expected

    def test_counter_keys_is_tuple(self):
        assert isinstance(_COUNTER_KEYS, tuple)

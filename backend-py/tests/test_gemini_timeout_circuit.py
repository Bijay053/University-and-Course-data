"""Task #233 — Gemini per-call timeout + circuit-breaker integration.

The per-course timeout used to live in an ``asyncio.wait_for`` *wrapping*
:func:`generate`.  On timeout that cancelled the inner coroutine before its
``except`` block ran, so timeouts never reached the quota tracker and never
tripped the breaker — every course on a slow-Gemini run paid the full timeout.

The timeout now lives *inside* ``generate`` (at the SDK boundary) and calls
``GeminiQuotaTracker.record_timeout`` directly.  These tests pin:

1. ``record_timeout`` opens the circuit once enough timeouts land in-window.
2. Timeouts and quota errors share the same deque / threshold.
3. The circuit re-closes after the cool-down.
4. ``generate(timeout_s=...)`` returns a *skipped* response (no raise, no
   retry) when the SDK call exceeds the timeout, and records the timeout.
"""
from __future__ import annotations

import asyncio
import collections
import sys
import types as _pytypes

import pytest

# When the real google-genai SDK has not already been imported (e.g. an isolated
# test run on a CPU-starved box where its heavy pydantic-model compilation
# stalls), install a tiny stub so generate()'s `from google.genai import types`
# + GenerateContentConfig(...) resolve instantly.  In the full suite the app has
# already imported the real SDK, so "google.genai" is in sys.modules and this is
# skipped — the real types are used.  We never stub the "google" namespace pkg
# itself, only the genai submodule, so google.auth/etc are untouched.
if "google.genai" not in sys.modules:
    _gg = _pytypes.ModuleType("google.genai")
    _ggt = _pytypes.ModuleType("google.genai.types")

    class _GenerateContentConfig:  # minimal stand-in
        def __init__(self, *_a, **_k) -> None:
            pass

    _ggt.GenerateContentConfig = _GenerateContentConfig
    _gg.types = _ggt
    sys.modules["google.genai"] = _gg
    sys.modules["google.genai.types"] = _ggt

from app.services.ai import gemini_client
from app.services.ai.gemini_client import GeminiQuotaTracker, GeminiResponse


def _mk(**kw) -> GeminiQuotaTracker:
    params = dict(failure_threshold=5, window_seconds=60, cool_down_seconds=300)
    params.update(kw)
    return GeminiQuotaTracker(**params)


def test_timeout_opens_circuit_at_threshold() -> None:
    t = _mk(failure_threshold=5)
    for _ in range(4):
        t.record_timeout(20.0)
    assert not t.is_circuit_open(), "circuit must stay closed below threshold"
    t.record_timeout(20.0)
    assert t.is_circuit_open(), "5th timeout in-window must open the circuit"


def test_timeout_below_threshold_stays_closed() -> None:
    t = _mk(failure_threshold=8)
    for _ in range(7):
        t.record_timeout(20.0)
    assert not t.is_circuit_open()


def test_timeout_and_quota_share_one_deque() -> None:
    """A mix of quota errors and timeouts must accumulate together."""
    t = _mk(failure_threshold=3)
    t.record_failure(429, "quota exceeded")
    t.record_timeout(20.0)
    assert not t.is_circuit_open(), "2 failures < threshold 3"
    t.record_timeout(20.0)
    assert t.is_circuit_open(), "3rd combined failure must trip the breaker"


def test_timeout_none_arg_is_safe() -> None:
    t = _mk(failure_threshold=2)
    t.record_timeout()
    t.record_timeout()
    assert t.is_circuit_open()


def test_circuit_recloses_after_cooldown() -> None:
    t = _mk(failure_threshold=2, cool_down_seconds=0.1)
    t.record_timeout(20.0)
    t.record_timeout(20.0)
    assert t.is_circuit_open()
    import time

    time.sleep(0.15)
    assert not t.is_circuit_open(), "circuit must re-close once cool-down elapses"


# ---------------------------------------------------------------------------
# generate() integration — timeout returns skipped (no raise / no retry)
# ---------------------------------------------------------------------------


class _SlowModels:
    async def generate_content(self, **_kw):  # noqa: ANN003
        await asyncio.sleep(5)  # far longer than the test timeout_s
        raise AssertionError("generate_content should have been cancelled")


class _SlowAio:
    def __init__(self) -> None:
        self.models = _SlowModels()


class _SlowClient:
    def __init__(self) -> None:
        self.aio = _SlowAio()


def _fresh_singleton(monkeypatch) -> GeminiQuotaTracker:
    """Swap the module singleton for a clean tracker so prior tests / runs
    can't leave the circuit open."""
    fresh = _mk(failure_threshold=5)
    fresh._recent_failures = collections.deque()
    fresh._circuit_open_until = None
    monkeypatch.setattr(gemini_client, "_quota_tracker", fresh)
    return fresh


def test_generate_times_out_returns_skipped(monkeypatch) -> None:
    tracker = _fresh_singleton(monkeypatch)
    monkeypatch.setattr(gemini_client, "_client", lambda: _SlowClient())

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(
            gemini_client.generate("prompt", timeout_s=0.05)
        )
    finally:
        loop.close()

    assert isinstance(resp, GeminiResponse)
    assert resp.skipped is True
    assert resp.skip_reason == "timeout after 0.05s"
    # The timeout must have been recorded on the (swapped-in) tracker.
    assert len(tracker._recent_failures) == 1


def test_generate_no_timeout_when_timeout_s_none(monkeypatch) -> None:
    """With timeout_s=None the SDK call is not wrapped in wait_for.  We assert
    the call is *attempted* (reaches our fake) rather than short-circuited."""
    reached = {"called": False}

    class _FastModels:
        async def generate_content(self, **_kw):  # noqa: ANN003
            reached["called"] = True
            raise RuntimeError("boom-after-reaching-sdk")

    class _FastAio:
        def __init__(self) -> None:
            self.models = _FastModels()

    class _FastClient:
        def __init__(self) -> None:
            self.aio = _FastAio()

    _fresh_singleton(monkeypatch)
    monkeypatch.setattr(gemini_client, "_client", lambda: _FastClient())

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(gemini_client.generate("p", timeout_s=None))
    finally:
        loop.close()

    assert reached["called"] is True
    # An SDK RuntimeError (not a timeout) is handled by the generic except —
    # the call still returns a GeminiResponse rather than raising.
    assert isinstance(resp, GeminiResponse)

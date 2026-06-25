"""Thin wrapper around google-genai with budget enforcement, circuit breaker,
and per-call cost accumulation.

Uses the current ``google-genai`` SDK (v1.x) — the old
``google-generativeai`` package is deprecated (EOL announced May 2025)
and may not be available on all hosts.  Falls back gracefully when the
key is missing or the daily budget is exhausted.

Cost estimate (per Google's published Gemini 2.0 Flash pricing as of 2026-04):
input  $0.075 / 1M tokens, output $0.30 / 1M tokens. We use a coarse
characters/4 -> tokens approximation good enough for the daily cap.

Circuit breaker (Component 2):
  After 8 quota errors (HTTP 429 / 503 / "exhausted") within 60 s, the
  circuit opens for 2 minutes. All calls during that window return an empty
  skipped GeminiResponse without hitting the API. The circuit auto-resets
  after the cool-down.  Every recorded failure is logged at DEBUG so the
  triggering errors are visible even when the circuit doesn't open.

Call log accumulator (Component 4):
  Each call appends a structured entry to the per-coroutine log list held in
  ``_call_log_var`` (a contextvars.ContextVar). Callers can read the
  accumulated entries via :func:`get_call_log` and clear them via
  :func:`reset_call_log`. The orchestrator uses this to persist call details
  to the ``gemini_call_log`` DB table without needing a session inside this
  module.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.services.ai import budget

log = logging.getLogger(__name__)

_INPUT_USD_PER_M = 0.075
_OUTPUT_USD_PER_M = 0.30

# ---------------------------------------------------------------------------
# 429 retry-with-backoff
# ---------------------------------------------------------------------------
# With 8+ concurrent workers hitting Gemini simultaneously the RPM is
# exhausted and every call gets 429 RESOURCE_EXHAUSTED.  We retry ONCE with
# a short wait rather than 3 times with 20s waits — the thundering-herd
# problem means all 8 coroutines sleep the same duration then retry together,
# repeating 3× and wasting up to 60s per course.  One quick retry (≤8s)
# is enough for transient spikes; the circuit breaker handles sustained quota
# exhaustion by skipping Gemini entirely for 2 minutes.
_MAX_RETRIES = 1               # was 3 — one retry is enough; circuit breaker handles sustained quota
_MAX_RETRY_WAIT_S = 15.0       # was 60.0
_JITTER_FACTOR = 0.30          # ±30 % — wider spread reduces thundering-herd retry sync

# Parse  'retryDelay': '20s'  from the 429 error body.
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?:\s*['\"]?(\d+)s?['\"]?", re.IGNORECASE)


def _parse_retry_delay(err_str: str, default: float = 8.0) -> float:
    """Return retry delay seconds suggested by the API, capped for speed.

    Google suggests 20s; we cap at 8s default because:
    - One fast retry is preferable to a long wait before the circuit trips.
    - The circuit breaker handles sustained quota failure (8 errors in 60s).
    """
    m = _RETRY_DELAY_RE.search(err_str)
    raw = float(m.group(1)) if m else default
    # Don't respect excessively long API-suggested delays — cap at 10s.
    return min(raw, 10.0)


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class GeminiResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    skipped: bool = False
    skip_reason: str | None = None
    call_type: str = "primary_full"
    model: str = ""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class GeminiQuotaTracker:
    """Tracks recent quota failures and trips a circuit breaker.

    Singleton per process — shared across all coroutines via module-level
    ``_quota_tracker``.
    """

    def __init__(
        self,
        failure_threshold: int = 8,
        window_seconds: int = 60,
        cool_down_seconds: int = 120,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cool_down_seconds = cool_down_seconds
        self._recent_failures: deque[datetime] = deque(maxlen=20)
        self._circuit_open_until: datetime | None = None

    def record_failure(self, error_code: int | None, error_message: str) -> None:
        """Record an API failure. Trips circuit when threshold is reached."""
        if not self._looks_like_quota(error_code, error_message):
            return
        now = datetime.now(timezone.utc)
        self._recent_failures.append(now)
        window_start = now - timedelta(seconds=self.window_seconds)
        recent = [t for t in self._recent_failures if t >= window_start]
        log.debug(
            "[GEMINI QUOTA FAILURE] code=%s msg=%.120s — %d/%d in window",
            error_code, error_message, len(recent), self.failure_threshold,
        )
        if len(recent) >= self.failure_threshold:
            self._circuit_open_until = now + timedelta(seconds=self.cool_down_seconds)
            log.warning(
                "[GEMINI CIRCUIT OPEN] %d quota errors in %ds — pausing %.0fs until %s "
                "| last_error: code=%s msg=%.200s",
                len(recent),
                self.window_seconds,
                self.cool_down_seconds,
                self._circuit_open_until.isoformat(),
                error_code,
                error_message,
            )

    def record_timeout(self, timeout_s: float | None = None) -> None:
        """Record a Gemini API-call timeout (SDK/network slowness — not a quota
        error).  Task #233.

        Timeouts are recorded into the SAME ``_recent_failures`` deque and use
        the SAME failure threshold as quota errors, so sustained Gemini slowness
        opens the circuit and subsequent calls short-circuit instantly instead of
        each paying the full per-call timeout.  A distinct log reason keeps
        timeouts observable separately from quota 429/503s.

        This exists because the per-course timeout previously lived in a
        ``wait_for`` *wrapping* :func:`generate`; on timeout that cancelled the
        inner coroutine before its ``except`` block ran, so timeouts never
        reached :meth:`record_failure` and never tripped the breaker.  The
        timeout now lives inside :func:`generate` and calls this method directly.
        """
        now = datetime.now(timezone.utc)
        self._recent_failures.append(now)
        window_start = now - timedelta(seconds=self.window_seconds)
        recent = [t for t in self._recent_failures if t >= window_start]
        log.debug(
            "[GEMINI TIMEOUT] timeout_s=%s — %d/%d in window",
            timeout_s, len(recent), self.failure_threshold,
        )
        if len(recent) >= self.failure_threshold:
            self._circuit_open_until = now + timedelta(seconds=self.cool_down_seconds)
            log.warning(
                "[GEMINI CIRCUIT OPEN] %d failures in %ds (incl. API timeouts) — "
                "pausing %.0fs until %s",
                len(recent),
                self.window_seconds,
                self.cool_down_seconds,
                self._circuit_open_until.isoformat(),
            )

    def is_circuit_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if datetime.now(timezone.utc) >= self._circuit_open_until:
            log.info("[GEMINI CIRCUIT CLOSED] cool-down complete")
            self._circuit_open_until = None
            self._recent_failures.clear()
            return False
        return True

    def time_until_circuit_close(self) -> float:
        if self._circuit_open_until is None:
            return 0.0
        return max(
            0.0,
            (self._circuit_open_until - datetime.now(timezone.utc)).total_seconds(),
        )

    @staticmethod
    def _looks_like_quota(error_code: int | None, message: str) -> bool:
        if error_code in (429, 503):
            return True
        if message and any(
            kw in message.lower()
            for kw in ("quota", "rate limit", "exhausted", "exceeded", "resource_exhausted")
        ):
            return True
        return False


# Process-level singleton
_quota_tracker = GeminiQuotaTracker()


def get_quota_tracker() -> GeminiQuotaTracker:
    """Return the process-level singleton circuit breaker (for tests)."""
    return _quota_tracker


# ---------------------------------------------------------------------------
# Per-coroutine call log accumulator
# ---------------------------------------------------------------------------

_call_log_var: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar(
    "gemini_call_log", default=None  # type: ignore[arg-type]
)


def _get_log_list() -> list[dict[str, Any]]:
    lst = _call_log_var.get(None)
    if lst is None:
        lst = []
        _call_log_var.set(lst)
    return lst


def get_call_log() -> list[dict[str, Any]]:
    """Return the list of Gemini call entries accumulated in this coroutine."""
    return _get_log_list().copy()


def reset_call_log() -> None:
    """Clear the accumulated call log for the current coroutine context."""
    _call_log_var.set([])


def _append_call_log(
    call_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    duration_ms: int,
    success: bool,
    error_message: str | None = None,
    course_url: str | None = None,
) -> None:
    _get_log_list().append(
        {
            "call_type": call_type,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "success": success,
            "error_message": error_message,
            "course_url": course_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_mime_type(img_bytes: bytes) -> str:
    """Detect image MIME type from leading magic bytes.

    Sending a PNG as ``image/jpeg`` caused Gemini to return an empty
    response with finish_reason=1 and no text parts — a silent failure
    that left all ASA Master English slots empty.  Auto-detecting the
    type per image byte stream fixes this.
    """
    if img_bytes[:4] == b"\x89PNG":
        return "image/png"
    if img_bytes[:3] == b"GIF":
        return "image/gif"
    if len(img_bytes) >= 12 and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _client():
    """Return an initialised google.genai Client, or None when unavailable."""
    if not settings.gemini_api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=settings.gemini_api_key)
    except Exception as exc:
        log.warning("Gemini client init failed: %s", exc)
        return None


async def _close_client(c: object) -> None:
    """Best-effort close of a google.genai Client's underlying httpx transport.

    The google-genai SDK wraps an httpx.AsyncClient internally.  If we don't
    close it explicitly, Python's garbage collector eventually calls aclose()
    on the transport *after* the Celery event loop has been torn down, which
    produces noisy but harmless log spam:

        RuntimeError: Event loop is closed
        Task exception was never retrieved — GeminiApiClient.aclose()

    We try a small set of known internal attribute paths.  Any that don't
    exist are silently skipped; any exception during close is also swallowed
    (best-effort only — the call has already returned its result).
    """
    for attr_path in ("_api_client", "aio._api_client"):
        try:
            obj = c
            for part in attr_path.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None and callable(getattr(obj, "aclose", None)):
                await obj.aclose()
                return
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate(
    prompt: str,
    *,
    max_output_tokens: int = 2048,
    call_type: str = "primary_full",
    course_url: str | None = None,
    timeout_s: float | None = None,
) -> GeminiResponse:
    started = datetime.now(timezone.utc)
    in_tok = _estimate_tokens(prompt)
    model_name = settings.gemini_model

    # Circuit breaker check
    if _quota_tracker.is_circuit_open():
        wait = _quota_tracker.time_until_circuit_close()
        log.info("[GEMINI SKIP-CIRCUIT] circuit open %.0fs more — skipping %s", wait, call_type)
        resp = GeminiResponse(
            "", in_tok, 0, 0.0,
            skipped=True, skip_reason="circuit_open",
            call_type=call_type, model=model_name,
        )
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "circuit_open", course_url)
        return resp

    # Daily budget check
    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        resp = GeminiResponse(
            "", in_tok, 0, 0.0,
            skipped=True, skip_reason="daily budget exhausted",
            call_type=call_type, model=model_name,
        )
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "budget_exhausted", course_url)
        return resp

    c = _client()
    if c is None:
        resp = GeminiResponse(
            "", in_tok, 0, 0.0,
            skipped=True, skip_reason="GEMINI_API_KEY not set",
            call_type=call_type, model=model_name,
        )
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "no_api_key", course_url)
        return resp

    from google.genai import types as _gtypes

    # Task #229: cross-process throttle so the 8-worker fleet doesn't burst the
    # shared Gemini quota into 429s.  No-op unless gemini_rate_limit_per_sec > 0.
    try:
        from app.services.scraper.rate_limiter import acquire_gemini
        await acquire_gemini()
    except Exception as _rl_exc:  # noqa: BLE001 — never block a call on the limiter
        log.debug("gemini rate-limit acquire skipped: %s", _rl_exc)

    try:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                _gen_coro = c.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=_gtypes.GenerateContentConfig(max_output_tokens=max_output_tokens),
                )
                # Task #233: bound the SDK call itself (the rate-limiter token was
                # already acquired above, so this measures genuine API/SDK latency
                # only — not limiter backpressure).  On timeout we trip the breaker
                # via record_timeout() so sustained slowness short-circuits the rest
                # of the run instead of every course paying the full timeout.
                if timeout_s is not None and timeout_s > 0:
                    resp = await asyncio.wait_for(_gen_coro, timeout=timeout_s)
                else:
                    resp = await _gen_coro
                text = (getattr(resp, "text", "") or "").strip()
                out_tok = _estimate_tokens(text)
                cost = (in_tok * _INPUT_USD_PER_M + out_tok * _OUTPUT_USD_PER_M) / 1_000_000
                budget.add_spend(cost)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                _append_call_log(call_type, model_name, in_tok, out_tok, cost, duration_ms, True, None, course_url)
                return GeminiResponse(text, in_tok, out_tok, cost, call_type=call_type, model=model_name)
            except asyncio.TimeoutError:
                # Genuine SDK/API slowness — do NOT retry (that would double the
                # per-course wall-time this task is trying to cut).  Record the
                # timeout so repeated ones open the circuit breaker.
                _quota_tracker.record_timeout(timeout_s)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                log.warning(
                    "[GEMINI TIMEOUT] %s timed out after %ss [%s]",
                    model_name, timeout_s, call_type,
                )
                _append_call_log(
                    call_type, model_name, in_tok, 0, 0.0, duration_ms, False,
                    f"timeout after {timeout_s}s", course_url,
                )
                return GeminiResponse(
                    "", in_tok, 0, 0.0,
                    skipped=True, skip_reason=f"timeout after {timeout_s}s",
                    call_type=call_type, model=model_name,
                )
            except Exception as exc:
                err_str = str(exc)
                err_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                is_quota = GeminiQuotaTracker._looks_like_quota(err_code, err_str)
                if is_quota and attempt < _MAX_RETRIES:
                    delay_s = min(_parse_retry_delay(err_str), _MAX_RETRY_WAIT_S)
                    jitter = delay_s * _JITTER_FACTOR * (random.random() * 2 - 1)
                    wait = max(1.0, delay_s + jitter)
                    log.warning(
                        "[GEMINI 429] attempt %d/%d — waiting %.1fs before retry [%s]",
                        attempt + 1, _MAX_RETRIES, wait, call_type,
                    )
                    await asyncio.sleep(wait)
                    continue
                _quota_tracker.record_failure(err_code, err_str)
                log.warning("Gemini generate failed [%s]: %s", call_type, exc)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                _append_call_log(call_type, model_name, in_tok, 0, 0.0, duration_ms, False, err_str[:500], course_url)
                return GeminiResponse(
                    "", in_tok, 0, 0.0,
                    skipped=True, skip_reason=err_str,
                    call_type=call_type, model=model_name,
                )
        # unreachable — loop always returns or continues
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="max_retries", call_type=call_type, model=model_name)
    finally:
        await _close_client(c)


async def generate_with_images(
    prompt: str,
    images: list[bytes],
    *,
    mime_type: str = "image/jpeg",
    max_output_tokens: int = 2048,
    call_type: str = "vision",
    course_url: str | None = None,
) -> GeminiResponse:
    """Multimodal generate — text prompt + 1-N inline images.

    Each image's MIME type is auto-detected from its magic bytes so PNG
    tables (MaSTER.png) are never sent as image/jpeg, which previously
    caused Gemini to return finish_reason=1 with no text.

    Returns the same ``GeminiResponse`` shape as :func:`generate`. On any
    error or budget exhaustion, ``text`` is empty and ``skipped`` is True.
    """
    if not images:
        return await generate(prompt, max_output_tokens=max_output_tokens, call_type=call_type, course_url=course_url)

    started = datetime.now(timezone.utc)
    model_name = settings.gemini_model
    in_tok = _estimate_tokens(prompt) + sum(max(1, len(img) // 4) for img in images)

    if _quota_tracker.is_circuit_open():
        wait = _quota_tracker.time_until_circuit_close()
        log.info("[GEMINI SKIP-CIRCUIT] circuit open %.0fs more — skipping vision", wait)
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "circuit_open", course_url)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="circuit_open", call_type=call_type, model=model_name)

    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "budget_exhausted", course_url)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="daily budget exhausted", call_type=call_type, model=model_name)

    c = _client()
    if c is None:
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, 0, False, "no_api_key", course_url)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set", call_type=call_type, model=model_name)

    from google.genai import types as _gtypes
    parts: list[_gtypes.Part] = []
    for img in images:
        detected = _detect_mime_type(img)
        parts.append(_gtypes.Part.from_bytes(data=img, mime_type=detected))
    parts.append(_gtypes.Part.from_text(text=prompt))

    # Task #229: cross-process throttle (shared with the text-only path above).
    try:
        from app.services.scraper.rate_limiter import acquire_gemini
        await acquire_gemini()
    except Exception as _rl_exc:  # noqa: BLE001 — never block a call on the limiter
        log.debug("gemini rate-limit acquire skipped: %s", _rl_exc)

    try:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await c.aio.models.generate_content(
                    model=model_name,
                    contents=parts,
                    config=_gtypes.GenerateContentConfig(max_output_tokens=max_output_tokens),
                )
                text = (getattr(resp, "text", "") or "").strip()
                out_tok = _estimate_tokens(text)
                cost = (in_tok * _INPUT_USD_PER_M + out_tok * _OUTPUT_USD_PER_M) / 1_000_000
                budget.add_spend(cost)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                _append_call_log(call_type, model_name, in_tok, out_tok, cost, duration_ms, True, None, course_url)
                return GeminiResponse(text, in_tok, out_tok, cost, call_type=call_type, model=model_name)
            except Exception as exc:
                err_str = str(exc)
                err_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                is_quota = GeminiQuotaTracker._looks_like_quota(err_code, err_str)
                if is_quota and attempt < _MAX_RETRIES:
                    delay_s = min(_parse_retry_delay(err_str), _MAX_RETRY_WAIT_S)
                    jitter = delay_s * _JITTER_FACTOR * (random.random() * 2 - 1)
                    wait = max(1.0, delay_s + jitter)
                    log.warning(
                        "[GEMINI 429] vision attempt %d/%d — waiting %.1fs before retry",
                        attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                _quota_tracker.record_failure(err_code, err_str)
                log.warning("Gemini vision generate failed: %s", exc)
                duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                _append_call_log(call_type, model_name, in_tok, 0, 0.0, duration_ms, False, err_str[:500], course_url)
                return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason=err_str, call_type=call_type, model=model_name)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="max_retries", call_type=call_type, model=model_name)
    finally:
        await _close_client(c)

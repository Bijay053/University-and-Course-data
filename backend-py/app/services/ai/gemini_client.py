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
  After 5 quota errors (HTTP 429 / 503 / "exhausted") within 60 s, the
  circuit opens for 5 minutes. All calls during that window return an empty
  skipped GeminiResponse without hitting the API. The circuit auto-resets
  after the cool-down.

Call log accumulator (Component 4):
  Each call appends a structured entry to the per-coroutine log list held in
  ``_call_log_var`` (a contextvars.ContextVar). Callers can read the
  accumulated entries via :func:`get_call_log` and clear them via
  :func:`reset_call_log`. The orchestrator uses this to persist call details
  to the ``gemini_call_log`` DB table without needing a session inside this
  module.
"""
from __future__ import annotations

import contextvars
import logging
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
        failure_threshold: int = 5,
        window_seconds: int = 60,
        cool_down_seconds: int = 300,
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
        if len(recent) >= self.failure_threshold:
            self._circuit_open_until = now + timedelta(seconds=self.cool_down_seconds)
            log.warning(
                "[GEMINI CIRCUIT OPEN] %d quota errors in %ds — pausing until %s",
                len(recent),
                self.window_seconds,
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate(
    prompt: str,
    *,
    max_output_tokens: int = 2048,
    call_type: str = "primary_full",
    course_url: str | None = None,
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

    try:
        from google.genai import types as _gtypes
        resp = await c.aio.models.generate_content(
            model=model_name,
            contents=prompt,
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
        _quota_tracker.record_failure(err_code, err_str)
        log.warning("Gemini generate failed [%s]: %s", call_type, exc)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, duration_ms, False, err_str[:500], course_url)
        return GeminiResponse(
            "", in_tok, 0, 0.0,
            skipped=True, skip_reason=err_str,
            call_type=call_type, model=model_name,
        )


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

    try:
        from google.genai import types as _gtypes
        parts: list[_gtypes.Part] = []
        for img in images:
            detected = _detect_mime_type(img)
            parts.append(_gtypes.Part.from_bytes(data=img, mime_type=detected))
        parts.append(_gtypes.Part.from_text(text=prompt))

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
        _quota_tracker.record_failure(err_code, err_str)
        log.warning("Gemini vision generate failed: %s", exc)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _append_call_log(call_type, model_name, in_tok, 0, 0.0, duration_ms, False, err_str[:500], course_url)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason=err_str, call_type=call_type, model=model_name)

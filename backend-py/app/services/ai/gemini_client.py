"""Thin wrapper around google-genai with budget enforcement.

Uses the current ``google-genai`` SDK (v1.x) — the old
``google-generativeai`` package is deprecated (EOL announced May 2025)
and may not be available on all hosts.  Falls back gracefully when the
key is missing or the daily budget is exhausted.

Cost estimate (per Google's published Gemini 2.0 Flash pricing as of 2026-04):
input  $0.075 / 1M tokens, output $0.30 / 1M tokens. We use a coarse
characters/4 -> tokens approximation good enough for the daily cap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.services.ai import budget

log = logging.getLogger(__name__)

_INPUT_USD_PER_M = 0.075
_OUTPUT_USD_PER_M = 0.30


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


@dataclass
class GeminiResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    skipped: bool = False
    skip_reason: str | None = None


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


async def generate(prompt: str, *, max_output_tokens: int = 2048) -> GeminiResponse:
    in_tok = _estimate_tokens(prompt)
    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="daily budget exhausted")

    c = _client()
    if c is None:
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set"
        )

    try:
        from google.genai import types as _gtypes
        resp = await c.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=_gtypes.GenerateContentConfig(max_output_tokens=max_output_tokens),
        )
        text = (getattr(resp, "text", "") or "").strip()
        out_tok = _estimate_tokens(text)
        cost = (in_tok * _INPUT_USD_PER_M + out_tok * _OUTPUT_USD_PER_M) / 1_000_000
        budget.add_spend(cost)
        return GeminiResponse(text, in_tok, out_tok, cost)
    except Exception as exc:
        log.warning("Gemini generate failed: %s", exc)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason=str(exc))


async def generate_with_images(
    prompt: str,
    images: list[bytes],
    *,
    mime_type: str = "image/jpeg",
    max_output_tokens: int = 2048,
) -> GeminiResponse:
    """Multimodal generate — text prompt + 1-N inline images.

    Each image's MIME type is auto-detected from its magic bytes so PNG
    tables (MaSTER.png) are never sent as image/jpeg, which previously
    caused Gemini to return finish_reason=1 with no text.

    Returns the same ``GeminiResponse`` shape as :func:`generate`. On any
    error or budget exhaustion, ``text`` is empty and ``skipped`` is True.
    """
    if not images:
        return await generate(prompt, max_output_tokens=max_output_tokens)

    in_tok = _estimate_tokens(prompt) + sum(max(1, len(img) // 4) for img in images)
    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="daily budget exhausted"
        )

    c = _client()
    if c is None:
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set"
        )

    try:
        from google.genai import types as _gtypes
        parts: list[_gtypes.Part] = []
        for img in images:
            detected = _detect_mime_type(img)
            parts.append(_gtypes.Part.from_bytes(data=img, mime_type=detected))
        parts.append(_gtypes.Part.from_text(text=prompt))

        resp = await c.aio.models.generate_content(
            model=settings.gemini_model,
            contents=parts,
            config=_gtypes.GenerateContentConfig(max_output_tokens=max_output_tokens),
        )
        text = (getattr(resp, "text", "") or "").strip()
        out_tok = _estimate_tokens(text)
        cost = (in_tok * _INPUT_USD_PER_M + out_tok * _OUTPUT_USD_PER_M) / 1_000_000
        budget.add_spend(cost)
        return GeminiResponse(text, in_tok, out_tok, cost)
    except Exception as exc:
        log.warning("Gemini vision generate failed: %s", exc)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason=str(exc))

"""Thin wrapper around google-generativeai with budget enforcement.

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

    The ``generate_with_images`` caller used to pass a fixed
    ``mime_type="image/jpeg"`` regardless of actual content. PNG images
    sent with ``image/jpeg`` cause Gemini to return an empty response
    with ``finish_reason=1`` (STOP) and no text parts, silently failing
    all vision-OCR extractions. Auto-detecting the type per image byte
    stream fixes the MaSTER.png / English-requirements table scenario.
    """
    if img_bytes[:4] == b"\x89PNG":
        return "image/png"
    if img_bytes[:3] == b"GIF":
        return "image/gif"
    if len(img_bytes) >= 12 and img_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # default — handles JFIF / EXIF


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


def _model():
    if not settings.gemini_api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        return genai.GenerativeModel(settings.gemini_model)
    except Exception as exc:
        log.warning("Gemini client init failed: %s", exc)
        return None


async def generate(prompt: str, *, max_output_tokens: int = 2048) -> GeminiResponse:
    in_tok = _estimate_tokens(prompt)
    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason="daily budget exhausted")

    m = _model()
    if m is None:
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set"
        )

    try:
        resp = await m.generate_content_async(
            prompt, generation_config={"max_output_tokens": max_output_tokens}
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

    Mirrors Node's ``analyzeImageWithGemini`` REST call shape. Cost is
    estimated by image bytes / 4 (rough proxy for the token equivalent
    Google bills) plus the text prompt tokens, so the per-image cost is
    bounded and the daily budget keeps applying.

    ``mime_type`` is now used only as a final fallback; each image's
    actual type is auto-detected from its magic bytes first.  Sending a
    PNG as ``image/jpeg`` caused Gemini to return finish_reason=1 with
    no text parts — a silent failure that left all ASA Master course
    English slots empty even though the vision pipeline was running.

    Returns the same ``GeminiResponse`` shape as :func:`generate`. On any
    error or budget exhaustion, ``text`` is empty and ``skipped`` is True
    so callers can degrade gracefully without try/except gymnastics.
    """
    if not images:
        return await generate(prompt, max_output_tokens=max_output_tokens)

    in_tok = _estimate_tokens(prompt) + sum(max(1, len(img) // 4) for img in images)
    estimated = (in_tok * _INPUT_USD_PER_M + max_output_tokens * _OUTPUT_USD_PER_M) / 1_000_000
    if not budget.has_budget(estimated):
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="daily budget exhausted"
        )

    m = _model()
    if m is None:
        return GeminiResponse(
            "", in_tok, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set"
        )

    # google-generativeai accepts a list whose elements are either str
    # (treated as text) or {"mime_type", "data"} dicts (treated as inline
    # binary). The vision-capable model is the same as the text one for
    # Gemini 2.0+; older v1 models would need an explicit ``-vision``
    # variant.
    # Each image's MIME type is auto-detected from its magic bytes.
    # The ``mime_type`` parameter is kept as a last-resort fallback for
    # callers that explicitly know the type and pass it in.
    parts: list = [prompt]
    for img in images:
        detected = _detect_mime_type(img)
        parts.append({"mime_type": detected, "data": img})

    try:
        resp = await m.generate_content_async(
            parts, generation_config={"max_output_tokens": max_output_tokens}
        )
        text = (getattr(resp, "text", "") or "").strip()
        out_tok = _estimate_tokens(text)
        cost = (in_tok * _INPUT_USD_PER_M + out_tok * _OUTPUT_USD_PER_M) / 1_000_000
        budget.add_spend(cost)
        return GeminiResponse(text, in_tok, out_tok, cost)
    except Exception as exc:
        log.warning("Gemini vision generate failed: %s", exc)
        return GeminiResponse("", in_tok, 0, 0.0, skipped=True, skip_reason=str(exc))

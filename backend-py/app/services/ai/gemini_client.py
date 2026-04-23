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
        return genai.GenerativeModel("gemini-2.0-flash-exp")
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

"""Thin async OpenAI client using the Replit AI Integrations proxy.

Environment variables (auto-provisioned by Replit):
  AI_INTEGRATIONS_OPENAI_BASE_URL  — proxy base URL
  AI_INTEGRATIONS_OPENAI_API_KEY   — dummy key accepted by the proxy

Usage:
    from app.services.ai.openai_client import chat_json

    data = await chat_json(
        system="You are an expert...",
        user="Analyse this...",
        max_tokens=2048,
    )
    # data is a parsed dict or None on failure
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

_MODEL = "gpt-5.4"


def _client():
    """Return a configured AsyncOpenAI client, or None if credentials missing."""
    if not settings.openai_api_key or not settings.openai_base_url:
        log.warning("openai_client: AI_INTEGRATIONS_OPENAI_BASE_URL / API_KEY not set")
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    except Exception as exc:
        log.warning("openai_client: init failed: %s", exc)
        return None


async def chat_json(
    *,
    system: str,
    user: str,
    max_tokens: int = 2048,
) -> dict[str, Any] | None:
    """Call OpenAI chat completions and return the parsed JSON response.

    Uses ``response_format={"type": "json_object"}`` so the model is
    guaranteed to return valid JSON.  Returns None on any error.
    """
    import json

    c = _client()
    if c is None:
        return None

    try:
        response = await c.chat.completions.create(
            model=_MODEL,
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            log.warning("openai_client: empty response from model")
            return None
        return json.loads(raw)
    except Exception as exc:
        log.warning("openai_client: chat_json failed: %s", exc)
        return None

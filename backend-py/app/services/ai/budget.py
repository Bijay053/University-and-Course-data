"""Per-day Gemini budget tracker keyed in Redis (``gemini:spend:YYYY-MM-DD``).

Falls back to an in-memory counter when Redis isn't reachable so dev
environments keep working.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import settings

log = logging.getLogger(__name__)

_in_memory: dict[str, float] = {}


def _key(date: datetime | None = None) -> str:
    d = (date or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"gemini:spend:{d}"


def _client():
    try:
        import redis  # noqa: WPS433

        return redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2)
    except Exception:
        return None


def get_spent_today() -> float:
    k = _key()
    r = _client()
    if r is None:
        return _in_memory.get(k, 0.0)
    try:
        v = r.get(k)
        return float(v) if v else 0.0
    except Exception:
        return _in_memory.get(k, 0.0)


def add_spend(usd: float) -> float:
    k = _key()
    r = _client()
    if r is None:
        _in_memory[k] = _in_memory.get(k, 0.0) + usd
        return _in_memory[k]
    try:
        new = r.incrbyfloat(k, usd)
        r.expire(k, 60 * 60 * 36)  # auto-cleanup after ~1.5 days
        return float(new)
    except Exception:
        _in_memory[k] = _in_memory.get(k, 0.0) + usd
        return _in_memory[k]


def has_budget(estimated_usd: float = 0.01) -> bool:
    return (get_spent_today() + estimated_usd) <= settings.daily_gemini_budget_usd

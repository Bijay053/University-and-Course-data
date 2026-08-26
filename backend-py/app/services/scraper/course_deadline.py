"""Shared wall-clock budget for one course extraction.

The orchestrator establishes the deadline once.  Nested fallbacks read it via a
ContextVar so direct extractor callers keep their historical standalone
timeouts while normal/recovery scrape paths share one monotonic budget.
"""
from __future__ import annotations

import time
from contextvars import ContextVar, Token
from typing import Any


_course_deadline: ContextVar[float | None] = ContextVar(
    "course_extraction_deadline",
    default=None,
)


def set_course_deadline(timeout_seconds: float) -> Token:
    """Set a deadline ``timeout_seconds`` from now and return its reset token."""
    timeout = max(0.0, float(timeout_seconds))
    return _course_deadline.set(time.monotonic() + timeout)


def reset_course_deadline(token: Token) -> None:
    """Restore the caller's previous deadline context."""
    _course_deadline.reset(token)


def remaining_seconds() -> float | None:
    """Return non-negative time left, or ``None`` outside a bounded course."""
    deadline = _course_deadline.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def clamp_timeout(requested_seconds: float | int | None) -> float | None:
    """Clamp a stage timeout to the current course budget.

    ``None`` remains unbounded only when no course deadline is active.  Under a
    deadline it means "use all remaining time".
    """
    remaining = remaining_seconds()
    if remaining is None:
        return None if requested_seconds is None else max(0.0, float(requested_seconds))
    if requested_seconds is None:
        return remaining
    return max(0.0, min(float(requested_seconds), remaining))


def has_budget(minimum_seconds: float = 0.05) -> bool:
    """Whether a new stage has enough time left to be worth starting."""
    remaining = remaining_seconds()
    return remaining is None or remaining >= max(0.0, float(minimum_seconds))


def required_course_fields_complete(payload: dict[str, Any]) -> bool:
    """Return True when expensive remote enrichment cannot add a required field.

    Multiple aliases are accepted because the pipeline carries both canonical
    staging slots (``duration``, ``intake_months``) and extractor-shape slots
    (``duration_value``, ``intake_text``) at different points.
    """

    def present(*keys: str) -> bool:
        return any(payload.get(key) not in (None, "", 0, []) for key in keys)

    mode = payload.get("study_mode") or payload.get("mode")
    is_online = str(mode or "").strip().lower() == "online"
    return all(
        (
            present("international_fee"),
            present(
                "ielts_overall",
                "pte_overall",
                "toefl_overall",
                "cambridge_overall",
                "duolingo_overall",
            ),
            present("duration", "duration_value", "duration_text"),
            present("intake_months", "intake_dates", "intake_text"),
            is_online or present("course_location", "location_text", "location"),
            present("study_mode", "mode"),
        )
    )
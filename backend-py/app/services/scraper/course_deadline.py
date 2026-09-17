"""Shared wall-clock budget for one course extraction.

The orchestrator establishes the deadline once.  Nested fallbacks read it via a
ContextVar so direct extractor callers keep their historical standalone
timeouts while normal/recovery scrape paths share one monotonic budget.
"""
from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


_course_deadline: ContextVar[float | None] = ContextVar(
    "course_extraction_deadline",
    default=None,
)

@dataclass(frozen=True, slots=True)
class RequiredCourseField:
    """One publishability fact and every pipeline slot that can satisfy it."""

    name: str
    operator_label: str
    aliases: tuple[str, ...]
    full_ai_fields: tuple[str, ...]
    optional_when_online: bool = False
    deterministic_only_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.operator_label.strip():
            raise ValueError(f"Required course field {self.name!r} needs an operator label")
        if not self.full_ai_fields and not str(self.deterministic_only_reason or "").strip():
            raise ValueError(
                f"Required course field {self.name!r} needs full AI fields "
                "or an explicit deterministic-only reason"
            )


REQUIRED_COURSE_FIELDS: tuple[RequiredCourseField, ...] = (
    RequiredCourseField(
        "international_fee",
        "International Fee",
        ("international_fee",),
        ("international_fee",),
    ),
    RequiredCourseField(
        "english_score",
        "English Requirements",
        (
            "ielts_overall",
            "pte_overall",
            "toefl_overall",
            "cambridge_overall",
            "duolingo_overall",
        ),
        (
            "ielts_overall",
            "pte_overall",
            "toefl_overall",
            "cambridge_overall",
            "duolingo_overall",
        ),
    ),
    RequiredCourseField(
        "duration",
        "Duration",
        ("duration", "duration_value", "duration_text"),
        ("duration_value", "duration_text"),
    ),
    RequiredCourseField(
        "intake",
        "Intake",
        ("intake_months", "intake_dates", "intake_text"),
        ("intake_text",),
    ),
    RequiredCourseField(
        "course_location",
        "Location",
        ("course_location", "location_text", "location"),
        ("location_text",),
        optional_when_online=True,
    ),
    RequiredCourseField(
        "study_mode",
        "Study Mode",
        ("study_mode", "mode"),
        ("mode",),
    ),
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


def missing_required_course_fields(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return stable, bounded names for missing required fact or alias groups.

    Multiple aliases are accepted because the pipeline carries both canonical
    staging slots (``duration``, ``intake_months``) and extractor-shape slots
    (``duration_value``, ``intake_text``) at different points.
    """

    mode = payload.get("study_mode") or payload.get("mode")
    is_online = str(mode or "").strip().lower() == "online"
    missing: list[str] = []
    for field in REQUIRED_COURSE_FIELDS:
        if field.optional_when_online and is_online:
            continue
        if not any(
            payload.get(key) not in (None, "", 0, [])
            for key in field.aliases
        ):
            missing.append(field.name)
    return tuple(missing)


def required_course_field_labels(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return contract-owned operator labels for required fact names."""
    labels = {field.name: field.operator_label for field in REQUIRED_COURSE_FIELDS}
    return tuple(labels[name] for name in names)


def required_course_fields_complete(payload: dict[str, Any]) -> bool:
    """Return True when expensive remote enrichment cannot add a required field."""
    return not missing_required_course_fields(payload)

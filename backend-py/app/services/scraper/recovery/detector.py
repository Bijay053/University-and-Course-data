"""Recovery detector — decide which fields need recovery for a staged course.

Given a staged course dict and its evidence rows, returns the list of field
names that are missing AND could benefit from a recovery search.

Phase 1 scope: international_fee, ielts_overall, intake_months, course_location,
               other_requirement.

Rules:
- Only include a field if the course value is NULL/empty.
- Skip the field if any existing evidence row has confidence >= 0.60 (a high-
  confidence extraction already ran; recovery would be noise).
- Skip fields that already have a configured central page in uniPages — the
  central-pages pipeline handles those.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Phase 1 target fields in priority order.
RECOVERY_FIELDS: tuple[str, ...] = (
    "international_fee",
    "ielts_overall",
    "intake_months",
    "course_location",
    "other_requirement",
)

# camelCase → snake_case aliases for course dicts coming from the API layer.
_CAMEL_TO_SNAKE: dict[str, str] = {
    "internationalFee": "international_fee",
    "ieltsOverall": "ielts_overall",
    "intakeMonths": "intake_months",
    "courseLocation": "course_location",
    "otherRequirement": "other_requirement",
}

# Confidence threshold: if any existing evidence row is this confident,
# skip recovery for that field.
_CONFIDENCE_THRESHOLD = 0.60


def _get_field_value(course: dict[str, Any], field: str) -> Any:
    """Read a field from a course dict that may use snake_case or camelCase keys."""
    if field in course:
        return course[field]
    # Try camelCase variant
    for cc, snake in _CAMEL_TO_SNAKE.items():
        if snake == field and cc in course:
            return course[cc]
    return None


def _is_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if isinstance(val, (list, dict)) and len(val) == 0:
        return True
    if isinstance(val, (int, float)) and val == 0:
        return True
    return False


def _has_high_confidence_evidence(
    evidence: list[dict[str, Any]], field: str
) -> bool:
    """True when at least one evidence row for the field meets the confidence bar."""
    for ev in evidence:
        ev_field = ev.get("field_key") or ev.get("fieldKey") or ""
        if ev_field != field:
            continue
        conf = ev.get("confidence")
        if conf is None:
            continue
        try:
            if float(conf) >= _CONFIDENCE_THRESHOLD:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _uni_pages_has_central(uni_scrape_config: dict | None, field: str) -> bool:
    """True when the university has a configured central page covering this field."""
    if not uni_scrape_config:
        return False
    uni_pages = uni_scrape_config.get("uniPages") or {}
    if field == "international_fee" and uni_pages.get("feePage"):
        return True
    if field in ("ielts_overall", "other_requirement") and (
        uni_pages.get("entryPage") or uni_pages.get("requirementsPage")
    ):
        return True
    return False


def detect_missing_fields(
    course: dict[str, Any],
    evidence: list[dict[str, Any]],
    uni_scrape_config: dict | None = None,
) -> list[str]:
    """Return the list of Phase 1 fields that need recovery.

    Parameters
    ----------
    course:
        Staged course dict (snake_case or camelCase keys accepted).
    evidence:
        List of evidence dicts for the course (from scraped_field_evidence).
    uni_scrape_config:
        University ``scrape_config`` JSONB, used to skip fields already covered
        by a configured central page.

    Returns
    -------
    list[str]
        Ordered list of snake_case field names needing recovery.
    """
    needed: list[str] = []
    course_id = course.get("id", "?")

    for field in RECOVERY_FIELDS:
        val = _get_field_value(course, field)
        if not _is_empty(val):
            log.debug(
                "[RECOVERY:detect] course=%s field=%s — already filled (%r), skipping",
                course_id, field, val,
            )
            continue

        if _has_high_confidence_evidence(evidence, field):
            log.debug(
                "[RECOVERY:detect] course=%s field=%s — high-confidence evidence exists, skipping",
                course_id, field,
            )
            continue

        if _uni_pages_has_central(uni_scrape_config, field):
            log.debug(
                "[RECOVERY:detect] course=%s field=%s — central page configured, skipping",
                course_id, field,
            )
            continue

        log.info(
            "[RECOVERY:detect] course=%s field=%s — MISSING, queued for recovery",
            course_id, field,
        )
        needed.append(field)

    return needed

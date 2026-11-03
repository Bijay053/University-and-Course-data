"""Semantic study-mode contract shared by input and persistence boundaries.

Keep the source's wording: this validates labels, it does not infer delivery
from a course description. Legacy reads are deliberately not normalized.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_MODE = (
    r"on[\s-]*campus|off[\s-]*campus|campus(?:[\s-]*based)?|on[\s-]*site|"
    r"face[\s-]*to[\s-]*face|in[\s-]*person|online|fully\s+online|"
    r"distance(?:\s+(?:learning|education))?|open\s+(?:and\s+)?distance\s+learning|"
    r"blended|hybrid|mixed[\s-]*mode|mixed\s+delivery|multi[\s-]*modal|"
    r"full[\s-]*time|part[\s-]*time(?:only)?|FT|PT(?:only)?|"
    r"flexible(?:\s+(?:learning|delivery|study)(?:\s+options)?)?(?=\s*(?:[:(/,;-]|$))|"
    r"internal|external|onshore|virtual|remote|ODL|block[\s-]+release"
)
_LABEL = re.compile(
    r"^(?:(?:mode\s+of\s+(?:study|delivery)|"
    r"(?:study|delivery|teaching)\s*(?:mode|method|format|options)?)\s*:\s*)?"
    r"(?:(?:study|studying|both)\s+)?"
    r"(?:(?:\d{1,3}\s*%|fully|entirely|mainly|primarily|predominantly)\s+)?"
    r"(?:" + _MODE + r")(?=\W|$)", re.I,
)
_DELIVERY_SENTENCE = re.compile(
    r"^(?:(?:this|the)\s+)?(?:course|programme|program|teaching)\s+"
    r"(?:is\s+(?:delivered|taught|offered)|takes\s+place)\s+"
    r"(?:(?:entirely|fully|primarily|predominantly|both|via|through)\s+)*"
    r"(?:" + _MODE + r")(?=\W|$)", re.I,
)
_NARRATIVE = re.compile(
    r"\b(?:equips?|prepares?)\s+(?:you|students?|graduates?)\b|"
    r"\b(?:you|students?|graduates?)\s+(?:will\s+)?"
    r"(?:learn|gain|develop|become|explore|graduate|build)\b|"
    r"\b(?:course|degree|programme|program)\s+(?:aims?|focuses?|supports?|covers?|explores?)\b|"
    r"\b(?:career|careers|jobs|employability|curriculum)\b|"
    r"\b(?:skills|knowledge)\s+(?:needed|required|in|to)\b", re.I,
)
_UNKNOWN = {"", "unknown", "not specified", "not available", "n/a", "none", "-"}


def normalize_study_mode(value: Any) -> str | None:
    """Preserve a delivery/load label, or return unknown for prose/garbage.

    Full/part-time labels remain available to the existing eligibility guards.
    Detailed mixed-delivery labels are not reduced to a guessed canonical mode.
    """
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    check = label.replace("–", "-").replace("—", "-").replace("‑", "-")
    if check.casefold() in _UNKNOWN or _NARRATIVE.search(check):
        return None
    if check.casefold() in {"both", "mixed"} or _LABEL.match(check) or _DELIVERY_SENTENCE.match(check):
        return label
    return None


def validate_study_mode_input(value: Any) -> str | None:
    """Reject bad explicit staff input without changing legacy read schemas."""
    cleaned = normalize_study_mode(value)
    if value is not None and cleaned is None and (
        not isinstance(value, str) or value.strip().casefold() not in _UNKNOWN
    ):
        raise ValueError("Study mode must describe delivery or study load, not course content")
    return cleaned


def _evidence_mode(evidence: dict[str, Any]) -> str | None:
    value = evidence.get("normalized") or evidence.get("value")
    if isinstance(value, dict):
        value = value.get("study_mode")
    return normalize_study_mode(value)


def sanitize_study_mode_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Last-write guard, called AFTER eligibility decisions on original facts."""
    if "study_mode" not in payload:
        return payload
    raw = payload["study_mode"]
    cleaned = normalize_study_mode(raw)
    result = {**payload, "study_mode": cleaned}
    if raw is not None and cleaned is None and (
        not isinstance(raw, str) or raw.strip().casefold() not in _UNKNOWN
    ):
        warnings = payload.get("scrape_warnings") or []
        warnings = list(warnings) if isinstance(warnings, (list, tuple)) else [warnings]
        result["scrape_warnings"] = list(dict.fromkeys([*warnings, "invalid_study_mode"]))
        if isinstance(payload.get("evidence"), list):
            result["evidence"] = [
                {**ev, "selected": False, "decision_status": "needs_review", "validation_status": "invalid"}
                if isinstance(ev, dict) and ev.get("field_key") == "study_mode"
                and _evidence_mode(ev) is None
                else ev
                for ev in payload["evidence"]
            ]
    elif cleaned is not None and isinstance(payload.get("scrape_warnings"), list):
        result["scrape_warnings"] = [
            warning for warning in payload["scrape_warnings"] if warning != "invalid_study_mode"
        ]
    return result


class StudyModeText(TypeDecorator):
    """Text-compatible bind guard, including ORM and typed bulk/Core writes.

    No result processor: loading an existing record never silently repairs it.
    The database column remains TEXT; no migration or data rewrite is needed.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return normalize_study_mode(value)

    def coerce_compared_value(self, op, value):
        # LIKE patterns and legacy-value lookups are queries, not writes.
        return Text()

"""Tests for the Gemini cost gate (Component 1).

Verifies the three decision branches:
  - all_high_value_fields_populated  → skip Gemini entirely
  - classification_only              → run with cheap prompt
  - full_extraction_needed           → run full prompt
"""
from __future__ import annotations

import pytest

from app.services.scraper.gemini_gate import (
    CONFIDENCE_THRESHOLD,
    GEMINI_HIGH_VALUE_FIELDS,
    should_skip_gemini_primary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_evidence(payload: dict, confidence: float = 0.85) -> list[dict]:
    """Build an evidence list from a payload dict."""
    return [
        {"field_key": k, "confidence": confidence, "value": v, "method": "test"}
        for k, v in payload.items()
        if v not in (None, "", 0, [])
    ]


def _full_payload(include_classification: bool = True) -> dict:
    base = {
        "international_fee": 29400,
        "ielts_overall": 6.5,
        "duration": 3.0,
        "intake_months": ["February", "June"],
        "course_name": "Bachelor of Arts",
        "study_mode": "On Campus",
    }
    if include_classification:
        base["category"] = "Arts & Humanities"
        base["sub_category"] = "Liberal Arts"
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_skip_when_all_fields_populated():
    payload = _full_payload(include_classification=True)
    evidence = _make_evidence(payload, confidence=0.85)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is True
    assert reason == "all_high_value_fields_populated"


def test_classification_only_when_only_category_missing():
    payload = _full_payload(include_classification=False)
    evidence = _make_evidence(payload, confidence=0.85)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False
    assert reason == "classification_only"


def test_classification_only_when_sub_category_missing():
    payload = _full_payload(include_classification=True)
    del payload["sub_category"]
    evidence = _make_evidence(payload, confidence=0.85)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False
    assert reason == "classification_only"


def test_full_extraction_when_fields_missing():
    payload = {"course_name": "Bachelor of Arts"}
    evidence = [{"field_key": "course_name", "confidence": 0.90, "value": "Bachelor of Arts", "method": "h1"}]
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False
    assert reason == "full_extraction_needed"


def test_low_confidence_doesnt_count_as_populated():
    payload = _full_payload(include_classification=True)
    # All fields populated but with LOW confidence (below 0.70)
    evidence = _make_evidence(payload, confidence=0.40)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    # Should run full extraction because confidence is too weak
    assert skip is False
    assert reason == "full_extraction_needed"


def test_threshold_boundary_just_at():
    """Exactly at CONFIDENCE_THRESHOLD should be considered populated."""
    payload = _full_payload(include_classification=True)
    evidence = _make_evidence(payload, confidence=CONFIDENCE_THRESHOLD)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is True
    assert reason == "all_high_value_fields_populated"


def test_threshold_boundary_just_below():
    """Just below CONFIDENCE_THRESHOLD should NOT be considered populated."""
    payload = _full_payload(include_classification=True)
    evidence = _make_evidence(payload, confidence=CONFIDENCE_THRESHOLD - 0.01)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False
    assert reason == "full_extraction_needed"


def test_none_value_doesnt_count_as_populated():
    """None payload values should not count toward coverage."""
    payload = _full_payload(include_classification=True)
    payload["international_fee"] = None
    evidence = _make_evidence(payload, confidence=0.95)
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False


def test_empty_list_doesnt_count_as_populated():
    """Empty list values should not count toward coverage."""
    payload = _full_payload(include_classification=True)
    payload["intake_months"] = []
    evidence = _make_evidence(payload, confidence=0.95)
    # intake_months is in GEMINI_HIGH_VALUE_FIELDS
    assert "intake_months" in GEMINI_HIGH_VALUE_FIELDS
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False


def test_empty_evidence_always_full_extraction():
    """No evidence at all → full extraction needed."""
    payload = _full_payload(include_classification=True)
    skip, reason = should_skip_gemini_primary(payload, [])
    assert skip is False
    assert reason == "full_extraction_needed"


def test_classification_only_prompt_is_short():
    """The classification-only prompt must fit within the token limit."""
    from app.services.scraper.gemini_gate import build_classification_only_prompt

    prompt = build_classification_only_prompt(
        "Bachelor of Commerce",
        "This degree provides..." * 200,
    )
    # 1 500 chars of page text + overhead — should not be enormous
    assert len(prompt) < 3000, "classification_only prompt is unexpectedly large"
    assert "category" in prompt
    assert "Bachelor of Commerce" in prompt

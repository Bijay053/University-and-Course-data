"""B20: regression tests for ``_apply_ai_duration_mapping``.

The AI fallback returns duration as ``duration_value`` + ``duration_unit``
(matching the prompt the model is shown), but the staged-course schema
stores it as ``duration`` (real) + ``duration_term`` (Year/Month/Week/...).
Without an explicit translation step the AI's answer was silently dropped:
the canonical keys remained empty and the row landed in the staging table
with a number-only duration ("3" instead of "3 Years"). These tests guard
the translation."""
from __future__ import annotations

from app.services.scraper.pipelines.single_course import _apply_ai_duration_mapping


def test_translates_years_to_canonical_keys_when_payload_empty():
    payload: dict = {}
    ai_filled = {"duration_value": 3, "duration_unit": "years"}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert ai_filled["duration"] == 3.0
    assert ai_filled["duration_term"] == "Year"


def test_translates_months_to_canonical_keys():
    payload: dict = {}
    ai_filled = {"duration_value": 18, "duration_unit": "months"}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert ai_filled["duration"] == 18.0
    assert ai_filled["duration_term"] == "Month"


def test_translates_weeks_for_vocational_courses():
    payload: dict = {}
    ai_filled = {"duration_value": 104, "duration_unit": "weeks"}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert ai_filled["duration"] == 104.0
    assert ai_filled["duration_term"] == "Week"


def test_rule_extractor_wins_over_ai():
    """Rule extractor's regex hit must always beat the AI guess; the AI
    keys are recorded (so the prompt's own value is still observable in
    evidence) but the canonical keys are NOT overwritten."""
    payload = {"duration": 2.0, "duration_term": "Year"}
    ai_filled = {"duration_value": 5, "duration_unit": "years"}
    _apply_ai_duration_mapping(payload, ai_filled)
    # Mapping should NOT have added canonical keys to ai_filled, because
    # they're already in payload — leaving them out means the
    # ``payload.setdefault(k, v)`` merge in the caller is a no-op.
    assert "duration" not in ai_filled
    assert "duration_term" not in ai_filled


def test_skips_when_ai_returned_neither_field():
    payload: dict = {}
    ai_filled: dict = {"international_fee": 30000}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert "duration" not in ai_filled
    assert "duration_term" not in ai_filled


def test_unrecognised_unit_is_dropped_not_stored():
    """If Gemini returns a junk unit ('credits', 'units') the helper
    must drop it rather than smuggle garbage into duration_term."""
    payload: dict = {}
    ai_filled = {"duration_value": 8, "duration_unit": "credits"}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert ai_filled["duration"] == 8.0  # value still translated
    assert "duration_term" not in ai_filled  # unit rejected


def test_non_numeric_duration_value_does_not_raise():
    payload: dict = {}
    ai_filled = {"duration_value": "three", "duration_unit": "years"}
    _apply_ai_duration_mapping(payload, ai_filled)
    # Coercion failed — `duration` left absent rather than crashing.
    assert "duration" not in ai_filled
    # Unit still mapped (it's an independent field).
    assert ai_filled["duration_term"] == "Year"

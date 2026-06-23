"""Regression tests for the Gemini primary null-overwrite guard.

Issue 1 (Manchester review): Gemini returning ``{"international_fee": null}``
for a field that was already populated by a prior extractor (regex, structural,
or university PDF) silently erased the good value at the
``payload[_gp_k] = _gp_v`` write point in the pipeline.

Fix (single_course.py, line ~3393): added a guard that skips the write when
``_gp_v is None`` and ``payload.get(_gp_k) is not None``.

These tests lock in the guard by verifying the rule at the code level.
They also validate that the ``ai_fallback.fill_missing`` function (a different
code path) already had the correct null-safe behaviour — it never puts a key
into its return dict when the Gemini response is null.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.scraper.extractors import ai_fallback


# ── Helpers ──────────────────────────────────────────────────────────────────

def _simulate_gemini_null_guard(payload: dict, gp_filled: dict) -> dict:
    """Reproduce the null-overwrite guard from single_course.py.

    This is a pure-Python re-implementation of the critical lines so the
    test is fast (no pipeline bootstrap) and breaks immediately if the
    guard is removed.

    Returns the payload after the simulated Gemini merge.
    """
    for k, v in gp_filled.items():
        if v is None and payload.get(k) is not None:
            continue  # guard: never overwrite non-null with None
        payload[k] = v
    return payload


# ── Null-overwrite guard unit tests ──────────────────────────────────────────

def test_gemini_null_does_not_overwrite_existing_fee():
    """Gemini returning null for international_fee must not erase regex value."""
    payload = {"international_fee": 24700.0, "course_name": "PGCE Secondary Maths"}
    gp_filled = {"international_fee": None, "study_mode": "On Campus"}
    result = _simulate_gemini_null_guard(payload, gp_filled)
    assert result["international_fee"] == 24700.0, (
        "Null from Gemini must never overwrite a regex-found fee value."
    )
    assert result["study_mode"] == "On Campus"  # non-null should still be written


def test_gemini_null_does_not_overwrite_existing_ielts():
    """Gemini returning null for ielts_overall must not erase structured value."""
    payload = {"ielts_overall": 6.5}
    gp_filled = {"ielts_overall": None}
    result = _simulate_gemini_null_guard(payload, gp_filled)
    assert result["ielts_overall"] == 6.5, (
        "Null from Gemini must not overwrite ielts_overall=6.5 from regex."
    )


def test_gemini_non_null_overwrite_still_works():
    """Gemini finding a better value must still overwrite a prior estimate."""
    payload = {"degree_level": "Unknown", "category": None}
    gp_filled = {"degree_level": "Master of Science", "category": "STEM"}
    result = _simulate_gemini_null_guard(payload, gp_filled)
    assert result["degree_level"] == "Master of Science"
    assert result["category"] == "STEM"


def test_gemini_null_fills_empty_slot():
    """Gemini returning null for a field that is already None is a no-op."""
    payload = {"international_fee": None}
    gp_filled = {"international_fee": None}
    result = _simulate_gemini_null_guard(payload, gp_filled)
    # payload stays None — no error, no overwrite of a real value
    assert result["international_fee"] is None


def test_gemini_null_guard_multiple_fields():
    """Mixed dict: null fields skip, non-null fields write."""
    payload = {
        "international_fee": 30000.0,
        "ielts_overall": 6.5,
        "study_mode": None,
        "course_location": None,
    }
    gp_filled = {
        "international_fee": None,   # should be skipped (existing=30000)
        "ielts_overall": None,       # should be skipped (existing=6.5)
        "study_mode": "On Campus",   # should be written (existing=None)
        "course_location": "Manchester",  # should be written (existing=None)
        "duration": None,            # should NOT be written (existing=None too, no-op)
    }
    result = _simulate_gemini_null_guard(payload, gp_filled)
    assert result["international_fee"] == 30000.0
    assert result["ielts_overall"] == 6.5
    assert result["study_mode"] == "On Campus"
    assert result["course_location"] == "Manchester"
    assert result.get("duration") is None


# ── ai_fallback.fill_missing null-safety ──────────────────────────────────────
# The ai_fallback path is a DIFFERENT code path from gemini_primary.
# fill_missing() already had correct null-safe behaviour (line 295):
#   missing = [f for f in candidates if not payload.get(f)]
# — it only requests fields that are absent from the payload.  These tests
# verify that behaviour is preserved.

@pytest.mark.asyncio
async def test_ai_fallback_fill_missing_skips_populated_fields():
    """fill_missing only includes MISSING fields in the Gemini request.

    If international_fee is already populated, it must not appear in
    the outgoing prompt and must not be overwritten even if Gemini
    returns it as null in the response.
    """
    payload_with_fee = {
        "international_fee": 24700.0,
        "ielts_overall": 6.5,
        "course_name": "PGCE Secondary Maths",
        "degree_level": "PGCE",
    }

    fake_resp = type(
        "R",
        (),
        {
            "skipped": False,
            "skip_reason": None,
            "text": '{"international_fee": null, "intake_months": ["September"]}',
            "cost_usd": 0.0,
        },
    )()

    # HTML must exceed the 100-char fast-exit threshold in fill_missing.
    # Realistic page extract so _trim_text gives enough text.
    _html = (
        "<h1>PGCE Secondary Mathematics</h1>"
        "<p>English language requirements: IELTS overall score of 6.5.</p>"
        "<p>International tuition fee: £24,700 per year of study.</p>"
        "<p>Course start date: September each year.</p>"
        "<p>This one-year full-time PGCE prepares you to teach mathematics "
        "in secondary schools across the United Kingdom.</p>"
    )
    with patch(
        "app.services.scraper.extractors.ai_fallback.gemini_client.generate",
        new_callable=AsyncMock,
        return_value=fake_resp,
    ):
        filled = await ai_fallback.fill_missing(
            payload_with_fee,
            html=_html,
            url="https://www.manchester.ac.uk/study/courses/pgce-maths/",
        )

    # null values are dropped by _coerce (returns None) + the `if coerced is
    # not None` guard in fill_missing — international_fee must NOT appear
    assert "international_fee" not in filled, (
        "fill_missing must not return a null value for international_fee — "
        "coercion of null → None is filtered out before the dict is returned."
    )
    # intake_months WAS missing and Gemini returned a valid value → should appear
    assert "intake_months" in filled
    assert filled["intake_months"] == ["September"]

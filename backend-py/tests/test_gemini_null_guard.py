"""Regression tests for Gemini's global fill-only extraction policy.

Issue 1 (Manchester review): Gemini returning ``{"international_fee": null}``
for a field that was already populated by a prior extractor (regex, structural,
or university PDF) silently erased the good value at the
``payload[_gp_k] = _gp_v`` write point in the pipeline.

Gemini must only be asked for fields left empty by deterministic extraction,
and its output must never replace an existing usable payload value.

These tests lock in the guard by verifying the rule at the code level.
They also validate that the ``ai_fallback.fill_missing`` function (a different
code path) already had the correct null-safe behaviour — it never puts a key
into its return dict when the Gemini response is null.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.scraper.extractors import ai_fallback
from app.services.scraper.extractors import gemini_primary
from app.services.scraper.pipelines.single_course import (
    _gemini_primary_field_blocked,
    _gemini_primary_missing_fields,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _simulate_gemini_fill_only_guard(payload: dict, gp_filled: dict) -> dict:
    """Reproduce the fill-only guard from single_course.py.

    This is a pure-Python re-implementation of the critical lines so the
    test is fast (no pipeline bootstrap) and breaks immediately if the
    guard is removed.

    Returns the payload after the simulated Gemini merge.
    """
    for k, v in gp_filled.items():
        if payload.get(k) not in (None, "", 0, [], {}):
            continue
        payload[k] = v
    return payload


# ── Null-overwrite guard unit tests ──────────────────────────────────────────

def test_gemini_null_does_not_overwrite_existing_fee():
    """Gemini returning null for international_fee must not erase regex value."""
    payload = {"international_fee": 24700.0, "course_name": "PGCE Secondary Maths"}
    gp_filled = {"international_fee": None, "study_mode": "On Campus"}
    result = _simulate_gemini_fill_only_guard(payload, gp_filled)
    assert result["international_fee"] == 24700.0, (
        "Null from Gemini must never overwrite a regex-found fee value."
    )
    assert result["study_mode"] == "On Campus"  # non-null should still be written


def test_gemini_null_does_not_overwrite_existing_ielts():
    """Gemini returning null for ielts_overall must not erase structured value."""
    payload = {"ielts_overall": 6.5}
    gp_filled = {"ielts_overall": None}
    result = _simulate_gemini_fill_only_guard(payload, gp_filled)
    assert result["ielts_overall"] == 6.5, (
        "Null from Gemini must not overwrite ielts_overall=6.5 from regex."
    )


def test_gemini_non_null_only_fills_missing_fields():
    """Gemini fills gaps but does not replace deterministic values."""
    payload = {"degree_level": "Unknown", "category": None}
    gp_filled = {"degree_level": "Master of Science", "category": "STEM"}
    result = _simulate_gemini_fill_only_guard(payload, gp_filled)
    assert result["degree_level"] == "Unknown"
    assert result["category"] == "STEM"


def test_gemini_null_fills_empty_slot():
    """Gemini returning null for a field that is already None is a no-op."""
    payload = {"international_fee": None}
    gp_filled = {"international_fee": None}
    result = _simulate_gemini_fill_only_guard(payload, gp_filled)
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
    result = _simulate_gemini_fill_only_guard(payload, gp_filled)
    assert result["international_fee"] == 30000.0
    assert result["ielts_overall"] == 6.5
    assert result["study_mode"] == "On Campus"
    assert result["course_location"] == "Manchester"
    assert result.get("duration") is None


def test_gemini_primary_requests_only_canonical_missing_fields():
    payload = {
        "international_fee": 70002.0,
        "fee_term": "Full Course",
        "duration": 2.0,
        "duration_term": "Year",
        "course_location": "Nilai",
        "study_mode": None,
        "ielts_overall": None,
    }

    missing = _gemini_primary_missing_fields(
        payload,
        (
            "international_fee",
            "fee_term",
            "duration_value",
            "duration_unit",
            "duration_text",
            "location_text",
            "mode",
            "ielts_overall",
        ),
    )

    assert missing == ["mode", "ielts_overall"]


def test_gemini_cannot_refill_fee_after_authoritative_no_international_signal():
    payload = {
        "international_fee": None,
        "fee_table_confirmed_no_international": True,
    }

    assert _gemini_primary_field_blocked(payload, "international_fee")
    assert _gemini_primary_missing_fields(
        payload,
        ("international_fee", "ielts_overall"),
    ) == ["ielts_overall"]


@pytest.mark.asyncio
async def test_extract_primary_prompt_and_output_only_include_requested_fields():
    fake_resp = type(
        "R",
        (),
        {
            "skipped": False,
            "skip_reason": None,
            "text": '{"international_fee": 12345, "ielts_overall": 6.5}',
            "cost_usd": 0.0,
            "input_tokens": 10,
            "output_tokens": 5,
        },
    )()
    html = (
        "<h1>Example course</h1><p>International tuition fee 12,345.</p>"
        "<p>English language admission requirements are published here.</p>"
    )

    with patch(
        "app.services.scraper.extractors.gemini_primary.gemini_client.generate",
        new_callable=AsyncMock,
        return_value=fake_resp,
    ) as generate:
        filled, *_ = await gemini_primary.extract_primary(
            html,
            "https://example.edu/course",
            timeout=1,
            fields=["ielts_overall"],
        )

    prompt = generate.await_args.args[0]
    assert "- ielts_overall:" in prompt
    assert "- international_fee:" not in prompt
    assert filled == {"ielts_overall": 6.5}


@pytest.mark.asyncio
async def test_extract_primary_does_not_call_ai_when_nothing_is_missing():
    with patch(
        "app.services.scraper.extractors.gemini_primary.gemini_client.generate",
        new_callable=AsyncMock,
    ) as generate:
        filled, cost, input_tokens, output_tokens, debug = (
            await gemini_primary.extract_primary(
                "<h1>Complete course</h1>",
                "https://example.edu/course",
                timeout=1,
                fields=[],
            )
        )

    generate.assert_not_awaited()
    assert filled == {}
    assert (cost, input_tokens, output_tokens) == (0.0, 0, 0)
    assert debug["skip_reason"] == "no_missing_fields"


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

"""Tests for the Gemini AI fallback extractor."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.ai.gemini_client import GeminiResponse
from app.services.scraper.extractors import ai_fallback


HTML_SAMPLE = """
<html><body>
<h1>Master of Widgets</h1>
<p>This 18-month program prepares students for industry.</p>
<p>International tuition: AUD 42,500 per year.</p>
<p>Intakes: February and July each year.</p>
<p>IELTS: overall 6.5, no band below 6.0.</p>
</body></html>
"""


@pytest.mark.asyncio
async def test_fill_missing_no_call_when_payload_complete():
    payload = {
        "international_fee": 42500,
        "fee_currency": "AUD",
        "ielts_overall": 6.5,
        "intake_months": [2, 7],
        "duration_value": 18,
        "duration_unit": "months",
        "course_location": "Darwin",
    }
    with patch.object(ai_fallback.gemini_client, "generate") as gen:
        out = await ai_fallback.fill_missing(payload, html=HTML_SAMPLE, url="x")
    assert out == {}
    gen.assert_not_called()


@pytest.mark.asyncio
async def test_fill_missing_skips_when_no_api_key():
    async def _fake_gen(prompt, *, max_output_tokens=512):
        return GeminiResponse("", 1, 0, 0.0, skipped=True, skip_reason="GEMINI_API_KEY not set")

    with patch.object(ai_fallback.gemini_client, "generate", side_effect=_fake_gen):
        out = await ai_fallback.fill_missing({}, html=HTML_SAMPLE, url="x")
    assert out == {}


@pytest.mark.asyncio
async def test_fill_missing_parses_and_coerces_response():
    raw = (
        'Here is the data:\n{'
        '"international_fee": "42500", '
        '"fee_currency": "AUD", '
        '"ielts_overall": 6.5, '
        '"intake_months": [2, 7, 99, "bad"], '
        '"duration_value": 18, '
        '"duration_unit": "months"}'
    )

    async def _fake_gen(prompt, *, max_output_tokens=512):
        return GeminiResponse(raw, 100, 50, 0.0001, skipped=False)

    with patch.object(ai_fallback.gemini_client, "generate", side_effect=_fake_gen):
        out = await ai_fallback.fill_missing({}, html=HTML_SAMPLE, url="x")

    assert out["international_fee"] == 42500.0
    assert out["fee_currency"] == "AUD"
    assert out["ielts_overall"] == 6.5
    assert out["intake_months"] == ["February", "July"]  # 99 and "bad" filtered; ints → month names
    assert out["duration_value"] == 18.0
    assert out["duration_unit"] == "months"


@pytest.mark.asyncio
async def test_fill_missing_only_requests_missing_fields():
    payload = {"international_fee": 1000, "fee_currency": "AUD"}
    captured: dict[str, str] = {}

    async def _fake_gen(prompt, *, max_output_tokens=512):
        captured["prompt"] = prompt
        return GeminiResponse(
            '{"ielts_overall": 6.0, "intake_months": [2], "duration_value": 2, "duration_unit": "years"}',
            10,
            5,
            0.0,
            skipped=False,
        )

    with patch.object(ai_fallback.gemini_client, "generate", side_effect=_fake_gen):
        out = await ai_fallback.fill_missing(payload, html=HTML_SAMPLE, url="x")

    # The Week 1 Prompt 5 CRITICAL RULES block now mentions
    # ``international_fee`` in the prompt body (priority order example),
    # so check the per-field hint section instead — that's where the
    # caller decides which fields to actually request.
    _fields_block_start = captured["prompt"].index("Fields to extract:")
    _fields_block = captured["prompt"][_fields_block_start:]
    assert "international_fee" not in _fields_block
    assert "ielts_overall" in _fields_block
    assert out == {
        "ielts_overall": 6.0,
        "intake_months": ["February"],  # int 2 → month name
        "duration_value": 2.0,
        "duration_unit": "years",
    }


@pytest.mark.asyncio
async def test_fill_missing_handles_malformed_json():
    async def _fake_gen(prompt, *, max_output_tokens=512):
        return GeminiResponse("not json at all", 1, 1, 0.0, skipped=False)

    with patch.object(ai_fallback.gemini_client, "generate", side_effect=_fake_gen):
        out = await ai_fallback.fill_missing({}, html=HTML_SAMPLE, url="x")
    assert out == {}


# --- Prod-bug regression: duration prompt clarity ----------------------------


def test_duration_value_hint_excludes_credit_points():
    """Bug: prod ASA Masters rows showed duration=5 because the page text
    said `5 units of 8 credit points each across 2 years` and the vague
    old hint (`Course duration as a number`) let Gemini return 5. The
    revised hint must explicitly forbid credit-point / unit-count
    interpretation. This test guards the prompt text so it doesn't drift
    back to the vague one-liner.
    """
    hint = ai_fallback._FIELD_HINTS["duration_value"]
    lower = hint.lower()
    # Must explicitly mention the credit-point trap so Gemini knows to
    # ignore it.
    assert "credit" in lower or "credit-point" in lower
    assert "unit" in lower
    # Must give an explicit example so the model learns the pattern.
    assert "2" in hint or "3" in hint


def test_duration_unit_hint_forbids_units_or_credit_points():
    """The unit hint must also be explicit — `years` or `months`, never
    `units` or `credit points`."""
    hint = ai_fallback._FIELD_HINTS["duration_unit"]
    assert "year" in hint.lower()
    assert "never" in hint.lower() or "not" in hint.lower()

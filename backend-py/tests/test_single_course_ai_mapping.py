"""B20: regression tests for ``_apply_ai_duration_mapping``.

The AI fallback returns duration as ``duration_value`` + ``duration_unit``
(matching the prompt the model is shown), but the staged-course schema
stores it as ``duration`` (real) + ``duration_term`` (Year/Month/Week/...).
Without an explicit translation step the AI's answer was silently dropped:
the canonical keys remained empty and the row landed in the staging table
with a number-only duration ("3" instead of "3 Years"). These tests guard
the translation."""
from __future__ import annotations

import pytest

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import ExtractionConfig, UniConfig
from app.services.scraper.extractors import ai_fallback, gemini_primary
from app.services.scraper.pipelines.single_course import _apply_ai_duration_mapping
from app.services.scraper.pipelines.single_course import extract_course


@pytest.fixture
def isolated_course_extraction(monkeypatch):
    """Install an in-memory config and fail if any network fallback runs."""
    config = UniConfig(
        slug="example",
        name="Example University",
        university_id=1,
        base_url="https://example.edu",
        scrape_url="https://example.edu/courses",
        extraction=ExtractionConfig(
            skip_browser_rescue=True,
            skip_per_course_browser=True,
        ),
    )
    token = current_uni_config.set(config)

    async def _unexpected_network(*args, **kwargs):
        raise AssertionError("isolated duration regression attempted network I/O")

    monkeypatch.setattr(
        "app.services.scraper.pipelines.single_course.fetch_html",
        _unexpected_network,
    )
    monkeypatch.setattr(
        "app.services.scraper.browser_pool.pool.fetch_html",
        _unexpected_network,
    )
    yield
    current_uni_config.reset(token)


def test_translates_years_to_canonical_keys_when_payload_empty():
    payload: dict = {}
    ai_filled = {"duration_value": 3, "duration_unit": "years"}
    _apply_ai_duration_mapping(payload, ai_filled)
    assert ai_filled["duration"] == 3.0
    assert ai_filled["duration_term"] == "Year"
    assert "duration_value" not in ai_filled
    assert "duration_unit" not in ai_filled


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
    """Rule extractor's regex hit must always beat the AI guess."""
    payload = {"duration": 2.0, "duration_term": "Year"}
    ai_filled = {"duration_value": 5, "duration_unit": "years"}
    _apply_ai_duration_mapping(payload, ai_filled)
    # Mapping must not add canonical keys because the deterministic pair is
    # already present. The AI-only aliases are consumed before payload merge.
    assert "duration" not in ai_filled
    assert "duration_term" not in ai_filled
    assert "duration_value" not in ai_filled
    assert "duration_unit" not in ai_filled


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ai_shape", "expected_duration", "expected_term"),
    [
        ({"duration_value": 3.0, "duration_unit": "years"}, 3.0, "Year"),
        ({"duration_value": 18.0, "duration_unit": "months"}, 18.0, "Month"),
        ({"duration_value": 13.0, "duration_unit": "weeks"}, 13.0, "Week"),
        ({"duration_value": None, "duration_unit": None}, None, None),
        ({"duration_value": 1.0, "duration_unit": "credits"}, 1.0, None),
        ({"duration_value": "three", "duration_unit": "years"}, None, "Year"),
    ],
    ids=["years", "months", "weeks", "nulls", "unknown-unit", "non-numeric"],
)
async def test_extract_course_normalises_actual_ai_fallback_duration_shape(
    monkeypatch,
    isolated_course_extraction,
    ai_shape,
    expected_duration,
    expected_term,
):
    async def _fill_missing(*args, **kwargs):
        return dict(ai_shape)

    async def _skip_primary(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr(ai_fallback, "fill_missing", _fill_missing)
    monkeypatch.setattr(gemini_primary, "extract_primary", _skip_primary)
    html = """
        <html><body>
          <h1>Graduate Certificate in Applied Studies</h1>
          <p>Detailed admissions, curriculum, fees, locations, and course
          information for international applicants.</p>"""
    html += "<p>General course information for applicants.</p>" * 60
    html += """
        </body></html>
    """

    result = await extract_course(
        "https://example.edu/courses/applied-studies",
        html=html,
        use_ai_fallback=True,
    )

    payload = result["payload"]
    assert payload.get("duration") == expected_duration
    assert payload.get("duration_term") == expected_term
    assert "duration_value" not in payload
    assert "duration_unit" not in payload


@pytest.mark.asyncio
async def test_extract_course_keeps_deterministic_duration_pair_over_ai(
    monkeypatch,
    isolated_course_extraction,
):
    async def _fill_missing(*args, **kwargs):
        return {"duration_value": 18.0, "duration_unit": "months"}

    async def _skip_primary(*args, **kwargs):
        return {}, 0.0, 0, 0, {"skipped": True}

    monkeypatch.setattr(ai_fallback, "fill_missing", _fill_missing)
    monkeypatch.setattr(gemini_primary, "extract_primary", _skip_primary)
    html = """
        <html><body>
          <h1>Graduate Diploma in Audio Production</h1>
          <dl><dt>Duration</dt><dd>2 years full-time</dd></dl>
          <p>Detailed admissions, curriculum, fees, locations, and course
          information for international applicants.</p>"""
    html += "<p>General course information for applicants.</p>" * 60
    html += """
        </body></html>
    """

    result = await extract_course(
        "https://example.edu/courses/audio-production",
        html=html,
        use_ai_fallback=True,
    )

    payload = result["payload"]
    assert payload["duration"] == 2.0
    assert payload["duration_term"] == "Year"
    assert "duration_value" not in payload
    assert "duration_unit" not in payload

"""Tests for the Gemini cost gate (Component 1).

Verifies the three decision branches:
  - all_high_value_fields_populated  → skip Gemini entirely
  - classification_only              → run with cheap prompt
  - full_extraction_needed           → run full prompt
"""
from __future__ import annotations

import logging

import pytest

from app.services.scraper.course_deadline import (
    REQUIRED_COURSE_FIELDS,
    RequiredCourseField,
)
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
    test_values = {
        "international_fee": 29400,
        "english_score": 6.5,
        "duration": 3.0,
        "intake": ["February", "June"],
        "course_location": "Main Campus",
        "study_mode": "On Campus",
    }
    base = {
        field.aliases[0]: test_values.get(field.name, "present")
        for field in REQUIRED_COURSE_FIELDS
    }
    base["course_name"] = "Bachelor of Arts"
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


@pytest.mark.parametrize(
    "required_field",
    REQUIRED_COURSE_FIELDS,
    ids=lambda field: field.name,
)
def test_each_missing_required_publishability_field_forces_full_extraction(
    required_field: RequiredCourseField,
):
    payload = _full_payload(include_classification=True)
    for alias in required_field.aliases:
        payload.pop(alias, None)
    evidence = _make_evidence(payload, confidence=0.95)

    skip, reason = should_skip_gemini_primary(payload, evidence)

    assert skip is False
    assert reason == "full_extraction_needed"


def test_every_required_fact_is_supported_by_full_ai_request_and_merge():
    from app.services.scraper.extractors.gemini_primary import (
        GEMINI_PRIMARY_SUPPORTED_FIELDS,
    )
    from app.services.scraper.pipelines.single_course import (
        GEMINI_PRIMARY_FIELD_TARGETS,
    )

    failures: list[str] = []
    for fact in REQUIRED_COURSE_FIELDS:
        if fact.deterministic_only_reason:
            continue
        aliases = set(fact.aliases)
        usable_fields = [
            ai_field
            for ai_field in fact.full_ai_fields
            if ai_field in GEMINI_PRIMARY_SUPPORTED_FIELDS
            and GEMINI_PRIMARY_FIELD_TARGETS.get(ai_field) in aliases
        ]
        if not usable_fields:
            failures.append(
                f"{fact.name}: AI fields {fact.full_ai_fields!r} are not both "
                "requestable and merged into one of "
                f"{fact.aliases!r}"
            )

    assert not failures, "\n".join(failures)


def test_full_ai_supported_field_contract_is_immutable_and_complete():
    from app.services.scraper.extractors.gemini_primary import (
        GEMINI_PRIMARY_FIELD_INSTRUCTIONS,
        GEMINI_PRIMARY_SUPPORTED_FIELDS,
    )

    assert isinstance(GEMINI_PRIMARY_SUPPORTED_FIELDS, tuple)
    assert GEMINI_PRIMARY_SUPPORTED_FIELDS == tuple(
        GEMINI_PRIMARY_FIELD_INSTRUCTIONS
    )
    with pytest.raises(TypeError):
        GEMINI_PRIMARY_FIELD_INSTRUCTIONS["new_field"] = "drift"  # type: ignore[index]


def test_required_fact_must_declare_ai_support_or_deterministic_only_reason():
    with pytest.raises(ValueError, match="full AI fields or an explicit"):
        RequiredCourseField("unsupported", "Unsupported", ("unsupported",), ())

    deterministic = RequiredCourseField(
        "deterministic_fact",
        "Deterministic Fact",
        ("deterministic_fact",),
        (),
        deterministic_only_reason="Derived from signed catalogue metadata",
    )
    assert deterministic.deterministic_only_reason


@pytest.mark.parametrize("taxonomy_field", ["category", "sub_category"])
def test_classification_only_is_limited_to_taxonomy_gaps(taxonomy_field: str):
    payload = _full_payload(include_classification=True)
    del payload[taxonomy_field]
    evidence = _make_evidence(payload, confidence=0.95)

    skip, reason = should_skip_gemini_primary(payload, evidence)

    assert skip is False
    assert reason == "classification_only"


def test_online_course_can_classify_without_physical_location():
    payload = _full_payload(include_classification=False)
    payload["study_mode"] = "Online"
    del payload["course_location"]
    evidence = _make_evidence(payload, confidence=0.95)

    skip, reason = should_skip_gemini_primary(payload, evidence)

    assert skip is False
    assert reason == "classification_only"


def test_full_extraction_when_fields_missing():
    payload = {"course_name": "Bachelor of Arts"}
    evidence = [{"field_key": "course_name", "confidence": 0.90, "value": "Bachelor of Arts", "method": "h1"}]
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is False
    assert reason == "full_extraction_needed"


def test_missing_required_alias_groups_are_named_in_gate_log(caplog):
    payload = _full_payload(include_classification=True)
    del payload["ielts_overall"]
    del payload["course_location"]

    with caplog.at_level(logging.DEBUG):
        skip, reason = should_skip_gemini_primary(
            payload,
            _make_evidence(payload, confidence=0.95),
        )

    assert skip is False
    assert reason == "full_extraction_needed"
    assert "english_score, course_location" in caplog.text


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


def test_classification_only_prompt_constrains_known_parent_subcategories():
    from app.services.scraper.gemini_gate import build_classification_only_prompt

    prompt = build_classification_only_prompt(
        "Bachelor of Media and Communication",
        "Study media, communication and contemporary culture.",
        "Media & Communications",
    )
    assert "MUST be exactly: Media & Communications" in prompt
    assert "Choose sub_category ONLY from:" in prompt
    assert "Communications" in prompt
    assert "Computer Science" not in prompt


def test_single_course_gemini_merge_keeps_nonblank_taxonomy_fill_only():
    import inspect
    from app.services.scraper.pipelines import single_course

    source = inspect.getsource(single_course.extract_course)
    assert '_gp_k in {"category", "sub_category"}' in source
    assert "payload.get(_gp_k)" in source


def _pipeline_course_html() -> str:
    return """
    <html><head><title>Bachelor of Testing | SIT</title></head><body>
      <span id="courseName">Bachelor of Testing</span>
      <div class="CourseInfo CourseSummary">
        <span id="currentCampusName">Invercargill</span>
        <div class="keyInfoPane">
          <div class="row no-gutters">
            <div>Qualification:</div><div>Degree</div>
            <div>Duration:</div><div>Three years full-time</div>
            <div>Study Modes:</div><div>On Campus</div>
          </div>
        </div>
        <div class="lightGrey_bg_1 mb-4">
          Dates: 2027 Semester 1: 15 February to 25 June 2027
          International Fees can be found here.
        </div>
        <div id="headerApplicationCriteria_123">
          International applicants require IELTS 6.0.
        </div>
      </div>
    </body></html>
    """


def _pipeline_central_data() -> dict:
    return {
        "fees": [
            {
                "program_pattern": "Bachelor of Testing",
                "international_fee": 19_000,
                "currency": "NZD",
                "per": "Annual",
            }
        ],
        "fee_page_url": "https://www.sit.ac.nz/Fees-Enrolments/International-Fees",
    }


@pytest.mark.asyncio
async def test_pipeline_missing_required_location_invokes_full_extraction(monkeypatch):
    from app.services.ai import gemini_client
    from app.services.scraper import gemini_gate
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.course_deadline import required_course_fields_complete
    from app.services.scraper.extractors import gemini_primary, location
    from app.services.scraper.pipelines import single_course

    set_uni_config(
        load_uni_config(
            slug="sit",
            scrape_url="https://www.sit.ac.nz",
            university_id=67,
            name="Southern Institute of Technology",
        )
    )
    original_gate = gemini_gate.should_skip_gemini_primary
    gate_calls: list[tuple[bool, str]] = []
    full_calls: list[tuple[str, ...]] = []

    async def no_location(*_args, **_kwargs):
        return []

    def record_gate(payload, evidence):
        assert not required_course_fields_complete(payload)
        assert required_course_fields_complete(
            {**payload, "course_location": "Invercargill"}
        )
        decision = original_gate(payload, evidence)
        gate_calls.append(decision)
        return decision

    async def record_full(_html, _url, *, fields, **_kwargs):
        full_calls.append(tuple(fields))
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": "test"}

    async def fail_classification(*_args, **_kwargs):
        raise AssertionError("missing location must not use classification-only")

    monkeypatch.setattr(location, "extract", no_location)
    monkeypatch.setattr(gemini_gate, "should_skip_gemini_primary", record_gate)
    monkeypatch.setattr(gemini_primary, "extract_primary", record_full)
    monkeypatch.setattr(gemini_client, "generate", fail_classification)

    await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Bachelor of Testing",
        country="New Zealand",
        html=_pipeline_course_html(),
        use_ai_fallback=False,
        central_data=_pipeline_central_data(),
    )

    assert gate_calls == [(False, "full_extraction_needed")]
    assert len(full_calls) == 1
    assert "location_text" in full_calls[0]


@pytest.mark.asyncio
async def test_pipeline_taxonomy_only_gap_uses_classification_prompt(monkeypatch):
    from app.services.ai import gemini_client
    from app.services.scraper import gemini_gate
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.config.loader import load_uni_config
    from app.services.scraper.course_deadline import required_course_fields_complete
    from app.services.scraper.extractors import gemini_primary
    from app.services.scraper.pipelines import single_course

    set_uni_config(
        load_uni_config(
            slug="sit",
            scrape_url="https://www.sit.ac.nz",
            university_id=67,
            name="Southern Institute of Technology",
        )
    )
    original_gate = gemini_gate.should_skip_gemini_primary
    classification_calls: list[str | None] = []

    def taxonomy_only_gate(payload, evidence):
        assert required_course_fields_complete(payload)
        payload["category"] = None
        payload["sub_category"] = None
        return original_gate(payload, evidence)

    async def fail_full(*_args, **_kwargs):
        raise AssertionError("taxonomy-only gap must not use full extraction")

    async def record_classification(*_args, **kwargs):
        classification_calls.append(kwargs.get("call_type"))
        return gemini_client.GeminiResponse(
            '{"category":"Business & Management","sub_category":"Business"}',
            10,
            5,
            0.0,
        )

    monkeypatch.setattr(gemini_gate, "should_skip_gemini_primary", taxonomy_only_gate)
    monkeypatch.setattr(gemini_primary, "extract_primary", fail_full)
    monkeypatch.setattr(gemini_client, "generate", record_classification)

    await single_course.extract_course(
        "https://www.sit.ac.nz/Programme/Course/Bachelor of Testing",
        country="New Zealand",
        html=_pipeline_course_html(),
        use_ai_fallback=False,
        central_data=_pipeline_central_data(),
    )

    assert classification_calls == ["classification_only"]

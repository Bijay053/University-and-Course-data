from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers.scrape import (
    ReExtractBody,
    _targeted_reextract_fields,
    analyze_staged,
)
from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.extractors.english_test import extract as extract_english
from app.services.scraper.pipelines.single_course import extract_course


FIXTURES = Path(__file__).parent / "fixtures"
SOCIOLOGY_URL = (
    "https://www.winchester.ac.uk/study/undergraduate/"
    "Courses/BA-Hons-Sociology/"
)
PUBLIC_HEALTH_URL = (
    "https://www.winchester.ac.uk/study/Postgraduate/"
    "Courses/MPH-Public-Health/"
)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _winchester_config(university_id: int):
    return get_config_for_host(
        hostname="www.winchester.ac.uk",
        name="University of Winchester",
        scrape_url="https://www.winchester.ac.uk/study/",
        university_id=university_id,
    )


def _normalized(results) -> dict:
    payload: dict = {}
    for result in results:
        payload.update(result.normalized or {})
    return payload


def test_runtime_id_uses_verified_winchester_recipe():
    # Production/dev IDs are not portable. Runtime university 87 must resolve
    # the sole hostname-verified recipe even though its filename carries 2229.
    config = _winchester_config(87)
    assert config.extraction.course_name.prefer_title_over_h1 is True
    assert config.extraction.english.trust_vision_ocr is False


@pytest.mark.asyncio
async def test_fix_analysis_targets_bare_winchester_name_and_english_companions(
    monkeypatch,
):
    row = SimpleNamespace(
        course_name="Sociology",
        course_website=SOCIOLOGY_URL,
        ielts_overall=6.0,
        international_fee=100,
        course_location="Winchester",
        study_mode="Full-time",
        duration=3,
        intake_months=["September"],
        academic_level="Undergraduate",
        other_requirement="A levels or equivalent",
    )
    university = SimpleNamespace(name="University of Winchester")

    class _Scalars:
        @staticmethod
        def all():
            return [row]

    class _Result:
        @staticmethod
        def scalars():
            return _Scalars()

    class _DB:
        @staticmethod
        async def get(*_args, **_kwargs):
            return university

        @staticmethod
        async def execute(*_args, **_kwargs):
            return _Result()

    monkeypatch.setattr(
        "app.services.scraper.requirement_status.effective_requirement_status",
        lambda _row: {
            "academic": {"state": "verified"},
            "englishComponents": {"state": "unverified"},
        },
    )
    analysis = await analyze_staged(
        ReExtractBody(ids=[1], universityId=87),
        _DB(),
    )
    issue_by_field = {item["field"]: item for item in analysis["issues"]}
    assert issue_by_field["course_name"]["label"] == "Missing Award in Course Title"
    assert "english_requirements" in issue_by_field

    # This is the same issue list the page sends as targetFields. Both the name
    # and IELTS component companions survive the targeted persistence boundary.
    targets = _targeted_reextract_fields(list(issue_by_field))
    assert targets is not None
    assert "course_name" in targets
    assert {
        "ielts_overall",
        "ielts_listening",
        "ielts_reading",
        "ielts_writing",
        "ielts_speaking",
    } <= targets

    row.course_name = "BA (Hons) Sociology"
    fixed = await analyze_staged(
        ReExtractBody(ids=[1], universityId=87),
        _DB(),
    )
    assert "course_name" not in {item["field"] for item in fixed["issues"]}


@pytest.mark.asyncio
async def test_sociology_ssr_extracts_overall_and_all_four_components():
    payload = _normalized(
        await extract_english(
            _html("winchester_ba_sociology_ssr.html"), SOCIOLOGY_URL
        )
    )
    assert payload == {
        "ielts_overall": 6.0,
        "ielts_listening": 5.5,
        "ielts_reading": 5.5,
        "ielts_writing": 5.5,
        "ielts_speaking": 5.5,
    }


@pytest.mark.asyncio
async def test_public_health_named_writing_floor_does_not_fabricate_other_bands():
    payload = _normalized(
        await extract_english(
            _html("winchester_mph_public_health_ssr.html"), PUBLIC_HEALTH_URL
        )
    )
    assert payload["ielts_overall"] == 6.0
    assert payload["ielts_writing"] == 5.5
    assert payload.get("ielts_listening") is None
    assert payload.get("ielts_reading") is None
    assert payload.get("ielts_speaking") is None


@pytest.mark.asyncio
async def test_saved_ssr_full_pipeline_preserves_course_owned_scores_over_pdf(
    monkeypatch,
):
    async def no_ai(*_args, **_kwargs):
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": "deterministic test"}

    monkeypatch.setattr(
        "app.services.scraper.extractors.gemini_primary.extract_primary", no_ai
    )
    set_uni_config(_winchester_config(87))
    result = await extract_course(
        SOCIOLOGY_URL,
        html=_html("winchester_ba_sociology_ssr.html"),
        use_ai_fallback=False,
        uni_pdf_data={
            "english": {
                "ielts_overall": 6.5,
                "pte_overall": 59,
                "toefl_overall": 80,
                "cambridge_overall": 170,
            },
            "requirements_pdf_url": "https://example.invalid/related-course.pdf",
        },
    )
    payload = result["payload"]
    assert payload["course_name"] == "BA (Hons) Sociology"
    assert payload["ielts_overall"] == 6.0
    assert payload["ielts_listening"] == 5.5
    assert payload["ielts_reading"] == 5.5
    assert payload["ielts_writing"] == 5.5
    assert payload["ielts_speaking"] == 5.5
    assert payload.get("pte_overall") is None
    assert payload.get("toefl_overall") is None
    assert payload.get("cambridge_overall") is None


@pytest.mark.asyncio
async def test_saved_public_health_full_pipeline_keeps_only_named_writing_floor(
    monkeypatch,
):
    async def no_ai(*_args, **_kwargs):
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": "deterministic test"}

    monkeypatch.setattr(
        "app.services.scraper.extractors.gemini_primary.extract_primary", no_ai
    )
    set_uni_config(_winchester_config(87))
    result = await extract_course(
        PUBLIC_HEALTH_URL,
        html=_html("winchester_mph_public_health_ssr.html"),
        use_ai_fallback=False,
    )
    payload = result["payload"]
    assert payload["course_name"] == "MPH Public Health"
    assert payload["ielts_overall"] == 6.0
    assert payload["ielts_writing"] == 5.5
    assert payload.get("ielts_listening") is None
    assert payload.get("ielts_reading") is None
    assert payload.get("ielts_speaking") is None
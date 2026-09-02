"""Regression coverage for Gemini PRIMARY intake arbitration."""

from app.services.scraper.pipelines.single_course import (
    _gemini_may_override_course_page_value,
)


def test_gemini_may_replace_only_weak_intake_regex_result() -> None:
    assert _gemini_may_override_course_page_value(
        "intake_months",
        "regex",
    )


def test_gemini_cannot_replace_authoritative_intake_structure() -> None:
    assert not _gemini_may_override_course_page_value(
        "intake_months",
        "intake.structural",
    )
    assert not _gemini_may_override_course_page_value(
        "intake_months",
        "intake.start_dates_section",
    )
    assert not _gemini_may_override_course_page_value(
        "intake_months",
        "intake.waikato_trimesters",
    )


def test_non_intake_regex_results_remain_protected() -> None:
    assert not _gemini_may_override_course_page_value(
        "duration",
        "regex",
    )


def test_missing_course_page_owner_remains_fillable() -> None:
    assert _gemini_may_override_course_page_value(
        "intake_months",
        None,
    )
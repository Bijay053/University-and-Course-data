"""Global enforcement tests for domestic-only and online-only courses."""
from __future__ import annotations

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import (
    _domestic_only_filter_enabled,
    _duration_labeled_values,
    _infer_study_load_from_text,
    _is_parttime_only_page,
    _parttime_only_filter_enabled,
)


def test_disabled_legacy_overrides_cannot_bypass_global_delivery_filters() -> None:
    """A stale YAML/admin `enabled: false` must not admit ineligible courses."""
    config = UniConfig.model_validate(
        {
            "slug": "legacy-disabled-example",
            "name": "Legacy Disabled Example University",
            "base_url": "https://example.edu",
            "scrape_url": "https://example.edu/courses",
            "extraction": {
                "filters": {
                    "domestic_only": {"enabled": False},
                    "online_only": {"enabled": False},
                }
            }
        }
    )
    token = current_uni_config.set(config)
    try:
        assert _domestic_only_filter_enabled() is True

        online_ok, online_reason = should_stage_course(
            "Online Master of Business",
            {
                "course_name": "Online Master of Business",
                "international_fee": 30000,
                "study_mode": "Online",
            },
            source_url="https://example.edu/courses/master-of-business-online",
        )
        assert online_ok is False
        assert online_reason == "online_only"

        domestic_ok, domestic_reason = should_stage_course(
            "Master of Business",
            {
                "course_name": "Master of Business",
                "international_fee": 30000,
                "study_mode": "Blended",
                "domestic_only": True,
            },
            source_url="https://example.edu/courses/master-of-business",
        )
        assert domestic_ok is False
        assert domestic_reason == "domestic_only"
    finally:
        current_uni_config.reset(token)


def test_full_time_wins_when_duration_also_mentions_part_time_equivalent() -> None:
    assert (
        _infer_study_load_from_text("2 years full-time or part-time equivalent")
        == "Full Time"
    )
    assert (
        _infer_study_load_from_text("3 years, or part-time equivalent")
        == "Full Time"
    )


def test_explicit_part_time_only_wording_overrides_equivalent_full_time_measure() -> None:
    assert (
        _infer_study_load_from_text(
            "Duration 1 year equivalent full-time study. Only available part-time."
        )
        == "Part Time"
    )


def test_uow_duration_row_is_not_part_time_only_when_full_time_is_offered() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">
        2 years full-time or part-time equivalent
      </div>
    </div>
    """
    assert _is_parttime_only_page(html) is False


def test_nested_duration_label_reads_its_list_item_value() -> None:
    html = """
    <ul class="details-listing">
      <li>
        <span class="details-listing__title"><strong>Duration</strong></span>
        <span class="details-listing__value">
          3 years full-time or equivalent part-time
        </span>
      </li>
    </ul>
    """
    assert (
        _infer_study_load_from_text(" ".join(_duration_labeled_values(html)))
        == "Full Time"
    )


def test_part_time_only_duration_is_globally_rejected() -> None:
    html = """
    <div class="cf-college-info__row">
      <div class="cf-college-info__left"><span>Duration</span></div>
      <div class="cf-college-info__right">2 years part-time</div>
    </div>
    """
    assert _parttime_only_filter_enabled() is True
    assert _is_parttime_only_page(html) is True

    accepted, reason = should_stage_course(
        "Master of Part-Time Study",
        {
            "course_name": "Master of Part-Time Study",
            "international_fee": 30000,
            "study_mode": "On Campus",
            "study_load": "Part Time",
        },
        source_url="https://example.edu/master-of-part-time-study",
    )
    assert accepted is False
    assert reason == "part_time_only"
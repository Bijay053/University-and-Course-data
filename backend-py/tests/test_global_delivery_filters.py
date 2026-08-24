"""Global enforcement tests for domestic-only and online-only courses."""
from __future__ import annotations

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import (
    _domestic_only_filter_enabled,
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
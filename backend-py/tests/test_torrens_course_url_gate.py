"""Regression tests for Torrens faculty/topic pages entering extraction."""

from __future__ import annotations

import re

import pytest

from app.services.scraper.config.loader import load_uni_config


@pytest.fixture(scope="module")
def torrens_detail_patterns() -> list[re.Pattern[str]]:
    cfg = load_uni_config(
        slug="torrens",
        name="Torrens University Australia",
        scrape_url="https://www.torrens.edu.au",
        university_id=22,
    )
    raw_patterns = cfg.discovery.course_detail_url_patterns
    assert raw_patterns, "Torrens needs a strict final extraction URL gate"
    return [re.compile(pattern, re.IGNORECASE) for pattern in raw_patterns]


def _matches(url: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(pattern.search(url) for pattern in patterns)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.torrens.edu.au/courses/health/faculty-of-health/nursing-school",
        "https://www.torrens.edu.au/courses/technology/information-technology",
        "https://www.torrens.edu.au/courses/technology/technology-school",
        "https://www.torrens.edu.au/courses/technology/cloud-computing",
        "https://www.torrens.edu.au/courses/technology/artificial-intelligence",
        "https://www.torrens.edu.au/courses/hospitality/hospitality-management-tourism",
        "https://www.torrens.edu.au/courses/hospitality/hotel-management",
    ],
)
def test_reported_non_course_pages_fail_final_extraction_gate(
    url: str,
    torrens_detail_patterns: list[re.Pattern[str]],
) -> None:
    assert not _matches(url, torrens_detail_patterns)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.torrens.edu.au/courses/design/advanced-diploma-of-game-programming",
        "https://www.torrens.edu.au/courses/hospitality/associate-degree-of-business-hospitality-and-tourism-management",
        "https://www.torrens.edu.au/courses/design/bachelor-of-3d-design-and-animation",
        "https://www.torrens.edu.au/courses/technology/diploma-of-information-technology",
        "https://www.torrens.edu.au/courses/business/graduate-certificate-of-business-analytics",
        "https://www.torrens.edu.au/courses/technology/graduate-diploma-of-cybersecurity",
        "https://www.torrens.edu.au/courses/business/master-of-business-administration",
        "https://www.torrens.edu.au/courses/health/professional-doctorate-applied-research",
        "https://www.torrens.edu.au/courses/health/undergraduate-certificate-of-mental-health",
    ],
)
def test_real_award_pages_pass_final_extraction_gate(
    url: str,
    torrens_detail_patterns: list[re.Pattern[str]],
) -> None:
    assert _matches(url, torrens_detail_patterns)


def test_future_topic_slug_is_rejected_without_a_slug_blocklist(
    torrens_detail_patterns: list[re.Pattern[str]],
) -> None:
    assert not _matches(
        "https://www.torrens.edu.au/courses/technology/quantum-computing",
        torrens_detail_patterns,
    )
    assert _matches(
        "https://www.torrens.edu.au/courses/technology/bachelor-of-quantum-computing",
        torrens_detail_patterns,
    )
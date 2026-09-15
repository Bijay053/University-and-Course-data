from __future__ import annotations

import re

from app.services.scraper.config.loader import load_uni_config


def _config():
    return load_uni_config(
        slug="massey",
        scrape_url="https://www.massey.ac.nz/study/courses/",
        university_id=47,
        name="Massey University",
    )


def test_massey_uses_complete_qualification_catalogue() -> None:
    config = _config()

    assert config.discovery.seed_urls == [
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
    ]
    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.expected_min_courses >= 170
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.skip_browser_discovery is True


def test_massey_only_promotes_qualification_detail_urls() -> None:
    config = _config()
    detail_pattern = re.compile(config.discovery.course_detail_url_patterns[0])
    candidate_pattern = re.compile(config.discovery.force_candidate_url_patterns[0])

    detail_url = (
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
        "bachelor-of-accountancy-UBACC/"
    )
    assert detail_pattern.fullmatch(detail_url)
    assert candidate_pattern.fullmatch(
        "/study/all-qualifications-and-degrees/bachelor-of-accountancy-UBACC/"
    )

    assert not detail_pattern.fullmatch(
        "https://www.massey.ac.nz/study/all-qualifications-and-degrees/"
    )
    assert not detail_pattern.fullmatch("https://www.massey.ac.nz/study/courses/")
    assert not detail_pattern.fullmatch(
        "https://www.massey.ac.nz/study/planning-your-study/"
        "prospectus-booklets-and-guides/"
    )
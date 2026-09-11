import re

from app.services.scraper.config.loader import load_uni_config


def _load_unsw(db_scrape_config=None):
    return load_uni_config(
        slug="unsw",
        name="UNSW Sydney",
        scrape_url="https://www.unsw.edu.au",
        university_id=32,
        db_scrape_config=db_scrape_config,
    )


def test_unsw_professional_development_catalogue_is_excluded() -> None:
    config = _load_unsw()
    patterns = [re.compile(pattern) for pattern in config.discovery.block_url_patterns]

    excluded = (
        "https://www.unsw.edu.au/study/professional-development",
        "https://www.unsw.edu.au/study/professional-development/course/"
        "master-of-data-science",
    )
    for url in excluded:
        assert any(pattern.search(url) for pattern in patterns)

    degree_url = "https://www.unsw.edu.au/study/postgraduate/master-of-data-science"
    assert not any(pattern.search(degree_url) for pattern in patterns)


def test_unsw_verified_degree_paths_override_stale_professional_course_rule() -> None:
    config = _load_unsw({
        "admin_config": {
            "discovery": {
                "allow_url_patterns": [
                    r"^https?://www\.unsw\.edu\.au/study/"
                    r"professional-development/course/[^/?#]+$"
                ],
                "course_detail_url_patterns": [
                    r"^https?://www\.unsw\.edu\.au/study/"
                    r"professional-development/course/[^/?#]+$"
                ],
            }
        }
    })

    allow_patterns = [
        re.compile(pattern) for pattern in config.discovery.allow_url_patterns
    ]
    detail_patterns = [
        re.compile(pattern)
        for pattern in config.discovery.course_detail_url_patterns
    ]
    degree_urls = (
        "https://www.unsw.edu.au/study/undergraduate/"
        "bachelor-of-advanced-computer-science-honours",
        "https://www.unsw.edu.au/study/postgraduate/master-of-data-science",
    )
    for url in degree_urls:
        assert any(pattern.search(url) for pattern in allow_patterns)
        assert any(pattern.search(url) for pattern in detail_patterns)

    professional_url = (
        "https://www.unsw.edu.au/study/professional-development/course/"
        "short-course"
    )
    assert not any(pattern.search(professional_url) for pattern in allow_patterns)
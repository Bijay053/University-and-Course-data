import re

from app.services.scraper.config.loader import load_uni_config


def _load_unsw():
    return load_uni_config(
        slug="unsw",
        name="UNSW Sydney",
        scrape_url="https://www.unsw.edu.au",
        university_id=32,
        db_scrape_config=None,
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
import pytest

from app.services.scraper.config.loader import load_uni_config


def _config(university_id: int):
    return load_uni_config(
        slug="canterbury",
        scrape_url="https://www.canterbury.ac.nz/",
        university_id=university_id,
        name="University of Canterbury",
        db_scrape_config={
            "auto_config": {
                "_blocked": True,
                "_strategy": "blocked",
                "discovery": {"use_stealth_browser": False},
                "extraction": {"fees": {"default_currency": "USD"}},
            }
        },
    )


@pytest.mark.parametrize("university_id", [12, 1750])
def test_any_database_id_uses_qualification_pages_not_subject_pages(
    university_id: int,
):
    config = _config(university_id)

    search = config.discovery.generic_search_api
    assert search.enabled is True
    assert "profile=qualifications-results-page" in search.url
    assert "sp-aem-qualifications" in search.url
    assert "all-subjects-results-page" not in search.url
    assert config.discovery.allow_url_patterns == [
        "/study/academic-study/qualifications/"
    ]
    assert "/study/academic-study/subjects/" in config.discovery.block_url_patterns
    assert config.discovery.bfs_page_budget == 0
    assert config.discovery.always_sitemap_supplement is False


@pytest.mark.parametrize("university_id", [12, 1750])
def test_any_database_id_routes_blocked_course_pages_through_static_proxy(
    university_id: int,
):
    config = _config(university_id)

    assert config.extraction.scrape_do_static is True
    assert config.extraction.skip_browser_rescue is True
    assert config.extraction.skip_per_course_browser is True
    assert config.extraction.max_parallel_fetch == 8
    assert config.extraction.fees.default_currency == "NZD"
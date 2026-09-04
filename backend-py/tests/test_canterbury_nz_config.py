import pytest

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.nz_programme_points import (
    find_programme_points_for_fee,
    full_time_years_from_nz_points,
)


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
    assert config.extraction.fees.fee_prevent_full_course_rollup is False


def test_special_programme_fee_uses_points_as_full_course_duration():
    text = """
    INTERNATIONAL
    2026 Special Programme Fee: $65,100 (180 points)
    2027 Special Programme Fee: $68,850 (180 points)
    2028 Special Programme Fee: $73,050 (180 points)
    STUDENT SERVICES LEVY (SSL)
    2026 SSL: $10.30 per point ($1,236.00 per 120 points)
    """

    points = find_programme_points_for_fee(text, 68_850)

    assert points == 180
    assert full_time_years_from_nz_points(points) == 1.5


def test_levy_per_point_text_does_not_relabel_programme_fee():
    text = "2027 Special Programme Fee: $68,850 (180 points); SSL $10.30 per point"

    assert find_programme_points_for_fee(text, 10.30) is None
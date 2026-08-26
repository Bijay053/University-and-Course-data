from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.discovery_cache_scope import (
    discovery_cache_coverage_sufficient,
)


def _griffith_config():
    return get_config_for_host(
        hostname="www.griffith.edu.au",
        name="Griffith University",
        scrape_url="https://www.griffith.edu.au/",
        university_id=2237,
        db_scrape_config={},
    )


def test_griffith_uses_international_2027_funnelback_catalogue():
    cfg = _griffith_config()
    api = cfg.discovery.generic_search_api

    assert api is not None
    assert api.url == (
        "https://dxp-au-search.funnelback.squiz.cloud/s/search.json"
    )
    assert api.params["f.Available to|studentType"] == "intl"
    assert api.params["smeta_intlCohortYears_orsand"] == "2027"
    assert api.root_path == "response.resultPacket.results"
    assert api.url_fields == ["liveUrl"]
    assert api.title_fields == ["title"]
    assert api.page_size == 500
    assert api.page_size_param == "num_ranks"
    assert api.offset_param == "start_rank"
    assert api.offset_start == 1


def test_griffith_rejects_the_five_link_poisoned_cache():
    cfg = _griffith_config()

    assert cfg.discovery.expected_min_courses == 281
    assert not discovery_cache_coverage_sufficient(
        course_count=5,
        expected_min_courses=cfg.discovery.expected_min_courses,
    )
    assert discovery_cache_coverage_sufficient(
        course_count=281,
        expected_min_courses=cfg.discovery.expected_min_courses,
    )


def test_griffith_only_extracts_real_degree_detail_urls():
    cfg = _griffith_config()
    patterns = cfg.discovery.course_detail_url_patterns

    import re

    assert any(
        re.search(pattern, "https://www.griffith.edu.au/study/degrees/bachelor-of-nursing-1162")
        for pattern in patterns
    )
    assert not any(
        re.search(pattern, "https://www.griffith.edu.au/study/degrees?term=")
        for pattern in patterns
    )
    assert not any(
        re.search(pattern, "https://www.griffith.edu.au/study/mba-management")
        for pattern in patterns
    )
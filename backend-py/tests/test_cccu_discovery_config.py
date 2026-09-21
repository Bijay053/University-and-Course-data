from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.discovery_cache_scope import (
    discovery_cache_coverage_sufficient,
)


def _config():
    return load_uni_config(
        slug="canterbury",
        scrape_url="https://www.canterbury.ac.uk/",
        university_id=1759,
        name="Canterbury Christ Church University",
    )


def test_cccu_uses_full_sitemap_and_rejects_undergraduate_only_cache():
    discovery = _config().discovery

    assert discovery.sitemap_url == "https://www.canterbury.ac.uk/sitemap.xml"
    assert discovery.always_sitemap_supplement is True
    assert discovery.expected_min_courses == 340
    assert not discovery_cache_coverage_sufficient(
        course_count=299,
        expected_min_courses=discovery.expected_min_courses,
    )
    assert discovery_cache_coverage_sufficient(
        course_count=360,
        expected_min_courses=discovery.expected_min_courses,
    )
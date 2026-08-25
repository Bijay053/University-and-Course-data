from app.services.scraper.discovery_cache_scope import (
    discovery_cache_coverage_sufficient,
    discovery_cache_metadata,
    discovery_cache_scope_key,
    normalize_discovery_start_url,
)


def test_normalize_discovery_start_url_is_stable():
    assert normalize_discovery_start_url(
        "HTTPS://WWW.Deakin.EDU.AU/study/find-a-course/"
    ) == "https://www.deakin.edu.au/study/find-a-course"


def test_malformed_port_never_breaks_optional_cache_fingerprinting():
    malformed = "https://www.latrobe.edu.au:not-a-port/courses"
    assert normalize_discovery_start_url(malformed) == malformed
    assert discovery_cache_scope_key(
        scrape_url=malformed,
        discovery_config={"seed_urls": []},
    )


def test_scope_changes_between_root_and_narrow_listing():
    config = {
        "seed_urls": ["https://www.deakin.edu.au/study/find-a-course/education"],
        "allow_url_patterns": ["/course/"],
    }
    root_scope = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au/",
        discovery_config=config,
    )
    narrow_scope = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au/study/find-a-course/education",
        discovery_config=config,
    )
    assert root_scope != narrow_scope


def test_scope_changes_when_discovery_config_changes():
    original = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au/",
        discovery_config={"seed_urls": ["https://www.deakin.edu.au/a"]},
    )
    expanded = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au/",
        discovery_config={
            "seed_urls": [
                "https://www.deakin.edu.au/a",
                "https://www.deakin.edu.au/b",
            ]
        },
    )
    assert original != expanded


def test_scope_is_stable_for_equivalent_dict_order():
    first = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au",
        discovery_config={"allow": ["/course/"], "budget": 20},
    )
    second = discovery_cache_scope_key(
        scrape_url="https://www.deakin.edu.au/",
        discovery_config={"budget": 20, "allow": ["/course/"]},
    )
    assert first == second


def test_metadata_marks_scope_and_normalized_start_url():
    metadata = discovery_cache_metadata(
        scrape_url="https://www.deakin.edu.au/",
        scope_key="abc123",
    )
    assert metadata == {
        "cache_meta": True,
        "scope_version": 1,
        "scope_key": "abc123",
        "scrape_url": "https://www.deakin.edu.au/",
    }


def test_cache_coverage_uses_expected_minimum_when_configured():
    assert not discovery_cache_coverage_sufficient(
        course_count=92,
        expected_min_courses=180,
    )
    assert discovery_cache_coverage_sufficient(
        course_count=180,
        expected_min_courses=180,
    )


def test_cache_coverage_keeps_five_course_floor_without_expected_minimum():
    assert not discovery_cache_coverage_sufficient(
        course_count=4,
        expected_min_courses=None,
    )
    assert discovery_cache_coverage_sufficient(
        course_count=5,
        expected_min_courses=None,
    )
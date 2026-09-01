"""Regression coverage for Torrens domestic-only and online-only staging filters."""
from __future__ import annotations

from app.services.scraper.config.context import current_uni_config
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import _is_domestic_only_page


def test_torrens_config_blocks_domestic_and_online_only_courses() -> None:
    """The real Torrens YAML must keep both international-audience filters on."""
    config = get_config_for_host(
        hostname="www.torrens.edu.au",
        name="Torrens University Australia",
        scrape_url="https://www.torrens.edu.au/",
        university_id=5,
        db_scrape_config={"admin_config": {}},
    )

    assert config.extraction.filters.domestic_only.enabled is True
    assert config.extraction.filters.online_only.enabled is True

    token = current_uni_config.set(config)
    try:
        online_ok, online_reason = should_stage_course(
            "Bachelor of Applied Business Marketing",
            {
                "course_name": "Bachelor of Applied Business Marketing",
                "international_fee": 31600,
                "study_mode": "Online",
            },
            source_url=(
                "https://www.torrens.edu.au/courses/business/"
                "bachelor-of-applied-business-marketing"
            ),
        )
        assert online_ok is False
        assert online_reason == "online_only"

        domestic_ok, domestic_reason = should_stage_course(
            "Master of Counselling",
            {
                "course_name": "Master of Counselling",
                "international_fee": 29500,
                "study_mode": "Blended",
                "domestic_only": True,
            },
            source_url=(
                "https://www.torrens.edu.au/courses/health/"
                "master-of-counselling"
            ),
        )
        assert domestic_ok is False
        assert domestic_reason == "domestic_only"
    finally:
        current_uni_config.reset(token)


def test_torrens_current_quickfacts_rejects_domestic_only_audience() -> None:
    html = """
    <section class="course-quick-facts">
      <div>Study mode</div><div>Blended</div>
      <div>Campus locations</div><div>Online</div>
      <div>Student</div><div>Domestic</div>
      <div>Course duration</div><div>2 years full time</div>
    </section>
    """

    assert _is_domestic_only_page(
        html,
        "https://www.torrens.edu.au/courses/health/master-of-counselling-advanced",
    ) is True


def test_torrens_current_quickfacts_keeps_international_audience() -> None:
    html = """
    <section class="course-quick-facts">
      <div>Study mode</div><div>Blended</div>
      <div>Campus locations</div><div>Sydney, Melbourne</div>
      <div>Student</div><div>Domestic International</div>
      <div>Course duration</div><div>3 years full time</div>
    </section>
    """

    assert _is_domestic_only_page(
        html,
        "https://www.torrens.edu.au/courses/business/bachelor-of-business",
    ) is False


def test_torrens_quickfacts_shape_is_host_scoped() -> None:
    html = """
    <div>Student</div><div>Domestic</div>
    <div>Course duration</div><div>2 years</div>
    """

    assert _is_domestic_only_page(
        html,
        "https://example.edu/courses/master-of-counselling",
    ) is False
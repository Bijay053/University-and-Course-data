from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import ExtractionConfig, UniConfig
from app.services.scraper.extractors import location
from app.services.scraper.guards import should_stage_course
from app.services.scraper.pipelines.single_course import (
    _is_deakin_online_only_page,
)


@pytest.mark.asyncio
async def test_deakin_locations_come_from_course_banner_tabs() -> None:
    html = """
    <html><head>
      <meta name="dkncourselocations" content="Burwood; Waterfront; Online">
    </head><body>
      <section id="banner">
        <div class="banner-course__locations">
          <div class="banner-course__tabs">
            <button role="tab"><span>Burwood</span></button>
            <button role="tab"><span>Waterfront</span></button>
            <button role="tab"><span>Online</span></button>
          </div>
        </div>
      </section>
      <footer><h2>Locations</h2>
        <p>Campuses, Corporate centres, International offices</p>
      </footer>
    </body></html>
    """

    result = await location.extract(
        html,
        "https://www.deakin.edu.au/course/bachelor-international-studies",
    )

    assert len(result) == 1
    assert result[0].value == "Burwood, Waterfront"
    assert result[0].method == "location.deakin_banner_tabs"


@pytest.mark.asyncio
async def test_deakin_location_fails_closed_without_physical_banner_tabs() -> None:
    html = """
    <html><head>
      <meta name="dkncourselocations" content="Burwood; Waterfront; Online">
    </head><body>
      <section id="banner">
        <div class="banner-course__locations">
          <div class="banner-course__tabs">
            <button role="tab"><span>Online</span></button>
          </div>
        </div>
      </section>
      <footer><h2>Locations</h2>
        <p>Campuses, Corporate centres, International offices</p>
      </footer>
    </body></html>
    """

    result = await location.extract(
        html,
        "https://www.deakin.edu.au/course/bachelor-international-studies",
    )

    assert result == []


def test_deakin_international_meta_is_rejection_only_for_virtual_locations() -> None:
    online_html = """
    <html><head>
      <meta name="dkncoursestudent" content="International">
      <meta name="dkncourselocations" content="Online">
    </head><body></body></html>
    """
    mixed_html = """
    <html><head>
      <meta name="dkncoursestudent" content="International">
      <meta name="dkncourselocations" content="Burwood; Online">
    </head><body></body></html>
    """
    domestic_html = """
    <html><head>
      <meta name="dkncoursestudent" content="Domestic">
      <meta name="dkncourselocations" content="Online">
    </head><body></body></html>
    """
    url = "https://www.deakin.edu.au/course/test"

    assert _is_deakin_online_only_page(online_html, url) is True
    assert _is_deakin_online_only_page(mixed_html, url) is False
    assert _is_deakin_online_only_page(domestic_html, url) is False


@pytest.mark.asyncio
async def test_initial_browser_fetch_receives_university_actions() -> None:
    actions = [
        {
            "navigate_url_suffix": "-international",
            "fallback_to_original_on_404": True,
            "required": True,
        },
        {
            "wait_for": {
                "any_text": [
                    "Go to domestic course details",
                    "This course is only available for international students.",
                    "This course is only available for domestic students.",
                    "Discontinued course",
                ]
            },
            "required": True,
        },
    ]
    set_uni_config(
        UniConfig(
            slug="deakin",
            name="Deakin University",
            base_url="https://www.deakin.edu.au",
            scrape_url="https://www.deakin.edu.au/",
            extraction=ExtractionConfig(actions=actions),
        )
    )

    browser_calls: list[dict[str, Any]] = []

    async def _http_none(url: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def _browser_html(url: str, **kwargs: Any) -> str:
        browser_calls.append(kwargs)
        return (
            "<html><body><section id='banner'>"
            "<div class='banner-course__locations'>"
            "<div class='banner-course__tabs'>"
            "<button role='tab'><span>Burwood</span></button>"
            "<button role='tab'><span>Waterfront</span></button>"
            "<button role='tab'><span>Online</span></button>"
            "</div></div></section>"
            "<h1>Bachelor of Testing</h1>"
            "<main><p>Duration 3 years full-time.</p>"
            "<p>International tuition fee AUD 38,200 per year.</p>"
            + ("Course information. " * 200)
            + "</main></body></html>"
        )

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_http_none,
        ),
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_browser_html,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.deakin.edu.au/course/bachelor-testing",
            country="Australia",
            use_ai_fallback=False,
        )

    assert browser_calls
    assert browser_calls[0]["actions"] == actions
    assert result.get("error") is None
    assert result["payload"]["international_fee"] == 38200
    assert result["payload"]["duration"] == 3.0
    assert result["payload"]["course_location"] == "Burwood, Waterfront"
    should_stage, reason = should_stage_course(
        result["payload"]["course_name"],
        result["payload"],
        "https://www.deakin.edu.au/course/bachelor-testing",
    )
    assert should_stage is True
    assert reason == "accepted"


@pytest.mark.asyncio
async def test_deakin_online_only_banner_is_explicitly_ineligible() -> None:
    set_uni_config(
        UniConfig(
            slug="deakin",
            name="Deakin University",
            base_url="https://www.deakin.edu.au",
            scrape_url="https://www.deakin.edu.au/",
        )
    )

    async def _http_none(url: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def _browser_html(url: str, **kwargs: Any) -> str:
        return (
            "<html><body><section id='banner'>"
            "<div class='banner-course__locations'>"
            "<div class='banner-course__tabs'>"
            "<button role='tab'><span>Online</span></button>"
            "</div></div></section>"
            "<h1>Graduate Certificate of Advanced Nursing</h1>"
            "<main><p>Duration 0.5 years full-time.</p>"
            "<p>International tuition fee AUD 14,700 per year.</p>"
            + ("Course information. " * 100)
            + "</main></body></html>"
        )

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_http_none,
        ),
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_browser_html,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.deakin.edu.au/course/graduate-certificate-advanced-nursing",
            country="Australia",
            use_ai_fallback=False,
        )

    assert result.get("error") is None
    assert result["payload"]["online_only"] is True
    assert result["payload"]["online_only_deakin"] is True
    should_stage, reason = should_stage_course(
        "Graduate Certificate of Advanced Nursing",
        result["payload"],
        "https://www.deakin.edu.au/course/graduate-certificate-advanced-nursing",
    )
    assert should_stage is False
    assert reason == "online_only"


@pytest.mark.asyncio
async def test_deakin_domestic_only_page_exits_cleanly_without_emitter() -> None:
    actions = [
        {
            "navigate_url_suffix": "-international",
            "fallback_to_original_on_404": True,
            "required": True,
        },
        {
            "wait_for": {
                "any_text": [
                    "Go to domestic course details",
                    "This course is only available for international students.",
                    "This course is only available for domestic students.",
                ]
            },
            "required": True,
        },
    ]
    set_uni_config(
        UniConfig(
            slug="deakin",
            name="Deakin University",
            base_url="https://www.deakin.edu.au",
            scrape_url="https://www.deakin.edu.au/",
            extraction=ExtractionConfig(actions=actions),
        )
    )

    async def _http_none(url: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def _browser_html(url: str, **kwargs: Any) -> str:
        return (
            "<html><body><h1>Associate Degree of Arts</h1>"
            "<main><p>This course is only available for domestic students.</p>"
            + ("Course information. " * 100)
            + "</main></body></html>"
        )

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_http_none,
        ),
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_browser_html,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.deakin.edu.au/course/associate-degree-arts",
            country="Australia",
            use_ai_fallback=False,
        )

    assert result.get("error") is None
    assert result["payload"]["domestic_only"] is True


@pytest.mark.asyncio
async def test_deakin_discontinued_international_page_is_ineligible() -> None:
    actions = [
        {
            "navigate_url_suffix": "-international",
            "fallback_to_original_on_404": True,
            "required": True,
        },
        {
            "wait_for": {
                "any_text": [
                    "Go to domestic course details",
                    "This course is only available for international students.",
                    "This course is only available for domestic students.",
                    "Discontinued course",
                ]
            },
            "required": True,
        },
    ]
    set_uni_config(
        UniConfig(
            slug="deakin",
            name="Deakin University",
            base_url="https://www.deakin.edu.au",
            scrape_url="https://www.deakin.edu.au/",
            extraction=ExtractionConfig(actions=actions),
        )
    )

    async def _http_none(url: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def _browser_html(url: str, **kwargs: Any) -> str:
        return (
            "<html><head><title>Discontinued course</title></head><body>"
            "<h1>Discontinued course</h1>"
            + ("Sorry, this course is no longer available. " * 100)
            + "</body></html>"
        )

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_http_none,
        ),
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_browser_html,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.deakin.edu.au/course/graduate-diploma-writing-and-literature",
            country="Australia",
            use_ai_fallback=False,
        )

    assert result.get("error") is None
    assert result["payload"]["domestic_only"] is True
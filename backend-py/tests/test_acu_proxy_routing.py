from __future__ import annotations

import asyncio
import html
import json
from unittest.mock import patch

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.config.schema import GenericSearchApiConfig
from app.services.scraper.generic_search_api import fetch_yaml_api_links
from app.services.scraper.http_fetcher import fetch_html, scrape_do_render_scope


def _load_acu_config():
    return get_config_for_host(
        hostname="www.acu.edu.au",
        name="Australian Catholic University (ACU)",
        scrape_url="https://www.acu.edu.au/courses",
        university_id=1756,
        db_scrape_config=None,
    )


def test_acu_config_routes_discovery_and_extraction_through_rendered_proxy():
    cfg = _load_acu_config()
    api = cfg.discovery.generic_search_api

    assert api is not None
    assert api.fetch_via_scrape_do is True
    assert api.scrape_do_render is True
    assert api.require_all_urls is True
    assert len([api.url, *api.additional_urls]) == 4

    extraction = cfg.extraction
    assert extraction.scrape_do_render is True
    assert extraction.scrape_do_skip_fallbacks is True
    assert extraction.scrape_do_static is False
    assert extraction.scrape_do_wait_for_ms == 1500
    assert extraction.skip_render_hydration_retry is True
    assert extraction.scrape_do_local_concurrency == 8
    assert extraction.max_parallel_fetch == 8
    assert extraction.skip_browser_rescue is True
    assert extraction.skip_per_course_browser is True
    assert extraction.skip_ai_when_text_empty is True
    assert extraction.english.skip_vision_when_core_found is True


def test_rendered_api_discovery_uses_shared_proxy_and_unwraps_chrome_json(
    monkeypatch,
):
    api = GenericSearchApiConfig(
        url=(
            "https://www.acu.edu.au/webapi/GetCourseResult/get"
            "?CourseType=Undergraduate&sr=%7Bcatalogue%7D"
        ),
        additional_urls=[
            (
                "https://www.acu.edu.au/webapi/GetCourseResult/get"
                "?CourseType=Research&sr=%7Bcatalogue%7D"
            )
        ],
        fetch_via_scrape_do=True,
        scrape_do_render=True,
        root_path="CoursesResults",
        url_fields=["URL"],
        title_fields=["CourseName"],
        normalize_relative_urls=True,
        base_url="https://www.acu.edu.au",
        max_pages=1,
    )
    calls: list[tuple[str, dict]] = []

    async def fake_scrape_do(url: str, **kwargs):
        calls.append((url, kwargs))
        course_type = "Research" if "CourseType=Research" in url else "Undergraduate"
        payload = {
            "CoursesResults": [
                {
                    "CourseName": f"{course_type} test course",
                    "URL": f"/course/{course_type.lower()}-test-course",
                }
            ]
        }
        return (
            "<html><body><pre>"
            + html.escape(json.dumps(payload))
            + "</pre></body></html>"
        )

    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.scraper.http_fetcher.fetch_html_scrape_do",
        fake_scrape_do,
    )

    links = asyncio.run(fetch_yaml_api_links(api))

    assert [link["url"] for link in links] == [
        "https://www.acu.edu.au/course/undergraduate-test-course",
        "https://www.acu.edu.au/course/research-test-course",
    ]
    assert len(calls) == 2
    assert "CourseType=Undergraduate" in calls[0][0]
    assert "sr=%7Bcatalogue%7D" in calls[0][0]
    for _url, kwargs in calls:
        assert kwargs == {
            "render": True,
            "wait_for_ms": 3000,
            "rate_limit": False,
            "max_retries": 3,
            "unescape_json_html": False,
        }


def test_required_api_slices_discard_partial_catalogue(monkeypatch):
    api = GenericSearchApiConfig(
        url="https://www.acu.edu.au/webapi/catalogue?CourseType=Undergraduate",
        additional_urls=[
            "https://www.acu.edu.au/webapi/catalogue?CourseType=Research"
        ],
        fetch_via_scrape_do=True,
        scrape_do_render=True,
        require_all_urls=True,
        root_path="CoursesResults",
        url_fields=["URL"],
        title_fields=["CourseName"],
        normalize_relative_urls=True,
        base_url="https://www.acu.edu.au",
        max_pages=1,
    )

    async def fake_scrape_do(url: str, **_kwargs):
        if "CourseType=Research" in url:
            return None
        return json.dumps(
            {
                "CoursesResults": [
                    {
                        "CourseName": "Bachelor test course",
                        "URL": "/course/bachelor-test-course",
                    }
                ]
            }
        )

    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.scraper.http_fetcher.fetch_html_scrape_do",
        fake_scrape_do,
    )

    assert asyncio.run(fetch_yaml_api_links(api)) == []


def test_acu_extraction_uses_render_before_any_direct_fetch(monkeypatch):
    cfg = _load_acu_config()
    set_uni_config(cfg)
    calls: list[tuple[str, bool]] = []
    rendered_html = (
        "<html><body><h1>Bachelor of Social Work</h1>"
        "<p>International annual fee AUD 33,360. IELTS overall 7.0.</p>"
        "</body></html>"
    ) * 8

    async def fake_scrape_do(url: str, *, render: bool = False, **_kwargs):
        calls.append((url, render))
        return rendered_html

    async def forbidden_direct_get(*_args, **_kwargs):
        raise AssertionError("ACU render-first extraction must not call direct HTTP")

    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")
    monkeypatch.setattr(
        "app.services.scraper.http_fetcher.fetch_html_scrape_do",
        fake_scrape_do,
    )

    with patch("httpx.AsyncClient.get", new=forbidden_direct_get):
        with scrape_do_render_scope():
            actual = asyncio.run(
                fetch_html(
                    "https://www.acu.edu.au/course/bachelor-of-social-work"
                    "?type=International"
                )
            )

    assert actual == rendered_html
    assert calls == [
        (
            "https://www.acu.edu.au/course/bachelor-of-social-work"
            "?type=International",
            True,
        )
    ]
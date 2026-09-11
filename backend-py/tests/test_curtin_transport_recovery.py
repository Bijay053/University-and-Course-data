from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    BodyPaginationConfig,
    ExtractionConfig,
    GenericSearchApiConfig,
    UniConfig,
)
from app.services.scraper.generic_search_api import fetch_yaml_api_links


def _curtin_config() -> UniConfig:
    return UniConfig(
        slug="curtin",
        name="Curtin University",
        base_url="https://www.curtin.edu.au",
        scrape_url="https://www.curtin.edu.au/study/courses/",
        extraction=ExtractionConfig(
            skip_browser_rescue=True,
            skip_per_course_browser=True,
            scrape_do_static_on_failure=True,
            scrape_do_geo="AU",
        ),
    )


@pytest.mark.asyncio
async def test_trpc_query_json_body_is_paginated_inside_get_parameter() -> None:
    cfg = GenericSearchApiConfig(
        method="GET",
        url="https://search.curtin.edu.au/api/trpc/search",
        params={"batch": "1"},
        fetch_via_scrape_do=True,
        body={
            "0": {
                "region": "international",
                "searchTerms": "",
                "facet": "courses",
                "page": 1,
                "availability": {"year": 2027},
            }
        },
        query_json_param="input",
        page_size=2,
        body_pagination=BodyPaginationConfig(current_path="0.page"),
        max_pages=3,
        root_path="0.result.data.results",
        url_fields=["courseDetails.url"],
        title_fields=["courseDetails.title"],
    )
    requested_pages: list[int] = []

    async def _fake_scrape_do(url: str, **_: Any) -> str:
        query = parse_qs(urlsplit(url).query)
        assert query["batch"] == ["1"]
        payload = json.loads(query["input"][0])
        page = payload["0"]["page"]
        requested_pages.append(page)
        rows = (
            [
                {
                    "courseDetails": {
                        "url": f"https://www.curtin.edu.au/study/offering/course-{n}",
                        "title": f"Course {n}",
                    }
                }
                for n in (1, 2)
            ]
            if page == 1
            else [
                {
                    "courseDetails": {
                        "url": "https://www.curtin.edu.au/study/offering/course-3",
                        "title": "Course 3",
                    }
                }
            ]
        )
        return json.dumps([{"result": {"data": {"results": rows}}}])

    with patch(
        "app.services.scraper.http_fetcher.fetch_html_scrape_do",
        side_effect=_fake_scrape_do,
    ):
        links = await fetch_yaml_api_links(cfg)

    assert requested_pages == [1, 2]
    assert [link["name"] for link in links] == ["Course 1", "Course 2", "Course 3"]


@pytest.mark.asyncio
async def test_curtin_direct_failure_uses_static_proxy_before_browser_skip() -> None:
    set_uni_config(_curtin_config())
    html = """
      <html><head><meta name="description" content="An international degree."></head>
      <body>
        <h1>Master of Architecture</h1>
        <div class="information">
          <div class="information__title"><h3>Duration</h3></div>
          <div class="information__content"><p>2 years full-time</p></div>
        </div>
        <div class="information">
          <div class="information__title"><h3>Location</h3></div>
          <div class="information__content"><p>Curtin Perth</p></div>
        </div>
        <div class="information">
          <div class="information__title"><h3>Attendance mode</h3></div>
          <div class="information__content"><p>On campus</p></div>
        </div>
        <p>IELTS overall 6.5, with no band below 6.0.</p>
        <p>Start date: February 2027.</p>
        <script type="application/ld+json">
        {"@type":"Course","offers":[
          {"@type":"Offer","name":"2027 - International - Indicative year 1 fee (2027)",
           "price":42000,"priceCurrency":"AUD"}
        ]}
        </script>
      </body></html>
    """
    proxy_calls: list[dict[str, Any]] = []
    browser_calls: list[str] = []

    async def _direct_none(*_: Any, **__: Any) -> None:
        return None

    async def _proxy_html(url: str, **kwargs: Any) -> str:
        proxy_calls.append({"url": url, **kwargs})
        return html

    async def _browser_fetch(url: str, **_: Any) -> None:
        browser_calls.append(url)
        return None

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_direct_none,
        ),
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html_scrape_do",
            side_effect=_proxy_html,
        ),
        patch(
            "app.services.scraper.browser_pool.pool",
            fetch_html=_browser_fetch,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.curtin.edu.au/study/offering/course-pg-master-of-architecture--mc-arch/"
        )

    assert len(proxy_calls) == 1
    assert proxy_calls[0]["render"] is False
    assert proxy_calls[0]["geo_code"] == "AU"
    assert browser_calls == []
    assert result.get("error") != "fetch_failed"
    assert result["payload"]["international_fee"] == 42000.0


@pytest.mark.asyncio
async def test_curtin_direct_exception_still_uses_static_proxy() -> None:
    set_uni_config(_curtin_config())
    html = """
      <h1>Master of Architecture</h1>
      <p>Duration: 2 years full-time</p>
      <p>Location: Curtin Perth</p>
      <p>Attendance mode: On campus</p>
      <p>IELTS overall 6.5. Start date: February 2027.</p>
      <script type="application/ld+json">
      {"@type":"Course","offers":[
        {"@type":"Offer","name":"2027 - International - Indicative year 1 fee (2027)",
         "price":42000,"priceCurrency":"AUD"}
      ]}
      </script>
    """
    proxy_kwargs: list[dict[str, Any]] = []

    async def _direct_raises(*_: Any, **__: Any) -> None:
        raise TimeoutError("origin timed out")

    async def _proxy_html(_: str, **kwargs: Any) -> str:
        proxy_kwargs.append(kwargs)
        return html

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_direct_raises,
        ),
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html_scrape_do",
            side_effect=_proxy_html,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.curtin.edu.au/study/offering/course-pg-master-of-architecture--mc-arch/"
        )

    assert len(proxy_kwargs) == 1
    assert proxy_kwargs[0]["request_timeout_seconds"] == 30.0
    assert result.get("error") != "fetch_failed"
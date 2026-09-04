from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.scraper.config.loader import load_uni_config


def test_segi_production_config_uses_current_official_catalogue() -> None:
    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
        db_scrape_config={
            "auto_config": {
                "discovery": {
                    "use_wayback": True,
                    "allow_url_patterns": [r"^/programs/dis_.*\\.htm$"],
                }
            }
        },
    )

    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.skip_home_page_redirect is True
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.skip_browser_discovery is True
    assert config.discovery.use_wayback is False
    assert config.discovery.archive_only is False
    assert config.discovery.seed_urls == [
        "https://university.segi.edu.my/site-map/"
    ]
    assert config.discovery.allowed_extra_hostnames == [
        "university.segi.edu.my"
    ]
    assert config.discovery.scrape_do_skip_fallbacks is True
    assert config.discovery.scrape_do_render is True
    assert config.discovery.allow_url_patterns == [
        r"^https?://university\.segi\.edu\.my/course/[^/?#]+/?$"
    ]
    assert re.search(
        config.discovery.allow_url_patterns[0],
        "https://university.segi.edu.my/course/bachelor-of-psychology-honours/",
    )
    assert not re.search(
        config.discovery.allow_url_patterns[0],
        "https://university.segi.edu.my/course-search/",
    )
    assert not re.search(
        config.discovery.allow_url_patterns[0],
        "https://www.segi.edu.my/course/master-of-accountancy/",
    )
    assert config.extraction.scrape_do_render is True
    assert config.extraction.scrape_do_skip_fallbacks is True
    assert config.extraction.scrape_do_render_only is True
    assert config.extraction.staging.require_international_fee is False
    assert config.extraction.staging.stage_on_parser_error is True
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"


@pytest.mark.asyncio
async def test_segi_course_fetch_routes_directly_to_rendered_proxy() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(return_value="<html>" + ("course " * 200) + "</html>"),
        ) as scrape_do,
        patch.object(
            http_fetcher,
            "fetch_html_cffi",
            new=AsyncMock(side_effect=AssertionError("curl_cffi must not run")),
        ) as cffi,
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(side_effect=AssertionError("direct HTTP must not run")),
        ) as direct,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/bachelor-of-psychology-honours/"
        )

    assert html
    scrape_do.assert_awaited_once()
    assert scrape_do.call_args.kwargs["render"] is True
    cffi.assert_not_awaited()
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_failed_render_never_uses_static_or_wayback() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        http_fetcher.scrape_do_render_scope(),
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(return_value=None),
        ) as scrape_do,
        patch.object(
            http_fetcher,
            "fetch_html_wayback",
            new=AsyncMock(side_effect=AssertionError("stale archive must not run")),
        ) as wayback,
        patch.object(http_fetcher.asyncio, "sleep", new=AsyncMock()),
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html is None
    assert scrape_do.await_count == 4
    assert all(call.kwargs["render"] is True for call in scrape_do.await_args_list)
    wayback.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_discovery_fetch_is_render_only() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(return_value=None),
        ) as scrape_do,
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(side_effect=AssertionError("direct HTTP must not run")),
        ) as direct,
        patch.object(
            http_fetcher,
            "fetch_html_cffi",
            new=AsyncMock(side_effect=AssertionError("curl_cffi must not run")),
        ) as cffi,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/site-map/"
        )

    assert html == ""
    scrape_do.assert_awaited_once()
    assert scrape_do.call_args.kwargs["render"] is True
    direct.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_wayback_discovery_uses_configured_cdx_prefix() -> None:
    from app.services.scraper.wayback_discover import wayback_discover

    captured_params: dict[str, str] = {}
    rows = [
        ["original", "timestamp"],
        ["https://www.segi.edu.my/course/master-of-accountancy/", "20250101000000"],
    ]

    class CdxResponse:
        status_code = 200
        text = json.dumps(rows)
        request = httpx.Request("GET", "http://web.archive.org/cdx/search/cdx")

        @staticmethod
        def raise_for_status() -> None:
            return None

    async def fake_cdx_get(self, endpoint_url, **kwargs):
        captured_params.update(kwargs["params"])
        return CdxResponse()

    with patch(
        "app.services.scraper.wayback_discover.httpx.AsyncClient.get",
        new=fake_cdx_get,
    ):
        discovered = await wayback_discover(
            "https://www.segi.edu.my/",
            max_courses=10,
            cdx_url_prefix="www.segi.edu.my/course/*",
        )

    assert captured_params["url"] == "www.segi.edu.my/course/*"
    assert discovered == [
        {
            "url": "https://www.segi.edu.my/course/master-of-accountancy/",
            "name": "",
        }
    ]


@pytest.mark.asyncio
async def test_wayback_discovery_rejects_cross_host_prefix() -> None:
    from app.services.scraper.wayback_discover import wayback_discover

    with patch(
        "app.services.scraper.wayback_discover.httpx.AsyncClient.get",
        side_effect=AssertionError("cross-host prefix must fail before network access"),
    ):
        discovered = await wayback_discover(
            "https://www.segi.edu.my/",
            cdx_url_prefix="other-university.example/course/*",
        )

    assert discovered == []
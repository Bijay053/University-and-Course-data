from __future__ import annotations

import json
import re
from types import SimpleNamespace
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
    assert config.discovery.scrape_do_skip_fallbacks is False
    assert config.discovery.scrape_do_render is False
    assert config.discovery.insecure_tls_direct_hostnames == [
        "university.segi.edu.my"
    ]
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
    assert config.extraction.scrape_do_render is False
    assert config.extraction.scrape_do_skip_fallbacks is False
    assert config.extraction.staging.require_international_fee is False
    assert config.extraction.staging.stage_on_parser_error is True
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"


@pytest.mark.asyncio
async def test_segi_course_fetch_uses_exact_host_tls_exception() -> None:
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
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    text="<html>" + ("course " * 200) + "</html>",
                )
            ),
        ) as direct,
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(side_effect=AssertionError("proxy must not run")),
        ) as scrape_do,
        patch.object(
            http_fetcher,
            "fetch_html_cffi",
            new=AsyncMock(side_effect=AssertionError("curl_cffi must not run")),
        ) as cffi,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/bachelor-of-psychology-honours/"
        )

    assert html
    direct.assert_awaited_once()
    scrape_do.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_failure_never_uses_proxy_or_wayback() -> None:
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
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                side_effect=httpx.ConnectError(
                    "official host temporarily unavailable"
                )
            ),
        ) as direct,
        patch.object(http_fetcher, "fetch_html_scrape_do", new=AsyncMock()) as scrape_do,
        patch.object(http_fetcher, "fetch_html_wayback", new=AsyncMock()) as wayback,
        patch.object(http_fetcher, "fetch_html_cffi", new=AsyncMock()) as cffi,
        patch.object(http_fetcher.asyncio, "sleep", new=AsyncMock()),
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html is None
    assert direct.await_count == 3
    scrape_do.assert_not_awaited()
    wayback.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_rejects_cross_host_redirect() -> None:
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
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=302,
                    headers={"location": "https://attacker.example/internal"},
                    text="",
                )
            ),
        ) as direct,
        patch.object(http_fetcher, "fetch_html_scrape_do", new=AsyncMock()) as scrape_do,
        patch.object(http_fetcher, "fetch_html_wayback", new=AsyncMock()) as wayback,
        patch.object(http_fetcher, "fetch_html_cffi", new=AsyncMock()) as cffi,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html is None
    direct.assert_awaited_once()
    assert http_fetcher.get_last_fetch_failure() == {
        "kind": "unsafe_redirect",
        "reason": (
            "Exact-host TLS exception rejected redirect from "
            "'https://university.segi.edu.my/course/current-course/' "
            "to 'https://attacker.example/internal'."
        ),
        "retryable": False,
        "transport": "direct_insecure_tls",
        "terminal": True,
        "status_code": 302,
    }
    scrape_do.assert_not_awaited()
    wayback.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_allows_same_host_https_redirect() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    final_html = "<html>" + ("course " * 200) + "</html>"

    with patch.object(
        http_fetcher.httpx.AsyncClient,
        "get",
        new=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status_code=301,
                    headers={"location": "/course/canonical/"},
                    text="",
                ),
                SimpleNamespace(status_code=200, headers={}, text=final_html),
            ]
        ),
    ) as direct:
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html == final_html
    assert [call.args[0] for call in direct.await_args_list] == [
        "https://university.segi.edu.my/course/current-course/",
        "https://university.segi.edu.my/course/canonical/",
    ]


@pytest.mark.asyncio
async def test_segi_tls_exception_does_not_apply_to_other_hosts() -> None:
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
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                side_effect=AssertionError(
                    "one-off insecure client must not run for another host"
                )
            ),
        ) as direct,
        patch.object(
            http_fetcher,
            "_get_shared_client",
            side_effect=RuntimeError("normal verified path reached"),
        ) as shared_client,
    ):
        html = await http_fetcher.fetch_html("https://other.example/course/x/")

    assert html is None
    assert shared_client.call_count == 3
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_online_mode_title_overrides_campus_derived_mode() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    html = """
    <html>
      <head><title>Bachelor of Psychology (Honours) – ODL (Online Mode)</title></head>
      <body>
        <main>
          <h1>Bachelor of Psychology (Honours) – ODL (Online Mode)</h1>
          <section><h2>Campus</h2><p>SEGi University</p></section>
          <section><h2>Entry requirements</h2><p>IELTS 5.0 or PTE 36</p></section>
        </main>
      </body>
    </html>
    """

    result = await extract_course(
        "https://university.segi.edu.my/course/bachelor-of-psychology-honours-odl/",
        country="Malaysia",
        html=html,
        use_ai_fallback=False,
    )

    assert result.get("error") is None
    assert result["payload"]["study_mode"] == "Online"
    assert any(
        evidence.get("field_key") == "study_mode"
        and evidence.get("method") == "study_mode:title_keyword"
        and evidence.get("value") == "Online"
        for evidence in result["evidence"]
    )


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
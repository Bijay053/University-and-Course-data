from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.scraper.config.loader import load_uni_config


def test_segi_production_config_is_archive_first_and_path_scoped() -> None:
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

    assert config.discovery.bfs_page_budget == 0
    assert config.discovery.skip_home_page_redirect is True
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.skip_browser_discovery is True
    assert config.discovery.use_wayback is True
    assert config.discovery.archive_only is True
    assert config.discovery.wayback_cdx_prefix == "www.segi.edu.my/course/*"
    assert config.discovery.allow_url_patterns == [
        r"^/course/(?!search(?:/|$))[^/?#]+/?$"
    ]
    assert config.extraction.force_wayback_first is True
    assert config.extraction.wayback_miss_fallback == "none"
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"


@pytest.mark.asyncio
async def test_segi_archive_only_discovery_makes_no_live_request(monkeypatch) -> None:
    from app.services.scraper import discovery

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )

    async def fail_live_fetch(*args, **kwargs):
        raise AssertionError("archive-only discovery must not fetch the live host")

    monkeypatch.setattr(discovery, "fetch_html", fail_live_fetch)
    links = await discovery.discover_course_links(
        config.scrape_url,
        max_pages=config.discovery.bfs_page_budget,
        max_courses=500,
        discovery_config=config.discovery,
    )

    assert links == []


@pytest.mark.asyncio
async def test_orchestrator_archive_only_path_clears_db_providers_and_calls_cdx() -> None:
    from app.services.scraper import orchestrator

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
        db_scrape_config={
            "auto_config": {
                "_api_provider": "generic_json",
                "_api_endpoint_hint": "https://www.segi.edu.my/api/courses",
                "discovery": {
                    "generic_search_api": {
                        "enabled": True,
                        "url": "https://www.segi.edu.my/api/courses",
                        "root_path": "results",
                        "url_fields": ["url"],
                    }
                },
            }
        },
    )
    archive_config = orchestrator._without_live_discovery_providers(config)
    assert archive_config.discovery.generic_search_api is None
    assert archive_config.discovery.auto_api_discovery is False

    expected = [
        {"url": "https://www.segi.edu.my/course/master-of-accountancy/", "name": ""}
    ]
    with patch(
        "app.services.scraper.wayback_discover.wayback_discover",
        new=AsyncMock(return_value=expected),
    ) as cdx:
        links = await orchestrator._discover_archive_only(
            scrape_url=archive_config.scrape_url,
            discovery_config=archive_config.discovery,
            max_courses=500,
            emit=AsyncMock(),
        )

    assert links == expected
    cdx.assert_awaited_once_with(
        "https://www.segi.edu.my/",
        max_courses=500,
        emit=cdx.call_args.kwargs["emit"],
        cdx_url_prefix="www.segi.edu.my/course/*",
    )


@pytest.mark.asyncio
async def test_run_scrape_calls_scoped_cdx_before_stale_db_discovery_providers() -> None:
    from app.services.scraper import orchestrator

    class _Result:
        def __init__(self, *, first=None, scalar=None):
            self._first = first
            self._scalar = scalar

        def first(self):
            return self._first

        def scalar_one_or_none(self):
            return self._scalar

    class _SessionContext:
        def __init__(self):
            self.session = SimpleNamespace(
                execute=AsyncMock(),
                commit=AsyncMock(),
            )

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    job = SimpleNamespace(
        university_id=13,
        url="https://www.segi.edu.my/",
        request_payload={},
        fast_mode=False,
        status="running",
        total_found=0,
        imported=0,
        skipped=0,
        errors=0,
        completed_at=None,
        error_message=None,
    )
    university = SimpleNamespace(
        id=13,
        name="SEGi University & Colleges",
        country="Malaysia",
        scrape_url="https://www.segi.edu.my/",
        scrape_config={
            "auto_config": {
                "_api_provider": "generic_json",
                "_api_endpoint_hint": "https://www.segi.edu.my/api/courses",
                "discovery": {
                    "generic_search_api": {
                        "enabled": True,
                        "url": "https://www.segi.edu.my/api/courses",
                        "root_path": "results",
                        "url_fields": ["url"],
                    }
                },
            },
            "recipe": {
                "discovery_strategy": "json_api",
                "api": {"endpoint": "https://www.segi.edu.my/api/recipe-courses"},
                "fallback_strategy": "none",
            },
        },
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(first=("job-segi-archive-only",)),
                _Result(scalar=university),
            ]
        ),
        get=AsyncMock(return_value=job),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    call_order: list[tuple[str, str | None]] = []

    async def _cdx_only(**kwargs):
        call_order.append(
            ("cdx", kwargs["discovery_config"].wayback_cdx_prefix)
        )
        raise RuntimeError("stop after archive-only sequencing assertion")

    async def _forbidden(*args, **kwargs):
        call_order.append(("forbidden_live_discovery", None))
        raise AssertionError("live/API discovery ran before archive-only CDX")

    async def _idle_task(*args, **kwargs):
        await asyncio.sleep(3600)

    with (
        patch.object(orchestrator, "_clear_stale_dedup", new=AsyncMock(return_value=0)),
        patch.object(orchestrator, "_discover_archive_only", side_effect=_cdx_only) as cdx,
        patch.object(orchestrator, "discover_course_links", side_effect=_forbidden) as bfs,
        patch(
            "app.services.scraper.json_api_discovery.fetch_json_api_links",
            side_effect=_forbidden,
        ) as recipe_api,
        patch(
            "app.services.scraper.generic_search_api.fetch_generic_api_links",
            side_effect=_forbidden,
        ) as auto_api,
        patch(
            "app.services.scraper.generic_search_api.fetch_yaml_api_links",
            side_effect=_forbidden,
        ) as yaml_api,
        patch.object(orchestrator, "_stop_poller", side_effect=_idle_task),
        patch.object(orchestrator, "_heartbeat_pulser", side_effect=_idle_task),
        patch.object(orchestrator, "AsyncSessionLocal", side_effect=_SessionContext),
        patch.object(orchestrator.settings, "max_concurrent_scrapes", 0),
        patch("redis.asyncio.from_url", side_effect=RuntimeError("redis disabled")),
    ):
        result = await orchestrator.run_scrape(db, "job-segi-archive-only")

    assert result["ok"] is False
    assert call_order == [("cdx", "www.segi.edu.my/course/*")]
    cdx.assert_awaited_once()
    assert cdx.call_args.kwargs["scrape_url"] == "https://www.segi.edu.my/"
    assert cdx.call_args.kwargs["discovery_config"].archive_only is True
    bfs.assert_not_awaited()
    recipe_api.assert_not_awaited()
    auto_api.assert_not_awaited()
    yaml_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_archive_miss_never_falls_through_to_live_transport() -> None:
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
            "fetch_html_wayback",
            new=AsyncMock(return_value=None),
        ) as wayback,
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(side_effect=AssertionError("Scrape.do must not run")),
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
            "https://www.segi.edu.my/course/current-only-course/"
        )

    assert html is None
    wayback.assert_awaited_once()
    scrape_do.assert_not_awaited()
    cffi.assert_not_awaited()
    direct.assert_not_awaited()


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
"""Regression coverage for blocked/archive-only scrape recovery."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.scraper.http_fetcher import (
    clear_wayback_timestamps,
    fetch_html,
    fetch_html_scrape_do,
    fetch_html_wayback,
    get_last_fetch_failure,
    scrape_do_render_scope,
    set_wayback_timestamps,
)
from app.services.scraper.orchestrator import (
    _extraction_failure_details,
    _recovery_accounting,
    _recovery_time_remaining,
    _select_recovery_work,
)
from app.services.scraper.url_identity import canonical_course_url_key


def test_course_url_identity_collapses_transport_and_noise_variants():
    variants = {
        canonical_course_url_key(
            "http://www.example.edu:80/course/master/?utm_source=x&studentType=international"
        ),
        canonical_course_url_key(
            "https://example.edu/course/master/?student_type=domestic"
        ),
        canonical_course_url_key("https://www.example.edu/course/master"),
    }
    assert variants == {"example.edu/course/master"}


def test_course_url_identity_preserves_semantic_query_and_sorts_pairs():
    first = canonical_course_url_key(
        "https://www.example.edu/course/x?year=2027&international=true"
    )
    second = canonical_course_url_key(
        "http://example.edu/course/x/?international=true&year=2027"
    )
    domestic = canonical_course_url_key("https://example.edu/course/x?year=2027")
    assert first == second
    assert first != domestic


@pytest.mark.asyncio
async def test_wayback_discovery_preserves_semantic_views_and_cache_timestamps():
    from app.services.scraper.config import get_config_for_host, set_uni_config
    from app.services.scraper.wayback_discover import wayback_discover

    clear_wayback_timestamps()
    cfg = get_config_for_host(
        hostname="www.notredame.edu.au",
        name="University of Notre Dame Australia",
        scrape_url="https://www.notredame.edu.au/",
        university_id=1165,
    )
    set_uni_config(cfg)
    base = (
        "https://www.notredame.edu.au/programs/school-of-business/"
        "postgraduate/master-of-business-administration"
    )
    international = f"{base}?international=true"
    rows = [
        ["original", "timestamp"],
        [
            base.replace(
                "https://www.notredame.edu.au",
                "http://www.notredame.edu.au:80",
            ),
            "20260101000000",
        ],
        [f"{base}?utm_source=archive", "20260202000000"],
        [international, "20260303000000"],
    ]

    class CdxResponse:
        status_code = 200
        text = json.dumps(rows)
        request = httpx.Request("GET", "http://web.archive.org/cdx/search/cdx")

        @staticmethod
        def raise_for_status():
            return None

    async def fake_cdx_get(self, endpoint_url, **kwargs):
        return CdxResponse()

    with patch(
        "app.services.scraper.wayback_discover.httpx.AsyncClient.get",
        new=fake_cdx_get,
    ):
        discovered = await wayback_discover(
            "https://www.notredame.edu.au/",
            max_courses=10,
        )

    discovered_urls = {item["url"] for item in discovered}
    assert base in discovered_urls
    assert international in discovered_urls
    assert len(discovered_urls) == 2

    snapshot_calls: list[str] = []

    class SnapshotResponse:
        status_code = 200
        text = "<html><body>" + ("course " * 200) + "</body></html>"

    async def fake_snapshot_get(self, endpoint_url, **kwargs):
        snapshot_calls.append(endpoint_url)
        return SnapshotResponse()

    with patch("httpx.AsyncClient.get", new=fake_snapshot_get):
        assert await fetch_html_wayback(base)
        assert await fetch_html_wayback(international)

    assert any("20260202000000id_" in url for url in snapshot_calls)
    assert any(
        "20260303000000id_" in url and "international=true" in url
        for url in snapshot_calls
    )

    async def must_not_fetch(self, endpoint_url, **kwargs):
        raise AssertionError("complete CDX scope must answer a known miss locally")

    with patch("httpx.AsyncClient.get", new=must_not_fetch):
        missing = await fetch_html_wayback(
            "https://www.notredame.edu.au/programs/school-of-business/"
            "postgraduate/not-in-the-cdx-result"
        )
    assert missing is None
    failure = get_last_fetch_failure()
    assert failure is not None
    assert failure["kind"] == "wayback_no_snapshot"
    assert failure["retryable"] is False
    assert failure["transport"] == "wayback_cdx_cache"


@pytest.mark.asyncio
async def test_wayback_cache_replays_exact_http_cdx_original():
    clear_wayback_timestamps()
    original = "http://www.example.edu:80/programs/business/master-of-x"
    normalized = "https://www.example.edu/programs/business/master-of-x"
    set_wayback_timestamps(
        {
            canonical_course_url_key(original): (
                "20260404000000",
                original,
            )
        }
    )
    calls: list[str] = []

    class SnapshotResponse:
        status_code = 200
        text = "<html><body>" + ("course " * 200) + "</body></html>"

    async def fake_get(self, endpoint_url, **kwargs):
        calls.append(endpoint_url)
        return SnapshotResponse()

    with patch("httpx.AsyncClient.get", new=fake_get):
        assert await fetch_html_wayback(normalized)

    assert calls == [
        "https://web.archive.org/web/20260404000000id_/"
        "http://www.example.edu:80/programs/business/master-of-x"
    ]


@pytest.mark.asyncio
async def test_cached_wayback_snapshot_retries_transient_without_per_url_cdx():
    clear_wayback_timestamps()
    url = "https://www.example.edu/programs/business/master-of-x"
    set_wayback_timestamps({url: "20260404000000"})
    calls: list[str] = []

    class SnapshotResponse:
        text = "<html><body>" + ("course " * 200) + "</body></html>"

        def __init__(self, status_code: int):
            self.status_code = status_code

    responses = [SnapshotResponse(429), SnapshotResponse(200)]

    async def fake_get(self, endpoint_url, **kwargs):
        calls.append(endpoint_url)
        assert "cdx/search" not in endpoint_url
        if len(calls) == 1:
            request = httpx.Request("GET", endpoint_url)
            raise httpx.ConnectError(
                "archive connection temporarily refused",
                request=request,
            )
        return responses.pop(0)

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", new=AsyncMock()),
    ):
        assert await fetch_html_wayback(url)

    assert len(calls) == 3
    assert calls[0] == calls[1] == calls[2]


@pytest.mark.asyncio
async def test_wayback_no_snapshot_is_a_permanent_typed_failure():
    clear_wayback_timestamps()
    url = "https://www.example.edu/programs/missing-course"

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return [["urlkey", "timestamp", "original"]]

    async def fake_get(self, endpoint_url, **kwargs):
        assert "cdx/search" in endpoint_url
        return FakeResponse()

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", new=AsyncMock()),
    ):
        assert await fetch_html_wayback(url) is None

    failure = get_last_fetch_failure()
    assert failure is not None
    assert failure["kind"] == "wayback_no_snapshot"
    assert failure["retryable"] is False
    assert failure["transport"] == "wayback_cdx"


@pytest.mark.asyncio
async def test_authoritative_wayback_scope_does_not_cross_www_apex_alias():
    clear_wayback_timestamps()
    set_wayback_timestamps(
        {},
        authoritative_prefixes=["https://www.example.edu/programs/*"],
    )
    calls: list[str] = []

    class EmptyCdxResponse:
        status_code = 200

        @staticmethod
        def json():
            return [["urlkey", "timestamp", "original"]]

    async def fake_get(self, endpoint_url, **kwargs):
        calls.append(endpoint_url)
        return EmptyCdxResponse()

    with patch("httpx.AsyncClient.get", new=fake_get):
        assert await fetch_html_wayback(
            "https://example.edu/programs/not-captured"
        ) is None

    assert len(calls) == 1
    assert "cdx/search" in calls[0]
    failure = get_last_fetch_failure()
    assert failure is not None
    assert failure["transport"] == "wayback_cdx"


@pytest.mark.asyncio
async def test_wayback_network_failure_remains_retryable():
    clear_wayback_timestamps()
    url = "https://www.example.edu/programs/transient-course"
    set_wayback_timestamps({url: "20260102030405"})

    async def fake_get(self, endpoint_url, **kwargs):
        request = httpx.Request("GET", endpoint_url)
        raise httpx.ConnectError("archive temporarily unavailable", request=request)

    with patch("httpx.AsyncClient.get", new=fake_get):
        assert await fetch_html_wayback(url) is None

    failure = get_last_fetch_failure()
    assert failure is not None
    assert failure["kind"] == "wayback_transient"
    assert failure["retryable"] is True
    assert failure["transport"] == "wayback_snapshot"


@pytest.mark.asyncio
async def test_notredame_recipe_prefers_rendered_live_with_wayback_fallback():
    from app.services.scraper.config import get_config_for_host, set_uni_config

    cfg = get_config_for_host(
        hostname="www.notredame.edu.au",
        name="University of Notre Dame Australia",
        scrape_url="https://www.notredame.edu.au/",
        university_id=1165,
    )
    set_uni_config(cfg)
    assert "/resources/snippets/" in cfg.discovery.block_url_patterns
    url = "https://www.notredame.edu.au/programs/school-of-law/test-course"
    rendered = "<html><body>" + ("live course " * 200) + "</body></html>"
    wayback = AsyncMock(return_value=None)
    live = AsyncMock(return_value=rendered)

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            new=wayback,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            new=live,
        ),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token"}),
    ):
        with scrape_do_render_scope():
            assert await fetch_html(url) == rendered

    wayback.assert_not_awaited()
    live.assert_awaited_once()
    assert live.await_args.kwargs["render"] is True


@pytest.mark.asyncio
async def test_notredame_recipe_falls_back_to_wayback_after_first_render_failure():
    from app.services.scraper.config import get_config_for_host, set_uni_config

    cfg = get_config_for_host(
        hostname="www.notredame.edu.au",
        name="University of Notre Dame Australia",
        scrape_url="https://www.notredame.edu.au/",
        university_id=1165,
    )
    set_uni_config(cfg)
    assert cfg.extraction.max_parallel_fetch == 8
    assert cfg.extraction.scrape_do_local_concurrency == 3
    url = "https://www.notredame.edu.au/programs/school-of-law/test-course"
    archived = "<html><body>" + ("archived course " * 200) + "</body></html>"
    wayback = AsyncMock(return_value=archived)
    live = AsyncMock(return_value=None)

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            new=wayback,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            new=live,
        ),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token"}),
    ):
        with scrape_do_render_scope():
            assert await fetch_html(url) == archived

    live.assert_awaited_once()
    assert live.await_args.kwargs["max_retries"] == 0
    assert live.await_args.kwargs["request_timeout_seconds"] == 20
    assert live.await_args.kwargs["local_concurrency_limit"] == 3
    wayback.assert_awaited_once_with(url)


@pytest.mark.asyncio
async def test_scrape_do_attempt_timeout_starts_after_local_provider_slot():
    shared_slot = asyncio.Semaphore(1)

    @asynccontextmanager
    async def account_slot():
        yield

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params):
            await asyncio.sleep(0.03)
            return httpx.Response(200, text="<html>" + ("course " * 200) + "</html>")

    with (
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token"}),
        patch(
            "app.services.scraper.http_fetcher._get_scrape_do_sem",
            return_value=shared_slot,
        ),
        patch(
            "app.services.scraper.scrape_do_semaphore.account_slot",
            new=account_slot,
        ),
        patch(
            "app.services.scraper.http_fetcher.httpx.AsyncClient",
            new=FakeClient,
        ),
    ):
        results = await asyncio.gather(
            fetch_html_scrape_do(
                "https://example.edu/course/a",
                render=True,
                rate_limit=False,
                max_retries=0,
                request_timeout_seconds=0.05,
                local_concurrency_limit=1,
            ),
            fetch_html_scrape_do(
                "https://example.edu/course/b",
                render=True,
                rate_limit=False,
                max_retries=0,
                request_timeout_seconds=0.05,
                local_concurrency_limit=1,
            ),
        )

    assert all(results)


@pytest.mark.asyncio
async def test_scrape_do_origin_404_is_terminal_not_retryable():
    class MissingResponse:
        status_code = 404
        text = "not found"
        headers: dict[str, str] = {}

    async def fake_get(self, endpoint_url, **kwargs):
        return MissingResponse()

    with (
        patch("httpx.AsyncClient.get", new=fake_get),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token"}),
    ):
        assert await fetch_html_scrape_do(
            "https://www.example.edu/programs/removed",
            render=True,
            max_retries=1,
            rate_limit=False,
        ) is None

    failure = get_last_fetch_failure()
    assert failure is not None
    assert failure["kind"] == "origin_not_found"
    assert failure["retryable"] is False
    assert failure["status_code"] == 404


@pytest.mark.asyncio
async def test_extract_course_threads_terminal_fetch_metadata_to_result():
    from app.services.scraper.config import get_config_for_host, set_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    cfg = get_config_for_host(
        hostname="www.notredame.edu.au",
        name="University of Notre Dame Australia",
        scrape_url="https://www.notredame.edu.au/",
        university_id=1165,
    )
    set_uni_config(cfg)
    failure = {
        "kind": "wayback_no_snapshot",
        "reason": "No 200-status Wayback snapshot exists for this URL.",
        "retryable": False,
        "transport": "wayback_cdx",
        "terminal": True,
    }
    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            return_value=None,
        ),
        patch(
            "app.services.scraper.pipelines.single_course.get_last_fetch_failure",
            return_value=failure,
        ),
    ):
        result = await extract_course(
            "https://www.notredame.edu.au/programs/not-a-school/"
            "postgraduate/missing-course",
            use_ai_fallback=False,
        )

    assert result["error"] == "fetch_failed_wayback_no_snapshot"
    assert result["fetch_failure_kind"] == "wayback_no_snapshot"
    assert result["retryable"] is False
    assert result["fetch_transport"] == "wayback_cdx"


def test_explicit_permanent_failure_never_becomes_generic_retryable_fetch():
    details = _extraction_failure_details(
        "fetch_failed_wayback_no_snapshot",
        result={
            "fetch_failure_kind": "wayback_no_snapshot",
            "error_reason": "No archived snapshot exists.",
            "retryable": False,
        },
    )
    assert details == {
        "reason": "wayback_no_snapshot",
        "detail": "No archived snapshot exists.",
        "retryable": False,
    }


def test_challenge_and_provider_failures_have_stable_classes():
    challenge = _extraction_failure_details(
        "challenge_shell",
        result={"error_type": "ChallengePage", "retryable": False},
    )
    provider = _extraction_failure_details(
        "extract: provider unavailable",
        result={
            "error_type": "ScrapedoAccountError",
            "error_reason": "Provider account unavailable.",
        },
    )
    assert challenge["reason"] == "challenge_page"
    assert challenge["retryable"] is False
    assert provider == {
        "reason": "provider_account_failure",
        "detail": "Provider account unavailable.",
        "retryable": False,
    }


def test_recovery_item_and_time_budgets_are_bounded():
    links = [{"url": f"https://example.edu/{i}"} for i in range(10)]
    work, overflow = _select_recovery_work(links, 3)
    assert len(work) == 3
    assert len(overflow) == 7
    assert _recovery_time_remaining(100.0, 30.0, now=115.0) == 15.0
    assert _recovery_time_remaining(100.0, 30.0, now=140.0) == 0.0


def test_recovery_accounting_conserves_total_failures():
    accounting = _recovery_accounting(total=10, attempted=4, recovered=3)
    assert accounting == {
        "recovery_queued": 10,
        "recovery_attempted": 4,
        "recovery_recovered": 3,
        "recovery_unresolved": 7,
    }
    assert accounting["recovery_recovered"] + accounting["recovery_unresolved"] == 10
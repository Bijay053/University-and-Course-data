import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.scraper import http_fetcher


CHALLENGE_HTML = (
    "<html><head><script>"
    'document.cookie="cookiesession8341=blocked";'
    "eval(function(){var request=new XMLHttpRequest();"
    "setTimeout(function(){request.open('GET','/challenge');},10);});"
    "</script></head><body>"
    + ("blocked " * 100)
    + "</body></html>"
)


@asynccontextmanager
async def _challenge_client():
    response = SimpleNamespace(
        status_code=200,
        text=CHALLENGE_HTML,
        headers={},
    )
    yield SimpleNamespace(get=AsyncMock(return_value=response))


async def _challenge_scrape_do(_url, *, render=False, **_kwargs):
    transport = "scrape_do_render" if render else "scrape_do_static"
    http_fetcher._record_fetch_failure(
        kind="challenge_page",
        reason="blocked",
        retryable=True,
        transport=transport,
        status_code=200,
    )
    return None


def test_challenge_rejections_and_unresolved_are_counted_by_transport():
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        http_fetcher._record_fetch_failure(
            kind="challenge_page",
            reason="blocked",
            retryable=True,
            transport="httpx",
            status_code=200,
        )
        http_fetcher._record_fetch_failure(
            kind="challenge_page",
            reason="blocked",
            retryable=True,
            transport="curl_cffi",
            status_code=200,
        )
        http_fetcher._mark_last_fetch_failure_terminal()
        http_fetcher._mark_last_fetch_failure_terminal()
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert counters == {
        "rejections": {"httpx": 1, "curl_cffi": 1},
        "unresolved": {"curl_cffi": 1},
    }


def test_successful_response_does_not_increment_challenge_failures():
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        rejected = http_fetcher._reject_direct_challenge_html(
            "<html><main><h1>Real course</h1></main></html>",
            url="https://example.edu/course",
            transport="httpx",
        )
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert rejected is False
    assert counters == {"rejections": {}, "unresolved": {}}


def test_recovered_challenge_is_not_counted_as_unresolved():
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        http_fetcher._record_fetch_failure(
            kind="challenge_page",
            reason="blocked",
            retryable=True,
            transport="httpx",
            status_code=200,
        )
        # A later fallback returned a real provider response.
        http_fetcher._last_fetch_failure.set(None)
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert counters == {
        "rejections": {"httpx": 1},
        "unresolved": {},
    }


def test_exact_host_tls_challenge_is_counted_as_unresolved(monkeypatch):
    config = SimpleNamespace(
        discovery=SimpleNamespace(
            insecure_tls_direct_hostnames=["secure.example.edu"],
        )
    )
    monkeypatch.setattr(
        "app.services.scraper.config.context.get_uni_config",
        lambda: config,
    )
    monkeypatch.setattr(http_fetcher.httpx, "AsyncClient", lambda **_kwargs: _challenge_client())
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        result = asyncio.run(
            http_fetcher.fetch_html("https://secure.example.edu/course", retries=0)
        )
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert result is None
    assert counters["unresolved"] == {"direct_insecure_tls": 1}


def test_discovery_fast_path_challenge_is_counted_as_unresolved(monkeypatch):
    config = SimpleNamespace(
        discovery=SimpleNamespace(
            insecure_tls_direct_hostnames=[],
            scrape_do_skip_fallbacks=True,
            scrape_do_render=False,
            scrape_do_super=False,
            scrape_do_wait_for_ms=3000,
        )
    )
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "configured")
    monkeypatch.setattr(
        "app.services.scraper.config.context.get_uni_config",
        lambda: config,
    )
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", _challenge_scrape_do)
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        result = asyncio.run(http_fetcher.fetch_html("https://example.edu/listing"))
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert result == ""
    assert counters["rejections"] == {
        "scrape_do_static": 1,
        "scrape_do_render": 1,
    }
    assert counters["unresolved"] == {"scrape_do_render": 1}


def test_cached_cloudflare_fast_path_challenge_is_counted_as_unresolved(monkeypatch):
    host = "cached.example.edu"
    config = SimpleNamespace(
        discovery=SimpleNamespace(
            insecure_tls_direct_hostnames=[],
            scrape_do_skip_fallbacks=False,
        )
    )
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "configured")
    monkeypatch.setattr(
        "app.services.scraper.config.context.get_uni_config",
        lambda: config,
    )
    monkeypatch.setattr(http_fetcher, "fetch_html_scrape_do", _challenge_scrape_do)
    http_fetcher._cf_always_scrape_do.add(host)
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        result = asyncio.run(http_fetcher.fetch_html(f"https://{host}/course"))
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)
        http_fetcher._cf_always_scrape_do.discard(host)

    assert result is None
    assert counters["unresolved"] == {"scrape_do_static": 1}


def test_httpx_challenge_recovered_by_cffi_has_no_unresolved_count(monkeypatch):
    config = SimpleNamespace(
        discovery=SimpleNamespace(
            insecure_tls_direct_hostnames=[],
            scrape_do_skip_fallbacks=False,
            use_wayback=False,
        )
    )
    monkeypatch.delenv("SCRAPE_DO_TOKEN", raising=False)
    monkeypatch.setattr(
        "app.services.scraper.config.context.get_uni_config",
        lambda: config,
    )
    monkeypatch.setattr(http_fetcher, "_client", _challenge_client)
    monkeypatch.setattr(
        http_fetcher,
        "fetch_html_cffi",
        AsyncMock(return_value="<html><body><h1>Recovered course</h1></body></html>"),
    )
    monkeypatch.setattr(
        "app.services.scraper.snapshot_context.stage_snapshot",
        lambda *_args, **_kwargs: None,
    )
    counters = {"rejections": {}, "unresolved": {}}
    token = http_fetcher._challenge_job_counters.set(counters)
    try:
        result = asyncio.run(
            http_fetcher.fetch_html("https://recover.example.edu/course", retries=0)
        )
    finally:
        http_fetcher._challenge_job_counters.reset(token)
        http_fetcher._last_fetch_failure.set(None)

    assert result is not None
    assert counters == {
        "rejections": {"httpx": 1},
        "unresolved": {},
    }
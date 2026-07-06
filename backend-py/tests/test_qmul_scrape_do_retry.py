"""QMUL fetch_failed fix — retry-after-double-failure for scrape_do_skip_fallbacks.

QMUL (uni_id=2203, job_5f5ab180197a, 2026-07-03) is configured with:

    extraction:
      scrape_do_render: true
      scrape_do_skip_fallbacks: true
      skip_browser_rescue: true

This combination makes the ``fetch_html()`` scrape_do_skip_fallbacks fast-path
(Scrape.do render -> Scrape.do static) the *only* fetch attempt for every
course page — there is no httpx/cffi/Wayback/browser tier below it, because
QMUL's datacenter-IP block affects both plain HTTP and our own Playwright pool.

Under concurrent load (12 parallel HTTP workers) Scrape.do's residential proxy
pool occasionally returns a transient failure (502 / "ROTATION_FAILED") for a
request that would succeed moments later. Without a retry, a single transient
blip on BOTH render and static permanently loses that course as
``fetch_failed`` with no further recourse. This happened to 47/409 QMUL
courses (~11%) in job_5f5ab180197a.

The first fix added a single short-backoff retry (render=True again) after
the existing render->static chain both fail. job_4fb674e585b2 (2026-07-06)
showed that under heavier cross-university Scrape.do contention a single
retry is no longer enough (279/409, ~68%, lost) — so the retry was widened
to a 3-step exponential-backoff ladder (render, static, render at 3s/8s/15s)
before finally falling back to Wayback. These tests verify the retry ladder
fires (and can rescue a transient failure) without changing behaviour when
the fast-path already succeeds on the first pass.
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    DiscoveryConfig,
    ExtractionConfig,
    UniConfig,
)
from app.services.scraper.http_fetcher import scrape_do_render_scope

_QMUL_COURSE_URL = (
    "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/tax-law-llm/"
)

_MINIMAL_HTML = """
<html><head><title>Tax Law LLM — Queen Mary University of London</title></head>
<body>
<h1>Tax Law LLM</h1>
<p>This programme provides a comprehensive grounding in tax law with a strong
international and comparative focus for students seeking advanced legal
training in taxation policy and practice across jurisdictions worldwide.</p>
</body></html>
""" * 3


def _qmul_uni_config() -> UniConfig:
    return UniConfig(
        slug="qmul",
        name="Queen Mary University of London",
        base_url="https://www.qmul.ac.uk/",
        scrape_url="https://www.qmul.ac.uk/",
        discovery=DiscoveryConfig(),
        extraction=ExtractionConfig(
            scrape_do_render=True,
            scrape_do_skip_fallbacks=True,
            skip_browser_rescue=True,
        ),
    )


@pytest.mark.asyncio
async def test_retry_rescues_transient_double_failure():
    """When render+static both fail once, a retry of render=True is attempted
    and its success is returned instead of giving up."""
    set_uni_config(_qmul_uni_config())

    calls: list[dict] = []

    async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
        calls.append({"url": url, "render": render})
        # First render attempt fails, static fallback fails, second render
        # attempt (the retry) succeeds — simulating a transient proxy blip.
        if len([c for c in calls if c["render"]]) >= 2:
            return _MINIMAL_HTML
        return None

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            side_effect=_mock_scrape_do,
        ),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        with scrape_do_render_scope():
            result = await fetch_html(_QMUL_COURSE_URL)

    render_calls = [c for c in calls if c["render"]]
    static_calls = [c for c in calls if not c["render"]]
    assert len(render_calls) == 2, (
        f"expected initial render attempt + one retry, got {len(render_calls)}: {calls}"
    )
    assert static_calls, "static fallback must still be tried between the two render attempts"
    assert result == _MINIMAL_HTML, (
        "the retry's successful result must be returned instead of None"
    )


@pytest.mark.asyncio
async def test_falls_back_to_wayback_when_all_scrape_do_attempts_fail():
    """When render, static, AND the full 3-step retry ladder all fail,
    fetch_html falls back to the Wayback Machine as a last resort (archive.org
    is not subject to the live WAF that blocks both httpx and Scrape.do's
    proxy pool for these universities) before finally giving up."""
    set_uni_config(_qmul_uni_config())

    calls: list[dict] = []

    async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
        calls.append({"url": url, "render": render})
        return None

    async def _mock_wayback(url: str) -> str | None:
        return _MINIMAL_HTML

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            side_effect=_mock_scrape_do,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            side_effect=_mock_wayback,
        ),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        with scrape_do_render_scope():
            result = await fetch_html(_QMUL_COURSE_URL)

    render_calls = [c for c in calls if c["render"]]
    static_calls = [c for c in calls if not c["render"]]
    assert len(render_calls) == 3, (
        f"expected initial render attempt + 2 render retries in the ladder, got "
        f"{len(render_calls)}: {calls}"
    )
    assert len(static_calls) == 2, (
        f"expected initial static attempt + 1 static retry in the ladder, got "
        f"{len(static_calls)}: {calls}"
    )
    assert result == _MINIMAL_HTML, (
        "Wayback Machine success must be returned instead of giving up"
    )


@pytest.mark.asyncio
async def test_returns_none_when_wayback_also_fails():
    """When render, static, retry, AND Wayback Machine all fail, fetch_html
    finally returns None (there is no further fallback tier)."""
    set_uni_config(_qmul_uni_config())

    calls: list[dict] = []

    async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
        calls.append({"url": url, "render": render})
        return None

    async def _mock_wayback(url: str) -> str | None:
        return None

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            side_effect=_mock_scrape_do,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            side_effect=_mock_wayback,
        ),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        with scrape_do_render_scope():
            result = await fetch_html(_QMUL_COURSE_URL)

    render_calls = [c for c in calls if c["render"]]
    static_calls = [c for c in calls if not c["render"]]
    assert len(render_calls) == 3, (
        f"expected initial render attempt + 2 render retries even on total "
        f"failure, got {len(render_calls)}: {calls}"
    )
    assert len(static_calls) == 2, (
        f"expected initial static attempt + 1 static retry even on total "
        f"failure, got {len(static_calls)}: {calls}"
    )
    assert result is None


@pytest.mark.asyncio
async def test_wayback_cdx_picks_latest_200_snapshot_ignoring_availability_api():
    """fetch_html_wayback must use a CDX search filtered to statuscode:200,
    NOT the Wayback Availability API.

    Root cause (QMUL job_5a35dc6f7f73, 2026-07-03): the Availability API
    returns the snapshot "closest" to a requested timestamp regardless of
    HTTP status. For several QMUL course URLs the closest-in-time snapshot
    was itself a 403 (captured while archive.org's crawler was also
    WAF-blocked), even though a perfectly good 200 snapshot existed at a
    different time. This test locks in the CDX-based replacement: given
    multiple 200-status snapshot rows, it must pick the most recent one and
    fetch it via the ``id_`` raw-HTML modifier.
    """
    from app.services.scraper.http_fetcher import fetch_html_wayback

    url = "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/urban-history-and-culture-ma/"
    cdx_rows = [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ["uk,ac,qmul)/...", "20191116235454", url, "text/html", "200", "AAA", "25241"],
        ["uk,ac,qmul)/...", "20250811143334", url, "text/html", "200", "BBB", "29150"],
    ]

    class _FakeResponse:
        def __init__(self, status_code: int, json_data: Any = None, text: str = ""):
            self.status_code = status_code
            self._json_data = json_data
            self.text = text

        def json(self) -> Any:
            return self._json_data

    calls: list[str] = []

    async def _fake_get(self: Any, endpoint_url: str, *, params: Any = None, headers: Any = None) -> _FakeResponse:
        calls.append(endpoint_url)
        if "cdx/search" in endpoint_url:
            return _FakeResponse(200, json_data=cdx_rows)
        if "web.archive.org/web/20250811143334id_/" in endpoint_url:
            return _FakeResponse(200, text=_MINIMAL_HTML)
        raise AssertionError(f"unexpected raw fetch URL: {endpoint_url}")

    with patch("httpx.AsyncClient.get", new=_fake_get):
        result = await fetch_html_wayback(url)

    assert result == _MINIMAL_HTML
    assert any("cdx/search" in c for c in calls), "must query CDX search, not the Availability API"
    assert not any("wayback/available" in c for c in calls), (
        "must NOT use the Availability API — it returns closest-by-time "
        "snapshots regardless of status code"
    )


@pytest.mark.asyncio
async def test_wayback_cdx_returns_none_when_no_200_snapshot_exists():
    """Genuine data gap: if CDX has no 200-status snapshot at all (e.g. QMUL's
    'MA War Studies' — every archived crawl of that URL was itself blocked),
    fetch_html_wayback must return None rather than raising or returning a
    bad snapshot."""
    from app.services.scraper.http_fetcher import fetch_html_wayback

    url = "https://www.qmul.ac.uk/postgraduate/taught/coursefinder/courses/war-studies-ma/"
    cdx_rows = [
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    ]

    class _FakeResponse:
        def __init__(self, status_code: int, json_data: Any = None):
            self.status_code = status_code
            self._json_data = json_data

        def json(self) -> Any:
            return self._json_data

    async def _fake_get(self: Any, endpoint_url: str, *, params: Any = None, headers: Any = None) -> _FakeResponse:
        assert "cdx/search" in endpoint_url
        return _FakeResponse(200, json_data=cdx_rows)

    with patch("httpx.AsyncClient.get", new=_fake_get):
        result = await fetch_html_wayback(url)

    assert result is None


@pytest.mark.asyncio
async def test_no_retry_needed_when_render_succeeds_first_try():
    """Happy path is unaffected: no extra calls or delay when render succeeds
    on the very first attempt."""
    set_uni_config(_qmul_uni_config())

    calls: list[dict] = []

    async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
        calls.append({"url": url, "render": render})
        return _MINIMAL_HTML

    with (
        patch(
            "app.services.scraper.http_fetcher.fetch_html_scrape_do",
            side_effect=_mock_scrape_do,
        ),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None) as mock_sleep,
        patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        with scrape_do_render_scope():
            result = await fetch_html(_QMUL_COURSE_URL)

    assert len(calls) == 1, f"only one Scrape.do call expected on the happy path, got {calls}"
    assert result == _MINIMAL_HTML
    mock_sleep.assert_not_called()

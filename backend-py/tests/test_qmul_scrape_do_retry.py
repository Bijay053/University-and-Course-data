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

The fix adds a single short-backoff retry (render=True again) after the
existing render->static chain both fail, before finally giving up. These
tests verify that retry fires (and can rescue a transient failure) without
changing behaviour when the fast-path already succeeds on the first pass.
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
async def test_returns_none_when_all_attempts_fail():
    """When render, static, AND the retry all fail, fetch_html returns None
    (there is no further fallback tier for skip_browser_rescue universities)."""
    set_uni_config(_qmul_uni_config())

    calls: list[dict] = []

    async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
        calls.append({"url": url, "render": render})
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
    assert len(render_calls) == 2, (
        f"expected initial render attempt + one retry even on total failure, got "
        f"{len(render_calls)}: {calls}"
    )
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

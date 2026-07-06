"""Kingston discovery-timeout fix — 429 backoff + use_wayback gating.

Kingston (job failing with "[DISCOVER] Discovery phase exceeded 300s deadline")
uses plain cffi bypass (no Scrape.do) with ``max_parallel_fetch: 2`` and
``bfs_page_budget: 35``. kingston.yaml documents that pages past ~11 trigger a
Cloudflare rate-limit (429) at higher concurrency, and that it explicitly
disables Wayback (``use_wayback: false``) since archive.org has nothing useful
for this host.

Root cause: ``fetch_html()``'s Cloudflare-block classifier
(``_is_cloudflare_block``) treats ANY 403/429/503 with cf-ray/cloudflare
headers identically, immediately escalating through the full ladder
(cffi retry -> Wayback -> Scrape.do static -> Scrape.do render) even for a
plain rate limit that a short backoff-and-retry would resolve on the SAME
transport. It also never consulted the per-university ``use_wayback`` flag
for this per-request fallback tier (that flag was only wired into a separate,
discovery-wide Wayback CDX sweep in orchestrator.py). Both defects add wasted
round-trip latency on every blocked BFS page, which is what exhausted
Kingston's 300s discovery_phase_timeout_s budget.

These tests lock in two fixes:

1. A 429 (rate limit) gets 2 short same-tier backoff retries via plain httpx
   before falling through to the heavier cffi/Wayback/Scrape.do ladder.
2. The Wayback tier is skipped entirely when the active UniConfig sets
   ``discovery.use_wayback: false``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    DiscoveryConfig,
    ExtractionConfig,
    UniConfig,
)

_KINGSTON_COURSE_URL = "https://www.kingston.ac.uk/undergraduate/course/computer-science/"

_MINIMAL_HTML = """
<html><head><title>Computer Science BSc — Kingston University</title></head>
<body>
<h1>Computer Science BSc</h1>
<p>This programme provides a comprehensive grounding in software engineering,
algorithms, and systems design for students seeking a rigorous technical
foundation across a broad range of computing disciplines and industries.</p>
</body></html>
""" * 3


def _kingston_uni_config(use_wayback: bool | None = False) -> UniConfig:
    return UniConfig(
        slug="kingston",
        name="Kingston University",
        base_url="https://www.kingston.ac.uk/",
        scrape_url="https://www.kingston.ac.uk/undergraduate/",
        discovery=DiscoveryConfig(use_wayback=use_wayback),
        extraction=ExtractionConfig(max_parallel_fetch=2),
    )


class _FakeResp:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _cf_429_headers() -> dict:
    return {"cf-ray": "abc123-LHR", "server": "cloudflare"}


@pytest.mark.asyncio
async def test_429_gets_backoff_retry_before_escalating_to_cffi():
    """A 429 with Cloudflare headers should be retried via plain httpx (with a
    short backoff) rather than immediately falling through to cffi/Wayback."""
    set_uni_config(_kingston_uni_config(use_wayback=False))

    call_count = {"n": 0}

    async def _fake_get(self: Any, url: str, *, cookies: Any = None) -> _FakeResp:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _FakeResp(429, headers=_cf_429_headers())
        return _FakeResp(200, text=_MINIMAL_HTML)

    with (
        patch("httpx.AsyncClient.get", new=_fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        result = await fetch_html(_KINGSTON_COURSE_URL)

    assert result == _MINIMAL_HTML
    assert call_count["n"] == 2, (
        f"expected initial 429 + one successful backoff retry via plain httpx, "
        f"got {call_count['n']} calls"
    )


@pytest.mark.asyncio
async def test_429_falls_through_to_cffi_when_backoff_retries_exhausted():
    """If the rate limit hasn't cleared after both backoff retries, the
    existing cffi/Wayback/Scrape.do ladder must still run as before."""
    set_uni_config(_kingston_uni_config(use_wayback=False))

    async def _fake_get(self: Any, url: str, *, cookies: Any = None) -> _FakeResp:
        return _FakeResp(429, headers=_cf_429_headers())

    async def _fake_cffi(url: str) -> str | None:
        return _MINIMAL_HTML

    with (
        patch("httpx.AsyncClient.get", new=_fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_cffi",
            side_effect=_fake_cffi,
        ),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        result = await fetch_html(_KINGSTON_COURSE_URL)

    assert result == _MINIMAL_HTML, "cffi tier must still rescue after 429 backoff exhausts"


@pytest.mark.asyncio
async def test_wayback_tier_skipped_when_use_wayback_false():
    """Kingston sets use_wayback: false — the per-request Wayback fallback
    tier must be skipped entirely (not just the discovery-wide CDX sweep)."""
    set_uni_config(_kingston_uni_config(use_wayback=False))

    async def _fake_get(self: Any, url: str, *, cookies: Any = None) -> _FakeResp:
        # 403 (not 429) so we skip straight past the rate-limit backoff path
        return _FakeResp(403, headers=_cf_429_headers())

    async def _fake_cffi(url: str) -> str | None:
        return None  # cffi also fails, forcing us into the Wayback decision point

    wayback_calls: list[str] = []

    async def _fake_wayback(url: str) -> str | None:
        wayback_calls.append(url)
        return _MINIMAL_HTML

    with (
        patch("httpx.AsyncClient.get", new=_fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_cffi",
            side_effect=_fake_cffi,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            side_effect=_fake_wayback,
        ),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        result = await fetch_html(_KINGSTON_COURSE_URL)

    assert not wayback_calls, "Wayback tier must not be called when use_wayback=false"
    assert result is None, "no other tier configured for Kingston in this test — must return None"


@pytest.mark.asyncio
async def test_wayback_tier_still_runs_when_use_wayback_not_disabled():
    """Regression guard: universities that don't disable Wayback must be
    unaffected by the new gating."""
    set_uni_config(_kingston_uni_config(use_wayback=None))

    from app.services.scraper import http_fetcher as _hf
    _hf._cf_always_scrape_do.discard("www.kingston.ac.uk")

    async def _fake_get(self: Any, url: str, *, cookies: Any = None) -> _FakeResp:
        return _FakeResp(403, headers=_cf_429_headers())

    async def _fake_cffi(url: str) -> str | None:
        return None

    async def _fake_wayback(url: str) -> str | None:
        return _MINIMAL_HTML

    with (
        patch("httpx.AsyncClient.get", new=_fake_get),
        patch("app.services.scraper.http_fetcher.asyncio.sleep", return_value=None),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_cffi",
            side_effect=_fake_cffi,
        ),
        patch(
            "app.services.scraper.http_fetcher.fetch_html_wayback",
            side_effect=_fake_wayback,
        ),
    ):
        from app.services.scraper.http_fetcher import fetch_html
        result = await fetch_html(_KINGSTON_COURSE_URL)

    assert result == _MINIMAL_HTML, "Wayback must still be used when use_wayback is not explicitly false"

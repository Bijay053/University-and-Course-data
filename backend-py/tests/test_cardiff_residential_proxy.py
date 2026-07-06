"""Cardiff residential proxy fix — verification tests (Task #254).

Task #253 added three flags to cardiff.yaml so Scrape.do's residential proxy
bypasses the Cloudflare Enterprise 403 that blocks every datacenter IP:

    extraction:
      scrape_do_render: true           # per-course fetch → Scrape.do headless Chrome
      scrape_do_skip_fallbacks: true   # skip httpx + cffi (both return 403, waste ~2s)
      skip_browser_rescue: true        # skip Playwright browser (also datacenter IP)

These tests verify all three flags are wired correctly end-to-end:

  1. Cardiff YAML loads without schema errors and exposes the three flags.
  2. scrape_do_skip_fallbacks fast-path fires: httpx/cffi never called, Scrape.do
     render=True is called directly.
  3. Static fallback: when render fails, Scrape.do static is tried before giving up.
  4. No-token guard: when SCRAPE_DO_TOKEN is absent, the fast-path is skipped and
     the normal httpx chain runs (so scraping degrades gracefully, not silently).
  5. skip_browser_rescue prevents Playwright browser from firing for Cardiff URLs.
  6. Full extract_course() smoke test: with Cardiff config active and mocked
     Scrape.do returning valid HTML, extract_course() returns a result (not error).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    DiscoveryConfig,
    ExtractionConfig,
    UniConfig,
)
from app.services.scraper.http_fetcher import scrape_do_render_scope


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CARDIFF_YAML_PATH = (
    "scraper_config/unis/cardiff.yaml"
)

_CARDIFF_COURSE_URL = (
    "https://www.cardiff.ac.uk/study/postgraduate/taught/courses/"
    "2025-26/computer-science-msc"
)

_MINIMAL_HTML = """
<html><head><title>Computer Science MSc — Cardiff University</title></head>
<body>
<h1>Computer Science MSc</h1>
<p>This programme provides advanced training in computer science, machine learning,
software engineering, and data analysis. Students develop strong analytical,
programming, and problem-solving skills. The course covers algorithms, system
design, artificial intelligence, distributed computing, and cloud-native
architectures, preparing graduates for careers in technology, research, and
industry leadership roles across a wide variety of sectors.</p>
<ul>
  <li><strong>Duration:</strong> 1 year full-time</li>
  <li><strong>Start date / intake:</strong> September 2025</li>
</ul>
</body></html>
"""


def _cardiff_uni_config() -> UniConfig:
    """Build a Cardiff-equivalent UniConfig with all three residential proxy flags."""
    extr = ExtractionConfig(
        scrape_do_render=True,
        scrape_do_skip_fallbacks=True,
        skip_browser_rescue=True,
        max_parallel_fetch=3,
    )
    return UniConfig(
        slug="cardiff",
        name="Cardiff University",
        university_id=None,
        base_url="https://www.cardiff.ac.uk",
        scrape_url="https://www.cardiff.ac.uk/study/undergraduate/courses/a-to-z",
        discovery=DiscoveryConfig(),
        extraction=extr,
    )


# ---------------------------------------------------------------------------
# 1. YAML schema validation
# ---------------------------------------------------------------------------


class TestCardiffYamlConfig:
    """Cardiff YAML loads without errors and exposes the three proxy flags."""

    def test_yaml_loads_without_error(self):
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        assert raw is not None, "cardiff.yaml must not be empty"

    def test_scrape_do_render_is_true(self):
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        assert raw["extraction"]["scrape_do_render"] is True, (
            "extraction.scrape_do_render must be True to route course fetches "
            "through Scrape.do headless Chrome"
        )

    def test_scrape_do_skip_fallbacks_is_true(self):
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        assert raw["extraction"]["scrape_do_skip_fallbacks"] is True, (
            "extraction.scrape_do_skip_fallbacks must be True to skip the doomed "
            "httpx + cffi datacenter attempts and save ~2s per course"
        )

    def test_skip_browser_rescue_is_true(self):
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        assert raw["extraction"]["skip_browser_rescue"] is True, (
            "extraction.skip_browser_rescue must be True to skip the Playwright "
            "browser rescue path (also runs from a datacenter IP, also gets 403)"
        )

    def test_seed_urls_are_present(self):
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        seeds = raw["discovery"]["seed_urls"]
        assert len(seeds) >= 3, "Cardiff must have at least 3 seed URLs (UG, PGR, PGT)"
        assert any("undergraduate" in s for s in seeds)
        assert any("postgraduate" in s for s in seeds)

    def test_schema_parses_cardiff_yaml(self):
        """Full schema parse: YAML + ExtractionConfig model must accept all fields."""
        with open(_CARDIFF_YAML_PATH) as f:
            raw = yaml.safe_load(f)
        extr_raw = raw.get("extraction", {})
        extr = ExtractionConfig(**extr_raw)
        assert extr.scrape_do_render is True
        assert extr.scrape_do_skip_fallbacks is True
        assert extr.skip_browser_rescue is True


# ---------------------------------------------------------------------------
# 2. scrape_do_skip_fallbacks fast-path in fetch_html()
# ---------------------------------------------------------------------------


class TestScrapeDoSkipFallbacks:
    """When skip_fallbacks=True, fetch_html() goes straight to Scrape.do render."""

    @pytest.mark.asyncio
    async def test_skip_fallbacks_bypasses_httpx(self):
        """httpx client must never be called when scrape_do_skip_fallbacks=True.

        We patch _get_shared_client() — the factory used by the for-loop inside
        fetch_html() — so that if httpx is ever reached, the test fails loudly.
        The fast-path at lines 586-612 of http_fetcher.py must return before the
        shared client is ever acquired.
        """
        set_uni_config(_cardiff_uni_config())

        scrape_do_calls: list[dict] = []

        async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str:
            scrape_do_calls.append({"url": url, "render": render})
            return _MINIMAL_HTML

        httpx_client_acquired: list[bool] = []

        def _sentinel_get_client():
            """Recording spy — records acquisition and returns a crash-on-get client."""
            httpx_client_acquired.append(True)
            m = MagicMock()
            m.get = AsyncMock(side_effect=AssertionError("httpx client.get must NOT be called"))
            return m

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch(
                "app.services.scraper.http_fetcher._get_shared_client",
                side_effect=_sentinel_get_client,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.http_fetcher import fetch_html

            with scrape_do_render_scope():
                result = await fetch_html(_CARDIFF_COURSE_URL)

        assert result == _MINIMAL_HTML, (
            f"Expected Scrape.do HTML; got: {result!r:.200}"
        )
        assert len(scrape_do_calls) >= 1, (
            "fetch_html_scrape_do must be called at least once"
        )
        assert scrape_do_calls[0]["render"] is True, (
            f"First Scrape.do call must use render=True; calls={scrape_do_calls}"
        )
        assert not httpx_client_acquired, (
            "httpx shared client must NOT be acquired when scrape_do_skip_fallbacks=True "
            f"— client was acquired {len(httpx_client_acquired)} time(s)"
        )

    @pytest.mark.asyncio
    async def test_skip_fallbacks_first_call_uses_render_true(self):
        """The first Scrape.do call when skip_fallbacks=True must use render=True."""
        set_uni_config(_cardiff_uni_config())

        calls: list[dict] = []

        async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str:
            calls.append({"url": url, "render": render})
            return _MINIMAL_HTML

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.http_fetcher import fetch_html
            with scrape_do_render_scope():
                await fetch_html(_CARDIFF_COURSE_URL)

        render_calls = [c for c in calls if c["render"]]
        assert render_calls, (
            "At least one Scrape.do call must have render=True; "
            f"all calls={calls}"
        )

    @pytest.mark.asyncio
    async def test_discovery_fast_path_exempt_from_rate_limiter(self):
        """Discovery-phase scrape.do calls must NOT go through acquire_scrape_do().

        Regression test — Cardiff job_68778b8f7bb2 (2026-07-06): the fleet-wide
        Scrape.do rate limiter (enabled to fix QMUL's fetch_failed burst) shares
        one small per-second token budget across every caller.  QMUL's parallel
        course-extraction retries saturated that budget, so each of Cardiff's
        one-at-a-time discovery calls waited up to 30s for a token and discovery
        blew through its 300s deadline after only 4/25 listing pages.  Discovery
        is low-volume and sequential — it is the victim of bursts, not the
        cause — so its fast-path fetches must bypass the limiter entirely.
        """
        cfg = _cardiff_uni_config()
        cfg.discovery = DiscoveryConfig(scrape_do_skip_fallbacks=True)
        set_uni_config(cfg)

        rate_limit_flags: list[bool] = []

        async def _mock_scrape_do(
            url: str, *, render: bool = False, rate_limit: bool = True, **kw: Any
        ) -> str:
            rate_limit_flags.append(rate_limit)
            return _MINIMAL_HTML

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.http_fetcher import fetch_html

            result = await fetch_html("https://www.cardiff.ac.uk/study/courses/")

        assert result == _MINIMAL_HTML
        assert rate_limit_flags, "discovery fast-path must call fetch_html_scrape_do"
        assert all(flag is False for flag in rate_limit_flags), (
            "discovery-phase fetch_html_scrape_do calls must pass rate_limit=False "
            f"so they never queue behind another university's burst; got {rate_limit_flags}"
        )

    @pytest.mark.asyncio
    async def test_discovery_rate_limit_false_actually_skips_acquire(self):
        """rate_limit=False on fetch_html_scrape_do must skip acquire_scrape_do()."""
        from app.services.scraper.http_fetcher import fetch_html_scrape_do

        acquire_called = False

        async def _fake_acquire() -> None:
            nonlocal acquire_called
            acquire_called = True

        with (
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
            patch(
                "app.services.scraper.rate_limiter.acquire_scrape_do",
                side_effect=_fake_acquire,
            ),
            patch("httpx.AsyncClient.get") as mock_get,
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = _MINIMAL_HTML
            mock_get.return_value = mock_resp
            await fetch_html_scrape_do(
                "https://www.cardiff.ac.uk/study/courses/",
                render=False,
                rate_limit=False,
            )

        assert not acquire_called, (
            "acquire_scrape_do() must NOT be called when rate_limit=False"
        )

    @pytest.mark.asyncio
    async def test_skip_fallbacks_static_fallback_when_render_fails(self):
        """When render=True returns None, static (render=False) is tried next."""
        set_uni_config(_cardiff_uni_config())

        calls: list[dict] = []

        async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str | None:
            calls.append({"url": url, "render": render})
            if render:
                return None  # render fails (Scrape.do 502)
            return _MINIMAL_HTML  # static succeeds

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.http_fetcher import fetch_html
            with scrape_do_render_scope():
                result = await fetch_html(_CARDIFF_COURSE_URL)

        render_calls = [c for c in calls if c["render"]]
        static_calls = [c for c in calls if not c["render"]]
        assert render_calls, "render=True must be attempted first"
        assert static_calls, (
            "static (render=False) fallback must be tried when render=True fails"
        )
        assert result == _MINIMAL_HTML, (
            "Result must come from the static fallback"
        )

    @pytest.mark.asyncio
    async def test_no_token_disables_fast_path(self):
        """When SCRAPE_DO_TOKEN is unset the skip-fallbacks path must not fire."""
        set_uni_config(_cardiff_uni_config())

        scrape_do_called: list[bool] = []

        async def _mock_scrape_do(url: str, **kw: Any) -> str | None:
            scrape_do_called.append(True)
            return None

        httpx_calls: list[str] = []

        # We need a fake httpx response to stop the chain without actually hitting CF
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = _MINIMAL_HTML

        mock_get = AsyncMock(return_value=fake_resp)

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {}, clear=False),
        ):
            # Remove SCRAPE_DO_TOKEN if set
            os.environ.pop("SCRAPE_DO_TOKEN", None)

            from app.services.scraper.http_fetcher import fetch_html, _get_shared_client
            client = _get_shared_client()
            with patch.object(client, "get", mock_get):
                with scrape_do_render_scope():
                    result = await fetch_html(_CARDIFF_COURSE_URL)

        # With no token, skip-fallbacks fast-path must be bypassed
        # (the httpx path must have been attempted instead)
        assert mock_get.called, (
            "Without SCRAPE_DO_TOKEN, fetch_html() must fall back to httpx"
        )


# ---------------------------------------------------------------------------
# 3. skip_browser_rescue for Cardiff
# ---------------------------------------------------------------------------


class TestCardiffSkipBrowserRescue:
    """skip_browser_rescue=true must suppress Playwright browser for Cardiff."""

    @pytest.mark.asyncio
    async def test_browser_not_called_for_cardiff_url(self):
        """Browser pool must never be called when skip_browser_rescue=true."""
        set_uni_config(_cardiff_uni_config())

        browser_called: list[str] = []

        async def _http_none(url: str, *a: Any, **kw: Any) -> None:
            return None

        async def _browser_fetch(url: str, **kw: Any) -> None:
            browser_called.append(url)
            return None

        emitted: list[dict] = []

        async def _emit(event: str, message: str, **kw: Any) -> None:
            emitted.append({"event": event, "message": message, **kw})

        with (
            patch(
                "app.services.scraper.pipelines.single_course.fetch_html",
                side_effect=_http_none,
            ),
            patch(
                "app.services.scraper.browser_pool.pool",
                fetch_html=_browser_fetch,
            ),
        ):
            from app.services.scraper.pipelines.single_course import extract_course
            result = await extract_course(_CARDIFF_COURSE_URL, emit=_emit)

        assert browser_called == [], (
            "Browser pool must NOT be called for Cardiff when skip_browser_rescue=true"
        )
        skip_events = [e for e in emitted if "[BROWSER↑ SKIPPED]" in e.get("message", "")]
        assert skip_events, (
            f"Expected a [BROWSER↑ SKIPPED] emit event; all events: "
            f"{[e['message'] for e in emitted]}"
        )

    @pytest.mark.asyncio
    async def test_browser_skip_log_attributes_to_skip_browser_rescue(self):
        """The skip log line must name skip_browser_rescue as the reason."""
        set_uni_config(_cardiff_uni_config())

        emitted: list[dict] = []

        async def _emit(event: str, message: str, **kw: Any) -> None:
            emitted.append({"event": event, "message": message, **kw})

        async def _http_none(url: str, *a: Any, **kw: Any) -> None:
            return None

        with patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_http_none,
        ):
            from app.services.scraper.pipelines.single_course import extract_course
            await extract_course(_CARDIFF_COURSE_URL, emit=_emit)

        skip_msgs = [
            e["message"] for e in emitted if "[BROWSER↑ SKIPPED]" in e.get("message", "")
        ]
        assert skip_msgs, "Expected [BROWSER↑ SKIPPED] event"
        assert "skip_browser_rescue=true" in skip_msgs[0], (
            f"Log must attribute skip to skip_browser_rescue; got: {skip_msgs[0]}"
        )


# ---------------------------------------------------------------------------
# 4. extract_course() smoke test with Cardiff config + mocked Scrape.do
# ---------------------------------------------------------------------------


class TestCardiffExtractCourseSmoke:
    """extract_course() with Cardiff config and mocked Scrape.do returns a result."""

    @pytest.mark.asyncio
    async def test_extract_course_returns_result_not_error(self):
        """With Cardiff config + working Scrape.do mock, extract_course() must not
        return an error dict for a standard Cardiff URL."""
        set_uni_config(_cardiff_uni_config())

        _RICH_HTML = """
        <html><head><title>Computer Science MSc — Cardiff University</title></head>
        <body>
        <h1>Computer Science MSc</h1>
        <ul>
          <li><strong>Duration:</strong> 1 year full-time</li>
          <li><strong>Start date:</strong> September 2025</li>
          <li><strong>intake:</strong> September</li>
        </ul>
        <div id="fees-row">
          <h2 id="fees">Tuition fees</h2>
          <h3>Overseas students</h3>
          <div class="table">
            <table><tbody><tr><td>Full time</td><td>£23,450</td></tr></tbody></table>
          </div>
        </div>
        <p>IELTS score of 6.5 with no element below 5.5 is required.</p>
        </body></html>
        """

        async def _mock_scrape_do(url: str, *, render: bool = False, **kw: Any) -> str:
            return _RICH_HTML

        async def _emit(event: str, message: str, **kw: Any) -> None:
            pass

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.pipelines.single_course import extract_course
            result = await extract_course(_CARDIFF_COURSE_URL, emit=_emit)

        assert isinstance(result, dict), f"Expected dict result; got {type(result)}"
        # Must not be a bare fetch_failed error — Scrape.do returned valid HTML
        assert result.get("error") not in ("fetch_failed", "fetch_failed_empty_text"), (
            f"extract_course() must not return fetch_failed when Scrape.do succeeds; "
            f"result={result}"
        )

    @pytest.mark.asyncio
    async def test_extract_course_sets_course_name_from_h1(self):
        """Course name must be extracted from the <h1> tag (cardiff.yaml selectors.course_name).

        extract_course() returns {'url': ..., 'payload': {...}, 'evidence': [...], ...}
        so course_name lives inside result['payload'].
        """
        set_uni_config(_cardiff_uni_config())

        _H1_HTML = """
        <html><body>
        <h1>Computer Science MSc</h1>
        <p>This programme provides advanced training in computer science and software
        engineering, covering algorithms, system design, artificial intelligence, and
        cloud-native architectures for technology and research careers.</p>
        <li><strong>Duration:</strong> 1 year full-time</li>
        <li><strong>Start date / intake:</strong> September 2025</li>
        </body></html>
        """

        async def _mock_scrape_do(url: str, **kw: Any) -> str:
            return _H1_HTML

        async def _emit(event: str, message: str, **kw: Any) -> None:
            pass

        with (
            patch(
                "app.services.scraper.http_fetcher.fetch_html_scrape_do",
                side_effect=_mock_scrape_do,
            ),
            patch.dict(os.environ, {"SCRAPE_DO_TOKEN": "test-token-xyz"}),
        ):
            from app.services.scraper.pipelines.single_course import extract_course
            result = await extract_course(_CARDIFF_COURSE_URL, emit=_emit)

        # extract_course returns {'url', 'payload': {...}, 'evidence': [...], ...}
        # course_name is inside result['payload'], not at the top level.
        payload = result.get("payload") or {}
        course_name = payload.get("course_name") or payload.get("name")
        assert course_name is not None, (
            "course_name must be extracted from <h1> and appear in result['payload']; "
            f"payload keys: {list(payload.keys())}"
        )
        assert "Computer Science" in str(course_name), (
            f"course_name should contain 'Computer Science'; got: {course_name!r}"
        )

"""ROTATION_FAILED fail-fast + discovery.scrape_do_render straight-to-render.

JCU regression (job_a127d35039d1, 2026-07-09): Scrape.do static ALWAYS 502s
with ROTATION_FAILED ("cannot connect target url") on JCU.  The T03 retry
ladder kept retrying static (~58s per attempt) and blew the 95s seed-prefetch
timeout before the static→render escalation could fire.

Two fixes under test:
1. fetch_html_scrape_do: a 5xx static response whose body contains
   ROTATION_FAILED returns None immediately (no backoff retries) so the
   caller's render tier fires while budget remains.  render=True keeps the
   normal retry ladder (residential browser pool rotation CAN succeed).
2. DiscoveryConfig.scrape_do_render: when True (with scrape_do_skip_fallbacks)
   the discovery fast-path in fetch_html skips the doomed static attempt and
   goes straight to render=True.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

_ROTATION_BODY = (
    '{"URL":"https://www.jcu.edu.au/courses","StatusCode":502,"ErrorCode":90,'
    '"ErrorType":"ROTATION_FAILED","Message":["Error: cannot connect target url",'
    '"Request failed and not charged. Please try again."]}'
)


def _make_response(status: int, text: str):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {}
    return r


def _mock_client(get_fn):
    async def _enter(self_):
        return self_

    async def _exit(self_, *a):
        return False

    mc = MagicMock()
    mc.__aenter__ = _enter
    mc.__aexit__ = _exit
    mc.get = get_fn
    return mc


class TestRotationFailedFailFast:
    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")

    def test_static_rotation_failed_no_retry(self, monkeypatch):
        """502 + ROTATION_FAILED body on render=False → None after ONE attempt."""
        import app.services.scraper.http_fetcher as m

        calls = {"n": 0}

        async def _get(url, params=None):
            calls["n"] += 1
            return _make_response(502, _ROTATION_BODY)

        sleeps: list[float] = []

        async def _fake_sleep(s):
            sleeps.append(s)

        with patch("httpx.AsyncClient", return_value=_mock_client(_get)), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            result = asyncio.run(
                m.fetch_html_scrape_do(
                    "https://www.jcu.edu.au/courses", render=False, rate_limit=False
                )
            )

        assert result is None
        assert calls["n"] == 1, "static ROTATION_FAILED must NOT be retried"
        assert not sleeps, "no backoff sleep should occur on the fail-fast path"

    def test_render_rotation_failed_still_retries(self, monkeypatch):
        """502 + ROTATION_FAILED on render=True keeps the retry ladder."""
        import app.services.scraper.http_fetcher as m

        calls = {"n": 0}
        good_html = "y" * 600

        async def _get(url, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _make_response(502, _ROTATION_BODY)
            return _make_response(200, good_html)

        async def _fake_sleep(s):
            pass

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None), \
             patch("httpx.AsyncClient", return_value=_mock_client(_get)), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            result = asyncio.run(
                m.fetch_html_scrape_do(
                    "https://www.jcu.edu.au/courses", render=True, rate_limit=False
                )
            )

        assert result == good_html
        assert calls["n"] == 2, "render ROTATION_FAILED must retry (and succeed)"

    def test_static_502_without_rotation_failed_still_retries(self, monkeypatch):
        """Plain 502 (no ROTATION_FAILED marker) keeps the normal ladder."""
        import app.services.scraper.http_fetcher as m

        calls = {"n": 0}
        good_html = "z" * 600

        async def _get(url, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return _make_response(502, "upstream hiccup")
            return _make_response(200, good_html)

        async def _fake_sleep(s):
            pass

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None), \
             patch("httpx.AsyncClient", return_value=_mock_client(_get)), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            result = asyncio.run(
                m.fetch_html_scrape_do(
                    "https://example.edu/x", render=False, rate_limit=False
                )
            )

        assert result == good_html
        assert calls["n"] == 2


class TestDiscoveryScrapeDoRenderFirst:
    """discovery.scrape_do_render=True → fetch_html skips the static attempt."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")

    def _run_fetch(self, m, cfg):
        from app.services.scraper.config.context import set_uni_config

        rendered_calls: list[bool] = []
        html = "<html><body>" + "course " * 200 + "</body></html>"

        async def _fake_scrape_do(url, render=False, rate_limit=True, **kw):
            rendered_calls.append(render)
            return html if render else None

        async def _go():
            set_uni_config(cfg)
            return await m.fetch_html("https://www.jcu.edu.au/courses")

        with patch.object(m, "fetch_html_scrape_do", side_effect=_fake_scrape_do), \
             patch.object(m, "_is_spa_shell", return_value=False), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None):
            result = asyncio.run(_go())
        return result, rendered_calls, html

    def test_render_first_skips_static(self):
        import app.services.scraper.http_fetcher as m
        from app.services.scraper.config.schema import UniConfig

        cfg = UniConfig(
            slug="jcu", name="JCU", base_url="https://www.jcu.edu.au",
            scrape_url="https://www.jcu.edu.au/",
        )
        cfg.discovery.scrape_do_skip_fallbacks = True
        cfg.discovery.scrape_do_render = True

        result, rendered_calls, html = self._run_fetch(m, cfg)

        assert result == html
        assert rendered_calls == [True], (
            f"expected a single render=True call, got {rendered_calls} — "
            "the doomed static attempt must be skipped entirely"
        )

    def test_static_first_when_flag_unset(self):
        import app.services.scraper.http_fetcher as m
        from app.services.scraper.config.schema import UniConfig

        cfg = UniConfig(
            slug="jcu", name="JCU", base_url="https://www.jcu.edu.au",
            scrape_url="https://www.jcu.edu.au/",
        )
        cfg.discovery.scrape_do_skip_fallbacks = True
        cfg.discovery.scrape_do_render = False

        result, rendered_calls, html = self._run_fetch(m, cfg)

        assert result == html
        assert rendered_calls == [False, True], (
            f"expected static-then-render escalation, got {rendered_calls}"
        )

    def test_jcu_yaml_sets_both_flags(self):
        from app.services.scraper.config.loader import get_config_for_host

        cfg = get_config_for_host(
            hostname="www.jcu.edu.au",
            name="James Cook University",
            scrape_url="https://www.jcu.edu.au/",
        )
        assert cfg.discovery.scrape_do_skip_fallbacks is True
        assert cfg.discovery.scrape_do_render is True
        assert cfg.extraction.scrape_do_render is True
        assert cfg.extraction.scrape_do_skip_fallbacks is True

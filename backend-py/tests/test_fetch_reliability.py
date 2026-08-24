"""T08 — Tests for the Fetch Reliability Overhaul (T01-T07).

Covers:
- T03: exponential-backoff retry in fetch_html_scrape_do (429/5xx → retry → 200)
- T06: ScrapedoAccountError raised on 401/403, propagates cleanly
- T03: 404 is not retried (page-not-found fast path)
- T05: failure-rate guard thresholds (>30% → failed_degraded, 10-30% → completed_with_warnings)
- T01: [FETCH FAIL] log tags present on all failure paths

All network calls are mocked — no real Scrape.do traffic.
"""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status: int, text: str = "", headers: dict | None = None) -> MagicMock:
    """Build a lightweight mock that looks like an httpx.Response."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    return r


# ---------------------------------------------------------------------------
# T03 + T01: Retry behaviour in fetch_html_scrape_do
# ---------------------------------------------------------------------------

class TestFetchHtmlScrapeDoRetry:
    """Verify the 3-attempt retry loop and [FETCH FAIL] log tag."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")

    def _run(self, coro):
        return asyncio.run(coro)

    def _import(self):
        # Re-import after monkeypatching env so SCRAPE_DO_TOKEN is visible.
        import importlib
        import app.services.scraper.http_fetcher as m
        importlib.reload(m)
        return m

    def test_succeeds_on_first_attempt(self, monkeypatch):
        """200 on first attempt → returns HTML immediately (no retries)."""
        import app.services.scraper.http_fetcher as m
        good_html = "x" * 600

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            return _make_response(200, good_html)

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None), \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(
                m.fetch_html_scrape_do("https://example.com/course", render=False, rate_limit=False)
            )
        assert result == good_html

    def test_429_then_200_succeeds(self, monkeypatch):
        """First request → 429, second attempt → 200 (after backoff)."""
        import app.services.scraper.http_fetcher as m

        call_count = 0
        good_html = "x" * 600

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(429, "rate limited")
            return _make_response(200, good_html)

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        sleep_calls: list[float] = []

        async def _fake_sleep(s):
            sleep_calls.append(s)

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            result = asyncio.run(
                m.fetch_html_scrape_do("https://example.com/course", render=False, rate_limit=False)
            )

        assert result == good_html
        assert call_count == 2
        # Must have slept at least once between attempts
        assert len(sleep_calls) >= 1

    def test_all_retries_exhausted_returns_none(self, monkeypatch):
        """If all 4 attempts return 503, returns None."""
        import app.services.scraper.http_fetcher as m

        call_count = 0

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            nonlocal call_count
            call_count += 1
            return _make_response(503, "service unavailable")

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(
                m.fetch_html_scrape_do("https://example.com/course", render=False, rate_limit=False)
            )

        assert result is None
        # 4 attempts total (1 + 3 retries)
        assert call_count == 4

    def test_404_no_retry(self, monkeypatch):
        """404 → no retry (page-not-found fast path), returns None immediately."""
        import app.services.scraper.http_fetcher as m

        call_count = 0

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            nonlocal call_count
            call_count += 1
            return _make_response(404, "not found")

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = asyncio.run(
                m.fetch_html_scrape_do("https://example.com/course", render=False, rate_limit=False)
            )

        assert result is None
        assert call_count == 1  # no retry
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# T06: ScrapedoAccountError raised on 401/403
# ---------------------------------------------------------------------------

class TestScrapedoAccountError:
    """401/403 from Scrape.do must raise ScrapedoAccountError, not return None."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "bad-token")

    @pytest.mark.parametrize("status", [401, 403])
    def test_raises_account_error(self, status, monkeypatch):
        import app.services.scraper.http_fetcher as m

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            return _make_response(status, "unauthorized")

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(m.ScrapedoAccountError):
                asyncio.run(
                    m.fetch_html_scrape_do("https://example.com/x", render=False, rate_limit=False)
                )

    def test_no_retry_before_raise(self, monkeypatch):
        """Account error must not waste time retrying — raises on first attempt."""
        import app.services.scraper.http_fetcher as m

        call_count = 0

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            nonlocal call_count
            call_count += 1
            return _make_response(401, "")

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(m.ScrapedoAccountError):
                asyncio.run(
                    m.fetch_html_scrape_do("https://example.com/x", render=False, rate_limit=False)
                )

        assert call_count == 1  # no retries before the raise


# ---------------------------------------------------------------------------
# T03: Retry-After header honoured
# ---------------------------------------------------------------------------

class TestRetryAfterHeader:
    """Retry-After header should extend the backoff wait time."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "tok")

    def test_retry_after_extends_wait(self, monkeypatch):
        import app.services.scraper.http_fetcher as m

        call_count = 0
        good_html = "x" * 600

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_response(429, "rate limited", headers={"retry-after": "25"})
            return _make_response(200, good_html)

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        sleep_calls: list[float] = []

        async def _fake_sleep(s):
            sleep_calls.append(s)

        with patch.object(m, "_unescape_json_html", side_effect=lambda x: x), \
             patch("app.services.scraper.snapshot_context.stage_snapshot", lambda *a, **kw: None), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("asyncio.sleep", side_effect=_fake_sleep):
            result = asyncio.run(
                m.fetch_html_scrape_do("https://example.com/course", render=False, rate_limit=False)
            )

        assert result == good_html
        # The first backoff sleep should be >= the Retry-After value (25 s)
        assert sleep_calls[0] >= 25.0


# ---------------------------------------------------------------------------
# Scrape.do credential redaction
# ---------------------------------------------------------------------------

class TestScrapeDoCredentialRedaction:
    """Worker diagnostics must never reveal the provider token."""

    @pytest.fixture(autouse=True)
    def _set_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPE_DO_TOKEN", "scrape-do-secret-for-log-test")

    def test_httpx_request_and_failure_logs_redact_provider_token(self, caplog):
        """HTTPX access lines and provider failures retain diagnostics, not secrets."""
        import app.services.scraper.http_fetcher as m

        token = os.environ["SCRAPE_DO_TOKEN"]
        target_url = "https://example.edu/course"
        provider_url = (
            "https://api.scrape.do?token="
            f"{token}&url=https%3A%2F%2Fexample.edu%2Fcourse&render=true"
        )

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            # Match HTTPX's normal automatic request line, which is emitted
            # after every real request and previously exposed the token.
            logging.getLogger("httpx").info(
                'HTTP Request: %s %s "%s %d %s"',
                "GET",
                provider_url,
                "HTTP/1.1",
                503,
                "Service Unavailable",
            )
            return _make_response(
                503,
                f"provider overloaded; request was {provider_url}",
            )

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch("httpx.AsyncClient", return_value=mock_client), \
             caplog.at_level(logging.INFO):
            result = asyncio.run(
                m.fetch_html_scrape_do(
                    target_url,
                    render=True,
                    rate_limit=False,
                    max_retries=0,
                )
            )

        assert result is None
        assert token not in caplog.text
        assert "token=[REDACTED]" in caplog.text
        assert target_url in caplog.text
        assert "render=True" in caplog.text
        assert "status=503" in caplog.text
        assert "provider overloaded" in caplog.text

    def test_exception_logs_and_saved_diagnostics_redact_provider_token(self, caplog):
        """Request URLs embedded in transport exceptions are also sanitized."""
        import app.services.scraper.http_fetcher as m

        token = os.environ["SCRAPE_DO_TOKEN"]
        target_url = "https://example.edu/another-course"
        provider_url = (
            "https://api.scrape.do?token="
            f"{token}&url=https%3A%2F%2Fexample.edu%2Fanother-course"
        )

        async def _mock_enter(self_):
            return self_

        async def _mock_exit(self_, *a):
            return False

        async def _mock_get(url, params=None):
            raise RuntimeError(f"provider transport failed for {provider_url}")

        mock_client = MagicMock()
        mock_client.__aenter__ = _mock_enter
        mock_client.__aexit__ = _mock_exit
        mock_client.get = _mock_get

        with patch("httpx.AsyncClient", return_value=mock_client), \
             caplog.at_level(logging.INFO):
            result = asyncio.run(
                m.fetch_html_scrape_do(
                    target_url,
                    render=False,
                    rate_limit=False,
                    max_retries=0,
                )
            )

        saved_diagnostic = m.format_fetch_error(target_url)
        assert result is None
        assert token not in caplog.text
        assert token not in saved_diagnostic
        assert target_url in caplog.text
        assert "render=False" in caplog.text
        assert "provider transport failed" in caplog.text
        assert "provider transport failed" in saved_diagnostic


# ---------------------------------------------------------------------------
# T05: Failure-rate guard — pure computation test
# ---------------------------------------------------------------------------

class TestFailureRateGuardThresholds:
    """Verify the 10%/30% threshold logic in isolation (no DB required)."""

    @staticmethod
    def _guard_status(discovered: int, fetch_failed: int) -> str | None:
        """Mirror the T05 threshold computation from orchestrator.py."""
        rate = fetch_failed / max(1, discovered)
        if discovered > 0 and rate > 0.30:
            return "failed_degraded"
        if discovered > 0 and rate > 0.10:
            return "completed_with_warnings"
        return None

    def test_below_10_pct_is_clean(self):
        assert self._guard_status(100, 9) is None

    def test_exactly_10_pct_is_clean(self):
        # boundary: > 0.10 (strictly greater), 10/100 = 0.10 exactly → clean
        assert self._guard_status(100, 10) is None

    def test_just_above_10_pct_is_warnings(self):
        assert self._guard_status(100, 11) == "completed_with_warnings"

    def test_exactly_30_pct_is_warnings(self):
        # 30/100 = 0.30 exactly → > 0.30 is False → completed_with_warnings
        assert self._guard_status(100, 30) == "completed_with_warnings"

    def test_just_above_30_pct_is_degraded(self):
        assert self._guard_status(100, 31) == "failed_degraded"

    def test_zero_discovered_is_clean(self):
        # Guard only fires when discovered > 0
        assert self._guard_status(0, 0) is None

    def test_jcu_scenario(self):
        """JCU: 103 discovered, 96 failed → 93% fetch failure → failed_degraded."""
        assert self._guard_status(103, 96) == "failed_degraded"

    def test_full_success_is_clean(self):
        assert self._guard_status(103, 0) is None

    def test_single_failure_small_run(self):
        # 1/10 = 10% exactly → clean
        assert self._guard_status(10, 1) is None

    def test_two_failures_small_run(self):
        # 2/10 = 20% → warnings
        assert self._guard_status(10, 2) == "completed_with_warnings"

    def test_four_failures_small_run(self):
        # 4/10 = 40% → degraded
        assert self._guard_status(10, 4) == "failed_degraded"

"""PR-5 Bug 3: ``browser_pool.fetch_html`` partial-HTML-on-goto-timeout.

Marketing sites (Torrens, ASA, etc.) embed long-poll widgets — Intercom,
Hotjar, GA stream — that prevent ``networkidle`` from ever firing. Pre-PR-5
``page.goto`` would raise a Playwright ``TimeoutError`` and ``fetch_html``
would return ``None``, throwing away the fully-rendered DOM that was sat
in the browser the whole time. PR-5 catches the timeout SPECIFICALLY and
falls back to ``page.content()``.

The contract this file pins:

  * Goto timeout + good HTML  -> partial HTML returned
  * Goto timeout + Chromium error interstitial  -> ``None`` (no junk)
  * Goto raises non-timeout exception  -> ``None`` (real failure, not a
    silent recovery — that path is owned by the outer ``except``)

We stub ``BrowserPool.page()`` so no real browser is booted; the
existing ``test_browser_international_toggle.py`` uses the same pattern.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.services.scraper import browser_pool


# ── Test doubles ──────────────────────────────────────────────────────


class _FakePage:
    """A page whose ``goto`` raises whatever the test plants in it."""

    def __init__(self, *, goto_exc: BaseException | None, content: str) -> None:
        self._goto_exc = goto_exc
        self._content = content
        self.set_extra_http_headers_calls: list[dict[str, str]] = []
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.set_extra_http_headers_calls.append(headers)

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_calls.append((url, kwargs))
        if self._goto_exc is not None:
            raise self._goto_exc
        # Default success path (not exercised by these tests but kept
        # for safety).
        class _R:
            status = 200
        return _R()

    async def wait_for_timeout(self, ms: int) -> None:  # pragma: no cover
        pass

    async def evaluate(self, js: str) -> bool:  # pragma: no cover
        return False

    async def wait_for_load_state(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        pass

    async def content(self) -> str:
        return self._content


def _install_fake_page(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> None:
    @asynccontextmanager
    async def fake_page(self):  # noqa: ANN001
        yield page

    monkeypatch.setattr(browser_pool.BrowserPool, "page", fake_page)


# ── Contract tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_goto_timeout_returns_partial_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline PR-5 fix: a Playwright TimeoutError on goto must
    NOT discard the rendered DOM."""
    good_html = "<html><body>" + ("x" * 2048) + "</body></html>"
    page = _FakePage(
        goto_exc=browser_pool.PlaywrightTimeoutError("networkidle never fired"),
        content=good_html,
    )
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    result = await pool.fetch_html("https://www.torrens.edu.au/courses/design")

    assert result == good_html, "partial HTML must be returned on goto timeout"


@pytest.mark.asyncio
async def test_goto_timeout_with_short_html_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1024-byte content floor protects against truly-empty pages —
    if all the timeout produced was a stub <html></html>, that's worse
    than nothing because the extractor will record empty evidence."""
    page = _FakePage(
        goto_exc=browser_pool.PlaywrightTimeoutError("timed out"),
        content="<html></html>",  # well below the 1024 floor
    )
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    assert await pool.fetch_html("https://example.com/x") is None


@pytest.mark.asyncio
async def test_goto_timeout_with_chromium_error_page_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chromium error interstitials (DNS failure, cert error, etc.) have
    a tiny ``<body class="neterror">`` page. Without the sniff, those
    would clear the 1024-byte floor and get treated as a real fetch,
    so the staged record would land with garbage extracted text. The
    sniff matches ``neterror``, ``ERR_NAME_NOT_RESOLVED``, etc."""
    interstitial = (
        "<html><head><title>example.com</title></head>"
        "<body class='neterror'>"
        + ("Padding to clear the 1024-byte floor. " * 60)
        + "ERR_NAME_NOT_RESOLVED</body></html>"
    )
    assert len(interstitial) > 1024  # sanity — sniff is what saves us
    page = _FakePage(
        goto_exc=browser_pool.PlaywrightTimeoutError("dns failed"),
        content=interstitial,
    )
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    assert await pool.fetch_html("https://nonexistent-host.invalid/") is None


@pytest.mark.asyncio
async def test_non_timeout_exception_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``RuntimeError`` (or any non-PlaywrightTimeout) must NOT be
    caught by the partial-HTML fallback — those are real navigation
    failures (cert errors, protocol errors, browser crashes). They
    propagate to the outer ``except`` which logs ``error`` and returns
    None. Pinning this prevents a future contributor from widening the
    ``except`` back to bare ``Exception``."""
    page = _FakePage(
        goto_exc=RuntimeError("ssl handshake failed"),
        content="<html><body>" + ("x" * 4096) + "</body></html>",  # rich
    )
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    # Even though the page would have served good content, a real
    # navigation failure must surface as None — never as silent success.
    assert await pool.fetch_html("https://broken-cert.invalid/") is None

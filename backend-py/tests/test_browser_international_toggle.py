"""Unit tests for the ``click_international`` toggle in browser_pool.fetch_html (T005).

We cannot spin up real Playwright in CI, so the test stubs out the
``BrowserPool.page()`` async-context-manager with a fake page that records
every call. This isolates the *logic* under test (toggle JS routing,
post-click waits, host gating) from Playwright runtime concerns.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.services.scraper import browser_pool


def _run(coro):  # noqa: ANN001
    # Fresh event loop per call — see the matching note in
    # tests/test_home_page_redirect.py::_run. Without this, every test
    # in this file fails when an async pytest-asyncio test ran first.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _FakePage:
    """Mimics the subset of Playwright's Page API the toggle code uses."""

    def __init__(self, *, evaluate_returns: bool = True) -> None:
        self.evaluate_calls: list[str] = []
        self.wait_for_load_state_calls: list[tuple[str, int | None]] = []
        self.wait_for_timeout_calls: list[int] = []
        self.set_extra_http_headers_calls: list[dict[str, str]] = []
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self._evaluate_returns = evaluate_returns

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.set_extra_http_headers_calls.append(headers)

    async def goto(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.goto_calls.append((url, kwargs))
        return _FakeResponse(200)

    async def wait_for_timeout(self, ms: int) -> None:
        self.wait_for_timeout_calls.append(ms)

    async def evaluate(self, js: str) -> bool:
        self.evaluate_calls.append(js)
        return self._evaluate_returns

    async def wait_for_load_state(
        self, state: str, *, timeout: int | None = None
    ) -> None:
        self.wait_for_load_state_calls.append((state, timeout))

    async def content(self) -> str:
        return "<html><body>fake</body></html>"


def _install_fake_page(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> None:
    """Replace ``BrowserPool.page()`` so it yields ``page`` instead of
    booting a real browser."""
    @asynccontextmanager
    async def fake_page(self):  # noqa: ANN001
        yield page

    monkeypatch.setattr(browser_pool.BrowserPool, "page", fake_page)


def test_toggle_default_off_does_not_evaluate_js(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``click_international=True`` the toggle JS must never run.

    Critical because the toggle JS performs DOM mutations and any
    accidental opt-in would change the rendered HTML for every host.
    """
    page = _FakePage()
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    _run(pool.fetch_html("https://example.com/course/foo"))

    assert page.evaluate_calls == [], (
        "evaluate() must not be called when click_international is False"
    )


def test_toggle_on_evaluates_international_js(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``click_international=True`` triggers exactly one evaluate() call
    with the canonical toggle JS."""
    page = _FakePage(evaluate_returns=True)
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    _run(
        pool.fetch_html(
            "https://vit.edu.au/courses/bachelor-of-business",
            click_international=True,
        )
    )

    assert len(page.evaluate_calls) == 1
    js = page.evaluate_calls[0]
    # Spot-check the JS body — these are the toggle's load-bearing
    # selectors / regexes that the Node port mirrors.
    assert "international" in js.lower()
    assert "input[type=\"radio\"]" in js or "type=\"radio\"" in js
    # Strategy 2 (text-based) must constrain to a strict label so we
    # don't click an unrelated nav link.
    assert "international(?:\\s+(?:students?|fees?|applicants?))?" in js


def test_toggle_clicked_waits_for_networkidle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the toggle JS reports ``true`` (click landed), the code must
    wait for networkidle so any post-click XHR / re-render completes
    before we read ``page.content()``."""
    page = _FakePage(evaluate_returns=True)
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    _run(
        pool.fetch_html(
            "https://vit.edu.au/courses/bachelor-of-business",
            click_international=True,
        )
    )

    assert any(
        state == "networkidle" for state, _ in page.wait_for_load_state_calls
    ), "expected wait_for_load_state('networkidle') after a successful click"
    # And a post-click static settle (~1.2s) for slow re-renders.
    assert 1200 in page.wait_for_timeout_calls


def test_toggle_unclicked_skips_post_click_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the toggle JS returns ``false`` (no matching element), the
    code must NOT pay the networkidle + 1.2s wait — that would penalise
    every page that lacks the toggle."""
    page = _FakePage(evaluate_returns=False)
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    _run(
        pool.fetch_html(
            "https://csu.edu.au/courses/foo",
            click_international=True,
        )
    )

    # JS still evaluated — but nothing followed.
    assert page.evaluate_calls != []
    assert page.wait_for_load_state_calls == []
    assert 1200 not in page.wait_for_timeout_calls


def test_toggle_evaluate_failure_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``page.evaluate()`` raises (e.g. context destroyed mid-click),
    the failure must not propagate — we still want the post-settle HTML."""
    class _RaisingPage(_FakePage):
        async def evaluate(self, js: str) -> bool:
            raise RuntimeError("Execution context was destroyed")

    page = _RaisingPage()
    _install_fake_page(monkeypatch, page)

    pool = browser_pool.BrowserPool()
    html = _run(
        pool.fetch_html(
            "https://vit.edu.au/courses/foo",
            click_international=True,
        )
    )
    # We still got HTML back even though the toggle errored.
    assert html == "<html><body>fake</body></html>"


def test_per_course_browser_passes_toggle_only_for_vit_hosts() -> None:
    """The host whitelist gate in per_course_browser._needs_international_toggle
    must answer True for vit.edu.au (and subdomains thereof) and False
    for everything else. This is the cheap-but-load-bearing host gate
    that prevents the toggle JS from running on hosts that don't have
    a fee toggle (where it would still cost a wasted evaluate())."""
    from app.services.scraper.per_course_browser import _needs_international_toggle

    assert _needs_international_toggle("https://vit.edu.au/courses/mba") is True
    assert _needs_international_toggle(
        "https://www.vit.edu.au/courses/mba"
    ) is True
    assert _needs_international_toggle(
        "https://study.csu.edu.au/courses/nursing"
    ) is False
    assert _needs_international_toggle(
        "https://usq.edu.au/study/degrees/bachelor-of-nursing"
    ) is False

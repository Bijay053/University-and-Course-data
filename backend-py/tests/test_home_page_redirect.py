"""Unit tests for the home-page → course-listing redirect detector.

Covers:
* :func:`_is_home_page` recognises only true marketing roots.
* :func:`detect_course_listing_page` honours the 3-step pipeline:
  - Step 1: high-priority HEAD probe (mocked).
  - Step 2: link-scan with weighted scoring (when step 1 fails).
  - Step 3: broad HEAD-probe fallback (when steps 1 & 2 fail).
* :func:`expand_course_list_with_categories` only fires on
  expand-eligible listing paths and merges new candidates from
  category-filter variants.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.scraper import home_page_redirect


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── _is_home_page ───────────────────────────────────────────────────────────
def test_is_home_page_recognises_root() -> None:
    assert home_page_redirect._is_home_page("https://vit.edu.au/")
    assert home_page_redirect._is_home_page("https://vit.edu.au")
    assert home_page_redirect._is_home_page("https://www.example.com/index.html")


def test_is_home_page_rejects_subpaths() -> None:
    assert not home_page_redirect._is_home_page("https://vit.edu.au/course-list")
    assert not home_page_redirect._is_home_page("https://vit.edu.au/study/")
    assert not home_page_redirect._is_home_page("https://vit.edu.au/courses?type=mba")


# ── detect_course_listing_page ──────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status: int, url: str | None = None) -> None:
        self.status_code = status
        self.url = url or ""


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` that lets each test
    program a deterministic HEAD-probe response per URL."""

    def __init__(self, head_responses: dict[str, _FakeResponse]) -> None:
        self._heads = head_responses

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def head(self, url: str, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        if url in self._heads:
            return self._heads[url]
        # Default to 404 — unknown URL means "not a course listing".
        return _FakeResponse(404)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        return _FakeResponse(404)


def test_detect_uses_high_priority_head_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``/course-list`` HEAD-probes 200, that wins immediately
    without parsing the home-page HTML or trying lower-priority paths."""

    def _client_factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(
            {
                "https://vit.edu.au/study/degrees-and-courses": _FakeResponse(404),
                "https://vit.edu.au/degrees": _FakeResponse(404),
                "https://vit.edu.au/course-list": _FakeResponse(
                    200, url="https://vit.edu.au/course-list"
                ),
            }
        )

    monkeypatch.setattr(home_page_redirect.httpx, "AsyncClient", _client_factory)
    result = _run(home_page_redirect.detect_course_listing_page("https://vit.edu.au/", ""))
    assert result == "https://vit.edu.au/course-list"


def test_detect_falls_back_to_link_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """When all HEAD probes return 404, the link-scan picks the highest-
    scoring anchor on the home page."""

    def _client_factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient({})  # everything 404s

    monkeypatch.setattr(home_page_redirect.httpx, "AsyncClient", _client_factory)
    html = """
    <html><body>
      <a href="/about">About</a>
      <a href="/blog">Blog</a>
      <a href="/find-a-course">Find a course</a>
      <a href="/contact">Contact us</a>
    </body></html>
    """
    result = _run(
        home_page_redirect.detect_course_listing_page("https://example.edu.au/", html)
    )
    assert result == "https://example.edu.au/find-a-course"


def test_detect_returns_none_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _client_factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient({})

    monkeypatch.setattr(home_page_redirect.httpx, "AsyncClient", _client_factory)
    html = "<html><body><a href='/about'>About</a></body></html>"
    result = _run(
        home_page_redirect.detect_course_listing_page("https://example.edu.au/", html)
    )
    assert result is None


# ── expand_course_list_with_categories ──────────────────────────────────────
def test_expand_skips_non_listing_paths() -> None:
    existing = [{"url": "https://vit.edu.au/courses/mba", "name": "MBA"}]
    result = _run(
        home_page_redirect.expand_course_list_with_categories(
            "https://vit.edu.au/about", existing
        )
    )
    assert result == existing  # short-circuit returns the original list


def test_expand_merges_new_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a /course-list listing URL, an HTTP HEAD-probe success on a
    category variant fetches the page and merges any new course links."""

    bbus_html = """
    <html><body>
      <a href="/courses/bbus-marketing">BBus - Marketing Specialisation</a>
      <a href="/courses/bbus-hr">BBus - HR Specialisation</a>
      <a href="/about">About</a>
    </body></html>
    """

    def _client_factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        # Only the bbus variant 200s; everything else 404s.
        return _FakeAsyncClient(
            {
                "https://vit.edu.au/course-list?course_categories[0]=bbus": _FakeResponse(200),
            }
        )

    async def _fake_fetch(url: str) -> str:
        if url == "https://vit.edu.au/course-list?course_categories[0]=bbus":
            return bbus_html
        return ""

    monkeypatch.setattr(home_page_redirect.httpx, "AsyncClient", _client_factory)
    monkeypatch.setattr(home_page_redirect, "fetch_html", _fake_fetch)

    existing = [{"url": "https://vit.edu.au/courses/mba", "name": "MBA"}]
    result = _run(
        home_page_redirect.expand_course_list_with_categories(
            "https://vit.edu.au/course-list", existing
        )
    )
    urls = {c["url"] for c in result}
    assert "https://vit.edu.au/courses/mba" in urls  # original preserved
    assert "https://vit.edu.au/courses/bbus-marketing" in urls
    assert "https://vit.edu.au/courses/bbus-hr" in urls
    # /about should NOT make it through — it doesn't look like a course.
    assert "https://vit.edu.au/about" not in urls

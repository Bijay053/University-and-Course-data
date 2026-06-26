"""Regression test: skip_browser_rescue / skip_per_course_browser gate the
[BROWSER↑] HTTP-failure fallback.

Task #245 root cause: `skip_browser_rescue: true` in Ulster's YAML was only
checked inside the *sparse-static rescue* block (post-Gemini).  The initial
HTTP-failure browser fallback at `if not html:` had NO such check, so 186
browser retries still fired even though the flag was set — each wasting
10-30 s on a Cloudflare-403 response.

These tests verify the fix: both `skip_browser_rescue` and
`skip_per_course_browser` now short-circuit the browser fallback and emit
`[BROWSER↑ SKIPPED]` instead of calling the browser pool.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.schema import (
    ExtractionConfig,
    UniConfig,
)


def _make_uni_config(**extraction_kwargs: Any) -> UniConfig:
    """Build a minimal UniConfig with the given extraction fields."""
    extr = ExtractionConfig(**extraction_kwargs)
    return UniConfig(
        slug="ulster_2176",
        name="Ulster University",
        base_url="https://www.ulster.ac.uk",
        scrape_url="https://www.ulster.ac.uk/courses",
        extraction=extr,
    )


@pytest.mark.asyncio
async def test_skip_browser_rescue_prevents_browser_fallback(monkeypatch: Any) -> None:
    """skip_browser_rescue=true must suppress the [BROWSER↑] browser retry when
    HTTP fetch returns None.  The browser pool must never be called."""
    set_uni_config(_make_uni_config(skip_browser_rescue=True))

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
        result = await extract_course(
            "https://www.ulster.ac.uk/courses/202627/test-course-123",
            emit=_emit,
        )

    assert browser_called == [], (
        "Browser pool must NOT be called when skip_browser_rescue=true"
    )
    skip_msgs = [e["message"] for e in emitted if "[BROWSER↑ SKIPPED]" in e["message"]]
    assert skip_msgs, (
        f"Expected [BROWSER↑ SKIPPED] emit event; got events: {[e['message'] for e in emitted]}"
    )
    assert "skip_browser_rescue=true" in skip_msgs[0], (
        f"Log must attribute the skip to skip_browser_rescue; got: {skip_msgs[0]}"
    )
    assert result.get("error") in ("fetch_failed", "fetch_failed_empty_text"), (
        f"Expected fetch_failed result; got: {result}"
    )


@pytest.mark.asyncio
async def test_skip_per_course_browser_prevents_browser_fallback(monkeypatch: Any) -> None:
    """skip_per_course_browser=true must also suppress the [BROWSER↑] retry."""
    set_uni_config(_make_uni_config(skip_per_course_browser=True))

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
        result = await extract_course(
            "https://www.ulster.ac.uk/courses/202627/another-course-456",
            emit=_emit,
        )

    assert browser_called == [], (
        "Browser pool must NOT be called when skip_per_course_browser=true"
    )
    skip_msgs = [e["message"] for e in emitted if "[BROWSER↑ SKIPPED]" in e["message"]]
    assert skip_msgs, (
        f"Expected [BROWSER↑ SKIPPED] emit event; got: {[e['message'] for e in emitted]}"
    )
    assert "skip_per_course_browser=true" in skip_msgs[0], (
        f"Log must attribute the skip to skip_per_course_browser; got: {skip_msgs[0]}"
    )


@pytest.mark.asyncio
async def test_skip_browser_rescue_prioritised_over_skip_per_course_browser(
    monkeypatch: Any,
) -> None:
    """When BOTH flags are set, skip_browser_rescue is named in the log line
    (it is checked first and is the more semantically targeted flag)."""
    set_uni_config(
        _make_uni_config(skip_browser_rescue=True, skip_per_course_browser=True)
    )

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
        await extract_course(
            "https://www.ulster.ac.uk/courses/202627/combined-flags-789",
            emit=_emit,
        )

    skip_msgs = [e["message"] for e in emitted if "[BROWSER↑ SKIPPED]" in e["message"]]
    assert skip_msgs, "Expected [BROWSER↑ SKIPPED] event"
    assert "skip_browser_rescue=true" in skip_msgs[0], (
        f"skip_browser_rescue should be named first; got: {skip_msgs[0]}"
    )

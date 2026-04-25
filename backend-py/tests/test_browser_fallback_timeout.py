"""Per-course browser fallback hard-timeout regression test.

Prod incident 2026-04-24: Celery worker job_2dc0ba6bf4c9 sat at 0/10 for
32 minutes because Playwright wedged on a single course page (likely an
infinite redirect or websocket loop). browser_pool.fetch_html sets a 30s
timeout on page.goto, but page.content(), the per-call semaphore, and
the context teardown all have NO ceiling — so a single hung page can
freeze the entire pipeline. The fix wraps the entire fetch in
asyncio.wait_for with a hard cap and logs a warning before aborting.

These tests pin that contract: a hung browser_pool.fetch_html must NOT
block maybe_browser_refetch past the per-host outer ceiling returned
by `_browser_config_for`, and the abort must emit a warning + a typed
status event so the UI shows the timeout instead of silently dropping
the run.

PR-5 Bug 3 changed the timeout knob from a module-level constant to
the per-host 4-tuple (`wait_until, settle_ms, outer_sec, goto_ms`)
returned by `_browser_config_for`. We monkeypatch that helper to drop
the ceiling to 0.5s for fast test runs.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper import per_course_browser


@pytest.mark.asyncio
async def test_browser_fallback_aborts_on_timeout(monkeypatch, caplog):
    """A wedged fetch_html must be cancelled and the function must return cleanly."""

    async def _hang_forever(url: str, **kw):  # noqa: ANN001
        # Mirrors the prod failure — the call never resolves.
        await asyncio.sleep(3600)

    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _hang_forever)
    # Bring the outer ceiling down to 0.5s so the test runs fast. The
    # product value (20s default / 30s VIT) is the same code path —
    # just longer.
    monkeypatch.setattr(
        per_course_browser,
        "_browser_config_for",
        lambda url: ("domcontentloaded", 0, 0.5, 200),
    )

    emitted: list[dict] = []

    async def _emit(event, message, **kw):  # noqa: ANN001
        emitted.append({"event": event, "message": message, **kw})

    payload: dict = {}  # all english slots empty -> fallback engages

    with caplog.at_level("WARNING"):
        filled, evidence, rendered = await per_course_browser.maybe_browser_refetch(
            "https://example.test/course",
            payload,
            emit=_emit,
        )

    assert filled == {}
    assert evidence == []
    assert rendered is None
    # The pre-abort breadcrumb the prod incident was missing.
    assert any(
        "browser fallback exceeded" in r.getMessage() for r in caplog.records
    ), "expected log.warning before timeout abort"
    # Typed status event so the UI can render the timeout in the live log.
    assert any(
        ev.get("kind") == "per_course_browser_timeout"
        and ev.get("url") == "https://example.test/course"
        for ev in emitted
    ), f"expected per_course_browser_timeout status event, got {emitted!r}"


@pytest.mark.asyncio
async def test_browser_fallback_skipped_when_already_filled(monkeypatch):
    """Sanity check: when the english slots are populated we must NOT call
    fetch_html at all — the timeout path should be unreachable."""

    called = []

    async def _track(url: str, **kw):  # noqa: ANN001
        called.append(url)
        return "<html></html>"

    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _track)

    payload = {"ielts_overall": 6.5}
    filled, evidence, rendered = await per_course_browser.maybe_browser_refetch(
        "https://example.test/course",
        payload,
    )
    assert called == []
    assert filled == {} and evidence == [] and rendered is None

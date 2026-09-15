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
from app.services.scraper.extractors.base import ExtractionResult


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
        filled, evidence, rendered, override = await per_course_browser.maybe_browser_refetch(
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
    filled, evidence, rendered, override = await per_course_browser.maybe_browser_refetch(
        "https://example.test/course",
        payload,
    )
    assert called == []
    assert filled == {} and evidence == [] and rendered is None and override is False


@pytest.mark.asyncio
async def test_force_browser_skips_uow_shape_when_all_required_fields_complete(
    monkeypatch,
):
    called = []

    async def _track(url: str, **kw):  # noqa: ANN001
        called.append(url)
        return "<html></html>"

    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _track)
    payload = {
        "international_fee": 19_488,
        "ielts_overall": 6.5,
        "duration": 3,
        "intake_months": ["March", "July"],
        "course_location": "Wollongong",
        "study_mode": "On Campus",
    }

    result = await per_course_browser.maybe_browser_refetch(
        "https://www.uow.edu.au/study/courses/example/",
        payload,
        force=True,
    )

    assert called == []
    assert result == ({}, [], None, False)


@pytest.mark.asyncio
async def test_uts_required_audience_actions_override_complete_static_payload(
    monkeypatch,
):
    called = []

    class _Extraction:
        actions = [
            {"click_text": "Domestic", "required": True},
            {"click_text": "International Student", "required": True},
        ]
        skip_per_course_browser = False

    class _Config:
        extraction = _Extraction()

    async def _track(url: str, **kw):  # noqa: ANN001
        called.append((url, kw.get("actions")))
        return "<html><body>International Student</body></html>"

    async def _international_values(rendered, url, payload, override=False):  # noqa: ANN001
        assert override is True
        return (
            {
                "international_fee": 44_778,
                "fee_term": "Annual",
            },
            [],
        )

    monkeypatch.setattr(per_course_browser, "get_uni_config", lambda: _Config())
    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _track)
    monkeypatch.setattr(per_course_browser, "_extended_extract", _international_values)

    payload = {
        "international_fee": 43_900,
        "ielts_overall": 7,
        "duration": 2,
        "intake_months": ["January", "July"],
        "course_location": "Sydney",
        "study_mode": "On Campus",
    }
    result = await per_course_browser.maybe_browser_refetch(
        "https://www.uts.edu.au/courses/master-of-indigenous-health-research",
        payload,
        force=True,
    )

    assert called == [
        (
            "https://www.uts.edu.au/courses/master-of-indigenous-health-research",
            _Extraction.actions,
        )
    ]
    assert result[0]["international_fee"] == 44_778
    assert result[0]["fee_term"] == "Annual"
    assert result[3] is True
    assert "www.uts.edu.au" in per_course_browser._EXTENDED_EXTRACT_HOSTS


@pytest.mark.asyncio
async def test_uow_fee_missing_still_runs_force_browser(monkeypatch):
    called = []

    async def _track(url: str, **kw):  # noqa: ANN001
        called.append(url)
        return "<html><body><h1>Example course</h1></body></html>"

    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _track)
    payload = {
        "international_fee": None,
        "ielts_overall": 6.5,
        "duration": 3,
        "intake_months": ["March", "July"],
        "course_location": "Wollongong",
        "study_mode": "On Campus",
    }

    await per_course_browser.maybe_browser_refetch(
        "https://www.uow.edu.au/study/courses/example/",
        payload,
        force=True,
    )

    assert called == ["https://www.uow.edu.au/study/courses/example/"]


@pytest.mark.asyncio
async def test_configured_full_rendered_extraction_recovers_missing_fee(
    monkeypatch,
):
    class _Extraction:
        actions = []
        skip_per_course_browser = False
        full_rendered_extraction = True

    class _Config:
        extraction = _Extraction()

    async def _render(url: str, **kw):  # noqa: ANN001
        return "<html><body>International tuition fee: $42,500</body></html>"

    async def _fee_extract(rendered: str, url: str):  # noqa: ANN001
        assert "42,500" in rendered
        return [
            ExtractionResult(
                field_key="international_fee",
                normalized={
                    "international_fee": 42_500,
                    "fee_currency": "AUD",
                    "fee_term": "Annual",
                },
                confidence=0.9,
                snippet="International tuition fee: $42,500",
            )
        ]

    async def _empty_extract(rendered: str, url: str):  # noqa: ANN001
        return []

    monkeypatch.setattr(per_course_browser, "get_uni_config", lambda: _Config())
    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _render)
    monkeypatch.setattr(per_course_browser.fee, "extract", _fee_extract)
    for extractor in (
        per_course_browser.course_name_extractor,
        per_course_browser.english_test,
        per_course_browser.intake,
        per_course_browser.duration,
        per_course_browser.location,
        per_course_browser.study_mode,
    ):
        monkeypatch.setattr(extractor, "extract", _empty_extract)

    filled, evidence, rendered, override = (
        await per_course_browser.maybe_browser_refetch(
            "https://configured.example/course",
            {"international_fee": None},
        )
    )

    assert filled["international_fee"] == 42_500
    assert filled["fee_currency"] == "AUD"
    assert filled["fee_term"] == "Annual"
    assert any(
        row["field_key"] == "international_fee"
        and row["method"] == "per_course_browser_extended"
        for row in evidence
    )
    assert rendered is not None
    assert override is False


@pytest.mark.asyncio
async def test_full_rendered_extraction_is_disabled_by_default(monkeypatch):
    class _Extraction:
        actions = []
        skip_per_course_browser = False

    class _Config:
        extraction = _Extraction()

    async def _render(url: str, **kw):  # noqa: ANN001
        return "<html><body>Rendered course page</body></html>"

    extended_called = False

    async def _extended(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal extended_called
        extended_called = True
        return {"international_fee": 42_500}, []

    async def _english_extract(rendered: str, url: str):  # noqa: ANN001
        return []

    monkeypatch.setattr(per_course_browser, "get_uni_config", lambda: _Config())
    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _render)
    monkeypatch.setattr(per_course_browser, "_extended_extract", _extended)
    monkeypatch.setattr(
        per_course_browser.english_test,
        "extract",
        _english_extract,
    )

    filled, _, rendered, _ = await per_course_browser.maybe_browser_refetch(
        "https://ordinary.example/course",
        {"international_fee": None},
    )

    assert extended_called is False
    assert filled == {}
    assert rendered is not None


@pytest.mark.asyncio
async def test_browser_timeout_is_clamped_to_shared_course_deadline(
    monkeypatch,
):
    from app.services.scraper.course_deadline import (
        reset_course_deadline,
        set_course_deadline,
    )

    fetch_kwargs = {}

    async def _hang_forever(url: str, **kw):  # noqa: ANN001
        fetch_kwargs.update(kw)
        await asyncio.sleep(3600)

    monkeypatch.setattr(per_course_browser.browser_pool, "fetch_html", _hang_forever)
    monkeypatch.setattr(
        per_course_browser,
        "_browser_config_for",
        lambda url: ("domcontentloaded", 0, 0.5, 500),
    )
    emitted = []

    async def _emit(event, message, **kw):  # noqa: ANN001
        emitted.append({"event": event, "message": message, **kw})

    token = set_course_deadline(0.08)
    try:
        started = __import__("time").monotonic()
        await per_course_browser.maybe_browser_refetch(
            "https://example.test/course",
            {},
            emit=_emit,
        )
        elapsed = __import__("time").monotonic() - started
    finally:
        reset_course_deadline(token)

    assert elapsed < 0.2
    assert 0 < fetch_kwargs["timeout"] < 80
    timeout_event = next(
        event for event in emitted
        if event.get("kind") == "per_course_browser_timeout"
    )
    assert 0 < timeout_event["timeout_seconds"] <= 0.08

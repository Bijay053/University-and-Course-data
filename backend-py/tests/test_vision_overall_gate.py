"""Task #233 — vision OCR early-exit gate.

Vision (Gemini image OCR) is the single most expensive per-course step.  When
*every* English overall slot is already populated AND the page has no tier-0
(English-section) image to recover sub-bands from, there is nothing for vision
to add — so ``maybe_vision_refetch`` should bail before downloading or OCR'ing
any image.

The gate sits immediately before ``_extract_page_text`` in
``maybe_vision_refetch``.  These tests use ``_extract_page_text`` as a tripwire:
if it runs, the gate did NOT fire (execution proceeded into the OCR pipeline);
if it never runs, the gate short-circuited.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper import per_course_vision as pcv


class _Proceeded(Exception):
    """Raised by the _extract_page_text tripwire to prove the gate did not
    short-circuit (execution reached the OCR pipeline)."""


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _gemini_key_and_tripwire(monkeypatch):
    """Ensure the API-key precondition passes and install the tripwire so any
    execution past the gate is observable."""
    monkeypatch.setattr(pcv.settings, "gemini_api_key", "test-key", raising=False)

    def _tripwire(*_a, **_k):
        raise _Proceeded()

    monkeypatch.setattr(pcv, "_extract_page_text", _tripwire)
    yield


def _all_overalls_filled() -> dict:
    return {slot: 6.5 for slot in pcv._ENGLISH_OVERALL_SLOTS}


def test_gate_skips_when_all_overalls_filled_and_no_tier0(monkeypatch) -> None:
    monkeypatch.setattr(
        pcv,
        "_extract_img_candidates",
        lambda _html, _url: ([("https://e.edu/x.png", "alt")], frozenset()),
    )
    result = _run(
        pcv.maybe_vision_refetch(
            "https://e.edu/course",
            "<html><body><img src='x.png'></body></html>",
            _all_overalls_filled(),
        )
    )
    assert result == ({}, []), "gate must return an empty no-op result"


def test_gate_proceeds_when_an_overall_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        pcv,
        "_extract_img_candidates",
        lambda _html, _url: ([("https://e.edu/x.png", "alt")], frozenset()),
    )
    payload = _all_overalls_filled()
    payload["ielts_overall"] = None  # one slot empty → vision still worthwhile
    with pytest.raises(_Proceeded):
        _run(
            pcv.maybe_vision_refetch(
                "https://e.edu/course",
                "<html><body><img src='x.png'></body></html>",
                payload,
            )
        )


def test_gate_proceeds_when_tier0_image_present(monkeypatch) -> None:
    """Even with all overalls filled, a tier-0 (English-section) image means
    sub-bands may still be recoverable — the gate must not fire."""
    tier0_url = "https://e.edu/english.png"
    monkeypatch.setattr(
        pcv,
        "_extract_img_candidates",
        lambda _html, _url: ([(tier0_url, "english requirements")], frozenset({tier0_url})),
    )
    with pytest.raises(_Proceeded):
        _run(
            pcv.maybe_vision_refetch(
                "https://e.edu/course",
                "<html><body><img src='english.png'></body></html>",
                _all_overalls_filled(),
            )
        )


def test_vision_download_and_gemini_timeout_use_remaining_course_budget(
    monkeypatch,
) -> None:
    from app.services.ai.gemini_client import GeminiResponse
    from app.services.scraper.course_deadline import (
        reset_course_deadline,
        set_course_deadline,
    )

    image_url = "https://e.edu/english.png"
    monkeypatch.setattr(
        pcv,
        "_extract_img_candidates",
        lambda _html, _url: (
            [(image_url, "English requirements")],
            frozenset({image_url}),
        ),
    )
    # This test exercises the remote-call path, not the earlier page-text
    # tripwire used by the three gate tests above.
    monkeypatch.setattr(pcv, "_extract_page_text", lambda _html: "IELTS PTE")
    captured: dict[str, float | None] = {}

    async def _download(_url, *, timeout_seconds=None):  # noqa: ANN001
        captured["download"] = timeout_seconds
        return b"\x89PNG\r\n\x1a\nfake"

    async def _vision(_prompt, _images, **kwargs):  # noqa: ANN001
        captured["vision"] = kwargs.get("timeout_s")
        return GeminiResponse(
            "",
            1,
            0,
            0.0,
            skipped=True,
            skip_reason="test",
        )

    monkeypatch.setattr(pcv, "_download", _download)
    monkeypatch.setattr(pcv.gemini_client, "generate_with_images", _vision)

    token = set_course_deadline(0.08)
    try:
        result = _run(
            pcv.maybe_vision_refetch(
                "https://e.edu/course",
                "<h2>English requirements</h2><img src='english.png'>",
                {},
            )
        )
    finally:
        reset_course_deadline(token)

    assert result == ({}, [])
    assert 0 < float(captured["download"] or 0) <= 0.08
    assert 0 < float(captured["vision"] or 0) <= 0.08


@pytest.mark.asyncio
async def test_cancelled_vision_cache_leader_does_not_cancel_waiter(
    monkeypatch,
) -> None:
    image_url = "https://e.edu/shared-english.png"
    monkeypatch.setattr(
        pcv,
        "_extract_img_candidates",
        lambda _html, _url: (
            [(image_url, "English requirements")],
            frozenset({image_url}),
        ),
    )
    monkeypatch.setattr(pcv, "_extract_page_text", lambda _html: "IELTS PTE")

    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def _blocked_download(_url, *, timeout_seconds=None):  # noqa: ANN001
        download_started.set()
        await release_download.wait()
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(pcv, "_download", _blocked_download)
    shared_cache: pcv.VisionImageCache = {}
    html = "<h2>English requirements</h2><img src='shared-english.png'>"

    leader = asyncio.create_task(
        pcv.maybe_vision_refetch(
            "https://e.edu/course-a",
            html,
            {},
            image_cache=shared_cache,
        )
    )
    await download_started.wait()
    waiter = asyncio.create_task(
        pcv.maybe_vision_refetch(
            "https://e.edu/course-b",
            html,
            {},
            image_cache=shared_cache,
        )
    )
    await asyncio.sleep(0)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    assert await asyncio.wait_for(waiter, timeout=0.2) == ({}, [])

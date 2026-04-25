"""Bug L: per-image vision OCR cache must coalesce concurrent
in-flight requests for the same URL.

When 4 ASA IT Master pages all link the SAME `MaSTER.png` and the
orchestrator runs them under `_MAX_PARALLEL_FETCH=4`, all 4 coroutines
reach `maybe_vision_refetch` for that image at roughly the same time.
A naive cache (set-after-await) would let all 4 see "URL not in
cache", all 4 fire Gemini, and produce the same non-determinism we
shipped this fix to eliminate. The Future-based in-flight cache must
ensure exactly one Gemini call regardless of concurrency.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.services.scraper import per_course_vision as pcv


_FAKE_GEMINI_TEXT = (
    "IELTS Academic Overall Band Score: 6.5\n"
    "IELTS Academic listening: 6\n"
    "IELTS Academic reading: 6\n"
    "IELTS Academic writing: 6\n"
    "IELTS Academic speaking: 6\n"
)

_HTML = (
    "<html><body><h1>Master of IT</h1>"
    '<img src="https://asa.edu.au/MaSTER.png" alt="english reqs">'
    "</body></html>"
)


class _FakeGeminiResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.skipped = False
        self.skip_reason = None


async def _slow_fake_gemini(*_args, **_kwargs):
    """Simulates Gemini taking ~50ms — enough that all 4 racing
    coroutines are guaranteed to be in-flight at the same time before
    any of them resolves. Without coalescing we'd see the call_count
    grow to 4."""
    await asyncio.sleep(0.05)
    return _FakeGeminiResponse(_FAKE_GEMINI_TEXT)


async def _fake_download(_url: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n_fake_image_bytes"


@pytest.mark.asyncio
async def test_in_flight_cache_coalesces_concurrent_calls(monkeypatch):
    """4 concurrent maybe_vision_refetch calls for pages that share
    the same image URL must yield exactly ONE Gemini invocation, AND
    all 4 must receive the same parsed sub-band values."""
    call_count = [0]

    async def _counting_gemini(*args, **kwargs):
        call_count[0] += 1
        return await _slow_fake_gemini(*args, **kwargs)

    monkeypatch.setattr(pcv, "_download", _fake_download)
    monkeypatch.setattr(
        pcv.gemini_client, "generate_with_images", _counting_gemini
    )
    # Exercise the same code path the orchestrator does — settings
    # must report a non-empty gemini_api_key for vision to fire.
    monkeypatch.setattr(pcv.settings, "gemini_api_key", "fake-test-key", raising=False)

    shared_cache: dict[str, asyncio.Future] = {}

    async def _one_course(url: str):
        return await pcv.maybe_vision_refetch(
            url,
            _HTML,
            payload={},  # everything missing → vision must fire
            image_cache=shared_cache,
        )

    # 4 sibling pages, each linking the SAME MaSTER.png
    results = await asyncio.gather(
        _one_course("https://asa.edu.au/master-ai"),
        _one_course("https://asa.edu.au/master-cyber"),
        _one_course("https://asa.edu.au/master-sad"),
        _one_course("https://asa.edu.au/master-pm"),
    )

    # ── Assertion 1: Gemini fired exactly once across all 4 siblings.
    assert call_count[0] == 1, (
        f"Expected exactly 1 Gemini call (in-flight coalescing); "
        f"got {call_count[0]}"
    )

    # ── Assertion 2: every sibling got the same parsed values.
    for filled, _evidence in results:
        assert filled.get("ielts_overall") == 6.5
        assert filled.get("ielts_listening") == 6.0
        assert filled.get("ielts_reading") == 6.0
        assert filled.get("ielts_writing") == 6.0
        assert filled.get("ielts_speaking") == 6.0

    # ── Assertion 3: at least 3 of the 4 evidence sets are tagged as
    # cached (the 4th — the leader — is the original miss).
    cached_count = 0
    for _filled, evidence in results:
        if evidence and evidence[0].get("method") == "per_course_vision_cached":
            cached_count += 1
    assert cached_count == 3, (
        f"Expected 3 of 4 sibling courses to record cache-hit "
        f"evidence; got {cached_count}"
    )


@pytest.mark.asyncio
async def test_cache_negative_result_skips_re_download(monkeypatch):
    """Image download failure (404 / oversized) must be cached too —
    sibling courses must not re-attempt the download."""
    download_count = [0]

    async def _counting_download(_url: str):
        download_count[0] += 1
        return None  # Simulates 404 / too-large

    monkeypatch.setattr(pcv, "_download", _counting_download)
    monkeypatch.setattr(pcv.settings, "gemini_api_key", "fake-test-key", raising=False)

    shared_cache: dict[str, asyncio.Future] = {}

    # Two sequential calls to mimic two course pages in one scrape that
    # both reference an image URL that 404s.
    await pcv.maybe_vision_refetch(
        "https://asa.edu.au/c1", _HTML, {}, image_cache=shared_cache
    )
    await pcv.maybe_vision_refetch(
        "https://asa.edu.au/c2", _HTML, {}, image_cache=shared_cache
    )

    assert download_count[0] == 1, (
        f"Expected the 404-image to be downloaded once and "
        f"negative-cached; got {download_count[0]} downloads"
    )

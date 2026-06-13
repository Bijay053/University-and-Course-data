"""Unit tests for the seen_pdf_urls dedup guard in the normal (non-repair) scrape run.

run_scrape() creates a single seen_pdf_urls set before its asyncio.gather() call and
passes it into every _extract_only() invocation via the _bounded() closure.  When two
(or more) course links in the same scrape job share the same PDF URL, the PDF must be
fetched and Gemini-processed exactly once — subsequent calls return immediately with an
'extract:pdf_already_fetched' error result rather than re-downloading the document.

Thread-safety note
------------------
asyncio.gather() uses cooperative multitasking: all coroutines run on a single OS
thread and only yield control at explicit ``await`` points.  Two coroutines therefore
cannot enter the ``seen_pdf_urls.add()`` call simultaneously, making a plain ``set``
safe without an asyncio.Lock or threading.Lock.  This is the same guarantee that makes
asyncio queues safe for single-consumer use without additional locking.
"""
from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop (test-helper only)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _extract_only: seen_pdf_urls dedup guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_only_pdf_deduped_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When two links share the same .pdf URL and a shared seen_pdf_urls set is
    passed, the second _extract_only call must return immediately with
    'extract:pdf_already_fetched' without calling extract_course at all.
    """
    from app.services.scraper import orchestrator as _orch_mod

    pdf_url = "https://example.test/fees/schedule.pdf"
    extract_call_count: list[int] = [0]

    async def _fake_extract_course(url: str, **kwargs: object) -> dict:
        extract_call_count[0] += 1
        return {"payload": {"course_name": "Test Course"}, "evidence": []}

    monkeypatch.setattr(_orch_mod, "extract_course", _fake_extract_course)

    seen: set[str] = set()
    link = {"url": pdf_url, "name": "PDF Course"}

    result1 = await _orch_mod._extract_only(link, "Australia", seen_pdf_urls=seen)
    result2 = await _orch_mod._extract_only(link, "Australia", seen_pdf_urls=seen)

    assert extract_call_count[0] == 1, (
        f"extract_course called {extract_call_count[0]} times; "
        "expected exactly 1 — seen_pdf_urls guard must skip the second PDF call"
    )
    assert result2.get("error") == "extract:pdf_already_fetched", (
        f"second call returned unexpected result: {result2}"
    )
    assert pdf_url in seen, "PDF URL must be in seen_pdf_urls after first call"


@pytest.mark.asyncio
async def test_extract_only_pdf_not_deduped_without_seen_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When seen_pdf_urls is None (not passed), the same PDF URL is fetched each
    time — this documents the pre-guard behaviour and ensures we did not break the
    no-op path.
    """
    from app.services.scraper import orchestrator as _orch_mod

    pdf_url = "https://example.test/fees/no-dedup.pdf"
    extract_call_count: list[int] = [0]

    async def _fake_extract_course(url: str, **kwargs: object) -> dict:
        extract_call_count[0] += 1
        return {"payload": {"course_name": "Test Course"}, "evidence": []}

    monkeypatch.setattr(_orch_mod, "extract_course", _fake_extract_course)

    link = {"url": pdf_url, "name": "PDF Course No Dedup"}

    await _orch_mod._extract_only(link, "Australia", seen_pdf_urls=None)
    await _orch_mod._extract_only(link, "Australia", seen_pdf_urls=None)

    assert extract_call_count[0] == 2, (
        f"extract_course called {extract_call_count[0]} times; "
        "expected 2 — without seen_pdf_urls every call must proceed"
    )


@pytest.mark.asyncio
async def test_extract_only_distinct_pdf_urls_both_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct PDF URLs must both be fetched even when seen_pdf_urls is shared."""
    from app.services.scraper import orchestrator as _orch_mod

    fetched_urls: list[str] = []

    async def _fake_extract_course(url: str, **kwargs: object) -> dict:
        fetched_urls.append(url)
        return {"payload": {"course_name": "Course"}, "evidence": []}

    monkeypatch.setattr(_orch_mod, "extract_course", _fake_extract_course)

    seen: set[str] = set()
    link_a = {"url": "https://example.test/fees/a.pdf", "name": "Course A"}
    link_b = {"url": "https://example.test/fees/b.pdf", "name": "Course B"}

    await _orch_mod._extract_only(link_a, "Australia", seen_pdf_urls=seen)
    await _orch_mod._extract_only(link_b, "Australia", seen_pdf_urls=seen)

    assert len(fetched_urls) == 2, (
        f"expected both distinct PDF URLs to be fetched; got {fetched_urls}"
    )
    assert "https://example.test/fees/a.pdf" in fetched_urls
    assert "https://example.test/fees/b.pdf" in fetched_urls


@pytest.mark.asyncio
async def test_extract_only_non_pdf_url_not_affected_by_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seen_pdf_urls guard only fires for URLs ending in .pdf.  HTML course URLs
    must pass through even if (theoretically) the same URL appears twice.
    """
    from app.services.scraper import orchestrator as _orch_mod

    extract_call_count: list[int] = [0]

    async def _fake_extract_course(url: str, **kwargs: object) -> dict:
        extract_call_count[0] += 1
        return {"payload": {"course_name": "HTML Course"}, "evidence": []}

    monkeypatch.setattr(_orch_mod, "extract_course", _fake_extract_course)

    seen: set[str] = set()
    html_link = {"url": "https://example.test/courses/mba", "name": "MBA"}

    await _orch_mod._extract_only(html_link, "Australia", seen_pdf_urls=seen)
    await _orch_mod._extract_only(html_link, "Australia", seen_pdf_urls=seen)

    assert extract_call_count[0] == 2, (
        f"HTML URLs must not be deduplicated by the PDF guard; "
        f"extract_course was called {extract_call_count[0]} times"
    )


@pytest.mark.asyncio
async def test_seen_pdf_urls_accumulates_across_gather_simulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate what asyncio.gather does: run several _extract_only coroutines
    concurrently sharing one seen_pdf_urls set.  The shared PDF must be fetched
    exactly once regardless of how many coroutines encounter it.

    This test also documents why a plain set is safe: asyncio.gather() is
    cooperative (single OS thread) so coroutines cannot interleave inside a
    synchronous block like set.add().  The guard check + add is not an atomic
    hardware operation, but no await point exists between the membership test
    and the add(), so no other coroutine can race between them.
    """
    from app.services.scraper import orchestrator as _orch_mod

    shared_pdf = "https://example.test/shared/all-fees.pdf"
    fetch_count: list[int] = [0]

    async def _fake_extract_course(url: str, **kwargs: object) -> dict:
        fetch_count[0] += 1
        return {"payload": {"course_name": "Course"}, "evidence": []}

    monkeypatch.setattr(_orch_mod, "extract_course", _fake_extract_course)

    seen: set[str] = set()
    links = [
        {"url": shared_pdf, "name": f"PDF Course {i}"}
        for i in range(5)
    ]

    results = await asyncio.gather(
        *[_orch_mod._extract_only(lk, "Australia", seen_pdf_urls=seen) for lk in links]
    )

    assert fetch_count[0] == 1, (
        f"expected exactly 1 PDF fetch across 5 concurrent coroutines; "
        f"got {fetch_count[0]}"
    )
    deduped = [r for r in results if r.get("error") == "extract:pdf_already_fetched"]
    assert len(deduped) == 4, (
        f"expected 4 deduped results (5 links - 1 fetch); got {len(deduped)}"
    )

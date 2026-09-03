"""Regression tests for La Trobe's bounded extraction recovery path.

The La Trobe bundle fetch can transiently exceed the orchestrator's 300-second
per-course safety cap under provider contention.  Those timeouts must be
classified, replayed by the sequential recovery pass, and surfaced with an
actionable reason if replay cannot recover them.
"""
from __future__ import annotations

import pytest


def test_timeout_is_retryable_with_an_actionable_reason() -> None:
    from app.services.scraper.orchestrator import (
        _PER_COURSE_EXTRACTION_TIMEOUT_SECONDS,
        _extraction_failure_details,
    )

    details = _extraction_failure_details("per_course_timeout")

    assert details["reason"] == "per_course_timeout"
    assert details["retryable"] is True
    assert (
        f"{_PER_COURSE_EXTRACTION_TIMEOUT_SECONDS:.0f}-second"
        in str(details["detail"])
    )


def test_timeout_detail_reports_per_university_configured_cap() -> None:
    from app.services.scraper.orchestrator import _extraction_failure_details

    details = _extraction_failure_details(
        "per_course_timeout",
        result={"error_reason": "Extraction exceeded the 20-second per-course safety cap"},
    )

    assert details["retryable"] is True
    assert "20-second" in str(details["detail"])


def test_fetch_failures_retry_but_extract_errors_stay_for_review() -> None:
    from app.services.scraper.orchestrator import _extraction_failure_details

    fetch_details = _extraction_failure_details("fetch_failed_empty_text")
    exception_details = _extraction_failure_details(
        "extract: RuntimeError: provider temporarily unavailable"
    )

    assert fetch_details["reason"] == "fetch_failed"
    assert fetch_details["retryable"] is True
    assert exception_details["reason"] == "extract_exception"
    assert exception_details["retryable"] is False
    assert "provider temporarily unavailable" in str(exception_details["detail"])


@pytest.mark.asyncio
async def test_extract_only_retains_exception_type_for_operator_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper import orchestrator

    async def _failing_extract(url: str, **kwargs: object) -> dict:
        raise RuntimeError("provider temporarily unavailable")

    monkeypatch.setattr(orchestrator, "extract_course", _failing_extract)

    result = await orchestrator._extract_only(
        {"name": "La Trobe test course", "url": "https://www.latrobe.edu.au/courses/test"},
        "Australia",
    )

    assert result["error"] == "extract: RuntimeError: provider temporarily unavailable"
    assert result["error_type"] == "RuntimeError"
    assert result["error_reason"] == "provider temporarily unavailable"


@pytest.mark.asyncio
async def test_recovery_extraction_timeout_is_bounded_and_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sequential replay shares the primary pass's hard timeout guard."""
    from app.services.scraper import orchestrator

    async def _hung_extract(*args: object, **kwargs: object) -> dict:
        await __import__("asyncio").sleep(60)
        return {}

    monkeypatch.setattr(orchestrator, "_extract_only", _hung_extract)

    result = await orchestrator._extract_with_hard_timeout(
        {"name": "La Trobe test course", "url": "https://www.latrobe.edu.au/courses/test"},
        "Australia",
        timeout_seconds=0.01,
    )

    assert result["error"] == "per_course_timeout"
    assert result["error_type"] == "TimeoutError"
    assert result["retryable"] is True
    assert result["_timed_out"] is True


@pytest.mark.asyncio
async def test_scrape_do_account_error_opens_circuit_and_skips_remaining_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep account failure must prevent calls for every remaining URL."""
    from app.services.scraper import orchestrator

    invoked: list[str] = []

    async def _would_extract(link: dict, *args: object, **kwargs: object) -> dict:
        invoked.append(link["url"])
        raise orchestrator.ScrapedoAccountError("Scrape.do account unavailable")

    monkeypatch.setattr(orchestrator, "_extract_with_hard_timeout", _would_extract)
    dead_circuit = [False]
    links = [
        {"url": "https://www.latrobe.edu.au/courses/one"},
        {"url": "https://www.latrobe.edu.au/courses/two"},
        {"url": "https://www.latrobe.edu.au/courses/three"},
    ]

    first = await orchestrator._extract_for_recovery_sweep(
        links[0],
        "Australia",
        scrape_do_dead_flag=dead_circuit,
    )
    remaining = [
        await orchestrator._extract_for_recovery_sweep(
            link,
            "Australia",
            scrape_do_dead_flag=dead_circuit,
        )
        for link in links[1:]
    ]

    assert first["error_type"] == "ScrapedoAccountError"
    assert first["_scrape_do_auth_error"] is True
    assert dead_circuit == [True]
    assert remaining == [None, None]
    assert invoked == [links[0]["url"]]
"""Cross-stage per-course deadline and latency-gate regressions."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services.scraper import course_deadline


def test_deadline_clamps_stages_and_resets_cleanly() -> None:
    token = course_deadline.set_course_deadline(0.2)
    try:
        remaining = course_deadline.remaining_seconds()
        assert remaining is not None
        assert 0 < remaining <= 0.2
        assert 0 < course_deadline.clamp_timeout(30.0) <= 0.2
    finally:
        course_deadline.reset_course_deadline(token)

    assert course_deadline.remaining_seconds() is None
    assert course_deadline.clamp_timeout(30.0) == 30.0


def test_required_fields_complete_accepts_canonical_pipeline_slots() -> None:
    payload = {
        "international_fee": 19_488,
        "ielts_overall": 6.5,
        "duration": 3,
        "intake_months": ["March", "July"],
        "course_location": "Wollongong",
        "study_mode": "On Campus",
    }
    assert course_deadline.required_course_fields_complete(payload)

    payload["international_fee"] = None
    assert not course_deadline.required_course_fields_complete(payload)


def test_online_course_does_not_require_a_physical_location() -> None:
    payload = {
        "international_fee": 30_000,
        "pte_overall": 58,
        "duration_value": 2,
        "intake_text": "February, July",
        "mode": "Online",
    }
    assert course_deadline.required_course_fields_complete(payload)


def test_sparse_browser_gate_requires_sparse_visible_html() -> None:
    from app.services.scraper.pipelines.single_course import (
        _should_force_sparse_browser,
    )

    payload = {"international_fee": None, "duration": None}
    force_short, short_len = _should_force_sparse_browser(
        payload,
        "<html><body>JavaScript required</body></html>",
    )
    force_long, long_len = _should_force_sparse_browser(
        payload,
        "<html><body><p>" + ("substantive course content " * 200) + "</p></body></html>",
    )

    assert force_short is True
    assert short_len < 2_000
    assert force_long is False
    assert long_len >= 2_000


def test_murdoch_config_skips_unproductive_per_course_browser() -> None:
    from app.services.scraper.config import get_config_for_host

    cfg = get_config_for_host(
        hostname="www.murdoch.edu.au",
        name="Murdoch University",
        scrape_url="https://www.murdoch.edu.au/course/postgraduate/m1358",
    )

    assert cfg.extraction.skip_per_course_browser is True


@pytest.mark.asyncio
async def test_outer_deadline_is_shared_by_sequential_child_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper import orchestrator

    observed: list[float] = []

    async def _two_slow_stages(*args: object, **kwargs: object) -> dict:
        first = course_deadline.clamp_timeout(0.04)
        assert first is not None
        observed.append(first)
        await asyncio.sleep(0.035)

        second = course_deadline.clamp_timeout(0.04)
        assert second is not None
        observed.append(second)
        await asyncio.sleep(0.1)
        return {}

    monkeypatch.setattr(orchestrator, "_extract_only", _two_slow_stages)

    started = time.monotonic()
    result = await orchestrator._extract_with_hard_timeout(
        {"name": "Deadline test", "url": "https://example.edu/course"},
        "Australia",
        timeout_seconds=0.06,
    )
    elapsed = time.monotonic() - started

    assert result["error"] == "per_course_timeout"
    assert result["retryable"] is True
    assert elapsed < 0.2
    assert len(observed) == 2
    assert observed[1] < observed[0]
    assert course_deadline.remaining_seconds() is None


@pytest.mark.asyncio
async def test_scrape_do_shared_deadline_queues_one_bounded_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient provider timeout reaches the sweep; account failures do not."""
    from app.services.scraper import orchestrator

    url = "https://example.edu/course/provider-timeout"

    async def _scrape_do_request_exhausts_deadline(
        *args: object,
        **kwargs: object,
    ) -> dict:
        await asyncio.sleep(60)
        return {}

    monkeypatch.setattr(
        orchestrator,
        "_extract_only",
        _scrape_do_request_exhausts_deadline,
    )
    timed_out = await orchestrator._extract_with_hard_timeout(
        {"name": "Provider timeout", "url": url},
        "Australia",
        timeout_seconds=0.01,
    )

    sweep_links: list[dict] = []
    sweep_url_keys: set[str] = set()
    assert timed_out["error"] == "per_course_timeout"
    assert timed_out["retryable"] is True
    assert orchestrator._queue_recovery_sweep_candidate(
        timed_out,
        counter="fetch_failed",
        links=sweep_links,
        url_keys=sweep_url_keys,
    )
    assert not orchestrator._queue_recovery_sweep_candidate(
        timed_out,
        counter="fetch_failed",
        links=sweep_links,
        url_keys=sweep_url_keys,
    )

    account_failure = {
        "name": "Permanent provider failure",
        "url": "https://example.edu/course/account-failure",
        "error": "extract: Scrape.do account unavailable",
        "error_type": "ScrapedoAccountError",
        "_scrape_do_auth_error": True,
        "retryable": False,
    }
    assert not orchestrator._queue_recovery_sweep_candidate(
        account_failure,
        counter="errors",
        links=sweep_links,
        url_keys=sweep_url_keys,
    )
    assert sweep_links == [
        {
            "url": url,
            "name": "Provider timeout",
            "counter": "fetch_failed",
            "source_error": "per_course_timeout",
            "reason": "per_course_timeout",
        }
    ]
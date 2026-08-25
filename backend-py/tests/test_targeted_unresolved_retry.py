"""Focused retry coverage for recovery-sweep URLs.

These tests keep the new history-to-worker contract independent of live
providers: operators can only submit URLs that the source run recorded as
unresolved, and the resulting job receives the course URLs explicitly.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.routers import scrape
from app.schemas.scrape import ScrapeStartResponse, StartScrapeBody
from app.services.scraper.orchestrator import (
    _inject_extra_course_urls,
    _target_course_urls_from_payload,
)


def test_start_scrape_body_accepts_targeted_course_urls() -> None:
    body = StartScrapeBody(
        universityId=7,
        courseUrls=["https://example.edu/course/a"],
        retrySourceJobId="job_source",
    )

    assert body.course_urls == ["https://example.edu/course/a"]
    assert body.retry_source_job_id == "job_source"


def test_targeted_urls_are_http_only_deduplicated_and_bounded() -> None:
    urls = _target_course_urls_from_payload({
        "courseUrls": [
            " https://example.edu/course/a ",
            "https://example.edu/course/a/",
            "ftp://example.edu/course/b",
            "not a url",
            "https://example.edu/course/c",
        ],
    })

    assert urls == [
        "https://example.edu/course/a",
        "https://example.edu/course/c",
    ]


def test_targeted_retry_does_not_expand_with_yaml_extra_urls() -> None:
    links = [{"url": "https://example.edu/course/a", "name": "Targeted retry"}]

    injected, moved = _inject_extra_course_urls(
        links,
        ["https://example.edu/course/unrelated"],
        targeted_retry=True,
    )

    assert (injected, moved) == (0, 0)
    assert links == [{"url": "https://example.edu/course/a", "name": "Targeted retry"}]


def test_unresolved_history_entries_keep_latest_reason_per_url() -> None:
    entries = scrape._unresolved_history_entries([
        {"payload": {"kind": "sweep_unresolved", "url": "https://example.edu/a", "reason": "fetch_failed"}},
        {"payload": {"kind": "other", "url": "https://example.edu/ignored"}},
        {
            "payload": {
                "kind": "sweep_unresolved",
                "url": "https://example.edu/a",
                "reason": "scrape_do_circuit_open",
                "detail": "Provider unavailable",
            },
            "createdAt": "2026-08-24T10:00:00+00:00",
        },
    ])

    assert entries == [{
        "url": "https://example.edu/a",
        "courseName": None,
        "reason": "scrape_do_circuit_open",
        "detail": "Provider unavailable",
        "sourceError": None,
        "retryError": None,
        "createdAt": "2026-08-24T10:00:00+00:00",
    }]


def test_unresolved_history_entries_include_budget_exhausted_failures() -> None:
    entries = scrape._unresolved_history_entries([
        {
            "payload": {
                "kind": "extract_error",
                "url": "https://example.edu/resolved",
                "reason": "per_course_timeout",
                "retryable": True,
            },
        },
        {
            "payload": {
                "kind": "extract_error",
                "url": "https://example.edu/budget-exhausted",
                "reason": "per_course_timeout",
                "retryable": True,
            },
        },
        {
            "payload": {
                "kind": "extract_error",
                "url": "https://example.edu/permanent",
                "reason": "not_found",
                "retryable": False,
            },
        },
        {
            "payload": {
                "kind": "sweep_recovered",
                "url": "https://example.edu/resolved",
            },
        },
        {
            "payload": {
                "kind": "sweep_budget_unresolved",
                "count": 1,
            },
        },
    ])

    assert [entry["url"] for entry in entries] == [
        "https://example.edu/budget-exhausted",
    ]


def test_retry_endpoint_passes_only_selected_recorded_urls(monkeypatch) -> None:
    captured: dict = {}

    class _Rows:
        def all(self):
            return [(
                {
                    "kind": "sweep_unresolved",
                    "url": "https://example.edu/course/a",
                    "reason": "fetch_failed",
                },
                None,
            )]

    class _Db:
        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
            )

        async def execute(self, _statement, _params):
            return _Rows()

    async def _fake_start(body, db):
        captured["body"] = body
        captured["db"] = db
        return ScrapeStartResponse(job_id="job_targeted", runtime_job_id="job_targeted")

    monkeypatch.setattr(scrape, "start_scrape", _fake_start)

    result = asyncio.run(scrape.retry_unresolved_history_urls(
        "job_source",
        scrape.RetryUnresolvedBody(urls=["https://example.edu/course/a"]),
        _Db(),
    ))

    assert result.job_id == "job_targeted"
    assert captured["body"].course_urls == ["https://example.edu/course/a"]
    assert captured["body"].retry_source_job_id == "job_source"
    assert captured["body"].university_id == 12


def test_continue_endpoint_retries_every_unresolved_url(monkeypatch) -> None:
    captured: dict = {}

    class _Rows:
        def all(self):
            return [
                (
                    {
                        "kind": "extract_error",
                        "url": "https://example.edu/course/a",
                        "reason": "per_course_timeout",
                        "retryable": True,
                    },
                    None,
                ),
                (
                    {
                        "kind": "extract_error",
                        "url": "https://example.edu/course/b",
                        "reason": "per_course_timeout",
                        "retryable": True,
                    },
                    None,
                ),
                (
                    {
                        "kind": "sweep_recovered",
                        "url": "https://example.edu/course/a",
                    },
                    None,
                ),
            ]

    class _Db:
        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
            )

        async def execute(self, _statement, _params):
            return _Rows()

    async def _fake_start(body, db):
        captured["body"] = body
        captured["db"] = db
        return ScrapeStartResponse(job_id="job_continued", runtime_job_id="job_continued")

    monkeypatch.setattr(scrape, "start_scrape", _fake_start)

    result = asyncio.run(scrape.continue_unresolved_history_urls(
        "job_source",
        _Db(),
    ))

    assert result.job_id == "job_continued"
    assert captured["body"].course_urls == ["https://example.edu/course/b"]
    assert captured["body"].retry_source_job_id == "job_source"
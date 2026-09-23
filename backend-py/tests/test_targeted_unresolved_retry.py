"""Focused retry coverage for recovery-sweep URLs.

These tests keep the new history-to-worker contract independent of live
providers: operators can only submit URLs that the source run recorded as
unresolved, and the resulting job receives the course URLs explicitly.
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from app.routers import scrape
from app.schemas.scrape import ScrapeStartResponse, StartScrapeBody
from app.services.scraper.orchestrator import (
    _inject_extra_course_urls,
    _is_targeted_retry_payload,
    _matched_resume_provenance,
    _normalize_course_url,
    _prior_targeted_retry_resolved_urls,
    _verification_resume_keys,
    _should_auto_discover_fee_page,
    _targeted_retry_all_filtered_diagnostic,
    _target_course_urls_from_payload,
)
from app.services.scraper.url_identity import canonical_course_url_key


def test_start_scrape_body_accepts_targeted_course_urls() -> None:
    body = StartScrapeBody(
        universityId=7,
        courseUrls=["https://example.edu/course/a"],
        retrySourceJobId="job_source",
    )

    assert body.course_urls == ["https://example.edu/course/a"]
    assert body.retry_source_job_id == "job_source"
    assert body.browser_rescue_attempted is False


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


def test_targeted_retry_payload_preserves_parent_review_rows() -> None:
    assert _is_targeted_retry_payload({
        "courseUrls": ["https://example.edu/course/a"],
        "retrySourceJobId": "job_parent",
    })
    assert not _is_targeted_retry_payload({"retrySourceJobId": "job_parent"})


def test_targeted_retry_does_not_expand_with_yaml_extra_urls() -> None:
    links = [{"url": "https://example.edu/course/a", "name": "Targeted retry"}]

    injected, moved = _inject_extra_course_urls(
        links,
        ["https://example.edu/course/unrelated"],
        targeted_retry=True,
    )

    assert (injected, moved) == (0, 0)
    assert links == [{"url": "https://example.edu/course/a", "name": "Targeted retry"}]


def test_targeted_retry_does_not_probe_course_samples_for_fee_discovery() -> None:
    assert not _should_auto_discover_fee_page(
        has_fee_page=False,
        has_links=True,
        targeted_retry=True,
    )
    assert _should_auto_discover_fee_page(
        has_fee_page=False,
        has_links=True,
        targeted_retry=False,
    )


def test_targeted_retry_all_filtered_has_durable_recovery_diagnostic() -> None:
    selected = [
        "https://example.edu/course/a",
        "https://example.edu/course/b",
    ]

    diagnostic = _targeted_retry_all_filtered_diagnostic(
        selected_urls=selected,
        retained_urls=[],
        retry_source_job_id="job_source",
    )

    assert diagnostic == {
        "error_type": "targeted_retry_all_filtered",
        "message": (
            "No selected courses were processed; 2 unresolved selected course "
            "URLs were rejected by URL filters."
        ),
        "selected_count": 2,
        "filtered_count": 2,
        "resolved_count": 0,
        "processed_count": 0,
        "selected_urls": selected,
        "filtered_urls": selected,
        "retry_source_job_id": "job_source",
        "source_review_job_id": "job_source",
        "recovery_action": "report_official_course_urls",
        "next_action": (
            "Submit the exact official course URLs using the established "
            "reporting form."
        ),
    }


def test_partially_filtered_targeted_retry_is_processed_normally() -> None:
    assert _targeted_retry_all_filtered_diagnostic(
        selected_urls=[
            "https://example.edu/course/a",
            "https://example.edu/course/b",
        ],
        retained_urls=["https://example.edu/course/b"],
        retry_source_job_id="job_source",
    ) is None


def test_already_resolved_targeted_retry_is_a_successful_noop() -> None:
    assert _targeted_retry_all_filtered_diagnostic(
        selected_urls=["https://example.edu/course/a"],
        retained_urls=[],
        resolved_urls=["https://example.edu/course/a/"],
        retry_source_job_id="job_source",
    ) is None


def test_mixed_filtered_and_resume_resolved_targeted_retry_fails_with_exact_rejections() -> None:
    filtered_url = "https://example.edu/course/filtered"
    resolved_url = "https://example.edu/course/already-resolved"

    diagnostic = _targeted_retry_all_filtered_diagnostic(
        selected_urls=[filtered_url, resolved_url],
        retained_urls=[resolved_url],
        extraction_urls=[],
        resolved_urls=[resolved_url],
        retry_source_job_id="job_continuation",
        source_review_job_id="job_original_review",
    )

    assert diagnostic is not None
    assert diagnostic["selected_count"] == 2
    assert diagnostic["filtered_count"] == 1
    assert diagnostic["resolved_count"] == 1
    assert diagnostic["selected_urls"] == [filtered_url, resolved_url]
    assert diagnostic["filtered_urls"] == [filtered_url]
    assert diagnostic["retry_source_job_id"] == "job_continuation"
    assert diagnostic["source_review_job_id"] == "job_original_review"
    assert diagnostic["message"] == (
        "No selected courses were processed; 1 unresolved selected course "
        "URL was rejected by URL filters."
    )


@pytest.mark.parametrize("final_diagnostic_saved", [False, True])
def test_redelivery_does_not_reinterpret_filtered_completion_checkpoints_as_resolved(
    final_diagnostic_saved,
) -> None:
    filtered = "https://example.edu/course/filtered"
    resolved = "https://example.edu/course/already-resolved"
    discovered_config = {
        "autonomousVerification": {
            "completed_urls": [filtered, resolved],
            "excluded_urls": [filtered],
            "completed_scope": (
                "settled attempts and eligibility exclusions; not successful recovery"
            ),
        },
        "targeted_retry_diagnostic": {
            "error_type": "targeted_retry_all_filtered",
            "filtered_urls": [filtered],
        },
    }
    if not final_diagnostic_saved:
        discovered_config.pop("targeted_retry_diagnostic")

    prior_resolved = _prior_targeted_retry_resolved_urls(discovered_config)
    diagnostic = _targeted_retry_all_filtered_diagnostic(
        selected_urls=[filtered, resolved],
        retained_urls=[resolved],
        extraction_urls=[],
        resolved_urls=prior_resolved,
        retry_source_job_id="job_continuation",
    )

    assert prior_resolved == [resolved]
    assert diagnostic is not None
    assert diagnostic["filtered_urls"] == [filtered]
    assert diagnostic["resolved_count"] == 1


def test_redelivery_does_not_reinterpret_error_completion_checkpoint_as_resolved() -> None:
    failed = "https://example.edu/course/exhausted-fetch"
    resolved = "https://example.edu/course/already-resolved"
    discovered_config = {
        "autonomousVerification": {
            "completed_urls": [failed, resolved],
            "url_outcomes": {failed: "error"},
            "completed_scope": (
                "settled attempts and eligibility exclusions; not successful recovery"
            ),
        },
    }

    assert _prior_targeted_retry_resolved_urls(discovered_config) == [resolved]


def test_verification_resume_keeps_error_outcome_available_for_redelivery() -> None:
    failed = "https://example.edu/course/exhausted-fetch"
    resolved = "https://example.edu/course/already-resolved"
    metadata = {
        "completed_urls": [failed, resolved],
        "url_outcomes": {failed: "error"},
    }
    error_keys = {
        canonical_course_url_key(url)
        for url, outcome in metadata["url_outcomes"].items()
        if outcome == "error"
    }
    resumable_acknowledgements = [
        url
        for url in metadata["completed_urls"]
        if canonical_course_url_key(url) not in error_keys
    ]

    _, processed = _verification_resume_keys([], resumable_acknowledgements)

    assert canonical_course_url_key(failed) not in processed
    assert canonical_course_url_key(resolved) in processed


def test_production_resume_classifies_after_already_resolved_filtering() -> None:
    """Lock the production ordering, not only the pure diagnostic helper."""
    from app.services.scraper.orchestrator import _run_claimed_scrape

    selected_url = "https://example.edu/course/already-resolved"
    post_filter_links = [{"url": selected_url, "name": "Resolved course"}]
    checkpoints = {
        _normalize_course_url(selected_url): (42, "job_source")
    }
    matched_keys, _, _ = _matched_resume_provenance(
        post_filter_links,
        checkpoints,
    )
    remaining_links = [
        link
        for link in post_filter_links
        if _normalize_course_url(link["url"]) not in checkpoints
    ]
    diagnostic = _targeted_retry_all_filtered_diagnostic(
        selected_urls=[selected_url],
        retained_urls=[link["url"] for link in post_filter_links],
        extraction_urls=[link["url"] for link in remaining_links],
        resolved_urls=sorted(matched_keys),
        retry_source_job_id="job_source",
    )
    forced_status = "failed" if diagnostic else None

    assert remaining_links == []
    assert matched_keys == {_normalize_course_url(selected_url)}
    assert diagnostic is None
    assert forced_status is None

    source = inspect.getsource(_run_claimed_scrape)
    resume_filter = source.index("_matched_resume_provenance(links, _done_rows)")
    resolved_capture = source.index(
        "_targeted_retry_resolved_urls = sorted(", resume_filter
    )
    diagnostic_call = source.index(
        "_targeted_retry_all_filtered_diagnostic(",
        resume_filter,
    )
    forced_failure = source.index(
        '"failed" if _targeted_retry_filter_diagnostic else None'
    )

    assert resume_filter < resolved_capture < diagnostic_call < forced_failure
    assert "resolved_urls=_targeted_retry_resolved_urls" in source[
        diagnostic_call:forced_failure
    ]
    assert "extraction_urls=[" in source[diagnostic_call:forced_failure]
    assert '_course_report.get("source_job_id")' in source[
        diagnostic_call - 700:diagnostic_call
    ]


def test_course_report_source_linkage_prefers_original_review_over_retry_parent() -> None:
    diagnostic = _targeted_retry_all_filtered_diagnostic(
        selected_urls=["https://example.edu/course/filtered"],
        retained_urls=[],
        retry_source_job_id="job_previous_continuation",
        source_review_job_id="job_original_review",
    )

    assert diagnostic is not None
    assert diagnostic["source_review_job_id"] == "job_original_review"
    assert diagnostic["retry_source_job_id"] == "job_previous_continuation"



def test_successfully_processed_targeted_retry_has_no_filter_diagnostic() -> None:
    url = "https://example.edu/course/a"

    assert _targeted_retry_all_filtered_diagnostic(
        selected_urls=[url],
        retained_urls=[url],
        retry_source_job_id="job_source",
    ) is None


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
        "kind": "sweep_unresolved",
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
        enable_browser_rescue=False,
    ))

    assert result.job_id == "job_continued"
    assert captured["body"].course_urls == ["https://example.edu/course/b"]
    assert captured["body"].retry_source_job_id == "job_source"


def test_continue_endpoint_excludes_urls_targeted_by_an_earlier_chain(
    monkeypatch,
) -> None:
    captured: dict = {}

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        execute_count = 0

        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
                request_payload={},
            )

        async def execute(self, _statement, _params):
            self.execute_count += 1
            if self.execute_count == 1:
                return _Result([
                    (
                        {
                            "kind": "extract_error",
                            "url": "https://example.edu/course/already-tried",
                            "reason": "per_course_timeout",
                            "retryable": True,
                        },
                        None,
                    ),
                    (
                        {
                            "kind": "extract_error",
                            "url": "https://example.edu/course/new",
                            "reason": "per_course_timeout",
                            "retryable": True,
                        },
                        None,
                    ),
                ])
            return _Result([
                (
                    {
                        "retrySourceJobId": "job_older",
                        "courseUrls": [
                            "https://example.edu/course/already-tried",
                        ],
                    },
                ),
            ])

    async def _fake_start(body, db):
        captured["body"] = body
        return ScrapeStartResponse(
            job_id="job_fresh_only",
            runtime_job_id="job_fresh_only",
        )

    monkeypatch.setattr(scrape, "start_scrape", _fake_start)

    result = asyncio.run(scrape.continue_unresolved_history_urls(
        "job_new_full_scrape",
        _Db(),
        enable_browser_rescue=False,
    ))

    assert result.job_id == "job_fresh_only"
    assert captured["body"].course_urls == ["https://example.edu/course/new"]


def test_continue_endpoint_rejects_urls_already_attempted_by_current_sweep() -> None:
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        execute_count = 0

        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
                request_payload={},
            )

        async def execute(self, _statement, _params):
            self.execute_count += 1
            if self.execute_count == 1:
                return _Result([
                    (
                        {
                            "kind": "extract_error",
                            "url": "https://example.edu/course/exhausted",
                            "reason": "per_course_timeout",
                            "retryable": True,
                        },
                        None,
                    ),
                    (
                        {
                            "kind": "sweep_unresolved",
                            "url": "https://example.edu/course/exhausted",
                            "reason": "per_course_timeout",
                        },
                        None,
                    ),
                ])
            return _Result([])

    with pytest.raises(scrape.HTTPException) as exc:
        asyncio.run(scrape.continue_unresolved_history_urls(
            "job_sweep_exhausted",
            _Db(),
            enable_browser_rescue=False,
        ))

    assert exc.value.status_code == 409
    assert "Recovery is exhausted" in exc.value.detail


def test_continue_endpoint_rejects_an_identical_second_continuation() -> None:
    class _Db:
        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
                request_payload={"retrySourceJobId": "job_parent"},
            )

    with pytest.raises(scrape.HTTPException) as exc:
        asyncio.run(scrape.continue_unresolved_history_urls(
            "job_continuation",
            _Db(),
            enable_browser_rescue=False,
        ))

    assert exc.value.status_code == 409
    assert "already retried unresolved URLs" in exc.value.detail


def test_continue_endpoint_rejects_repeated_browser_rescue() -> None:
    class _Db:
        async def get(self, _model, _job_id):
            return SimpleNamespace(
                university_id=12,
                url="https://example.edu/courses",
                request_payload={
                    "retrySourceJobId": "job_parent",
                    "browserRescueAttempted": True,
                },
            )

    with pytest.raises(scrape.HTTPException) as exc:
        asyncio.run(scrape.continue_unresolved_history_urls(
            "job_browser_continuation",
            _Db(),
            enable_browser_rescue=True,
        ))

    assert exc.value.status_code == 409
    assert "already been attempted" in exc.value.detail


def test_continue_endpoint_can_enable_browser_rescue_after_proven_skip(monkeypatch) -> None:
    captured: dict = {}
    job = SimpleNamespace(
        university_id=12,
        url="https://example.edu/courses",
    )
    university = SimpleNamespace(
        scrape_config={
            "admin_config": {
                "extraction": {
                    "skip_browser_rescue": True,
                    "skip_per_course_browser": True,
                },
            },
        },
    )

    class _Rows:
        def all(self):
            return [
                (
                    {
                        "kind": "extract_error",
                        "url": "https://example.edu/course/a",
                        "reason": "fetch_failed",
                        "retryable": True,
                        "message": (
                            "[BROWSER↑ SKIPPED] "
                            "skip_per_course_browser=true"
                        ),
                    },
                    None,
                ),
            ]

    class _Db:
        committed = False

        async def get(self, model, _row_id):
            return university if model is scrape.University else job

        async def execute(self, _statement, _params):
            return _Rows()

        async def commit(self):
            self.committed = True

    async def _fake_start(body, db):
        captured["body"] = body
        captured["db"] = db
        return ScrapeStartResponse(job_id="job_browser", runtime_job_id="job_browser")

    db = _Db()
    monkeypatch.setattr(scrape, "start_scrape", _fake_start)

    result = asyncio.run(scrape.continue_unresolved_history_urls(
        "job_source",
        db,
        enable_browser_rescue=True,
    ))

    assert result.job_id == "job_browser"
    assert db.committed is True
    assert university.scrape_config["admin_config"]["extraction"]["skip_browser_rescue"] is False
    assert university.scrape_config["admin_config"]["extraction"]["skip_per_course_browser"] is False
    assert captured["body"].course_urls == ["https://example.edu/course/a"]
    assert captured["body"].browser_rescue_attempted is True
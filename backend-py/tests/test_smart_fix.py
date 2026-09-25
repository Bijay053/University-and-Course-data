from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers import scrape
from app.services.scraper import smart_fix


def analysis(*fields, url=True, total=1):
    return {
        "total": total, "courses_with_url": int(url),
        "issues": [{"field": field, "missing": 1} for field in fields],
    }


def body(ids=None, targets=None, **kwargs):
    return scrape.ReExtractBody(
        ids=ids or [1], universityId=7, targetFields=targets or [], smart=True, **kwargs,
    )


def test_planner_exact_field_identity_and_scope():
    issues = analysis("duration", "english_requirements", "score_type")["issues"]
    assert smart_fix.plan_targets(issues, ["duration"]) == ["duration"]
    assert smart_fix.plan_targets(issues, ["english_requirements"]) == ["english_requirements"]
    assert smart_fix.plan_targets(issues, ["ielts_overall"]) == []
    assert smart_fix.plan_targets(issues, []) == ["duration", "english_requirements"]
    assert smart_fix.plan_targets([], [], ["course_location"]) == ["course_location"]


@pytest.mark.asyncio
async def test_per_row_targets_and_resolution_not_metadata(monkeypatch):
    analyzer = AsyncMock(side_effect=[
        analysis("duration", "academic_score"), analysis("academic_score"),
        analysis("academic_score"), analysis("academic_score"),
    ])
    extract = AsyncMock(side_effect=[
        {"results": [{"id": 1, "ok": True, "updated_fields": ["duration"]}]},
        {"results": [{"id": 2, "ok": True, "made_progress": True,
                      "refreshed_evidence_fields": ["academic_score"]}]},
    ])
    monkeypatch.setattr(scrape, "analyze_staged", analyzer)
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    results = (await smart_fix.run_smart_batch(
        body([1, 2], ["duration", "academic_score"]), SimpleNamespace(),
    ))["results"]
    assert extract.call_args_list[0].args[0].target_fields == ["academic_score", "duration"]
    assert extract.call_args_list[1].args[0].target_fields == ["academic_score"]
    assert results[0]["resolved_fields"] == ["duration"]
    assert results[0]["made_progress"]
    assert results[1]["made_progress"] is False
    assert results[1]["attempted"]
    assert results[1]["next_action"] == "report_official_url"
    assert extract.await_count == 2  # no blind repeat of identical inputs


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot,targets,reason", [
    (analysis(), ["duration"], "already_resolved"),
    (analysis("duration", url=False), ["duration"], "missing_official_url"),
    (analysis(), ["score_type"], "unsupported_targets"),
    (analysis(total=0), ["duration"], "course_not_found"),
])
async def test_no_source_resolved_unsupported_safety(monkeypatch, snapshot, targets, reason):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(return_value=snapshot))
    extract = AsyncMock()
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    result = (await smart_fix.run_smart_batch(body(targets=targets), SimpleNamespace()))["results"][0]
    assert result["reason_code"] == reason
    assert not result["attempted"]
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_deadline_no_new_extraction(monkeypatch):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(return_value=analysis("duration")))
    extract = AsyncMock()
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    clock = iter([0, 241])
    monkeypatch.setattr(smart_fix, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = (await smart_fix.run_smart_batch(body(targets=["duration"]), SimpleNamespace()))["results"][0]
    assert result["reason_code"] == "budget_exhausted"
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_isolated_to_row(monkeypatch):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(side_effect=[
        analysis("duration"), analysis("duration"), analysis(),
    ]))
    extract = AsyncMock(side_effect=[
        RuntimeError("provider failed"),
        {"results": [{"id": 2, "ok": True, "updated_fields": ["duration"]}]},
    ])
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    db = SimpleNamespace(rollback=AsyncMock())
    results = (await smart_fix.run_smart_batch(body([1, 2], ["duration"]), db))["results"]
    assert results[0]["reason_code"] == "source_recovery_failed"
    assert results[1]["reason_code"] == "issues_resolved"
    db.rollback.assert_awaited_once()


def test_smart_mode_part_of_durable_identity():
    job = SimpleNamespace(request_payload={
        "courseIds": [1], "targetFields": ["duration"], "sourceJobId": "review",
        "smart": True,
    })
    scope = dict(course_ids=[1], target_fields=["duration"], source_job_id="review")
    assert scrape._bulk_fix_request_matches(job, **scope, smart=True)
    assert not scrape._bulk_fix_request_matches(job, **scope)
    assert not scrape._bulk_fix_request_matches(
        job, course_ids=[1], target_fields=["international_fee"],
        source_job_id="review", smart=True,
    )


def test_central_candidates_are_configured_and_relevant_only():
    config = {"uniPages": {"feePage": "https://example.edu/fees",
                           "entryPage": "https://example.edu/english",
                           "accommodationPage": "https://example.edu/housing"}}
    assert smart_fix.central_recovery_config(config, ["duration"]) is None
    assert smart_fix.central_recovery_config(config, ["international_fee"])["uniPages"] == {
        "feePage": "https://example.edu/fees",
    }
    assert smart_fix.central_recovery_config({}, ["international_fee"]) is None


@pytest.mark.asyncio
async def test_fresh_central_no_identical_evidence_retry(monkeypatch):
    from app.services.scraper import central_pages
    from app.services import scraper_config_ai
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda url: (True, ""))
    previous = {"english": {"ielts_overall": 6}, "fees": [{"amount": 10000}]}
    prefetch = AsyncMock(return_value={"english": {"ielts_overall": 6}, "fees": []})
    monkeypatch.setattr(central_pages, "prefetch_central_pages", prefetch)
    config = {"uniPages": {"entryPage": "https://example.edu/english"}}
    assert await smart_fix.refresh_central_recovery(
        config, ["english_requirements"], previous, 20,
    ) is None
    assert prefetch.call_args.kwargs == {"university_id": None}
    prefetch.return_value = {"english": {"ielts_overall": 6, "ielts_writing": 5.5}}
    fresh = await smart_fix.refresh_central_recovery(
        config, ["english_requirements"], previous, 20,
    )
    assert fresh["fees"] == previous["fees"]
    assert fresh["english"]["ielts_writing"] == 5.5


@pytest.mark.asyncio
async def test_no_source_or_budget_no_central_fetch(monkeypatch):
    from app.services.scraper import central_pages
    fetch = AsyncMock()
    monkeypatch.setattr(central_pages, "prefetch_central_pages", fetch)
    assert await smart_fix.refresh_central_recovery({}, ["international_fee"], {}, 30) is None
    assert await smart_fix.refresh_central_recovery(
        {"uniPages": {"feePage": "https://example.edu/fees"}},
        ["international_fee"], {}, 0,
    ) is None
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_after_extraction_does_not_resolve_issues(monkeypatch):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(side_effect=[
        analysis("duration"), analysis(total=0),
    ]))
    monkeypatch.setattr(scrape, "re_extract_staged", AsyncMock(return_value={
        "results": [{"id": 1, "ok": True, "made_progress": True,
                     "updated_fields": ["duration"]}],
    }))
    result = (await smart_fix.run_smart_batch(
        body(targets=["duration"]), SimpleNamespace(),
    ))["results"][0]
    assert not result["ok"]
    assert result["reason_code"] == "course_changed_during_fix"
    assert result["resolved_fields"] == []
    assert result["unresolved_fields"] == ["duration"]
    assert result["made_progress"] is False
    assert result["attempted"] is True


@pytest.mark.asyncio
async def test_post_analysis_failure_preserves_other_row_results(monkeypatch):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(side_effect=[
        analysis("duration"), analysis(),
        analysis("duration"), RuntimeError("database unavailable"),
        analysis("duration"), analysis(),
    ]))
    monkeypatch.setattr(scrape, "re_extract_staged", AsyncMock(side_effect=[
        {"results": [{"id": i, "ok": True, "updated_fields": ["duration"]}]}
        for i in (1, 2, 3)
    ]))
    db = SimpleNamespace(rollback=AsyncMock())
    results = (await smart_fix.run_smart_batch(
        body([1, 2, 3], ["duration"]), db,
    ))["results"]
    assert [r["reason_code"] for r in results] == [
        "issues_resolved", "post_analysis_failed", "issues_resolved",
    ]
    assert results[1]["updated_fields"] == ["duration"]  # committed change is still visible
    assert results[1]["resolved_fields"] == []
    assert results[1]["unresolved_fields"] == ["duration"]
    assert not results[1]["made_progress"]
    assert not results[1]["ok"]
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"id": 1, "ok": False, "made_progress": True},
    {"id": 2, "ok": True, "made_progress": True},
])
async def test_failed_or_mismatched_extraction_cannot_claim_resolution(monkeypatch, result):
    analyzer = AsyncMock(side_effect=[analysis("duration"), analysis()])
    monkeypatch.setattr(scrape, "analyze_staged", analyzer)
    monkeypatch.setattr(scrape, "re_extract_staged", AsyncMock(return_value={"results": [result]}))
    item = (await smart_fix.run_smart_batch(
        body(targets=["duration"]), SimpleNamespace(),
    ))["results"][0]
    assert item["id"] == 1
    assert not item["ok"]
    assert not item["made_progress"]
    assert item["resolved_fields"] == []
    assert item["unresolved_fields"] == ["duration"]
    analyzer.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_analysis_failure_isolated(monkeypatch):
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(side_effect=[
        RuntimeError("analysis failed"), analysis(),
    ]))
    extractor = AsyncMock()
    monkeypatch.setattr(scrape, "re_extract_staged", extractor)
    db = SimpleNamespace(rollback=AsyncMock())
    results = (await smart_fix.run_smart_batch(
        body([1, 2], ["duration"]), db,
    ))["results"]
    assert results[0]["reason_code"] == "initial_analysis_failed"
    assert results[0]["resolved_fields"] == []
    assert results[1]["reason_code"] == "already_resolved"
    extractor.assert_not_awaited()


def test_new_evidence_retry_preserves_first_pass_improvements_and_scope():
    row = SimpleNamespace(duration=None, international_fee=None, ielts_overall=6,
                          ielts_writing=None, course_location="London")
    first_pass = {"duration": 3, "international_fee": 18000, "ielts_overall": 6,
                  "ielts_writing": None, "course_location": "Unrelated"}
    fields = smart_fix.smart_retry_fields(
        {"duration", "international_fee", "ielts_overall", "ielts_writing"}, row, first_pass,
    )
    assert fields == {"ielts_overall", "ielts_writing"}
    second_pass = {"duration": 4, "international_fee": 25000, "ielts_overall": 6,
                   "ielts_writing": 5.5, "course_location": "Different"}
    for field, value in second_pass.items():
        if field in fields and value not in (None, "", []):
            first_pass[field] = value
    assert first_pass["duration"] == 3
    assert first_pass["international_fee"] == 18000
    assert first_pass["ielts_writing"] == 5.5
    assert first_pass["course_location"] == "Unrelated"


@pytest.mark.asyncio
async def test_private_configured_source_is_not_fetched(monkeypatch):
    from app.services.scraper import central_pages
    from app.services import scraper_config_ai
    monkeypatch.setattr(
        scraper_config_ai.socket, "getaddrinfo",
        lambda *_: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    fetch = AsyncMock()
    monkeypatch.setattr(central_pages, "prefetch_central_pages", fetch)
    with pytest.raises(ValueError, match="public HTTP"):
        await smart_fix.refresh_central_recovery(
            {"uniPages": {"feePage": "http://169.254.169.254/latest/meta-data"}},
            ["international_fee"], {}, 30,
        )
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_explicit_configured_external_source_passes_to_existing_fetch(monkeypatch):
    from app.services.scraper import central_pages
    from app.services import scraper_config_ai
    checked = []

    def public(url):
        checked.append(url)
        return True, ""

    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", public)
    fetch = AsyncMock(return_value={})
    monkeypatch.setattr(central_pages, "prefetch_central_pages", fetch)
    official_document = "https://official-documents.example/fees.pdf"
    await smart_fix.refresh_central_recovery(
        {"uniPages": {"feesPdf": official_document,
                      "entryPage": "https://unrelated.example/english",
                      "unknown": "http://127.0.0.1/private"}},
        ["international_fee"], {}, 30,
    )
    assert checked == [official_document]
    assert fetch.call_args.args[0]["uniPages"] == {"feesPdf": official_document}
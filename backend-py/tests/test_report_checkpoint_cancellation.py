"""Execute production settlement/gate helpers through real asyncio deadlines."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers.scrape_reports import report_result
from app.services.scraper import orchestrator as orch
from app.services.scraper.autonomous_verification import (
    VerificationLimits, checkpoint_report_urls, run_bounded_verification,
)


URL = "https://uni.edu/cpd/workshop"
NEXT = "https://uni.edu/course/degree?award=2"


def memory():
    job = SimpleNamespace(
        runtime_job_id="child", university_id=7, status="running",
        request_payload={"courseReport": {"id": "report", "source_job_id": "source"},
                         "course_urls": [URL, NEXT]},
        discovered_config={}, fast_mode=False, total_found=2, imported=0,
        skipped=0, errors=0, error_message=None, gate_skip_counts={},
        cost_ceiling_hit=False, completed_at=None,
    )
    durable = {}

    async def commit():
        durable.clear()
        durable.update(deepcopy(vars(job)))

    async def rollback():
        vars(job).clear()
        vars(job).update(deepcopy(durable))

    db = SimpleNamespace(
        commit=AsyncMock(side_effect=commit), rollback=AsyncMock(side_effect=rollback),
        get=AsyncMock(return_value=job),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: 0)),
    )
    policy = VerificationLimits("source", "report", time_budget_seconds=0.03, round_index=1)
    return db, job, policy, durable


def remaining(durable):
    # Read only committed/reloaded state, not the mutated ORM object.
    return report_result(
        SimpleNamespace(**deepcopy(durable)),
        {"autonomous": {"phase": "needs_review"}},
    )["continuation"]["remaining_urls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    {"url": URL, "error": "skipped:cpd_short_course"},
    {"url": URL, "error": "fetch_failed"},
    RuntimeError("extractor failed"),
    {"url": URL, "_retry_after": 2},
])
async def test_timeout_during_settled_skip_error_event_keeps_checkpoint(outcome):
    db, job, policy, durable = memory()
    event_started = asyncio.Event()

    async def emit_progress(link):
        event_started.set()
        await asyncio.Event().wait()

    async def run():
        return await orch._settle_course_with_retries(
            {"url": URL}, AsyncMock(return_value=outcome), emit_progress,
            max_retries=0,
            record_settled=lambda link, result: orch._checkpoint_extraction_outcome(
                db, job, policy, link, result,
            ),
        )

    result = await run_bounded_verification(db, job, policy, run)
    assert event_started.is_set()
    assert result["reason"] == "time_budget_exhausted"
    assert remaining(durable) == [NEXT]
    assert durable["discovered_config"]["autonomousVerification"]["completed_urls"] == [URL]
    assert durable["imported"] == 0


@pytest.mark.asyncio
async def test_timeout_during_prefetch_exclusion_event_keeps_gate_checkpoint():
    db, job, policy, durable = memory()
    event_started = asyncio.Event()
    links = [{"url": URL, "name": "Professional CPD workshop"},
             {"url": NEXT, "name": "Bachelor of Science"}]

    async def run():
        retained, dropped = await orch._filter_report_non_degree_candidates(
            db, job, policy, links, enabled=True,
            force_url_patterns=[r"/cpd/"], allow_url_patterns=[],
            allow_title_patterns=[], force_title_patterns=[],
        )
        assert [link["url"] for link in retained] == [NEXT]
        assert [link["url"] for link in dropped] == [URL]
        # Real cancellation at the very next event await; the normal end-of-run
        # acknowledgement is deliberately never executed.
        event_started.set()
        await asyncio.Event().wait()

    result = await run_bounded_verification(db, job, policy, run)
    assert event_started.is_set()
    assert result["reason"] == "time_budget_exhausted"
    assert remaining(durable) == [NEXT]


@pytest.mark.asyncio
async def test_cancel_during_checkpoint_waits_for_commit_even_with_repeat_cancel():
    db, job, policy, durable = memory()
    await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    save = db.commit.side_effect

    async def slow_commit():
        entered.set()
        await release.wait()
        await save()

    db.commit.side_effect = slow_commit
    task = asyncio.create_task(checkpoint_report_urls(db, job, policy, [URL]))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert remaining(durable) == [URL, NEXT]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.rollback()
    assert remaining(durable) == [NEXT]


@pytest.mark.asyncio
async def test_successful_fetch_not_yet_staged_remains_after_timeout():
    db, job, policy, durable = memory()

    async def progress(link):
        await asyncio.Event().wait()

    async def run():
        return await orch._settle_course_with_retries(
            {"url": URL}, AsyncMock(return_value={"url": URL, "payload": {"course_name": "Degree"}}),
            progress,
            record_settled=lambda link, result: orch._checkpoint_extraction_outcome(
                db, job, policy, link, result,
            ),
        )

    await run_bounded_verification(db, job, policy, run)
    assert remaining(durable) == [URL, NEXT]


@pytest.mark.asyncio
async def test_retryable_result_not_acknowledged_before_retry_exhaustion():
    db, job, policy, durable = memory()

    async def sleep(delay):
        await asyncio.Event().wait()

    async def run():
        return await orch._settle_course_with_retries(
            {"url": URL}, AsyncMock(return_value={"url": URL, "_retry_after": 1}),
            AsyncMock(), sleep=sleep,
            record_settled=lambda link, result: orch._checkpoint_extraction_outcome(
                db, job, policy, link, result,
            ),
        )

    await run_bounded_verification(db, job, policy, run)
    assert remaining(durable) == [URL, NEXT]


@pytest.mark.asyncio
async def test_checkpoint_commit_failure_is_explicit_not_success():
    db, job, policy, durable = memory()
    await db.commit()
    db.commit.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await checkpoint_report_urls(db, job, policy, [URL])
    assert "completed_urls" not in job.discovered_config.get("autonomousVerification", {})
    await db.rollback()
    assert remaining(durable) == [URL, NEXT]


@pytest.mark.asyncio
async def test_budget_timeout_waits_for_prefetch_checkpoint_before_rollback():
    db, job, policy, durable = memory()
    save = db.commit.side_effect
    committing = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_checkpoint_commit():
        nonlocal calls
        calls += 1
        if calls == 2:  # first commit is run_bounded_verification initialization
            committing.set()
            await release.wait()
        await save()

    db.commit.side_effect = slow_checkpoint_commit

    async def run():
        await orch._filter_report_non_degree_candidates(
            db, job, policy,
            [{"url": URL, "name": "CPD workshop"}, {"url": NEXT, "name": "Bachelor of Science"}],
            enabled=True, force_url_patterns=[r"/cpd/"],
        )
        pytest.fail("Cancellation must propagate after the protected commit")

    task = asyncio.create_task(run_bounded_verification(db, job, policy, run))
    await committing.wait()
    await asyncio.sleep(policy.time_budget_seconds * 2)
    assert not task.done()
    db.rollback.assert_not_awaited()
    release.set()
    result = await task
    assert result["reason"] == "time_budget_exhausted"
    assert remaining(durable) == [NEXT]
    db.rollback.assert_awaited_once()
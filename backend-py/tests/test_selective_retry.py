"""Selected retries against a socket-only disposable PostgreSQL schema."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, text

from app.models import ScrapeRuntimeJob, ScrapedCourse
from app.models.scrape_runtime import ScrapeRuntimeLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import selective_retry as retry, scrape_assistant as service
from tests.test_scrape_prompt_assistant import store, assistant_postgres
from tests.test_assistant_eligibility import seed_thread

URL = "https://www.example.edu/course/failed?option=standard"


@pytest.mark.asyncio
async def test_real_recipe_fingerprint_includes_database_overrides():
    uni = SimpleNamespace(id=1, slug="", name="Example University",
        scrape_url="https://www.example.edu", scrape_config={})
    before = await retry.effective_config(None, uni)
    uni.scrape_config = {"extraction": {"fee_year": 2027}}
    assert before != await retry.effective_config(None, uni)


async def seed(store, monkeypatch, *, count=1):
    await seed_thread(store)
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    monkeypatch.setattr(retry, "effective_config", AsyncMock(return_value="config-v1"))
    from app.tasks.scrape_tasks import scrape_university
    sent = []
    monkeypatch.setattr(scrape_university, "delay", lambda jid: sent.append(jid))
    monkeypatch.setattr("app.tasks.scrape_tasks.set_initial_dispatch_lock", lambda jid: None)
    async with store() as db:
        await service.ensure_schema(db)
        source = await db.get(ScrapeRuntimeJob, "source")
        source.completed_at = datetime.now(timezone.utc)
        source.url = "https://www.example.edu"
        source.current = 17
        source.worker_id = "old-worker"
        for n in range(count):
            db.add(ScrapeRuntimeLog(runtime_job_id="source", sequence=n + 1, event="status",
                payload={"kind": "extract_error", "url": URL + str(n) if count > 1 else URL,
                         "retryable": True, "reason": "per_course_timeout"}))
        await db.commit()
    return sent


@pytest.mark.asyncio
async def test_reservation_is_atomic_idempotent_and_keeps_source_progress(store, monkeypatch):
    sent = await seed(store, monkeypatch)
    async with store() as db:
        action = await retry.proposal(db, "source", [URL], attempts=2, backoff=7)
        child = await retry.queue(db, action, 77)
        jid = child.runtime_job_id
        assert child.request_payload["courseUrls"] == [URL]
        assert child.request_payload["selectiveRetry"]["budgets"] == {URL: 2}
        assert "fullCatalogueReviewOnly" not in child.request_payload
        assert child.request_payload["selectiveRetry"]["after"]["reservedAttempts"][URL] == 2
    async with store() as db:
        assert (await retry.queue(db, action, 77)).runtime_job_id == jid
        with pytest.raises(ValueError, match="reserved or exhausted"):
            await retry.proposal(db, "source", [URL], attempts=2)
        source = await db.get(ScrapeRuntimeJob, "source")
        assert (source.status, source.current, source.imported) == ("completed", 17, 1)
    assert sent == [jid]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "queued", "awaiting_approval"])
async def test_active_source_never_queues_overlapping_recovery(store, monkeypatch, status):
    sent = await seed(store, monkeypatch)
    async with store() as db:
        source = await db.get(ScrapeRuntimeJob, "source")
        source.status = status
        await db.commit()
        with pytest.raises(ValueError, match="has not settled"):
            await retry.proposal(db, "source")
    assert sent == []


@pytest.mark.asyncio
async def test_only_current_unresolved_variants_are_eligible(store, monkeypatch):
    await seed(store, monkeypatch)
    async with store() as db:
        with pytest.raises(ValueError, match="exact scrape"):
            await retry.proposal(db, "source", ["https://www.example.edu/course/failed?option=placement"])
        db.add(ScrapeRuntimeLog(runtime_job_id="source", sequence=2, event="status",
            payload={"kind": "sweep_recovered", "url": URL}))
        await db.commit()
        with pytest.raises(ValueError, match="No settled"):
            await retry.proposal(db, "source")


@pytest.mark.asyncio
async def test_saved_success_wins_over_old_failure_without_reextracting(store, monkeypatch):
    await seed(store, monkeypatch)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_website = URL
        await db.commit()
        with pytest.raises(ValueError, match="No settled"):
            await retry.proposal(db, "source")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["config", "owner", "overlap"])
async def test_stale_proofs_and_provider_overlap_refuse_without_child(store, monkeypatch, changed):
    sent = await seed(store, monkeypatch)
    async with store() as db:
        action = await retry.proposal(db, "source", [URL])
        if changed == "config":
            retry.effective_config.return_value = "config-v2"
        elif changed == "owner":
            (await db.get(ScrapeRuntimeJob, "source")).worker_id = "different-worker"
            await db.commit()
        else:
            db.add(ScrapeRuntimeJob(runtime_job_id="other", university_id=1, job_type="scrape", status="queued"))
            await db.commit()
        with pytest.raises(ValueError):
            await retry.queue(db, action, 77)
    assert sent == []


@pytest.mark.asyncio
async def test_durable_attempt_cap_includes_interrupted_attempts(store, monkeypatch):
    await seed(store, monkeypatch)
    async with store() as db:
        child = await retry.queue(db, await retry.proposal(db, "source", attempts=2), 77)
        child.status = "running"
        await db.commit()
        jid = child.runtime_job_id
    first = await retry.reserve_attempt(jid, URL)
    assert first[1] == 1
    # No finish receipt: a killed/uncertain attempt must remain spent.
    second = await retry.reserve_attempt(jid, URL)
    assert second[1] == 2 and await retry.reserve_attempt(jid, URL) is None
    await retry.finish_attempt(second[0], {"error": "per_course_timeout", "retryable": True})
    async with store() as db:
        records = (await db.execute(text(
            "SELECT outcome FROM scrape_retry_attempts WHERE job_id=:job ORDER BY ordinal"
        ), {"job": jid})).scalars().all()
    assert records == ["started", "error"]


@pytest.mark.asyncio
async def test_confirmation_and_result_readback_use_stored_rows(store, monkeypatch):
    await seed(store, monkeypatch, count=11)
    from app.routers.scrape_assistant import confirm_action, ConfirmBody, response
    async with store() as db:
        action = await retry.proposal(db, "source")
        assert action["requiresConfirmation"]
        action["_turnId"] = "turn"
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status = "idle"
        thread.messages = [{"role": "assistant", "actions": [action]}]
        await db.commit()
        reply = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        jid = reply["messages"][0]["actions"][0]["jobId"]
        child = await db.get(ScrapeRuntimeJob, jid)
        child.status = "completed"
        await db.commit()
        reply = await response(thread, db, "source")
        result = reply["messages"][0]["actions"][0]
        assert result["state"] == "error"  # completed worker is not verified recovery
        assert "11 unverified" in result["resultSummary"]


@pytest.mark.parametrize("attempts,delay", [(0, 30), (4, 30), (True, 30), (1, 0), (1, 301), (1, True)])
def test_limits_are_bounded(attempts, delay):
    with pytest.raises(ValueError):
        retry.limits(attempts, delay)


@pytest.mark.asyncio
async def test_timeout_recovery_backoff_and_one_progress_increment():
    from app.services.scraper.orchestrator import _settle_course_with_retries
    attempt = AsyncMock(side_effect=[{"error": "per_course_timeout", "retryable": True}, {"payload": {"course_name": "BSc"}}])
    progress, sleep = AsyncMock(), AsyncMock()
    result = await _settle_course_with_retries({"url": URL}, attempt, progress,
        sleep=sleep, max_retries=1, retry_errors=True, minimum_backoff=7, initial_delay=7)
    assert result["payload"]["course_name"] == "BSc"
    assert attempt.await_count == 2 and progress.await_count == 1
    assert [c.args[0] for c in sleep.await_args_list] == [7, 7]


@pytest.mark.asyncio
async def test_challenge_is_not_retried_and_revoked_owner_is_never_swallowed():
    from app.services.scraper.orchestrator import _settle_course_with_retries
    from app.services.worker_fencing import OwnershipLost
    attempt = AsyncMock(return_value={"error": "challenge_page", "retryable": False})
    await _settle_course_with_retries({}, attempt, AsyncMock(), retry_errors=True)
    assert attempt.await_count == 1
    with pytest.raises(OwnershipLost):
        await _settle_course_with_retries({}, AsyncMock(side_effect=OwnershipLost("revoked")), AsyncMock(), retry_errors=True)


@pytest.mark.parametrize("prompt,expected", [
    ("Retry only failed courses, do not stop the job", True),
    ("Please retry the failed URLs. Don't retry successful courses", True),
    ("Why did the retry of failed courses happen?", False),
    ("Do not retry failed courses", False),
    ("Retry failed courses, but actually don't retry them", False),
])
def test_retry_permission_comes_from_the_actual_user_instruction(prompt, expected):
    assert retry.retry_requested(prompt) == expected


@pytest.mark.asyncio
async def test_process_recovery_returns_existing_child_without_second_reservation(store, monkeypatch):
    sent = await seed(store, monkeypatch)
    async with store() as db:
        first = await retry.prepare_for_turn(db, "source", [URL], 77, "turn", "eligibility")
    async with store() as db:
        second = await retry.prepare_for_turn(db, "source", [URL], 77, "turn", "eligibility")
    assert first["jobId"] == second["jobId"] and len(sent) == 1


@pytest.mark.asyncio
async def test_named_scope_and_timeout_scope_do_not_expand_to_all_failures(store, monkeypatch):
    await seed(store, monkeypatch)
    accounting = "https://www.example.edu/courses/bsc-accounting"
    async with store() as db:
        db.add(ScrapeRuntimeLog(runtime_job_id="source", sequence=2, event="status",
            payload={"kind": "extract_error", "url": accounting, "retryable": True, "reason": "http_503"}))
        await db.commit()
        assert await retry.scope_from_prompt(db, "source", "Retry failed BSc Accounting") == [accounting]
        assert await retry.scope_from_prompt(db, "source", "Retry timeouts only") == [URL]
        with pytest.raises(ValueError, match="No current failed"):
            await retry.scope_from_prompt(db, "source", "Retry failed BSc Psychology")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 11])
async def test_real_chat_handler_saves_queue_or_confirmation_receipt(store, monkeypatch, count):
    sent = await seed(store, monkeypatch, count=count)
    assert await retry.handle(service, "eligibility", "turn", "owner",
        {"job": {"id": "source"}, "university_id": 1}, "Retry all failed courses with 2 attempts and backoff 7s")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        assert action["kind"] == "retry_failed" and action["courseCount"] == count
        assert action["state"] == ("running" if count == 1 else "proposed")
        assert action["requiresConfirmation"] == (count > 10)
        assert len(sent) == (1 if count == 1 else 0)
        assert thread.status == "ready"


@pytest.mark.asyncio
async def test_superseded_chat_turn_cannot_queue_retry(store, monkeypatch):
    sent = await seed(store, monkeypatch)
    assert await retry.handle(service, "eligibility", "older", "older-owner",
        {"job": {"id": "source"}}, "Retry failed courses")
    assert sent == []


@pytest.mark.asyncio
async def test_actual_staging_boundary_rechecks_source_after_attempt_reservation(store, monkeypatch):
    from app.services.scraper import orchestrator
    import inspect
    # Protect the real production callsite, not an unused convenience helper.
    source = inspect.getsource(orchestrator._run_claimed_scrape)
    assert "await _stage_claimed_result(" in source and "await stage_course(" not in source
    await seed(store, monkeypatch)
    async with store() as db:
        job = await retry.queue(db, await retry.proposal(db, "source"), 77)
        job.status = "running"
        await db.commit()
    assert await retry.reserve_attempt(job.runtime_job_id, URL)
    # Simulate ownership changing while the reserved extraction is in flight.
    async with store() as db:
        parent = await db.get(ScrapeRuntimeJob, "source")
        parent.worker_id = "replacement-owner"
        await db.commit()
    persist = AsyncMock()
    monkeypatch.setattr(orchestrator, "stage_course", persist)
    async with store() as stage_db:
        with pytest.raises(ValueError, match="source ownership changed"):
            await orchestrator._stage_claimed_result(stage_db, job=job,
                scrape_job_id=job.runtime_job_id, university_id=1,
                course_name="BSc Accounting", source_url=URL,
                payload={"course_name": "BSc Accounting"})
    persist.assert_not_awaited()
    async with store() as db:
        assert len((await db.execute(select(ScrapedCourse))).scalars().all()) == 1
        assert (await db.get(ScrapeRuntimeJob, "source")).current == 17


@pytest.mark.asyncio
async def test_actual_staging_boundary_holds_source_lock_through_commit(store, monkeypatch):
    import asyncio
    from app.services.scraper import orchestrator
    await seed(store, monkeypatch)
    async with store() as db:
        job = await retry.queue(db, await retry.proposal(db, "source"), 77)
    started = asyncio.Event()
    writer = None
    async def replace_owner():
        async with store() as db:
            started.set()
            await db.execute(text("UPDATE scrape_runtime_jobs SET worker_id='replacement' WHERE runtime_job_id='source'"))
            await db.commit()
    async def persist(stage_db, **kwargs):
        nonlocal writer
        writer = asyncio.create_task(replace_owner())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not writer.done(), "Source ownership must remain locked until course commit"
        await stage_db.commit()
        return SimpleNamespace(saved=True)
    monkeypatch.setattr(orchestrator, "stage_course", persist)
    try:
        async with store() as stage_db:
            assert (await orchestrator._stage_claimed_result(stage_db, job=job,
                scrape_job_id=job.runtime_job_id, university_id=1)).saved
        await asyncio.wait_for(writer, 5)
    finally:
        if writer and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

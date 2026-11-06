"""Real worker admission and isolated PostgreSQL receipts; no live scrape runs."""
import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from tests.test_scrape_prompt_assistant import store, assistant_postgres
from app.models import CourseAuditLog, ScrapeRuntimeJob, University, ScrapedCourse
from app.models.user import User
from app.models.scrape_assistant import ScrapeAssistantThread
from app.routers.scrape_assistant import confirm_action, ConfirmBody, get_assistant
from app.services import scrape_runtime_controls as controls
from app.services.scrape_assistant_autorun import choose_action

ACTOR = {"id": 7, "is_super_admin": True}


async def worker(store, job_id="source", ceiling=4):
    async with store() as db:
        if not await db.get(User, 7):
            db.add(User(id=7, email="runtime@example.test", password_hash="unused",
                        is_active=True, is_super_admin=True))
        job = await db.get(ScrapeRuntimeJob, job_id)
        if not job:
            job = ScrapeRuntimeJob(runtime_job_id=job_id, university_id=1,
                                   job_type="scrape", status="running")
            db.add(job)
        job.status, job.worker_id, job.worker_pid = "running", "worker-1", 321
        job.claim_count, job.claimed_at = 1, datetime.now(timezone.utc)
        job.request_payload = {"preserved": "original evidence"}
        await db.commit()
        owner = controls.generation(job)
    gate = controls.RuntimeGate(job_id, ceiling, owner, session_factory=store)
    await gate.refresh()
    return gate


async def preview(store, job_id="source", **values):
    async with store() as db:
        job = await db.get(ScrapeRuntimeJob, job_id)
        state = await controls.control(db, job)
        action = controls.proposal(job, {**state["applied"], **values}, "turn", state)
        thread = await db.get(ScrapeAssistantThread, "thread-" + job_id)
        if not thread:
            thread = ScrapeAssistantThread(id="thread-" + job_id, job_id=job_id,
                actor_id=7, turn_id="turn", status="ready", messages=[])
            db.add(thread)
        thread.messages = [{"role": "assistant", "actions": [action]}]
        await db.commit()
        return action


async def confirm(store, action, job_id="source"):
    async with store() as db:
        return await confirm_action(job_id, action["id"], ConfirmBody(confirm=True), db, ACTOR)


async def tick(gate):
    gate.last_poll = 0
    await gate.refresh()


@pytest.mark.asyncio
async def test_confirm_pending_ack_audit_scope_and_progress_preserved(store):
    gate = await worker(store)
    other = await worker(store, "other")
    action = await preview(store, concurrency=2, retries=1, backoffSeconds=30)
    assert choose_action([action]) is None  # Never automatic, even below ten rows.
    result = await confirm(store, action)
    assert result["messages"][0]["actions"][0]["state"] == "running"
    assert gate.values["concurrency"] == 4
    await confirm(store, action)  # Idempotent, never dispatch twice.
    await tick(gate)
    async with store() as db:
        result = await get_assistant("source", db, ACTOR)
        public = result["messages"][0]["actions"][0]
        assert public["state"] == "completed"
        assert "Worker acknowledged" in public["resultSummary"]
        assert "_generation" not in public
        job = await db.get(ScrapeRuntimeJob, "source")
        assert job.imported == 1 and job.current == 0 and job.status == "running"
        assert job.request_payload == {"preserved": "original evidence"}
        assert (await db.get(ScrapedCourse, 1)).status == "pending"
        assert (await db.get(University, 1)).scrape_config == {}
        audits = (await db.execute(select(CourseAuditLog))).scalars().all()
        assert [a.action for a in audits] == ["runtime_tuning_requested", "runtime_tuning_applied"]
        assert all(a.actor == "7" and "Job source" in a.reason for a in audits)
        assert "'concurrency': 4" in audits[0].old_value
        assert "'concurrency': 2" in audits[0].new_value
    assert gate.values == {"concurrency": 2, "retries": 1, "backoffSeconds": 30}
    assert other.values == {"concurrency": 4, "retries": 2, "backoffSeconds": 0}
    await confirm(store, action)


@pytest.mark.asyncio
async def test_change_during_active_work_drains_without_cancelling(store):
    gate = await worker(store, ceiling=2)
    first_done, second_done, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def active(release):
        async with gate:
            await release.wait()
    a = asyncio.create_task(active(first_done))
    b = asyncio.create_task(active(second_done))
    await asyncio.sleep(0.05)
    assert gate.active == 2
    await confirm(store, await preview(store, concurrency=1))
    await tick(gate)
    async def queued():
        async with gate:
            started.set()
    c = asyncio.create_task(queued())
    try:
        first_done.set()
        await a
        await asyncio.sleep(0.3)
        assert not started.is_set() and not b.done()
        second_done.set()
        await asyncio.wait_for(asyncio.gather(b, c), 2)
        assert started.is_set() and gate.active == 0
        # Restore only up to the original ceiling, never acquire fleet capacity.
        await confirm(store, await preview(store, concurrency=2))
        await tick(gate)
        assert gate.values["concurrency"] == 2
    finally:
        for task in (a, b, c):
            task.cancel()
        await asyncio.gather(a, b, c, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["confirmation", "consumption"])
async def test_revoked_or_disabled_actor_cannot_apply(store, when):
    gate = await worker(store)
    action = await preview(store, concurrency=1)
    if when == "consumption":
        await confirm(store, action)
    async with store() as db:
        user = await db.get(User, 7)
        user.is_super_admin = False
        user.is_active = when != "consumption"
        await db.commit()
    if when == "confirmation":
        with pytest.raises(HTTPException) as exc:
            await confirm(store, action)
        assert exc.value.status_code == 403
    else:
        await tick(gate)
        async with store() as db:
            result = await get_assistant("source", db, ACTOR)
            assert result["messages"][0]["actions"][0]["state"] == "error"
    assert gate.values["concurrency"] == 4


@pytest.mark.asyncio
async def test_stale_preview_and_wrong_job_or_actor_refused(store):
    gate = await worker(store)
    await worker(store, "other")
    action = await preview(store)
    async with store() as db:
        for job_id, actor in (("other", ACTOR), ("source", {"id": 8})):
            with pytest.raises(HTTPException) as exc:
                await confirm_action(job_id, action["id"], ConfirmBody(confirm=True), db, actor)
            assert exc.value.status_code == 404
        job = await db.get(ScrapeRuntimeJob, "source")
        state = await controls.control(db, job)
        state["revision"] += 1
        await controls.store(db, job, state)
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await confirm(store, action)
    assert exc.value.status_code == 409
    assert gate.values["concurrency"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["expired", "generation", "stopped"])
async def test_pending_request_cannot_be_applied_by_stale_worker(store, stale):
    gate = await worker(store)
    action = await preview(store, concurrency=1)
    await confirm(store, action)
    async with store() as db:
        job = await db.get(ScrapeRuntimeJob, "source")
        state = await controls.control(db, job)
        if stale == "expired":
            state["request"]["requestedAt"] = time.time() - 121
            await controls.store(db, job, state)
        elif stale == "generation":
            job.claim_count += 1
        else:
            job.stop_requested = True
        await db.commit()
    if stale == "expired":
        await tick(gate)
    else:
        with pytest.raises(RuntimeError):
            await tick(gate)
    assert gate.values["concurrency"] == 4
    async with store() as db:
        result = await get_assistant("source", db, ACTOR)
        assert result["messages"][0]["actions"][0]["state"] == "error"


@pytest.mark.asyncio
async def test_provider_ceiling_schema_and_stale_ack_are_fail_closed(store):
    await worker(store, ceiling=2)
    with pytest.raises(ValueError):
        await preview(store, concurrency=3)
    for values in ({"retries": 3}, {"concurrency": 0}, {"backoffSeconds": 301},
                   {"concurrency": True}, {"restart": True}, {"discardProgress": True}):
        with pytest.raises(ValidationError):
            await preview(store, **values)
    action = await preview(store, concurrency=1)
    async with store() as db:
        job = await db.get(ScrapeRuntimeJob, "source")
        state = await controls.control(db, job)
        state["request"] = {"id": action["id"], "state": "applied",
                            "new": action["_new"], "revision": 0}
        # An invented receipt without the effective values cannot report success.
        assert controls.acknowledgement(job, action, state)[0] == "error"


@pytest.mark.asyncio
async def test_retry_limits_are_live_and_never_shorten_provider_delay(store):
    from app.services.scraper.orchestrator import _settle_course_with_retries
    gate = await worker(store)
    await confirm(store, await preview(store, retries=1, backoffSeconds=30))
    await tick(gate)
    attempts, completions, sleeps = [], [], []
    async def attempt():
        attempts.append(1)
        return {"_retry_after": 60}
    async def completed(link):
        completions.append(link)
    async def sleep(delay):
        sleeps.append(delay)
    await _settle_course_with_retries({"url": "https://example.test"}, attempt, completed,
                                     sleep=sleep, runtime_gate=gate)
    assert len(attempts) == 2 and len(completions) == 1 and sleeps == [60]
    await confirm(store, await preview(store, retries=0))
    await tick(gate)
    attempts.clear()
    await _settle_course_with_retries({}, attempt, completed, runtime_gate=gate)
    assert len(attempts) == 1
    await confirm(store, await preview(store, retries=2))
    await tick(gate)
    attempts.clear()
    await _settle_course_with_retries({}, attempt, completed,
                                     max_retries=0, runtime_gate=gate)
    assert len(attempts) == 1  # Live tuning cannot widen selected retry budgets.
    attempts.clear()
    sleeps.clear()
    await _settle_course_with_retries({}, attempt, completed, max_retries=1,
                                     minimum_backoff=90, sleep=sleep, runtime_gate=gate)
    assert len(attempts) == 2 and sleeps == [90]


@pytest.mark.asyncio
async def test_new_owner_never_replays_old_requests_or_receipts(store):
    gate = await worker(store)
    action = await preview(store, concurrency=1)
    await confirm(store, action)
    async with store() as db:
        job = await db.get(ScrapeRuntimeJob, "source")
        job.claim_count += 1
        await db.commit()
        new_owner = controls.generation(job)
        state = await controls.control(db, job)
        assert controls.acknowledgement(job, action, state)[0] == "error"
        from app.services.live_scrape_diagnostics import summarize
        diagnostics = summarize(job, [], runtime=state)
        assert not diagnostics["runtime"]["current"]
        assert not diagnostics["capabilities"]["runtimeChanges"]
    successor = controls.RuntimeGate("source", 4, new_owner, session_factory=store)
    await successor.refresh()
    assert successor.values["concurrency"] == 4
    with pytest.raises(RuntimeError):
        await tick(gate)


@pytest.mark.asyncio
async def test_authentication_permission_and_explicit_confirmation_at_http_boundary(store):
    import httpx
    from app.main import app
    from app.database import get_db
    from app.dependencies import get_current_user
    await worker(store)
    action = await preview(store, concurrency=1)
    actor = {**ACTOR}
    async def database():
        async with store() as db:
            yield db
    async def user():
        return actor
    old = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = user
    url = f"/api/scrape/jobs/source/assistant/actions/{action['id']}"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post(url, json={"confirm": False})).status_code == 422
            assert (await client.post(url, json={"confirm": "true"})).status_code == 422
            assert (await client.post(url, json={"confirm": True, "discardProgress": True})).status_code == 422
            actor.clear()
            actor.update(id=7, permissions=["scraping.view"])
            assert (await client.post(url, json={"confirm": True})).status_code == 403
            app.dependency_overrides.pop(get_current_user)
            assert (await client.post(url, json={"confirm": True})).status_code == 401
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.asyncio
async def test_migration_roundtrip_is_additive_and_preserves_job_evidence(store):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    # Load the migration without confusing the installed Alembic package with
    # the project's migration directory.
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path
    spec = spec_from_file_location("runtime_migration", Path(__file__).parents[1] /
                                  "alembic/versions/393_scrape_runtime_controls.py")
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with store() as db:
        connection = await db.connection()
        def roundtrip(conn):
            migration.op = Operations(MigrationContext.configure(conn))
            migration.downgrade()
            migration.upgrade()
        await connection.run_sync(roundtrip)
        await db.commit()
        assert await db.scalar(text("SELECT count(*) FROM scrape_runtime_controls")) == 0
        assert (await db.get(ScrapeRuntimeJob, "source")).imported == 1
        assert (await db.get(ScrapedCourse, 1)).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("v2_enabled", [False, True])
async def test_ui_prompt_through_engine_selection_confirmation_and_worker_ack(store, monkeypatch, v2_enabled):
    """Do not mock research or its reply: traverse the real UI API contract."""
    import httpx
    from unittest.mock import AsyncMock
    from app.main import app
    from app.config import settings
    from app.database import get_db
    from app.dependencies import get_current_user
    from app.services import scrape_assistant as assistant, live_scrape_diagnostics
    gate = await worker(store)
    monkeypatch.setattr(settings, "scrape_agent_v2_enabled", v2_enabled)
    monkeypatch.setattr(settings, "scrape_agent_v2_user_ids", "7" if v2_enabled else "")
    monkeypatch.setattr(live_scrape_diagnostics, "worker_observation", AsyncMock(return_value={
        "workerStatus": "task_observed", "queueStatus": "active"}))
    ai = AsyncMock(side_effect=AssertionError("Runtime preview must not use AI or source research"))
    monkeypatch.setattr(assistant, "chat_json", ai)
    monkeypatch.setattr(assistant, "inspect_source", ai)
    async def database():
        async with store() as db:
            yield db
    async def user():
        return ACTOR
    old = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            base = "/api/scrape/jobs/source/assistant"
            posted = await client.post(base, json={
                "prompt": "Set concurrency to 2, retries to 1, backoffSeconds to 30"})
            assert posted.status_code == 202, posted.text
            queued = posted.json()
            await assistant.investigate(queued["threadId"], queued["turnId"])
            researched = (await client.get(base)).json()
            assert researched["status"] == "ready", researched
            action = researched["messages"][-1]["actions"][0]
            assert action["kind"] == "runtime_tuning"
            assert action["state"] == "proposed" and action["requiresConfirmation"]
            assert not any(key.startswith("_") for key in action)
            confirmed = await client.post(base + "/actions/" + action["id"], json={"confirm": True})
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["messages"][-1]["actions"][0]["state"] == "running"
            assert gate.values["concurrency"] == 4
            await tick(gate)
            completed = (await client.get(base)).json()["messages"][-1]["actions"][0]
            assert completed["state"] == "completed" and "Worker acknowledged" in completed["resultSummary"]
            assert gate.values == {"concurrency": 2, "retries": 1, "backoffSeconds": 30}
            ai.assert_not_called()
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.asyncio
async def test_shared_runtime_dispatch_preserves_selected_failure_retry_routing(store, monkeypatch):
    from unittest.mock import AsyncMock
    from app.config import settings
    from app.services import scrape_assistant as assistant, live_scrape_diagnostics
    from app.routers.scrape_assistant import prompt_assistant, PromptBody
    monkeypatch.setattr(settings, "scrape_agent_v2_enabled", False)
    legacy = AsyncMock()
    diagnostics = AsyncMock(side_effect=AssertionError("A selected retry is not a diagnostic"))
    monkeypatch.setattr(assistant, "_investigate", legacy)
    monkeypatch.setattr(live_scrape_diagnostics, "handle", diagnostics)
    async with store() as db:
        queued = await prompt_assistant("source", PromptBody(prompt="Retry failed courses"), db, ACTOR)
    await assistant.investigate(queued["threadId"], queued["turnId"])
    legacy.assert_awaited_once()
    diagnostics.assert_not_called()

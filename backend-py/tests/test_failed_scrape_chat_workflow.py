"""Replay a zero-row failed run through the real tools in private PostgreSQL."""
import pytest
from sqlalchemy import func, select
from app.models import ScrapeRuntimeJob, ScrapedCourse
from app.models.scrape_assistant import ScrapeAssistantThread
from app.models.user import User
from app.services import scrape_assistant as service
from app.routers.scrape_assistant import get_assistant
from tests.test_scrape_prompt_assistant import store, assistant_postgres, prompt_thread


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
@pytest.mark.parametrize("has_metrics", [True, False, "zero"])
@pytest.mark.parametrize("prompt,repair", [
    ("why it fail fix the bug", True),
    ("why did this fail only explain the bug", False),
    ("why this scraoying is not working fix it", True),
    ("fix it", True),
])
async def test_failed_zero_row_chat_reads_real_stored_metrics_and_prepares_guarded_repair(
    store, monkeypatch, engine, prompt, repair, has_metrics,
):
    async with store() as db:
        await db.delete(await db.get(ScrapedCourse, 1))
        job = await db.get(ScrapeRuntimeJob, "source")
        job.status, job.total_found, job.imported, job.errors, job.current = "failed_degraded", 0, 0, 0, 0
        raw = 655 if has_metrics is True else 0
        job.discovered_config = ({
            "pipeline_stats": {"raw_discovered":raw, "after_filter":0, "filter_drop_count":raw,
                               "filter_drop_pct":100 if raw else 0},
        } if has_metrics else {})
        job.gate_skip_counts = ({"catalogue_guard":{"kind":"discovery_filter_collapse"}}
                                if has_metrics is True else {})
        db.add(User(id=1, email="assistant-fixture@example.invalid", password_hash="not-a-login",
                    full_name="Private fixture", is_active=True, is_super_admin=True))
        await db.commit()
    async def forbidden(*args, **kwargs):
        raise AssertionError("Failed-job diagnosis must not fetch the homepage or use the general AI answer")
    monkeypatch.setattr(service, "inspect_source", forbidden)
    monkeypatch.setattr(service, "chat_json", forbidden)
    monkeypatch.setattr(service, "_investigate", forbidden)
    tid, turn = await prompt_thread(store, prompt)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, tid)
        messages = list(thread.messages)
        messages[-1] = {**messages[-1], "_engine":engine}
        thread.messages = messages
        await db.commit()
    await service.investigate(tid, turn)
    async with store() as db:
        result = await get_assistant("source", db, {"id":1})
        assert result["status"] == "ready", result.get("error")
        reply = result["messages"][-1]
        assert reply["providerRepair"]["counts"]["staged"] == 0
        assert reply["providerRepair"]["counts"]["errors"] == 0
        if has_metrics is True:
            assert reply["providerRepair"]["pipelineEvidence"] == {
                "rawCandidates":655, "extractableCandidates":0, "filterDropped":655,
            }
            assert "655 raw candidates became 0 extractable URLs" in reply["content"]
            assert "discovery/filter collapse" in reply["content"]
        else:
            assert reply["providerRepair"]["counts"]["candidates"] == 0
            assert "Zero pending rows do not prevent provider-level diagnosis." in reply["content"]
        assert not reply["sources"]
        assert bool(reply["actions"]) is repair
        if repair:
            action, = reply["actions"]
            assert action["kind"] == "repair_catalogue"
            assert action["courseCount"] == 50 and action["requiresConfirmation"] is True
            assert action["state"] == "proposed"
            assert not action.get("jobId")
        assert (await db.execute(select(func.count()).select_from(ScrapedCourse))).scalar_one() == 0
        assert (await db.execute(select(func.count()).select_from(ScrapeRuntimeJob))).scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_after_lock", [False, True])
async def test_empty_failure_dispatch_enters_catalogue_probe_and_rechecks_locked_counts(
    store, monkeypatch, healthy_after_lock,
):
    from unittest.mock import AsyncMock, Mock
    from sqlalchemy import update
    from fastapi import HTTPException
    from app.routers.scrape import start_ai_repair
    from app.services import ai_repair_workflow as workflow
    from app.services import worker_fencing_schema
    from app.services.scraper import ai_repair_agent as agent
    from app.tasks import auto_repair_task
    async with store() as db:
        await db.delete(await db.get(ScrapedCourse,1))
        job=await db.get(ScrapeRuntimeJob,"source")
        job.status,job.total_found,job.imported="failed_degraded",0,0
        job.discovered_config,job.gate_skip_counts={},{}
        await db.commit()
    saved=AsyncMock()
    # This ORM-only private schema lacks the migration-created fencing indexes.
    # The separate prerequisite is tested in test_worker_fencing_schema; keep
    # real SQL target validation and row locking here, with no worker launched.
    monkeypatch.setattr(worker_fencing_schema,"require_worker_fencing_schema",AsyncMock())
    dispatch=Mock()
    lease=Mock(return_value=True)
    monkeypatch.setattr(workflow,"save",saved)
    monkeypatch.setattr(workflow,"active_audit",AsyncMock(return_value=None))
    monkeypatch.setattr(agent,"acquire_repair_lease",lease)
    monkeypatch.setattr(auto_repair_task,"dispatch_ai_repair",dispatch)
    original_lock=workflow.lock
    async def lock(db, university_id):
        await original_lock(db,university_id)
        if healthy_after_lock:
            await db.execute(update(ScrapeRuntimeJob).where(
                ScrapeRuntimeJob.runtime_job_id=="source").values(
                    status="completed",total_found=40,imported=40))
    monkeypatch.setattr(workflow,"lock",lock)
    async with store() as db:
        if healthy_after_lock:
            with pytest.raises(HTTPException) as error:
                await start_ai_repair("source",db,{"id":1},evidence_urls=None)
            assert error.value.status_code==422
            saved.assert_not_awaited()
            dispatch.assert_not_called()
            lease.assert_not_called()
        else:
            await start_ai_repair("source",db,{"id":1},evidence_urls=None)
            session=saved.await_args.args[0]
            assert session["autonomous"]["catalogue_problem"] is True
            assert session["autonomous"]["baseline_counters"]["total_found"]==0
            dispatch.assert_called_once_with("source",session["university_id"],session["session_id"])
            assert (await db.execute(select(func.count()).select_from(ScrapedCourse))).scalar_one()==0

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
@pytest.mark.parametrize("prompt,repair", [
    ("why it fail fix the bug", True),
    ("why did this fail only explain the bug", False),
])
async def test_failed_zero_row_chat_reads_real_stored_metrics_and_prepares_guarded_repair(
    store, monkeypatch, engine, prompt, repair,
):
    async with store() as db:
        await db.delete(await db.get(ScrapedCourse, 1))
        job = await db.get(ScrapeRuntimeJob, "source")
        job.status, job.total_found, job.imported, job.errors, job.current = "failed_degraded", 0, 0, 0, 0
        job.discovered_config = {
            "pipeline_stats": {"raw_discovered":655, "after_filter":0, "filter_drop_count":655,
                               "filter_drop_pct":100},
        }
        job.gate_skip_counts = {"catalogue_guard":{"kind":"discovery_filter_collapse"}}
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
        assert reply["providerRepair"]["pipelineEvidence"] == {
            "rawCandidates":655, "extractableCandidates":0, "filterDropped":655,
        }
        assert "655 raw candidates became 0 extractable URLs" in reply["content"]
        assert "discovery/filter collapse" in reply["content"]
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

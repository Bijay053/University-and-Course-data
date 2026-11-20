"""A repeated repair request must report real progress, not launch another copy."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_provider_repair as provider


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["fix this scraping bug", "still same bug"])
@pytest.mark.parametrize("phase", ["verifying", "verification_queued", "planning"])
async def test_active_repair_reports_current_run_without_duplicate(monkeypatch, prompt, phase):
    from app.models import ScrapeRuntimeJob
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services.scrape_agent_v2 import tools as module

    thread = SimpleNamespace(
        job_id="original", messages=[{"role": "assistant", "providerRepair": {"status": "failed_degraded"}}]
    )
    job = SimpleNamespace(status="failed_degraded", university_id=114)

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, identity):
            return thread if model is ScrapeAssistantThread else job if model is ScrapeRuntimeJob else None

    diagnosis = {
        "status": "failed_degraded", "repairAvailable": True,
        "counts": {"candidates": 0, "processed": 0, "staged": 0, "skipped": 0, "errors": 0},
    }
    previous = {
        "ok": True, "status": "running", "phase": phase,
        "verificationRun": {
            "processed": 48, "total": 50, "staged": 0, "skipped": 0,
            "errors": 0, "releaseRevision": "actual-worker-release",
        },
    }
    calls = []

    class Tools:
        def __init__(self, *args):
            pass

        async def execute(self, name, args, call_id):
            calls.append(name)
            if name == "diagnose_scraper":
                return diagnosis
            if name == "read_scraper_repair":
                return previous
            raise AssertionError("An active repair must never be started again")

    monkeypatch.setattr(module, "DataTools", Tools)
    service = SimpleNamespace(
        AsyncSessionLocal=DB, save_research_reply=AsyncMock(),
        now=lambda: datetime.now(timezone.utc),
    )
    assert await provider.handle(service, "thread", "turn", "run", {"job": {"id": "original"}}, prompt)
    reply = service.save_research_reply.call_args.args[3]
    assert reply["actions"] == []
    assert "48 processed from 50" in reply["content"]
    assert "actual-worker-release" in reply["content"]
    assert "0 processed from 0" not in reply["content"]
    assert reply["currentProviderRepair"] == previous
    assert calls == ["diagnose_scraper", "read_scraper_repair"]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_provider", [True, False])
async def test_current_verification_is_read_only_and_provider_scoped(monkeypatch, matching_provider):
    from app.models import ScrapeRuntimeJob, University
    from app.services.scrape_agent_v2 import repair
    from app.services.scraper import ai_repair_agent

    parent = SimpleNamespace(university_id=114)
    child = SimpleNamespace(
        university_id=114 if matching_provider else 999, runtime_job_id="child",
        status="running", current=48, total_found=50, imported=0,
        skipped=0, errors=0, release_revision="actual-worker-release",
    )
    session = {"status": "running", "autonomous": {
        "phase": "verifying", "verification_job_id": "child",
    }}
    monkeypatch.setattr(ai_repair_agent, "load_repair_audit", AsyncMock(return_value=session))

    class DB:
        async def get(self, model, identity):
            if model is University:
                return SimpleNamespace(id=114)
            assert model is ScrapeRuntimeJob
            return parent if identity == "original" else child

        async def execute(self, *args):
            raise AssertionError("Progress must not launch work or query field mutations")

    tools = SimpleNamespace(ctx={"university_id": 114})
    result = await repair.execute(tools, DB(), SimpleNamespace(job_id="original"), "read_scraper_repair", "read")
    assert result["changed"] is False
    if matching_provider:
        assert result["verificationRun"]["processed"] == 48
        assert result["verificationRun"]["releaseRevision"] == "actual-worker-release"
    else:
        assert result["verificationRun"] is None

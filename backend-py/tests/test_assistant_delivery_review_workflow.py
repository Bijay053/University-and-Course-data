"""Real PostgreSQL proposal/confirmation tests; sources and model are controlled."""
from copy import deepcopy
import pytest
from app.models import ScrapedCourse, ScrapeRuntimeJob
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.routers.scrape_assistant import get_assistant, confirm_action, ConfirmBody
from tests.test_scrape_prompt_assistant import (
    store, assistant_postgres, prompt_thread, research_fakes, propose,
)


@pytest.mark.asyncio
async def test_actual_research_turn_retains_all_145_rows_despite_model_omitting_action(store, monkeypatch):
    research_fakes(monkeypatch)
    async with store() as db:
        first = await db.get(ScrapedCourse, 1)
        first.study_mode = "Blended"
        db.add_all([ScrapedCourse(
            id=i, university_id=1, scrape_job_id="source", status="pending",
            course_name=f"Course {i}", course_website=f"https://www.example.edu/course-{i}",
            study_mode="On Campus",
        ) for i in range(2, 146)])
        db.add(ScrapedCourse(
            id=200, university_id=1, scrape_job_id="source", status="approved",
            course_name="Already approved", study_mode="Blended",
        ))
        await db.commit()
    async def model(**kwargs):
        assert '"studyMode": "Blended"' in kwargs["user"]
        return {"answer":"The example has conflicting Online and Blended descriptions.",
                "citations":[], "action":None}
    monkeypatch.setattr(service, "chat_json", model)
    tid, turn = await prompt_thread(store, (
        "https://www.example.edu/course\n"
        "is course is offer only online not Blended fix this bug\nalso review all courses"))
    await service.investigate(tid, turn)
    async with store() as db:
        response = await get_assistant("source", db, {"id":1})
        assert response["status"] == "ready", response.get("error")
        reply = response["messages"][-1]
        action, = reply["actions"]
        assert action["kind"] == "fix_fields" and action["fields"] == ["study_mode"]
        assert action["courseCount"] == len(action["courseScope"]) == 145
        assert action["requiresConfirmation"] is True
        assert action["state"] == "proposed" and not action.get("jobId")
        assert {r["id"] for r in action["courseScope"]} == set(range(1,146))
        assert "conflicting" in reply["content"] and "145 existing pending" in reply["content"]
        saved = await db.get(ScrapeAssistantThread, tid)
        private = saved.messages[-1]["actions"][0]
        assert set(private["_versions"]) == {str(i) for i in range(1, 146)}
        assert private["_configVersion"] == service.fingerprint({})
        assert private["_evidenceUrls"] == ["https://www.example.edu/course"]
        assert (await db.get(ScrapedCourse, 1)).study_mode == "Blended"
        assert (await db.get(ScrapedCourse, 200)).study_mode == "Blended"


@pytest.mark.asyncio
async def test_confirmed_mode_repair_uses_existing_job_and_forces_populated_mode_only(store, monkeypatch):
    from app.routers import scrape
    async def source(url, home):
        return {"url":url, "verified":True}
    monkeypatch.setattr(service, "inspect_source", source)
    calls = []
    async def start(body, db, actor):
        calls.append(body)
        assert body.ids == [1] and body.source_job_id == "source"
        assert body.target_fields == body.force_fields == ["study_mode"]
        assert set(body.force_reasons) == {"study_mode"}
        db.add(ScrapeRuntimeJob(runtime_job_id="mode-fix", university_id=1,
                               job_type="bulk_fix", status="queued", total_found=1))
        await db.commit()
        return {"jobId":"mode-fix"}
    monkeypatch.setattr(scrape, "start_bulk_fix_job", start)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.study_mode = "Blended"
        await db.commit()
        action = await propose(db)
        thread = await db.get(ScrapeAssistantThread, "ready")
        messages = deepcopy(thread.messages)
        messages[-1]["actions"][0].update(fields=["study_mode"], requiresConfirmation=True)
        thread.messages = messages
        await db.commit()
        result = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id":7})
        saved = result["messages"][-1]["actions"][0]
        assert saved["state"] == "running" and saved["jobId"] == "mode-fix"
        assert len(calls) == 1
        assert (await db.get(ScrapedCourse, 1)).study_mode == "Blended"

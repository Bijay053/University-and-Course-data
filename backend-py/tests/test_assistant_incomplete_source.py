from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.models import ScrapedCourse
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from tests.test_assistant_eligibility import seed_thread
from tests.test_scrape_prompt_assistant import store, assistant_postgres

URL = "https://www.bradford.ac.uk/courses/ug/foundation-clinical-sciences-medicine"
PROMPT = (
    "why each band ielts is missing\nall courses has each band are in text\n"
    "English language requirements\n6.5 overall with no sub-test less than 6.0.\n" + URL
)


@pytest.mark.asyncio
async def test_incomplete_page_does_not_invent_historical_cause_or_all_review_repair(store, monkeypatch):
    await seed_thread(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_website = URL
        row.course_name = "Foundation in Clinical Sciences and Medicine Leading to BSc Clinical Sciences"
        row.ielts_overall = 6.5
        row.ielts_listening = row.ielts_reading = row.ielts_writing = row.ielts_speaking = None
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        thread.messages = [{"role":"user", "content":PROMPT, "_engine":"legacy", "autoRun":True,
                            "createdAt":service.now().isoformat()}]
        await db.commit()
    ctx = await service.context("source")
    ctx["home"] = "https://www.bradford.ac.uk"
    ctx["samples"] += [
        {**deepcopy(ctx["samples"][0]),"id":i,"name":f"Other course {i}","url":ctx["home"]+f"/other-{i}"}
        for i in range(2,140)
    ]
    ctx["review_counts"]["pending"] = 139
    monkeypatch.setattr(service, "context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(service, "SOURCE_ACCESS_REGISTRY", False)
    inspect = AsyncMock(return_value={
        "url":URL,"verified":False,"sourceType":"html","httpStatus":200,
        "failureStage":"source_body","restrictionKind":"incomplete_response",
        "error":"Official source returned HTTP 200, but its response body could not be read completely.",
    })
    model = AsyncMock(side_effect=AssertionError("No model explanation is justified by an unreadable source"))
    monkeypatch.setattr(service, "inspect_source", inspect)
    monkeypatch.setattr(service, "chat_json", model)
    await service.investigate("eligibility", "turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready", thread.error
        reply = thread.messages[-1]
        assert not reply["actions"] and not reply["sources"]
        assert "HTTP 200" in reply["content"]
        assert "does not establish why" in reply["content"]
        assert "No wider course repair is promised" in reply["content"]
        assert "139" not in reply["content"]
        row = await db.get(ScrapedCourse, 1)
        assert row.ielts_overall == 6.5 and row.ielts_listening is None
    inspect.assert_awaited_once()
    model.assert_not_awaited()

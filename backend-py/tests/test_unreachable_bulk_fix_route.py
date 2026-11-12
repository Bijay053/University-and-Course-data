import pytest
from app.services import scrape_assistant as service
from app.routers.scrape_assistant import confirm_action,ConfirmBody
from tests.test_scrape_prompt_assistant import store,assistant_postgres,propose

@pytest.mark.asyncio
async def test_no_bulk_job_dispatched_when_site_unreachable(store,monkeypatch):
    from app.routers import scrape
    async def blocked(url,home):return {"verified":False,"error":"robots.txt blocked"}
    async def forbidden_dispatch(*args,**kwargs):raise AssertionError("Must not dispatch a bulk fix")
    monkeypatch.setattr(service,"inspect_source",blocked)
    monkeypatch.setattr(scrape,"start_bulk_fix_job",forbidden_dispatch)
    async with store() as db:
        action=await propose(db)
        result=await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
        saved=result["messages"][-1]["actions"][0]
        assert saved["state"]=="error"
        assert "not queued" in saved["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("access_refused", [False, True])
async def test_confirmation_uses_bounded_fallback_without_ignoring_access_refusal(
    store, monkeypatch, access_refused,
):
    from copy import deepcopy
    from app.models import ScrapedCourse, ScrapeRuntimeJob
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services import assistant_source_cache
    from app.routers import scrape

    seen, dispatched = [], []

    async def inspect(url, home):
        seen.append(url)
        if not access_refused and url.endswith("course-6"):
            return {"url": url, "verified": True, "facts": []}
        return {"url": url, "verified": False, "error": "Source unavailable",
                **({"accessIssue": "robots_unavailable"} if access_refused
                   else {"failureStage": "source_body"})}

    async def cached(service, thread_id, turn_id, run_id, url, home, reader, timeout):
        assert timeout == 16
        return await reader(url, home)

    async def dispatch(body, db, actor):
        assert body.target_fields == ["ielts_overall"]
        dispatched.append(body.ids)
        db.add(ScrapeRuntimeJob(
            runtime_job_id="bulk-seven", university_id=1, job_type="bulk_fix",
            status="queued", total_found=7, current=0,
        ))
        await db.commit()
        return {"jobId": "bulk-seven"}

    monkeypatch.setattr(service, "inspect_source", inspect)
    monkeypatch.setattr(assistant_source_cache, "cached_source", cached)
    monkeypatch.setattr(scrape, "start_bulk_fix_job", dispatch)
    async with store() as db:
        extra = [ScrapedCourse(
            id=i, university_id=1, scrape_job_id="source", status="pending",
            course_name=f"Course {i}", course_website=f"https://www.example.edu/course-{i}",
        ) for i in range(2, 8)]
        db.add_all(extra)
        await db.commit()
        action = await propose(db)
        action["fields"] = ["ielts_overall"]
        action["_ids"] = list(range(1, 8))
        action["courseCount"] = 7
        action["_versions"].update({str(row.id): service.row_version(row) for row in extra})
        thread = await db.get(ScrapeAssistantThread, "ready")
        messages = deepcopy(thread.messages)
        messages[-1]["actions"] = [action]
        thread.messages = messages
        await db.commit()
        result = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        saved = result["messages"][-1]["actions"][0]
        if access_refused:
            assert saved["state"] == "error" and not dispatched
            assert len(seen) == 3
        else:
            assert saved["state"] == "running" and saved["jobId"] == "bulk-seven"
            assert dispatched == [list(range(1, 8))]
            assert len(seen) == len(set(seen)) == 6
            assert "https://www.example.edu/course-6" in seen

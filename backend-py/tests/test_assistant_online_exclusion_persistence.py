import pytest
from types import SimpleNamespace
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import assistant_eligibility as eligibility
from app.services import assistant_online_exclusion as online
from app.services import scrape_assistant as service
from tests.test_assistant_eligibility import seed_thread, store, assistant_postgres


async def setup(store, count=3, prompt="reject online only courses", auto=True):
    await seed_thread(store)
    async with store() as db:
        for n in range(2, count + 1):
            db.add(ScrapedCourse(id=n, university_id=1, scrape_job_id="source",
                                course_name=f"Programme {n}", status="pending",
                                course_website=f"https://www.example.edu/course-{n}"))
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        message = dict(thread.messages[0], content=prompt, autoRun=auto)
        thread.messages = [message]
        await db.commit()
        rows = (await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars().all()
        return {"university_id": 1, "_eligibilityKind": "online",
                "home": "https://www.example.edu",
                "versions": {str(r.id): service.row_version(r) for r in rows},
                "samples": [{"id": r.id, "name": r.course_name, "url": r.course_website} for r in rows]}


@pytest.mark.asyncio
async def test_pending_only_exclusion_retains_mixed_unavailable_and_preserves_fees(store):
    ctx = await setup(store)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], {
        1: ("Location: Online", "checked"), 2: (None, "mixed_mode"), 3: (None, "unavailable")})
    async with store() as db:
        rows = (await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars().all()
        assert [r.status for r in rows] == ["rejected", "pending", "pending"]
        assert rows[0].international_fee == 9790
        assert rows[0].rejection_reason.startswith("online_only:")
        reply = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]
        assert "Checked 2/3" in reply["content"]
        assert "Mixed delivery: 1" in reply["content"] and "unavailable: 1" in reply["content"]
        assert reply["actions"][0]["kind"] == "exclude_online"
        assert reply["actions"][0]["courseCount"] == 1
        assert reply["actions"][1]["kind"] == "undo_exclusion"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["approved", "version", "other_provider", "other_job"])
async def test_stale_or_foreign_records_are_not_rejected(store, change):
    ctx = await setup(store, count=1)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        if change == "approved":
            row.status = "approved"
        elif change == "version":
            row.study_mode = "Blended"
        elif change == "other_provider":
            ctx["university_id"] = 999
        else:
            row.scrape_job_id = "different"
        await db.commit()
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"],
                              {1: ("Location: Online", "checked")})
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).status != "rejected"


@pytest.mark.asyncio
async def test_over_ten_wait_for_confirmation_then_do_it_uses_exact_preview(store, monkeypatch):
    ctx = await setup(store, count=12)
    results = {n: ("Location: Online", "checked") for n in range(1, 13)}
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], results)
    async with store() as db:
        assert all(r.status == "pending" for r in (await db.execute(select(ScrapedCourse))).scalars())
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        plan = thread.messages[-1]["actions"][0]
        assert plan["state"] == "proposed" and plan["requiresConfirmation"]
        thread.messages = list(thread.messages) + [{
            "role": "user", "turnId": "approval", "content": "do it", "autoRun": True}]
        thread.turn_id, thread.run_id, thread.status = "approval", "new-owner", "researching"
        await db.commit()
    async def never_read(*args, **kwargs):
        raise AssertionError("Approval must not restart source research")
    monkeypatch.setattr(online, "check", never_read)
    assert await online.confirm_followup(service, "eligibility", "approval", "new-owner", ctx, "do it")
    async with store() as db:
        assert all(r.status == "rejected" for r in (await db.execute(select(ScrapedCourse))).scalars())
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.messages[-3]["actions"][0]["state"] == "superseded"
        assert thread.messages[-1]["actions"][0]["state"] == "completed"


@pytest.mark.asyncio
async def test_complaint_checks_every_row_and_only_prepares_confirmation(store, monkeypatch):
    ctx = await setup(store, count=13,
                      prompt="WHY THERE IS STILL ONLINE COURSES LISTED IN PENDING LIT FIX IT")
    called = []
    async def check(*args):
        row = args[-2]
        called.append(row["id"])
        return (None, "mixed_mode") if row["id"] == 1 else ("Location: Online", "checked")
    monkeypatch.setattr(online, "check", check)
    await eligibility.run(service, "eligibility", "turn", "owner", ctx,
                          "WHY THERE IS STILL ONLINE COURSES LISTED IN PENDING LIT FIX IT")
    assert sorted(called) == list(range(1, 14))
    async with store() as db:
        assert all(r.status == "pending" for r in (await db.execute(select(ScrapedCourse))).scalars())
        reply = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]
        assert "Checked 13/13" in reply["content"]
        assert reply["actions"][0]["courseCount"] == 12
        assert reply["actions"][0]["requiresConfirmation"]


@pytest.mark.asyncio
async def test_interrupted_check_continues_without_rechecking_settled_rows(store, monkeypatch):
    ctx = await setup(store)
    clock = [0]
    called = []
    async def check(*args):
        row = args[-2]
        called.append(row["id"])
        clock[0] = 50
        return None, "mixed_mode"
    monkeypatch.setattr(eligibility, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(online, "check", check)
    await eligibility.run(service, "eligibility", "turn", "owner", ctx, "reject online only courses")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "queued"
        assert len(thread.messages[0]["_onlineEligibilityChecks"]) == 1
        thread.run_id, thread.status = "continued", "researching"
        await db.commit()
    monkeypatch.setattr(eligibility.time, "monotonic", lambda: 0)
    await eligibility.run(service, "eligibility", "turn", "continued", ctx, "reject online only courses")
    assert sorted(called) == [1, 2, 3]
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready" and "Checked 3/3" in thread.messages[-1]["content"]


@pytest.mark.asyncio
async def test_continuation_preserves_original_versions_even_when_context_is_refreshed(store, monkeypatch):
    ctx = await setup(store, count=1)
    # Simulate a saved, interrupted check followed by a staff delivery edit.
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        request = dict(thread.messages[0], _onlineEligibilityScope={
            "universityId": 1, "rows": ctx["samples"], "versions": ctx["versions"]})
        thread.messages = [request]
        row = await db.get(ScrapedCourse, 1)
        row.study_mode = "On Campus"
        await db.commit()
        refreshed = {**ctx, "versions": {"1": service.row_version(row)}}
    async def check(*args):
        return "Location: Online", "checked"
    monkeypatch.setattr(online, "check", check)
    await eligibility.run(service, "eligibility", "turn", "owner", refreshed, "reject online only courses")
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.status == "pending" and row.study_mode == "On Campus"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_real_turn_dispatch_runs_existing_row_operation_before_either_model_engine(store, monkeypatch, engine):
    ctx = await setup(store, count=1)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.messages = [dict(thread.messages[0], _engine=engine)]
        thread.status, thread.run_id = "queued", None
        await db.commit()
    async def context(*args):
        return ctx
    async def check(*args):
        return "Location: Online", "checked"
    monkeypatch.setattr(service, "context", context)
    monkeypatch.setattr(online, "check", check)
    await service.investigate("eligibility", "turn")
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).status == "rejected"
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready"
        assert thread.messages[-1]["actions"][0]["kind"] == "exclude_online"


@pytest.mark.asyncio
async def test_online_exclusion_has_working_fenced_undo(store):
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    ctx = await setup(store, count=1)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"],
                              {1: ("Location: Online", "checked")})
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        undo_id = thread.messages[-1]["actions"][1]["id"]
    async with store() as db:
        await confirm_action("source", undo_id, ConfirmBody(confirm=True), db, {"id": 77})
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.status == "pending" and row.rejection_reason is None and row.international_fee == 9790


@pytest.mark.asyncio
async def test_online_raw_dom_capability_refresh_does_not_invalidate_unrelated_research(store):
    from app.services.assistant_source_cache import cached_source
    ctx = await setup(store, count=1)
    url = ctx["samples"][0]["url"]
    old = {"url": url, "sourceType": "html", "verified": True, "text": "Stored source text",
           "_sourceScopeVersion": 2, "_ieltsFactsVersion": 2}
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.messages = [dict(thread.messages[0], _sourceChecks={url: old})]
        await db.commit()
    calls = []
    async def inspect(url, home):
        calls.append(url)
        return {**old, "_onlineExclusionSchema": 1,
                "_onlineExclusion": {"state": "checked", "name": "BSc Music Technology",
                                     "quote": "Delivery: Online"}}
    await cached_source(service, "eligibility", "turn", "owner", url, ctx["home"], inspect, 12)
    assert calls == []
    await cached_source(service, "eligibility", "turn", "owner", url, ctx["home"], inspect, 12,
                        online_exclusion_schema=1)
    assert calls == [url]

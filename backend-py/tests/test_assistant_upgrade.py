import pytest
from sqlalchemy import select
from app.models import ScrapedCourse, ScrapeFeedback, University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import assistant_eligibility as eligibility, scrape_assistant as service
from app.services.assistant_review_commands import handle_read_or_rule
from app.services.assistant_source_cache import cached_source
from app.routers.scrape_assistant import confirm_action, ConfirmBody, get_assistant
from tests.test_assistant_eligibility import seed_thread, snapshot
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest.mark.asyncio
async def test_undo_is_fenced_audited_and_idempotent(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"],
                              {1: ("Available to: Domestic students only", "checked")})
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        undo = t.messages[-1]["actions"][-1]
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 77})
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 77})
        assert (await db.get(ScrapedCourse, 1)).status == "pending"
        logs = (await db.execute(select(ScrapeFeedback))).scalars().all()
        assert len(logs) == 2 and all(r.status == "resolved" for r in logs)


@pytest.mark.asyncio
async def test_eleven_exclusions_require_confirmation(store):
    await seed_thread(store)
    async with store() as db:
        for i in range(2, 12):
            db.add(ScrapedCourse(id=i, university_id=1, scrape_job_id="source", course_name=f"Course {i}",
                                course_website=f"https://www.example.edu/{i}", status="pending"))
        await db.commit()
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        ctx = {"university_id": 1, "versions": {str(r.id): service.row_version(r) for r in rows}}
        samples = [{"id": r.id, "name": r.course_name, "url": r.course_website} for r in rows]
    results = {r["id"]: ("Available to: Domestic students only", "checked") for r in samples}
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, samples, results)
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        action = t.messages[-1]["actions"][0]
        assert action["state"] == "proposed" and action["requiresConfirmation"]
        assert all(r.status == "pending" for r in (await db.execute(select(ScrapedCourse))).scalars())
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        assert all(r.status == "rejected" for r in (await db.execute(select(ScrapedCourse))).scalars())


@pytest.mark.asyncio
async def test_status_uses_database_not_university_website(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    ctx["job"] = {"id": "source"}
    async with store() as db:
        r = await db.get(ScrapedCourse, 1)
        r.status, r.rejection_reason = "rejected", "domestic_only"
        await db.commit()
    assert await handle_read_or_rule(service, "eligibility", "turn", "owner", ctx,
                                    "is row 1 removed?", [])
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        assert "rejected" in t.messages[-1]["content"]
        assert "database only" in t.messages[-1]["content"]
        assert not t.messages[-1]["sources"]


@pytest.mark.asyncio
async def test_standing_rule_persists_without_mutating_courses(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    assert await handle_read_or_rule(service, "eligibility", "turn", "owner", ctx,
                                    "we don't need domestic-only courses", [])
    async with store() as db:
        assert (await db.get(University, 1)).scrape_config["assistant_rules"]["exclude_confirmed_domestic_only"]
        assert (await db.get(ScrapedCourse, 1)).status == "pending"


@pytest.mark.asyncio
async def test_cache_reuses_source_and_keeps_it_private(store):
    await seed_thread(store)
    calls = []
    async def inspect(url, home):
        calls.append(url)
        return {"verified": True, "url": url, "text": "official evidence", "_fullText": "large"}
    args = (service, "eligibility", "turn", "owner", "https://www.example.edu/course",
            "https://www.example.edu", inspect, 5)
    await cached_source(*args)
    await cached_source(*args)
    assert len(calls) == 1
    await cached_source(*args, fresh=True)
    assert len(calls) == 2
    async with store() as db:
        public = await get_assistant("source", db, {"id": 77})
        assert all("_sourceChecks" not in message for message in public["messages"])


def test_autorun_respects_threshold_and_publication():
    from app.services.scrape_assistant_autorun import choose_action
    assert choose_action([{"kind": "fix_fields", "state": "proposed", "courseCount": 11}]) is None
    assert choose_action([{"kind": "approve_course", "state": "proposed", "courseCount": 1}]) is None
    assert choose_action([{"kind": "fix_fields", "state": "proposed", "courseCount": 10}])


@pytest.mark.asyncio
async def test_explicit_rescrape_scopes_row_without_model(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    assert await handle_read_or_rule(service, "eligibility", "turn", "owner", ctx, "re-scrape row 1", [])
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        action = t.messages[-1]["actions"][0]
        assert action["kind"] == "fix_fields"
        assert action["_ids"] == [1]
        assert set(action["fields"]) == service.FIELDS
        assert action["_versions"] == ctx["versions"]


@pytest.mark.asyncio
async def test_eligibility_reuses_research_source(store, monkeypatch):
    await seed_thread(store)
    ctx = await snapshot(store)
    row = ctx["samples"][0]
    async def inspect(url, home):
        return {"verified": True, "url": url, "text": "Official eligibility",
                "_eligibilityName": row["name"],
                "_eligibilityQuote": "Available to: Domestic students only"}
    await cached_source(service, "eligibility", "turn", "owner", row["url"],
                        "https://www.example.edu", inspect, 5)
    async def forbidden(*args):
        raise AssertionError("A second source read is forbidden")
    from app.services import scrape_assistant_tools
    monkeypatch.setattr(scrape_assistant_tools, "inspect_source", forbidden)
    token = eligibility._source_context.set((service, "eligibility", "turn", "owner"))
    try:
        assert await eligibility.check_course(row, "https://www.example.edu") == "Available to: Domestic students only"
    finally:
        eligibility._source_context.reset(token)


@pytest.mark.asyncio
async def test_review_projection_preserves_cricos_warning(store):
    await seed_thread(store)
    from app.routers.scrape import _staged_row_to_dict
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.cricos_code = None
        row.eligibility_reason = "CRICOS registration unverified: no course code established."
        data = _staged_row_to_dict(row)
        assert data["eligibilityStatus"] == "unknown"
        assert data["eligibilityReason"] == row.eligibility_reason


@pytest.mark.parametrize("country,code,flagged", [
    ("Australia", None, True), ("AU", None, True),
    ("United Kingdom", None, False), ("New Zealand", None, False),
    ("Australia", "012345A", False),
])
def test_registration_absence_is_australian_review_only(country, code, flagged):
    from types import SimpleNamespace
    from app.services.scraper.registration_review import flag_registration_review
    row = SimpleNamespace(cricos_code=None, eligibility_status="eligible",
                          eligibility_reason=None, status="pending", auto_publish_status="ready")
    flag_registration_review(row, country, {"cricos_code": code})
    assert (row.eligibility_status == "unknown") == flagged
    assert (row.auto_publish_status == "review") == flagged
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_resumed_eligibility_check_reuses_checkpoint(store, monkeypatch):
    await seed_thread(store)
    ctx = await snapshot(store)
    ctx["home"] = "https://www.example.edu"
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        messages = [dict(m) for m in t.messages]
        messages[0]["_eligibilityChecks"] = {
            "1": {"version": ctx["versions"]["1"],
                  "result": ["Available to: Domestic students only", "checked"]}
        }
        t.messages = messages
        await db.commit()
    async def forbidden(*args):
        raise AssertionError("Completed page must not be downloaded again")
    monkeypatch.setattr(eligibility, "check_course", forbidden)
    await eligibility.run(service, "eligibility", "turn", "owner", ctx, "remove all domestic only courses")
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).status == "rejected"


@pytest.mark.asyncio
async def test_approval_is_proposed_not_autopublished(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    assert await handle_read_or_rule(service, "eligibility", "turn", "owner", ctx, "approve row 1", [])
    async with store() as db:
        t = await db.get(ScrapeAssistantThread, "eligibility")
        action = t.messages[-1]["actions"][0]
        assert action["kind"] == "approve_course" and action["requiresConfirmation"]
        assert (await db.get(ScrapedCourse, 1)).status == "pending"
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as caught:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        assert caught.value.status_code == 403

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse, ScrapeFeedback
from app.models.scrape_assistant import ScrapeAssistantThread
from app.models.user import User
from app.services import assistant_eligibility as eligibility
from app.services import scrape_assistant as service
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest.mark.parametrize("prompt,expected", [
    ("remove domestic only courses check other as well", True),
    ("delete all domestic-only courses", True),
    ("why is this domestic only?", False),
    ("don't remove domestic only courses", False),
    ("do not delete domestic only courses", False),
    ("only explain why you remove domestic courses", False),
])
def test_intent(prompt, expected):
    assert eligibility.requested(prompt) is expected


def test_other_courses_includes_whole_review():
    rows = [{"id": 1, "name": "Course A", "url": "https://example.edu/a"},
            {"id": 2, "name": "Course B", "url": "https://example.edu/b"}]
    assert eligibility.scope("Course A is domestic only remove it check other as well", rows) == rows
    assert eligibility.scope("Remove Course A domestic only", rows) == rows[:1]
    assert eligibility.scope("remove domestic only from https://example.edu/b", rows) == rows[1:]
    assert eligibility.scope("remove it domestic only", rows) == []


@pytest.mark.parametrize("body,expected", [
    ("<p>Available to: Domestic students only</p>", True),
    ("<p>Available to: Domestic and international students</p>", False),
    ("<p>Domestic only fee: $9,100</p>", False),
    ("<p>Not domestic students only</p>", False),
    ("<aside><p>Available to: Domestic students only</p></aside>", False),
    ('<div class="related-courses"><p>Available to: Domestic students only</p></div>', False),
    ("<p>Available to: Domestic students only except international research students</p>", False),
])
def test_source_authority(body, expected):
    html = "<h1>Graduate Certificate in Christian Leadership</h1><main>" + body + "</main>"
    assert bool(eligibility.domestic_evidence(html, "Graduate Certificate in Christian Leadership")) is expected
    assert eligibility.domestic_evidence(html, "Bachelor of Nursing") is None


async def seed_thread(store):
    async with store() as db:
        db.add(User(id=77, email="eligibility@example.test", full_name="Eligibility reviewer",
                    password_hash="not-a-real-password", is_active=True, is_super_admin=True))
        db.add(ScrapeAssistantThread(
            id="eligibility", job_id="source", actor_id=77, turn_id="turn", run_id="owner",
            status="researching", phase="", activity=[], messages=[{
                "role": "user", "content": "remove all domestic only courses",
                "autoRun": True,
            }],
        ))
        await db.commit()


async def snapshot(store):
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        return {
            "university_id": 1,
            "versions": {"1": service.row_version(row)},
            "samples": [{"id": 1, "name": row.course_name, "url": row.course_website}],
        }


@pytest.mark.asyncio
async def test_persist_rejects_pending_audits_and_is_idempotent(store):
    await seed_thread(store)
    ctx = await snapshot(store)
    evidence = {1: ("Available to: Domestic students only", "checked")}
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], evidence)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], evidence)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.status == "rejected" and row.reviewed_at
        assert row.course_website in row.rejection_reason
        assert len((await db.execute(select(ScrapeFeedback))).scalars().all()) == 1
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready"
        assert thread.messages[-1]["actions"][0]["state"] == "completed"
        assert "Excluded 1" in thread.messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["approved", "edited", "permission", "owner", "unavailable", "other_job"])
async def test_exclusion_fails_closed(store, change):
    await seed_thread(store)
    ctx = await snapshot(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        if change == "approved":
            row.status = "approved"
        elif change == "edited":
            row.course_name = "Changed title"
        elif change == "permission":
            (await db.get(User, 77)).is_active = False
        elif change == "owner":
            (await db.get(ScrapeAssistantThread, "eligibility")).run_id = "new-owner"
        elif change == "other_job":
            ctx["university_id"] = 999
        await db.commit()
    evidence = {1: (None, "unavailable")} if change == "unavailable" else {
        1: ("Available to: Domestic students only", "checked")}
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], evidence)
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).status != "rejected"
        assert not (await db.execute(select(ScrapeFeedback))).scalars().all()


@pytest.mark.asyncio
async def test_investigation_executes_without_model_and_checks_others(store, monkeypatch):
    await seed_thread(store)
    ctx = await snapshot(store)
    async with store() as db:
        for i in (2, 3):
            row = ScrapedCourse(id=i, university_id=1, scrape_job_id="source",
                                course_name=f"Course {i}", status="pending",
                                course_website=f"https://www.example.edu/course-{i}")
            db.add(row)
        await db.commit()
        for i in (2, 3):
            row = await db.get(ScrapedCourse, i)
            ctx["samples"].append({"id": i, "name": row.course_name, "url": row.course_website})
            ctx["versions"][str(i)] = service.row_version(row)
    ctx.update(home="https://www.example.edu", config=None)
    checked = []
    async def context(_):
        return ctx
    async def check(row, home):
        checked.append(row["id"])
        if row["id"] == 2:
            return None
        if row["id"] == 3:
            raise TimeoutError
        return "Available to: Domestic students only"
    monkeypatch.setattr(service, "context", context)
    monkeypatch.setattr(eligibility, "check_course", check)
    from app.services.scraper.config import context as config_context
    monkeypatch.setattr(config_context, "set_uni_config", lambda _: None)
    await service._investigate("eligibility", "turn", "owner")
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).status == "rejected"
        assert (await db.get(ScrapedCourse, 2)).status == "pending"
        assert (await db.get(ScrapedCourse, 3)).status == "pending"
        assert sorted(checked) == [1, 2, 3]
        reply = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]
        assert "Checked 2/3" in reply["content"]
        assert len(reply["actions"][0]["courseResults"]) == 3

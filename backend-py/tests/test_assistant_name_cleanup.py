from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.user import User
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service, assistant_name_cleanup as cleanup
from app.services import assistant_pending_automation as policy, scrape_assistant_autorun
from tests.test_assistant_intake_batches import prepare
from tests.test_assistant_intake_consolidation import sample, HOME
from tests.test_scrape_prompt_assistant import store, assistant_postgres  # noqa: F401

PROMPT = "Remove intake dates from course names for all pending courses"


@pytest.mark.parametrize("prompt", [
    PROMPT, "Remove Jan and Sep intake dates from all course names",
    "Clean intake dates from all pending Aston course names",
])
def test_name_only_cleanup_is_separate_from_merging(prompt):
    assert cleanup.requested(prompt)


@pytest.mark.parametrize("prompt", [
    "Merge all intake variants", "Fix IELTS for all courses",
    "remove intake from course name and make course with multiple intake",
    "Fix intake dates in course names and consolidate the courses",
    "Check Mechanical Engineering MSc",
    "there as other courses same like this with jan sep in course name fix this all",
])
def test_other_tasks_keep_their_original_routing(prompt):
    assert not cleanup.requested(prompt)


def test_only_matching_stored_intakes_and_course_urls_support_cleanup():
    row = sample(1, "January")
    assert cleanup.clean_title(row, HOME) == "Advanced Computer Science MSc"
    row.intake_months = ["September"]
    assert cleanup.clean_title(row, HOME) is None
    row.intake_months = ["January"]
    row.course_website = row.course_website.replace("january", "september")
    assert cleanup.clean_title(row, HOME) is None


async def stage(store, monkeypatch, *, readonly=False):
    # Use the same authenticated, isolated PostgreSQL cohort as real merging.
    await prepare(store, monkeypatch, conflict=True)
    async with store() as db:
        db.add(User(id=7, email="name-cleanup@example.invalid", password_hash="unused",
                    is_active=True, is_super_admin=True))
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.actor_id = 7
        thread.status, thread.run_id = "researching", "cleanup-run"
        thread.messages = [{"role": "user", "content": PROMPT, "autoRun": True, "turnId": "turn",
                            "createdAt": service.now().isoformat()}]
        await db.commit()
    reply = AsyncMock()
    monkeypatch.setattr(service, "save_research_reply", reply)
    ctx = await service.context("source")
    await cleanup.handle(service, "eligibility", "turn", "cleanup-run", ctx,
                         "Read-only: " + PROMPT if readonly else PROMPT)
    action = reply.await_args.args[3]["actions"][0]
    return action


def row_values(row):
    return {c.key: deepcopy(getattr(row, c.key)) for c in row.__table__.columns
            if c.key not in {"course_name", "updated_at"}}


@pytest.mark.asyncio
async def test_bulk_cleanup_preserves_conflicts_fees_intakes_and_records_then_undoes(store, monkeypatch):
    action = await stage(store, monkeypatch)
    assert action["courseCount"] == 12
    settings = SimpleNamespace(scrape_assistant_auto_pending_user_ids="7")
    policy.prepare({"actions": [action]}, {"content": PROMPT, "autoRun": True}, settings, 7)
    assert action["automaticPending"] and not action["requiresConfirmation"]
    assert scrape_assistant_autorun.choose_action([action]) is action
    async with store() as db:
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        before = {r.id: row_values(r) for r in rows}
        names = {r.id: r.course_name for r in rows}
        await cleanup.apply(service, db, action, 1, 7)
        await db.commit()
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        assert {r.id: row_values(r) for r in rows} == before
        assert all("(" not in r.course_name for r in rows if r.id <= 12)
        assert len((await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.action == "assistant_name_cleanup"))).scalars().all()) == 12
        undo = cleanup.undo_proposal(action)
        assert scrape_assistant_autorun.choose_action([undo]) is None
        await cleanup.apply(service, db, undo, 1, 7, undo=True)
        await db.commit()
        assert {r.id: r.course_name for r in rows} == names
        assert {r.id: row_values(r) for r in rows} == before


@pytest.mark.asyncio
async def test_changed_row_stops_entire_batch_before_any_name_is_changed(store, monkeypatch):
    action = await stage(store, monkeypatch)
    async with store() as db:
        row = await db.get(ScrapedCourse, 12)
        row.study_mode = "Online"
        await db.commit()
        with pytest.raises(ValueError, match="changed"):
            await cleanup.apply(service, db, action, 1, 7)
        await db.rollback()
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        assert all("(" in r.course_name for r in rows if r.id <= 12)


@pytest.mark.asyncio
async def test_readonly_and_other_accounts_never_get_automatic_cleanup(store, monkeypatch):
    action = await stage(store, monkeypatch, readonly=True)
    settings = SimpleNamespace(scrape_assistant_auto_pending_user_ids="7")
    for account, prompt in [(8, PROMPT), (7, "Read-only: " + PROMPT)]:
        candidate = deepcopy(action)
        policy.prepare({"actions": [candidate]}, {"content": prompt, "autoRun": True}, settings, account)
        assert candidate["requiresConfirmation"] and not candidate.get("automaticPending")


@pytest.mark.asyncio
async def test_unknown_named_course_cannot_expand_into_all_rows(store, monkeypatch):
    await stage(store, monkeypatch)
    captured = AsyncMock()
    monkeypatch.setattr(service, "save_research_reply", captured)
    ctx = await service.context("source")
    await cleanup.handle(service, "eligibility", "turn", "cleanup-run", ctx,
                         "Remove intake dates from course names for Nonexistent Engineering MSc")
    assert captured.await_args.args[3]["actions"] == []


@pytest.mark.asyncio
async def test_undo_refuses_to_overwrite_later_fee_edits(store, monkeypatch):
    action = await stage(store, monkeypatch)
    async with store() as db:
        await cleanup.apply(service, db, action, 1, 7)
        await db.commit()
        undo = cleanup.undo_proposal(action)
        row = await db.get(ScrapedCourse, 12)
        row.international_fee = 12345
        await db.commit()
        with pytest.raises(ValueError, match="changed"):
            await cleanup.apply(service, db, undo, 1, 7, undo=True)
        await db.rollback()
        row = await db.get(ScrapedCourse, 12)
        assert float(row.international_fee) == 12345
        assert "(" not in row.course_name


@pytest.mark.asyncio
async def test_normal_server_autorun_applies_all_names_once_and_provides_a_fenced_undo(store, monkeypatch):
    from app.config import settings
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    save = service.save_research_reply
    await stage(store, monkeypatch)
    monkeypatch.setattr(service, "save_research_reply", save)
    monkeypatch.setattr(service, "run_automatic_action", scrape_assistant_autorun.autorun)
    monkeypatch.setattr(settings, "scrape_assistant_auto_pending_user_ids", "7")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        await db.commit()
    await service.investigate("eligibility", "turn")
    await scrape_assistant_autorun.autorun("eligibility", "turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action, undo = thread.messages[-1]["actions"]
        assert not action.get("error"), action.get("error")
        assert action["state"] == "completed" and action["automaticPending"]
        assert undo["kind"] == "undo_name_cleanup"
        assert len((await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.action == "assistant_name_cleanup"))).scalars().all()) == 12
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 7})
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        assert all("(" in row.course_name for row in rows if row.id <= 12)

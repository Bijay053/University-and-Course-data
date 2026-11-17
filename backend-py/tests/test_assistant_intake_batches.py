from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import assistant_intake_consolidation as merge
from tests.test_assistant_intake_consolidation import setup, HOME
from tests.test_scrape_prompt_assistant import store, assistant_postgres

NAMES = ["Artificial Intelligence MSc", "Business Analytics MSc", "Cyber Security MSc", "English MA"]
MONTHS = ["January", "April", "September"]
PROMPT = (
    "\n".join(f"{name} ({month} 2027)" for name in NAMES[:2] for month in MONTHS)
    + "\nBusiness Analytics BSc (Hons)\nthere same same issue other course fix all this listed is just sample"
)


@pytest.mark.parametrize("name", NAMES)
def test_dated_examples_override_stale_conversation_fields(name):
    prompt = "\n".join(f"{name} ({month} 2027)" for month in MONTHS) + "\nsame issue fix all courses"
    assert merge.requested(prompt, [{"role": "assistant", "content": "Check delivery mode across all courses"}])
    assert not merge.requested(prompt + "\nOnly fix IELTS bands")
    assert not merge.requested(prompt + "\nSet intake February and July")


async def prepare(store, monkeypatch, engine="legacy", *, conflict=False, readonly=False, family_count=4,
                  prompt=PROMPT):
    ctx, _ = await setup(store, engine=engine)
    async with store() as db:
        original = await db.get(ScrapedCourse, 1)
        template = {c.key: deepcopy(getattr(original, c.key)) for c in original.__table__.columns}
        for group, name in enumerate(NAMES[:family_count]):
            slug = name.lower().replace(" ", "-")
            for n, month in enumerate(MONTHS, 1):
                identifier = group * 3 + n
                row = await db.get(ScrapedCourse, identifier)
                if row is None:
                    row = ScrapedCourse(**{**template, "id": identifier})
                row.course_name = f"{name} ({month} 2027)"
                row.course_website = f"{HOME}/study/courses/{slug}/{month.lower()}-2027"
                row.intake_months = [month]
                if conflict and identifier == 12:
                    row.international_fee = 99999
                db.add(row)
        db.add(ScrapedCourse(**{**template, "id": 13, "course_name": "Business Analytics BSc (Hons)",
                               "course_website": HOME + "/study/courses/business-analytics-bsc"}))
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        thread.messages = [
            {"role": "assistant", "content": "Check study mode across all courses", "actions": []},
            {"role": "user", "content": ("Do not merge any records.\n" if readonly else "") + prompt,
             "_engine": engine, "autoRun": True, "turnId": "turn", "createdAt": service.now().isoformat()},
        ]
        await db.commit()
    monkeypatch.setattr(service, "context", AsyncMock(return_value=ctx))
    generic = AsyncMock(side_effect=AssertionError("No generic agent or paid extraction should run"))
    monkeypatch.setattr(service, "_investigate", generic)
    await service.investigate("eligibility", "turn")
    generic.assert_not_awaited()
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready", thread.error
        return deepcopy(thread.messages[-1]["actions"][0])


@pytest.mark.parametrize("prompt", [
    "Merge all intakes for Artificial Intelligence MSc",
    "Merge all rows for Artificial Intelligence MSc",
])
def test_all_intakes_of_named_course_never_expands_to_other_awards(prompt):
    assert not merge.all_families_requested(prompt, named=True)


ACTUAL_MONTH_NAME_REQUEST = (
    "there as other courses same like this with jan sep in course name fix this all\n\n"
    "notes: only fixed this from Scrape assistant"
)


@pytest.mark.parametrize("prompt", [
    ACTUAL_MONTH_NAME_REQUEST,
    "Fix all courses with January and September in the course name",
    "Remove Jan and Sept from names of courses; fix them all",
])
def test_month_abbreviations_route_name_cleanup_to_consolidation(prompt):
    assert merge.requested(prompt)
    assert merge.all_families_requested(prompt)


@pytest.mark.parametrize("prompt", [
    "Add January and September intakes to all courses",
    "Why are January and September fees different?",
    "Course name includes Jan Sep; only fix IELTS bands",
    "Fix January in this course name",
])
def test_month_mentions_do_not_convert_other_field_requests_to_merges(prompt):
    assert not merge.requested(prompt)


def test_other_courses_fix_this_all_expands_but_named_intakes_do_not():
    assert merge.all_families_requested(ACTUAL_MONTH_NAME_REQUEST, named=True)
    assert not merge.all_families_requested(
        "Fix this all intakes for Artificial Intelligence MSc", named=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_exact_production_month_name_request_prepares_all_without_fetching(store, monkeypatch, engine):
    action = await prepare(store, monkeypatch, engine, prompt=ACTUAL_MONTH_NAME_REQUEST)
    assert action["kind"] == "merge_intakes" and action["state"] == "proposed"
    assert action["courseCount"] == 12 and action["requiresConfirmation"] is True
    assert len(action["intakeConsolidations"]) == 4 and not action.get("jobId")
    async with store() as db:
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()
        statuses = (await db.execute(select(ScrapedCourse.status))).scalars().all()
        assert len(statuses) == 13 and set(statuses) == {"pending"}


@pytest.mark.asyncio
async def test_small_batch_respects_explicit_no_merge_with_autorun_enabled(store, monkeypatch):
    action = await prepare(store, monkeypatch, readonly=True, family_count=2)
    assert action["courseCount"] == 6 and action["requiresConfirmation"]
    async with store() as db:
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_actual_sample_request_checks_all_families_without_fetching(store, monkeypatch, engine):
    action = await prepare(store, monkeypatch, engine)
    assert action["kind"] == "merge_intakes" and action["courseCount"] == 12
    assert action["requiresConfirmation"] and action["state"] == "proposed"
    assert len(action["intakeConsolidations"]) == 4
    assert set(action["_ids"]) == set(range(1, 13))
    assert not action.get("jobId")
    async with store() as db:
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()
        assert (await db.get(ScrapedCourse, 13)).course_name == "Business Analytics BSc (Hons)"


@pytest.mark.asyncio
async def test_conflicts_do_not_block_other_previews_or_bypass_batch_confirmation(store, monkeypatch):
    action = await prepare(store, monkeypatch, conflict=True)
    assert action["courseCount"] == 9 and action["requiresConfirmation"]
    assert len(action["intakeConsolidations"]) == 3
    assert len(action["blockedIntakeConsolidations"]) == 1
    assert "international_fee" in action["blockedIntakeConsolidations"][0]["conflicts"]


@pytest.mark.asyncio
async def test_one_confirmation_merges_all_families_and_one_undo_restores_them(store, monkeypatch):
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    action = await prepare(store, monkeypatch)
    async with store() as db:
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        assert sum(r.status == "pending" for r in rows) == 5  # four MSc/MA families + the distinct BSc
        assert [rows[n].course_name for n in (0, 3, 6, 9)] == NAMES
        assert all(rows[n].intake_months == MONTHS for n in (0, 3, 6, 9))
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        undo = thread.messages[-1]["actions"][-1]
        assert undo["courseCount"] == 12
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        assert len((await db.execute(select(CourseAuditLog))).scalars().all()) == 12
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 77})
    async with store() as db:
        rows = list((await db.execute(select(ScrapedCourse))).scalars())
        assert all(r.status == "pending" and r.course_id is None for r in rows)
        assert all("(" in r.course_name for r in rows)


@pytest.mark.asyncio
async def test_late_conflict_rolls_back_every_earlier_family(store, monkeypatch):
    action = await prepare(store, monkeypatch)
    async with store() as db:
        row = await db.get(ScrapedCourse, 12)
        row.international_fee = 99999
        await db.commit()
        with pytest.raises(ValueError, match="changed"):
            await merge.apply(service, db, action, 1, 77)
        rows = list((await db.execute(select(ScrapedCourse))).scalars())
        assert all(r.status == "pending" for r in rows)
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()

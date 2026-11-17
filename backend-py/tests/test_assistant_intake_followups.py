from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import assistant_intake_consolidation as merge
from app.services.assistant_intake_context import followup, matches
from tests.test_assistant_intake_consolidation import setup, sample, NAME, BASE, HOME
from tests.test_scrape_prompt_assistant import store, assistant_postgres

FOLLOWUP = "fix this issue in course we neeed one course and multiple intake only"


def groups():
    rows = [sample(1, "January"), sample(2, "September")]
    return {(NAME.casefold(), BASE, 2027): rows}


def test_actual_first_duplicate_report_routes_before_generic_research():
    assert merge.requested(
        "Digital Chemical Engineering MSc (September 2027)\n\n"
        "Digital Chemical Engineering MSc still shown 2 3 courses")
    assert merge.requested(FOLLOWUP)


def test_followup_uses_the_nearest_user_course_not_assistant_claims():
    history = [
        {"role": "user", "content": NAME + " (September 2027)\nstill shown 2 3 courses"},
        {"role": "assistant", "content": "This course is in another review."},
        {"role": "user", "content": FOLLOWUP},
    ]
    assert [r.id for r in followup(FOLLOWUP, history, groups())[0]] == [1, 2]


@pytest.mark.parametrize("topic", [
    "Software Engineering MSc has the same problem",
    "https://another.example/course",
])
def test_nearest_new_topic_never_falls_back_to_an_older_course(topic):
    history = [
        {"role": "user", "content": NAME},
        {"role": "assistant", "intakeConsolidation": {"canonicalUrl": BASE}},
        {"role": "user", "content": topic},
        {"role": "user", "content": FOLLOWUP},
    ]
    assert followup(FOLLOWUP, history, groups()) == []


def test_explicit_unknown_course_does_not_inherit_history():
    assert followup("Fix this Software Engineering MSc course with multiple intakes",
                    [{"role": "user", "content": NAME}], groups()) == []
    assert matches("merge intake records for " + NAME + " with placement", groups()) == []


def test_ambiguous_previous_samples_do_not_choose_one_family():
    all_groups = groups()
    other = [sample(3, "January", course_name="Data Science MSc (January 2027)",
                    course_website=HOME + "/data-science/january-2027"),
             sample(4, "April", course_name="Data Science MSc (April 2027)",
                    course_website=HOME + "/data-science/april-2027")]
    all_groups[("data science msc", HOME + "/data-science", 2027)] = other
    assert followup(FOLLOWUP, [{"role": "user", "content": NAME + "\nData Science MSc"}], all_groups) == []


@pytest.mark.asyncio
async def test_actual_followup_prepares_the_existing_family_without_fetching(store, monkeypatch):
    ctx, _ = await setup(store)
    fetch = AsyncMock(side_effect=AssertionError("A clear consolidation must not fetch"))
    monkeypatch.setattr(service, "inspect_source", fetch)
    history = [
        {"role": "user", "content": NAME + " (September 2027)\nstill shown 2 3 courses"},
        {"role": "assistant", "content": "One record in another review"},
        {"role": "user", "content": FOLLOWUP},
    ]
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, FOLLOWUP, history)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        assert action["kind"] == "merge_intakes"
        assert action["_ids"] == [1, 2, 3]
        assert action["intakeConsolidation"]["courseName"] == NAME
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatched_intake_uses_source_backed_repair_for_only_that_row(store, monkeypatch):
    ctx, _ = await setup(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 3)
        row.intake_months = ["January"]
        await db.commit()
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        ctx.update(samples=[{"id": r.id, "name": r.course_name, "url": r.course_website,
                             "intakes": deepcopy(r.intake_months)} for r in rows],
                   versions={str(r.id): service.row_version(r) for r in rows}, config_version="fixture")
    from app.services import assistant_source_cache
    quote = "Start date September 2027"
    cache = AsyncMock(return_value={
        "verified": True, "url": BASE + "/september-2027", "text": quote,
        "facts": [{"field": "intake_months", "normalized": {"intake_months": ["September"]},
                   "quote": quote, "method": "intake:selected_start_option"}],
    })
    monkeypatch.setattr(assistant_source_cache, "cached_source", cache)
    history = [{"role": "user", "content": NAME + " (September 2027)\nstill shown 2 3 courses"},
               {"role": "user", "content": FOLLOWUP}]
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, FOLLOWUP, history)
    cache.assert_awaited_once()
    assert cache.await_args.args[4] == BASE + "/september-2027"
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        reply = thread.messages[-1]
        action = reply["actions"][0]
        assert action["kind"] == "fix_fields" and action["fields"] == ["intake_months"]
        assert action["_ids"] == [3] and action["courseCount"] == 1
        assert action["requiresConfirmation"] is False
        assert action["repairPreview"][0]["changes"] == [{
            "field": "intake_months", "oldValue": ["January"], "newValue": ["September"],
        }]
        assert reply["intakeConsolidation"]["courseName"] == NAME
        assert "row 3 stores" in reply["content"] and "URL dates are not evidence" in reply["content"]
        assert (await db.get(ScrapedCourse, 3)).intake_months == ["January"]
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
async def test_readonly_duplicate_question_does_not_start_a_repair(store, monkeypatch):
    ctx, _ = await setup(store)
    async with store() as db:
        (await db.get(ScrapedCourse, 3)).intake_months = ["January"]
        await db.commit()
    from app.services import assistant_intake_review
    repair = AsyncMock(side_effect=AssertionError("Question is not repair consent"))
    monkeypatch.setattr(assistant_intake_review, "handle", repair)
    prompt = NAME + " (September 2027)\nstill shown 2 3 courses"
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, prompt, [])
    repair.assert_not_awaited()
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.messages[-1]["actions"][0]["state"] == "needs_review"

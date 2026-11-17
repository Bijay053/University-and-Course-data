"""Client requests need a course name, not internal row IDs or policy prose."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.services import assistant_course_names as names
from tests.test_scrape_prompt_assistant import store, assistant_postgres  # noqa: F401

HOME = "https://www.example.edu"
TITLE = "Mechanical Engineering MSc"


def option(month, *, title=TITLE, path="mechanical-engineering-msc", year=2027):
    return {"title": f"{title} ({month} {year})",
            "url": f"{HOME}/study/courses/{path}/{month.lower()}-{year}"}


OPTIONS = [option(month) for month in ("January", "April", "September")]


@pytest.mark.parametrize("prompt", [
    "Check Mechanical Engineering MSc",
    "check mechanical engineering msc",
    "Check Mechanical Engineering MSc and fix any incorrect details",
    "Mechanical Engineering MSc has the wrong delivery mode",
    "Why is Mechanical Engineering MSc online?",
    "Investigate Mechanical Engineering MSc only, don't change anything",
])
def test_plain_course_name_resolves_all_three_intakes_without_ids(prompt):
    found = names.matches(prompt, OPTIONS)
    assert found == OPTIONS
    assert names.result(found)["state"] == "resolved"


@pytest.mark.parametrize("month,expected", [
    ("January", 0), ("Jan", 0), ("Apr.", 1), ("Sept", 2), ("September", 2),
])
def test_explicit_option_narrows_the_family(month, expected):
    assert names.matches(f"Check {TITLE} {month} 2027", OPTIONS) == [OPTIONS[expected]]


def test_awards_and_delivery_variants_are_never_treated_as_one_identity():
    bachelors = option("January", title="Mechanical Engineering BEng",
                       path="mechanical-engineering-beng")
    online = option("January", title=TITLE + " Online",
                    path="mechanical-engineering-msc-online")
    placement = option("January", title=TITLE + " with placement",
                       path="mechanical-engineering-msc-placement")
    assert names.matches("Check Mechanical Engineering MSc", OPTIONS + [bachelors, online, placement]) == OPTIONS
    assert names.matches("Check Mechanical Engineering MSc Online", OPTIONS + [online]) == [online]
    assert names.result(OPTIONS + [online])["state"] == "choice"
    ambiguous = names.matches("Check Mechanical Engineering", OPTIONS + [bachelors])
    assert names.result(ambiguous)["state"] == "choice"


def test_date_label_and_url_must_agree_before_grouping():
    wrong = {**OPTIONS[0], "url": OPTIONS[2]["url"]}
    assert names.result([wrong, OPTIONS[1]])["state"] == "choice"
    different_query = {**OPTIONS[0], "url": OPTIONS[0]["url"] + "?delivery=online"}
    assert names.result([different_query, OPTIONS[1]])["state"] == "choice"


def test_topic_changes_and_negated_courses_do_not_revive_an_old_target():
    other = {"title": "Accounting MSc", "url": HOME + "/accounting"}
    assert names.matches("Check Accounting MSc, not Mechanical Engineering MSc", OPTIONS + [other]) == [other]
    assert names.matches("Actually check Mechanical Engineering MSc instead of Accounting MSc",
                         OPTIONS + [other]) == OPTIONS


def test_options_cannot_silently_overflow_resolution_limit():
    choices = [option(month, year=year) for year in (2026, 2027, 2028, 2029)
               for month in ("January", "April", "September")]
    assert names.result(choices)["state"] == "choice"
    assert names.result(choices)["incomplete"]


@pytest.mark.asyncio
async def test_resolver_finds_actual_pending_records_without_catalogue_or_writes(store):
    async with store() as db:
        for index, item in enumerate(OPTIONS, 100):
            db.add(ScrapedCourse(id=index, university_id=1, scrape_job_id="source",
                                course_name=item["title"], course_website=item["url"],
                                status="pending"))
        await db.commit()
    catalogue = AsyncMock(side_effect=AssertionError("Known names need no catalogue discovery"))
    answer = await names.resolve(
        f"Check {TITLE} and fix incorrect details",
        [{"role": "user", "content": f"Check {TITLE}"}],
        {"home": HOME, "university_id": 1}, store, catalogue,
    )
    assert answer["state"] == "resolved"
    assert set(item["url"] for item in answer["choices"]) == {item["url"] for item in OPTIONS}
    catalogue.assert_not_awaited()
    async with store() as db:
        rows = (await db.execute(select(ScrapedCourse).where(ScrapedCourse.id >= 100))).scalars().all()
        assert len(rows) == 3 and all(row.status == "pending" for row in rows)
        assert {row.course_name for row in rows} == {item["title"] for item in OPTIONS}


@pytest.mark.asyncio
async def test_native_agent_name_filter_is_case_insensitive_and_wildcards_are_literal(store):
    async with store() as db:
        for index, item in enumerate(OPTIONS, 100):
            db.add(ScrapedCourse(id=index, university_id=1, scrape_job_id="source",
                                course_name=item["title"], course_website=item["url"],
                                status="pending"))
        await db.commit()
        statement = select(ScrapedCourse).where(
            ScrapedCourse.university_id == 1,
            names.name_predicate(ScrapedCourse.course_name, "mechanical engineering msc"),
        )
        assert len((await db.execute(statement)).scalars().all()) == 3
        for wildcard in ("%", "_", r"\%", TITLE + "%"):
            statement = select(ScrapedCourse).where(names.name_predicate(ScrapedCourse.course_name, wildcard))
            assert not (await db.execute(statement)).scalars().all()


@pytest.mark.asyncio
async def test_plain_prompt_runs_normal_investigation_for_three_exact_options(store, monkeypatch):
    import json
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services import scrape_assistant as service, assistant_source_cache

    prompt = f"Check {TITLE}"
    async with store() as db:
        for index, item in enumerate(OPTIONS, 100):
            db.add(ScrapedCourse(id=index, university_id=1, scrape_job_id="source",
                                course_name=item["title"], course_website=item["url"],
                                status="pending"))
        db.add(ScrapeAssistantThread(
            id="short-request", actor_id=7, job_id="source", turn_id="short-turn",
            status="queued", messages=[{"role": "user", "content": prompt,
                                       "createdAt": service.now().isoformat()}],
        ))
        await db.commit()

    async def source(*args, **kwargs):
        return {"url": args[4], "title": TITLE, "verified": True, "facts": [],
                "sourceType": "html", "text": "Course-owned fixture text.",
                "excerpts": [{"id": "course-label", "text": "Campus: Birmingham"}]}

    source_reader = AsyncMock(side_effect=source)
    model = AsyncMock(return_value={
        "answer": "The three intake options were checked separately. No changes requested.",
        "citations": [], "action": None,
    })
    catalogue = AsyncMock(side_effect=AssertionError("Existing named courses need no discovery"))
    monkeypatch.setattr(service, "inspect_comparison", catalogue)
    monkeypatch.setattr(service, "chat_json", model)
    monkeypatch.setattr(assistant_source_cache, "cached_source", source_reader)

    await service.investigate("short-request", "short-turn")
    assert {call.args[4] for call in source_reader.await_args_list} == {o["url"] for o in OPTIONS}
    catalogue.assert_not_awaited()
    model.assert_awaited_once()
    payload = json.loads(model.await_args.kwargs["user"])
    assert set(payload["requestedCourseUrls"]) == {o["url"] for o in OPTIONS}
    assert {row["id"] for row in payload["sampledRows"]} == {100, 101, 102}
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "short-request")
        assert thread.status == "ready"
        reply = thread.messages[-1]
        assert reply["actions"] == []
        assert len(reply["resolvedCourses"]) == 3
        assert len((await db.execute(select(ScrapedCourse))).scalars().all()) == 4

"""Persisted staff edits: no model, discovery, or page evidence is needed."""
import pytest
from sqlalchemy import select
from app.models import ScrapedCourse, Fee, Intake, EnglishRequirement, University
from app.models.course import Course
from app.models.evidence import ScrapedFieldEvidence
from app.models.audit import CourseAuditLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services.assistant_staff_locations import handle
from app.services.assistant_staff_values import parse
from app.services.assistant_staff_edits import apply
from tests.test_assistant_eligibility import seed_thread, snapshot
from tests.test_scrape_prompt_assistant import store, assistant_postgres

CASES = [
    ("set intake February and July on all courses", {"intake_months":["February", "July"]}),
    ("set tuition AUD 24000 annual 2026 on all courses",
     {"international_fee":24000, "currency":"AUD", "fee_term":"Annual", "fee_year":2026}),
    ("set IELTS 6.5 with no band below 6 on all courses",
     {"ielts_overall":6.5, **{f"ielts_{k}":6 for k in ["listening", "reading", "writing", "speaking"]}}),
    ("set duration 2 years on all courses", {"duration":2, "duration_term":"Year"}),
    ("in all missing course intake add intake Feb, July\nalso in all course location add sydney",
     {"intake_months": ["February", "July"], "course_location": "Sydney"}),
]

COMPOUND = "in all missing course intake add intake Feb, July\n\nalso in all course location add sydney"


def test_exact_compound_and_followup_keep_per_field_scope():
    request = parse(COMPOUND)
    assert request.values == {"intake_months": ["February", "July"], "course_location": "Sydney"}
    assert request.missing_only == {"intake_months"}
    repeated = parse("no need to match it just add this requested",
                     [{"role": "user", "content": COMPOUND}])
    assert repeated.values == request.values and repeated.missing_only == request.missing_only
    assert parse("?", [{"role": "user", "content": COMPOUND},
                       {"role": "assistant", "content": "Awaiting confirmation."}]).error == "Awaiting confirmation."


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 11])
async def test_compound_request_preserves_existing_intakes_and_batch_confirmation(store, count):
    await seed_thread(store)
    async with store() as db:
        original = await db.get(ScrapedCourse, 1)
        original.intake_months = None
        for ident in range(2, count + 1):
            db.add(ScrapedCourse(id=ident, university_id=1, scrape_job_id=original.scrape_job_id,
                course_name=f"Bachelor of Testing {ident}", status="pending",
                course_website=f"https://example.edu/course/{ident}",
                intake_months=["September"], course_location="Melbourne"))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), COMPOUND, [])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        assert action["courseCount"] == count
        assert action["requiresConfirmation"] is (count > 10)
        if count > 10:
            assert (await db.get(ScrapedCourse, 1)).intake_months is None
            assert (await db.get(ScrapedCourse, 2)).course_location == "Melbourne"
            assert not list((await db.execute(select(CourseAuditLog))).scalars())
            await apply(service, db, action, 1, thread.actor_id, confirmed=True)
            await db.commit()
        first = await db.get(ScrapedCourse, 1)
        second = await db.get(ScrapedCourse, 2)
        assert first.intake_months == ["February", "July"]
        assert second.intake_months == ["September"]
        assert first.course_location == second.course_location == "Sydney"
        audits = list((await db.execute(select(CourseAuditLog))).scalars())
        assert len(audits) == count + 1
        assert all("staff-provided" in a.reason for a in audits)


@pytest.mark.asyncio
async def test_single_row_tool_does_not_overwrite_a_nonmissing_intake(store):
    from app.services.scrape_agent_v2.tools import DataTools
    await seed_thread(store)
    async with store() as db:
        (await db.get(ScrapedCourse, 1)).intake_months = ["September"]
        await db.commit()
    tools = DataTools(service, "eligibility", "turn", "owner", await snapshot(store),
                      "set intake February and July for row 1 missing intake")
    result = await tools.execute("edit_staff_values", {"row_id": 1}, "missing-only")
    assert not result["changed"]
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).intake_months == ["September"]
        assert not list((await db.execute(select(CourseAuditLog))).scalars())


@pytest.mark.parametrize("prompt,values", CASES)
@pytest.mark.asyncio
async def test_literal_values_persist_without_network(store, monkeypatch, prompt, values):
    from app.services.scrape_agent_v2 import runtime
    async def forbidden(*args, **kwargs):
        pytest.fail("Supplied values must not invoke model or network/discovery tools")
    monkeypatch.setattr(runtime, "run_loop", forbidden)
    monkeypatch.setattr(runtime, "_client", lambda: pytest.fail("No model needed"))
    await seed_thread(store)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.messages = [{"role":"user", "content":prompt}]
        await db.commit()
    async def context(job_id):
        return await snapshot(store)
    monkeypatch.setattr(service, "context", context)
    await runtime.investigate(service, "eligibility", "turn", "owner")
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert all(getattr(row, k) == v for k, v in values.items())
        proofs = list((await db.execute(select(ScrapedFieldEvidence))).scalars())
        assert {p.field_key for p in proofs} == set(values)
        assert all(p.source_url is None and p.confidence is None and p.validation_status == "staff-provided" for p in proofs)
        assert all(row.extraction_method[k] == "staff-provided" for k in values)
        audits = list((await db.execute(select(CourseAuditLog))).scalars())
        assert len(audits) == len(values) and all("staff-provided" in a.reason for a in audits)


@pytest.mark.parametrize("prompt,values", CASES[:3])
@pytest.mark.asyncio
async def test_contradictions_abort_atomic_bundle(store, prompt, values):
    await seed_thread(store)
    field = next(iter(values))
    async with store() as db:
        db.add(ScrapedFieldEvidence(scraped_course_id=1, field_key=field, selected=True,
            validation_status="verified", source_url="https://example.edu/course",
            normalized_value='["September"]' if field == "intake_months" else "8", snippet="Official value"))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), prompt, [])
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.international_fee == 9790 and row.intake_months is None and row.ielts_overall is None
        assert not list((await db.execute(select(CourseAuditLog))).scalars())
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert "conflict" in thread.messages[-1]["content"]


@pytest.mark.parametrize("prompt,values", CASES[:3])
@pytest.mark.asyncio
async def test_published_confirmation_and_related_staleness(store, prompt, values):
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    from fastapi import HTTPException
    await seed_thread(store)
    async with store() as db:
        db.add(Course(id=99, university_id=1, name="BSc Music Technology", status="active"))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), prompt, [])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        assert action["requiresConfirmation"] and action["publishedCount"] == 1
        assert (await db.get(ScrapedCourse, 1)).intake_months is None
        with pytest.raises(ValueError, match="Confirmation"):
            await apply(service, db, action, 1, 77)
        # A related-row insertion must stale the preview even without touching Course.updated_at.
        db.add(Fee(course_id=99, international_fee=333))
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id":77})
        assert error.value.status_code == 409
    # Fresh preview succeeds and confirmation is idempotent.
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status = "researching"
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), prompt, [])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id":77})
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id":77})
        from app.services.assistant_staff_edits import state
        saved, _ = await state(db, await db.get(Course, 99), "published", values)
        assert saved == values
        assert len(list((await db.execute(select(CourseAuditLog))).scalars())) == 2 * len(values)


@pytest.mark.asyncio
async def test_latest_field_repeat_is_provider_wide_not_old_location(store):
    await seed_thread(store)
    history = [{"role":"user", "content":"set location Sydney on all courses"},
               {"role":"user", "content":"set intake February and July for row 1"}]
    async with store() as db:
        db.add(University(id=2, name="Other provider", country="UK", city="Example"))
        await db.flush()
        db.add_all([ScrapedCourse(id=2, scrape_job_id="source", university_id=1, course_name="Another", status="pending"),
                    ScrapedCourse(id=3, scrape_job_id="source", university_id=2, course_name="Other", status="pending")])
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), "apply this to all courses", history)
    async with store() as db:
        for id in (1, 2):
            row = await db.get(ScrapedCourse, id)
            assert row.intake_months == ["February", "July"] and row.course_location is None
        assert (await db.get(ScrapedCourse, 3)).intake_months is None


@pytest.mark.parametrize("prompt", ["set tuition 24000 for row 1", "set duration 2 for row 1", "set IELTS 9.5 for row 1"])
@pytest.mark.asyncio
async def test_incomplete_or_invalid_bundles_do_not_write(store, prompt):
    await seed_thread(store)
    assert await handle(service, "eligibility", "turn", "owner", await snapshot(store), prompt, [])
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).international_fee == 9790
        assert not list((await db.execute(select(CourseAuditLog))).scalars())


@pytest.mark.parametrize("prompt", ["only explain set intake February for row 1",
                                   "do not set IELTS 6 for row 1", "read-only set tuition AUD 24000 annual 2026"])
def test_read_only(prompt):
    assert parse(prompt) is None


def test_unsupported_intervening_edit_blocks_old_location_and_repeat_chains():
    history = [{"role":"user", "content":"set location Sydney"},
               {"role":"user", "content":"set study mode online"}]
    assert parse("apply this to all courses", history).error
    history[1]["content"] = "set IELTS 6.5 for row 1"
    history.append({"role":"user", "content":"apply this to all courses"})
    assert parse("apply this to all courses", history).values == {"ielts_overall":6.5}


@pytest.mark.asyncio
async def test_large_batch_permission_and_stale_preview(store):
    from app.models.user import User
    await seed_thread(store)
    async with store() as db:
        db.add_all([ScrapedCourse(id=i, scrape_job_id="source", university_id=1, course_name=f"Course {i}", status="pending") for i in range(2, 12)])
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), CASES[0][0], [])
    async with store() as db:
        action = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]["actions"][0]
        assert action["courseCount"] == 11 and action["requiresConfirmation"]
        with pytest.raises(ValueError, match="another provider"):
            await apply(service, db, action, 2, 77, confirmed=True)
        (await db.get(User, 77)).is_active = False
        await db.commit()
        with pytest.raises(ValueError, match="permission"):
            await apply(service, db, action, 1, 77, confirmed=True)
        (await db.get(User, 77)).is_active = True
        (await db.get(ScrapedCourse, 11)).course_name = "Renamed"
        await db.commit()
        with pytest.raises(ValueError, match="changed"):
            await apply(service, db, action, 1, 77, confirmed=True)
        assert (await db.get(ScrapedCourse, 1)).intake_months is None


@pytest.mark.parametrize("prompt,values", CASES[:3])
@pytest.mark.asyncio
async def test_tool_provenance_retry_and_atomic_undo(store, prompt, values):
    from app.services.scrape_agent_v2.tools import DataTools
    await seed_thread(store)
    tools = DataTools(service, "eligibility", "turn", "owner", await snapshot(store),
                      prompt.replace("on all courses", "for row 1"))
    result = await tools.execute("edit_staff_values", {"row_id":1}, "staff-1")
    assert result["changed"] and result["provenance"] == "staff-provided"
    assert not (await tools.execute("edit_staff_values", {"row_id":1}, "staff-1"))["changed"]
    tools.prompt = "undo that"
    assert (await tools.execute("undo_last", {}, "undo-staff"))["changed"]
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.international_fee == 9790 and row.intake_months is None and row.ielts_overall is None
        assert not row.extraction_method
        assert all(not p.selected for p in (await db.execute(select(ScrapedFieldEvidence))).scalars())


@pytest.mark.asyncio
async def test_tool_supplied_values_cannot_trigger_research(store):
    from app.services.scrape_agent_v2.tools import DataTools
    await seed_thread(store)
    tools = DataTools(service, "eligibility", "turn", "owner", await snapshot(store),
                      "set intake February for row 1")
    result = await tools.execute("start_batch_check", {}, "forbidden")
    assert not result["ok"] and "without source research" in result["reason"]
    assert not (await tools.execute("edit_staff_values", {"row_id":999}, "outside"))["changed"]
    tools.prompt = "only explain set IELTS 6.5 for row 1"
    assert not (await tools.execute("edit_staff_values", {"row_id":1}, "read-only"))["changed"]


@pytest.mark.asyncio
async def test_old_turn_preview_cannot_be_confirmed(store):
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    from fastapi import HTTPException
    await seed_thread(store)
    async with store() as db:
        db.add(Course(id=99, university_id=1, name="Example", status="active"))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), CASES[0][0], [])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        thread.turn_id = "new-turn"
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id":77})
        assert error.value.status_code == 409
        assert (await db.get(ScrapedCourse, 1)).intake_months is None


@pytest.mark.parametrize("scope_text,expected_kind", [("row 1", "pending"), ("published row 1", "published")])
@pytest.mark.asyncio
async def test_numbered_rows_never_cross_record_namespaces(store, scope_text, expected_kind):
    await seed_thread(store)
    async with store() as db:
        db.add(Course(id=1, university_id=1, name="Unrelated published course", status="active"))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store),
                 f"set intake February for {scope_text}", [])
    async with store() as db:
        action = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]["actions"][0]
        assert [t["kind"] for t in action["_targets"]] == [expected_kind]
        if expected_kind == "published":
            assert (await db.get(ScrapedCourse, 1)).intake_months is None
        else:
            assert (await db.get(ScrapedCourse, 1)).intake_months == ["February"]
        assert not list((await db.execute(select(Intake))).scalars())


@pytest.mark.asyncio
async def test_closed_published_intake_never_reopened(store):
    await seed_thread(store)
    async with store() as db:
        db.add(Course(id=99, university_id=1, name="Published course", status="active"))
        await db.flush()
        db.add(Intake(id=99, course_id=99, intake_month="September", is_open=False))
        await db.commit()
    await handle(service, "eligibility", "turn", "owner", await snapshot(store), CASES[0][0], [])
    async with store() as db:
        action = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]["actions"][0]
        with pytest.raises(ValueError, match="closed"):
            await apply(service, db, action, 1, 77, confirmed=True)
        assert not (await db.get(Intake, 99)).is_open
        assert (await db.get(Intake, 99)).intake_month == "September"
        assert (await db.get(ScrapedCourse, 1)).intake_months is None
        assert not list((await db.execute(select(CourseAuditLog))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize("use_tool", [False, True])
async def test_leave_others_unchanged_never_expands_write_scope(store, use_tool):
    from app.services.scrape_agent_v2.tools import DataTools
    await seed_thread(store)
    async with store() as db:
        db.add(ScrapedCourse(id=2, scrape_job_id="source", university_id=1,
                            course_name="Another course", status="pending"))
        await db.commit()
    prompt = "set intake February for row 1; leave others unchanged"
    ctx = await snapshot(store)
    if use_tool:
        tools = DataTools(service, "eligibility", "turn", "owner", ctx, prompt)
        assert not (await tools.execute("edit_staff_values", {"row_id":2}, "excluded"))["changed"]
        assert (await tools.execute("edit_staff_values", {"row_id":1}, "included"))["changed"]
    else:
        await handle(service, "eligibility", "turn", "owner", ctx, prompt, [])
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).intake_months == ["February"]
        assert (await db.get(ScrapedCourse, 2)).intake_months is None
        assert {a.scraped_course_id for a in (await db.execute(select(CourseAuditLog))).scalars()} == {1}


@pytest.mark.asyncio
async def test_provider_wide_exclusion_is_not_silently_ignored(store):
    await seed_thread(store)
    await handle(service, "eligibility", "turn", "owner", await snapshot(store),
                 "set intake February on all courses except row 1", [])
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).intake_months is None
        assert not list((await db.execute(select(CourseAuditLog))).scalars())

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import assistant_intake_consolidation as merge
from app.services.scrape_assistant_autorun import choose_action
from tests.test_assistant_eligibility import seed_thread
from tests.test_scrape_prompt_assistant import store, assistant_postgres

HOME = "https://www.example.edu"
BASE = HOME + "/study/courses/advanced-computer-science-msc"
NAME = "Advanced Computer Science MSc"


def sample(identifier, month, **changes):
    values = {c.key: None for c in ScrapedCourse.__table__.columns}
    values.update(
        id=identifier, university_id=1, scrape_job_id="source", course_id=None,
        course_name=f"{NAME} ({month} 2027)", course_website=f"{BASE}/{month.lower()}-2027",
        status="pending", intake_months=[month], international_fee=24800,
        fee_year=2027, currency="GBP", study_mode="On Campus",
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("prompt", [
    f"{NAME} why same course is showing 3 time? only intake is different",
    "1 2 3 all are same course",
    f"remove intake from course name and make course with multiple intake {BASE}",
    f"merge the intake rows for {NAME}",
])
def test_consolidation_intent_precedes_name_choice_and_discovery(prompt):
    assert merge.requested(prompt)


@pytest.mark.parametrize("prompt", [
    "add January as intake", "fix all IELTS bands", "why fees are missing",
    "merge student accounts", "yes",
    "fix IELTS for the same course", "what are the fees for the same course",
])
def test_ordinary_requests_are_not_hijacked(prompt):
    assert not merge.requested(prompt)


def test_yes_to_legacy_choices_prepares_a_real_preview_not_another_choice_loop():
    choices = [{"title": f"{NAME} ({m} 2027)", "url": f"{BASE}/{m.lower()}-2027"}
               for m in ("January", "April", "September")]
    assert merge.requested("yes", [{"role": "assistant", "courseChoices": choices}])
    choices[-1]["title"] = "Software Engineering MSc (September 2027)"
    assert not merge.requested("yes", [{"role": "assistant", "courseChoices": choices}])


def test_identical_course_combines_intakes_and_preserves_award():
    rows = [sample(1, "January"), sample(2, "April"), sample(3, "September")]
    plan = merge.make_plan(rows, HOME)
    assert plan["courseName"] == NAME and plan["canonicalUrl"] == BASE
    assert plan["intakes"] == ["January", "April", "September"]
    assert plan["keepId"] == 1 and plan["excludeIds"] == [2, 3]
    assert not plan["conflicts"]


def test_existing_multiple_intakes_are_retained_not_silently_dropped():
    rows = [sample(1, "January", intake_months=["January", "October"]),
            sample(2, "April", intake_months=["April", "October"])]
    plan = merge.make_plan(rows, HOME)
    assert not plan["conflicts"]
    assert plan["intakes"] == ["January", "April", "October"]


@pytest.mark.parametrize("field,value", [
    ("fee_year", 2026), ("international_fee", 29000), ("currency", "USD"),
    ("course_location", "London"), ("duration", 2), ("study_mode", "Online"),
    ("ielts_overall", 7), ("degree_level", "Bachelor"), ("fee_scope_key", "placement"),
])
def test_distinct_offerings_and_requirements_are_never_discarded(field, value):
    plan = merge.make_plan([sample(1, "January"), sample(2, "April", **{field: value})], HOME)
    assert field in plan["conflicts"]


@pytest.mark.parametrize("changes", [
    {"course_name": "Computer Science BSc (April 2027)"},
    {"course_website": BASE + "/april-2026"},
    {"course_website": "https://unrelated.example/msc/april-2027"},
    {"course_website": BASE + "/april-2027?campus=london"},
    {"scrape_job_id": "other-review"},
    {"status": "approved"},
    {"course_id": 99},
])
def test_other_years_awards_hosts_reviews_and_published_rows_fail_closed(changes):
    with pytest.raises(ValueError):
        merge.make_plan([sample(1, "January"), sample(2, "April", **changes)], HOME)


def test_url_month_is_not_a_substitute_for_a_successful_intake_repair():
    plan = merge.make_plan([sample(1, "January"), sample(2, "September", intake_months=["January"])], HOME)
    assert plan["conflicts"]["intake_months"] == [
        {"id": 2, "value": ["January"], "expectedIntake": "September"},
    ]


async def setup(store, *, conflict=False, engine="legacy"):
    await seed_thread(store)
    async with store() as db:
        original = await db.get(ScrapedCourse, 1)
        values = {c.key: deepcopy(getattr(original, c.key)) for c in original.__table__.columns}
        for identifier, month in enumerate(("January", "April", "September"), 1):
            row = original if identifier == 1 else ScrapedCourse(**{**values, "id": identifier})
            row.course_name = f"{NAME} ({month} 2027)"
            row.course_website = f"{BASE}/{month.lower()}-2027"
            row.intake_months = [month]
            row.international_fee, row.currency, row.fee_year = 24800, "GBP", 2027
            if conflict and identifier == 3:
                row.fee_year, row.intake_months = 2026, ["January"]
            db.add(row)
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        prompt = f"remove intake from course name and make course with multiple intake {BASE}"
        thread.messages = [{"role": "user", "content": prompt, "autoRun": True,
                            "_engine": engine, "turnId": "turn", "createdAt": service.now().isoformat()}]
        await db.commit()
    return {"job": {"id": "source"}, "home": HOME, "university_id": 1}, prompt


async def preview(store, monkeypatch, *, conflict=False):
    ctx, prompt = await setup(store, conflict=conflict)
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, prompt, [
        {"role": "user", "content": prompt},
    ])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        return deepcopy(thread.messages[-1]["actions"][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["legacy", "v2"])
async def test_both_engines_route_consolidation_before_generic_names_or_fee_repair(store, monkeypatch, engine):
    ctx, _ = await setup(store, engine=engine)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        thread.status, thread.run_id = "queued", None
        await db.commit()
    monkeypatch.setattr(service, "context", AsyncMock(return_value=ctx))
    legacy = AsyncMock(side_effect=AssertionError("Must not ask generic course choices or fetch pages"))
    monkeypatch.setattr(service, "_investigate", legacy)
    await service.investigate("eligibility", "turn")
    legacy.assert_not_awaited()
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.status == "ready"
        assert thread.messages[-1]["actions"][0]["kind"] == "merge_intakes"
        assert "different awards" in thread.messages[-1]["content"]
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
async def test_clear_preview_has_exact_before_after_and_respects_auto_run_threshold(store, monkeypatch):
    action = await preview(store, monkeypatch)
    assert action["state"] == "proposed" and not action["requiresConfirmation"]
    assert choose_action([action]) == action
    assert not choose_action([{**action, "courseCount": 11}])
    assert not choose_action([{**action, "requiresConfirmation": True}])
    assert action["intakeConsolidation"]["rows"][1]["name"] == NAME + " (April 2027)"
    assert action["intakeConsolidation"]["intakes"] == ["January", "April", "September"]


@pytest.mark.asyncio
async def test_clear_pending_merge_runs_through_the_real_automatic_dispatcher(store, monkeypatch):
    from app.services.scrape_assistant_autorun import autorun
    await preview(store, monkeypatch)
    await autorun("eligibility", "turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.messages[-1]["actions"][0]["state"] == "completed"
        assert (await db.get(ScrapedCourse, 1)).intake_months == ["January", "April", "September"]
        assert (await db.get(ScrapedCourse, 2)).status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [
    f"why is {NAME} showing as the same course with different intake",
    f"only explain how to merge intake records for {NAME}",
    f"do not merge the intake records for {NAME}",
    f"don't remove the intake from the course name for {NAME}",
])
async def test_read_only_and_negated_requests_cannot_automatically_merge(store, monkeypatch, prompt):
    ctx, _ = await setup(store)
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, prompt,
                              [{"role": "user", "content": prompt}])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        action = thread.messages[-1]["actions"][0]
        assert action["requiresConfirmation"]
        assert not choose_action([action])
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    BASE + "-conversion",
    "https://another.example/study/courses/advanced-computer-science-msc",
    NAME + " with placement",
])
async def test_similar_title_or_url_never_selects_another_course_family(store, monkeypatch, target):
    ctx, _ = await setup(store)
    prompt = f"merge intake records for {target}"
    assert await merge.handle(service, "eligibility", "turn", "owner", ctx, prompt,
                              [{"role": "user", "content": prompt}])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        assert thread.messages[-1]["actions"] == []


@pytest.mark.asyncio
async def test_actual_report_conflicts_stop_automatic_merging_without_refetch(store, monkeypatch):
    action = await preview(store, monkeypatch, conflict=True)
    assert action["state"] == "needs_review" and not choose_action([action])
    assert set(action["intakeConsolidation"]["conflicts"]) == {"fee_year", "intake_months"}
    async with store() as db:
        with pytest.raises(ValueError, match="Conflicting"):
            await merge.apply(service, db, action, 1, 77)
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()
        assert len((await db.execute(select(ScrapedCourse).where(ScrapedCourse.status == "pending"))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_confirm_merge_is_audited_idempotent_and_undo_restores_rows(store, monkeypatch):
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    action = await preview(store, monkeypatch)
    async with store() as db:
        before = [{c.key: deepcopy(getattr(r, c.key)) for c in r.__table__.columns}
                  for r in (await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars()]
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        undo = deepcopy(thread.messages[-1]["actions"][-1])
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        assert [r.status for r in rows] == ["pending", "rejected", "rejected"]
        assert rows[0].course_name == NAME and rows[0].course_website == BASE
        assert rows[0].intake_months == ["January", "April", "September"]
        assert all(r.international_fee == 24800 for r in rows)
        assert len((await db.execute(select(CourseAuditLog))).scalars().all()) == 3
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        assert len((await db.execute(select(CourseAuditLog))).scalars().all()) == 3
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 77})
    async with store() as db:
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        assert [{c.key: getattr(r, c.key) for c in r.__table__.columns} for r in rows] == before
        assert all(r.status == "pending" for r in rows)
        assert [r.intake_months for r in rows] == [["January"], ["April"], ["September"]]
        assert [r.course_name for r in rows] == [f"{NAME} ({m} 2027)" for m in ("January", "April", "September")]
        assert not (await db.execute(select(ScrapedFieldEvidence).where(ScrapedFieldEvidence.selected.is_(True)))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("later_change", ["edit", "approved", "provider", "existing_canonical"])
async def test_stale_or_conflicting_preview_has_no_partial_writes(store, monkeypatch, later_change):
    action = await preview(store, monkeypatch)
    async with store() as db:
        row = await db.get(ScrapedCourse, 3)
        if later_change == "edit":
            row.course_name = "A later staff edit"
        elif later_change == "approved":
            row.status = "approved"
        elif later_change == "provider":
            action["_jobId"] = "another-review"
        else:
            db.add(ScrapedCourse(id=4, university_id=1, scrape_job_id="source",
                course_name=NAME, course_website=BASE, status="pending"))
        await db.commit()
        with pytest.raises(ValueError):
            await merge.apply(service, db, action, 1, 77)
        assert (await db.get(ScrapedCourse, 1)).course_name == NAME + " (January 2027)"
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["row", "evidence", "edit_away_back", "evidence_away_back"])
async def test_undo_refuses_later_row_or_evidence_changes(store, monkeypatch, change):
    action = await preview(store, monkeypatch)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()
        undo = merge.undo_proposal(action)
        row = await db.get(ScrapedCourse, 1)
        if change in {"evidence", "evidence_away_back"}:
            proof = await db.get(ScrapedFieldEvidence, action["_evidenceIds"][0])
            old_snippet = proof.snippet
            proof.snippet = "Changed proof"
        else:
            row.course_location = "Later staff location"
        await db.commit()
        if change == "edit_away_back":
            row.course_location = None
            await db.commit()
        if change == "evidence_away_back":
            proof.snippet = old_snippet
            await db.commit()
        with pytest.raises(ValueError, match="changed"):
            await merge.apply(service, db, undo, 1, 77, undo=True)
        assert (await db.get(ScrapedCourse, 2)).status == "rejected"


@pytest.mark.asyncio
async def test_merge_can_be_repeated_after_undo_without_evidence_identity_collision(store, monkeypatch):
    action = await preview(store, monkeypatch)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()
        await merge.apply(service, db, merge.undo_proposal(action), 1, 77, undo=True)
        await db.commit()
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        again = {**action, "id": "another-approved-consolidation", "state": "proposed",
                 "_versions": {str(r.id): merge.version(service, r, "pending") for r in rows}}
        await merge.apply(service, db, again, 1, 77)
        await db.commit()
        proofs = list((await db.execute(select(ScrapedFieldEvidence))).scalars())
        assert len(proofs) == 2 and sum(p.selected for p in proofs) == 1
        assert (await db.get(ScrapedCourse, 1)).intake_months == ["January", "April", "September"]

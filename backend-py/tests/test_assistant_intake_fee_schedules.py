from copy import deepcopy
import asyncio

import pytest
from sqlalchemy import select

from app.models import Course, Fee, ScrapedCourse
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import assistant_intake_consolidation as merge
from app.services import assistant_intake_fees as fees
from app.services import scrape_assistant as service
from app.services.scrape_assistant_autorun import choose_action
from tests import test_assistant_intake_consolidation as base
from tests.test_scrape_prompt_assistant import store, assistant_postgres


def samples():
    return [base.sample(1, "January", fee_term="Annual", fee_year=2026),
            base.sample(2, "April", fee_term="Annual", fee_year=2026),
            base.sample(3, "September", fee_term="Annual", fee_year=2027)]


def test_same_price_different_years_are_preserved_with_intake_and_source_applicability():
    plan = merge.make_plan(samples(), base.HOME)
    assert not plan["conflicts"]
    assert "fee_year" not in plan["_values"]
    assert [(s["feeYear"], s["intakes"]) for s in plan["feeSchedules"]] == [
        (2026, ["January", "April"]), (2027, ["September"]),
    ]
    assert plan["feeSchedules"][0]["sourceRowIds"] == [1, 2]
    assert plan["feeSchedules"][1]["sourceUrls"] == [base.BASE + "/september-2027"]
    assert fees.intact(plan["_feeSchedules"])
    assert plan["_feeSchedules"]["verification"] == "unverified"
    assert plan["_feeSchedules"]["publicationHold"]


@pytest.mark.parametrize("field,value", [
    ("international_fee", 29000), ("international_fee", None), ("international_fee", float("inf")),
    ("international_fee", -1), ("currency", "USD"), ("currency", None),
    ("fee_term", "Full Course"), ("fee_term", None),
    ("fee_year", None), ("fee_year", True), ("fee_year", 99999),
    ("extraction_method", {"fee_variants": {"status": "uniform"}}),
])
def test_incomplete_or_conflicting_tuition_never_becomes_a_safe_schedule(field, value):
    rows = samples()
    setattr(rows[2], field, value)
    assert "fee_year" in merge.make_plan(rows, base.HOME)["conflicts"]


def test_intake_disagreement_still_blocks_merge_even_when_fee_years_are_retained():
    rows = samples()
    rows[2].intake_months = ["January"]
    plan = merge.make_plan(rows, base.HOME)
    assert "intake_months" in plan["conflicts"] and "fee_year" not in plan["conflicts"]
    assert plan["feeSchedules"][1]["applicabilityNeedsReview"]
    assert plan["feeSchedules"][1]["intakes"] == ["September"]  # proposal, not a verified write


def test_one_intake_cannot_be_assigned_to_two_fee_years():
    rows = samples()
    rows[2].intake_months = ["September", "January"]
    assert "fee_year" in merge.make_plan(rows, base.HOME)["conflicts"]


def test_valid_case_variations_do_not_create_false_applicability_conflicts():
    rows = samples()
    rows[0].intake_months = ["january"]
    plan = merge.make_plan(rows, base.HOME)
    assert not plan["conflicts"]
    assert not plan["feeSchedules"][0]["applicabilityNeedsReview"]


async def prepared(store):
    ctx, prompt = await base.setup(store)
    async with store() as db:
        for row in (await db.execute(select(ScrapedCourse))).scalars():
            row.fee_term = "Annual"
            row.fee_year = 2027 if row.id == 3 else 2026
        await db.commit()
    await merge.handle(service, "eligibility", "turn", "owner", ctx, prompt, [
        {"role": "user", "content": prompt},
    ])
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        return deepcopy(thread.messages[-1]["actions"][0])


@pytest.mark.asyncio
async def test_multi_year_preview_requires_confirmation_even_for_three_pending_rows(store):
    action = await prepared(store)
    assert action["state"] == "proposed" and action["requiresConfirmation"]
    assert len(action["intakeConsolidation"]["feeSchedules"]) == 2
    assert choose_action([action]) is None


@pytest.mark.asyncio
async def test_confirm_retains_two_schedules_in_full_and_summary_reads_and_undo_restores_every_column(store):
    from app.routers.scrape import _staged_row_to_dict, _staged_summary
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    action = await prepared(store)
    async with store() as db:
        before = [{c.key: deepcopy(getattr(r, c.key)) for c in r.__table__.columns}
                  for r in (await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars()]
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 77})
        row = await db.get(ScrapedCourse, 1)
        assert row.fee_year == 2026 and row.international_fee == 24800
        manifest = deepcopy(row.extraction_method[fees.KEY])
        assert len(manifest["schedules"]) == 2 and fees.intact(manifest)
        full = _staged_row_to_dict(row)
        summary = _staged_summary(deepcopy(full))
        assert summary["intakeFeeSchedules"] == full["intakeFeeSchedules"]
        assert full["intakeFeeSchedules"]["snapshotMatchesCurrent"]
        assert not (await db.execute(select(Fee))).scalars().all()
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        undo = deepcopy(thread.messages[-1]["actions"][-1])
        assert len(undo["_evidenceIds"]) == 2
        await confirm_action("source", undo["id"], ConfirmBody(confirm=True), db, {"id": 77})
        rows = list((await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars())
        assert [{c.key: getattr(r, c.key) for c in r.__table__.columns} for r in rows] == before
        assert await fees.publication_hold(db, rows[0]) is None
        proofs = list((await db.execute(select(ScrapedFieldEvidence))).scalars())
        assert all(not p.selected and p.validation_status == "superseded" for p in proofs)


@pytest.mark.asyncio
@pytest.mark.parametrize("target,erase_metadata", [(1, False), (1, True), (2, False)])
async def test_publication_cannot_reduce_schedules_or_publish_an_excluded_duplicate(store, target, erase_metadata):
    from app.services.scraper.approve_course import approve_scraped_course, ApprovalValidationError
    action = await prepared(store)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()
        row = await db.get(ScrapedCourse, target)
        if erase_metadata:
            row.extraction_method = {}  # simulate a later extractor replacing its JSON
            await db.commit()
        with pytest.raises(ApprovalValidationError, match="tuition-year|excluded source"):
            await approve_scraped_course(db, row, actor="77", commit=False)
        assert not (await db.execute(select(Course))).scalars().all()
        assert not (await db.execute(select(Fee))).scalars().all()


@pytest.mark.asyncio
async def test_later_scalar_edits_flag_the_saved_snapshot_and_make_undo_refuse(store):
    action = await prepared(store)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()
        row = await db.get(ScrapedCourse, 1)
        row.fee_year = 2028
        await db.commit()
        from app.routers.scrape import _staged_row_to_dict
        assert not _staged_row_to_dict(row)["intakeFeeSchedules"]["snapshotMatchesCurrent"]
        with pytest.raises(ValueError, match="changed"):
            await merge.apply(service, db, merge.undo_proposal(action), 1, 77, undo=True)


@pytest.mark.asyncio
async def test_fee_schedule_evidence_is_covered_by_undo_fences(store):
    action = await prepared(store)
    async with store() as db:
        await merge.apply(service, db, action, 1, 77)
        await db.commit()
        proof = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.field_key == fees.KEY,
        ))).scalar_one()
        proof.snippet = "A later review changed this evidence"
        await db.commit()
        with pytest.raises(ValueError, match="evidence changed"):
            await merge.apply(service, db, merge.undo_proposal(action), 1, 77, undo=True)


@pytest.mark.asyncio
async def test_approval_waits_for_merge_then_refreshes_stale_scalar_values(store, monkeypatch):
    from app.services.scraper.approve_course import approve_scraped_course, ApprovalValidationError
    action = await prepared(store)
    async with store() as merging, store() as approving:
        stale = await approving.get(ScrapedCourse, 1)
        assert not (stale.extraction_method or {}).get(fees.KEY)
        await merge.apply(service, merging, action, 1, 77)
        waiting = asyncio.Event()
        execute = approving.execute
        async def observed(statement, *args, **kwargs):
            if "pg_advisory_xact_lock" in str(statement):
                waiting.set()
            return await execute(statement, *args, **kwargs)
        monkeypatch.setattr(approving, "execute", observed)
        task = asyncio.create_task(approve_scraped_course(approving, stale, actor="77", commit=False))
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            assert not task.done()
            await merging.commit()
            with pytest.raises(ApprovalValidationError, match="tuition-year"):
                await asyncio.wait_for(task, 5)
            assert not (await approving.execute(select(Course))).scalars().all()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_changed_schedule_fingerprint_does_not_display_a_corrupt_snapshot():
    plan = merge.make_plan(samples(), base.HOME)
    manifest = deepcopy(plan["_feeSchedules"])
    manifest["schedules"] = "invalid"
    projection = fees.projection({"extraction_method": {fees.KEY: manifest}})
    assert projection["schedules"] == []
    assert not projection["snapshotMatchesCurrent"]


def test_malformed_snapshot_is_rejected_even_with_a_recomputed_fingerprint():
    manifest = deepcopy(merge.make_plan(samples(), base.HOME)["_feeSchedules"])
    manifest["schedules"] = [None, None]
    manifest["fingerprint"] = fees.digest({k: v for k, v in manifest.items() if k != "fingerprint"})
    assert not fees.intact(manifest)
    assert fees.projection({"extraction_method": {fees.KEY: manifest}})["schedules"] == []

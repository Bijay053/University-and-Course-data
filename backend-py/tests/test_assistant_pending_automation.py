from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.services import assistant_pending_automation as policy
from app.services.scrape_assistant_autorun import choose_action, execution_requested

SETTINGS = SimpleNamespace(scrape_assistant_auto_pending_user_ids="1, 7")


def batch():
    return {"id":"batch","kind":"merge_intakes","state":"proposed","courseCount":39,
            "requiresConfirmation":True,"_ids":list(range(1,40)),
            "intakeConsolidations":[{"keepId":i,"excludeIds":[i+1,i+2],"conflicts":{},
                "feeSchedules":[{"feeYear":2026},{"feeYear":2027}]}
                for i in range(1,40,3)],
            "blockedIntakeConsolidations":[{"conflicts":{"study_mode":["Online","On Campus"]}}]}


def test_opted_in_large_fee_schedule_merge_runs_without_expanding_to_conflicts():
    action = batch()
    original = deepcopy(action)
    policy.prepare({"actions":[action]},{"autoRun":True,"content":"Fix all intake variants"},SETTINGS,1)
    assert action["automaticPending"] and not action["requiresConfirmation"]
    assert choose_action([action]) is action
    assert action["_ids"] == original["_ids"]
    assert action["intakeConsolidations"] == original["intakeConsolidations"]
    assert action["blockedIntakeConsolidations"] == original["blockedIntakeConsolidations"]


@pytest.mark.parametrize("actor,prompt,automatic", [
    (2,"Merge all intake variants",True),
    (1,"Read-only: merge all intake variants",True),
    (1,"Prepare a preview to merge all intake variants",True),
    (1,"Show me a preview of the intake merges",True),
    (1,"Preview the intake merges",True),
    (1,"Merge intake variants, preview only",True),
    (1,"Do not merge any rows",True),
    (1,"Merge all intake variants",False),
])
def test_other_accounts_and_preview_only_requests_keep_confirmation(actor,prompt,automatic):
    action = batch()
    policy.prepare({"actions":[action]},{"autoRun":automatic,"content":prompt},SETTINGS,actor)
    assert action["requiresConfirmation"] and not action.get("automaticPending")
    assert choose_action([action]) is None


@pytest.mark.parametrize("change", [
    {"state":"needs_review"}, {"state":"completed"}, {"publishedCount":1},
    {"courseCount":40}, {"_ids":[1]}, {"intakeConsolidations":[
        {"keepId":1,"excludeIds":[2],"conflicts":{"study_mode":["Online","On Campus"]}}]},
])
def test_conflicting_published_malformed_and_terminal_actions_never_auto_run(change):
    action = {**batch(),**change}
    assert not policy.supported(action)


@pytest.mark.parametrize("kind",["staff_edit","staff_location"])
def test_staff_changes_are_pending_only(kind):
    action = {"kind":kind,"state":"proposed","courseCount":11,
              "_targets":[{"id":i,"kind":"pending"} for i in range(11)]}
    assert policy.supported(action)
    action["_targets"][0]["kind"] = "published"
    assert not policy.supported(action)


def test_document_updates_require_existing_applicability_validation_and_pending_targets():
    action = {"kind":"document_update","state":"proposed","courseCount":12,
        "_applicabilityVersion":1,"_targets":[{"id":i,"version":"checked","changes":[
            {"field":"ielts_overall","newValue":6.5}]} for i in range(12)]}
    assert policy.supported(action)
    action["_targets"][0]["kind"] = "published"
    assert not policy.supported(action)
    action["_targets"][0].pop("kind")
    action.pop("_applicabilityVersion")
    assert not policy.supported(action)


def test_new_document_request_retires_old_run_buttons_without_executing_them():
    old = {"kind":"document_update","state":"proposed","_turnId":"old"}
    current = {"kind":"document_update","state":"proposed","_turnId":"current"}
    finished = {"kind":"document_update","state":"completed","_turnId":"old"}
    other = {"kind":"fix_fields","state":"proposed","_turnId":"old"}
    messages = [{"actions":[old,finished,other]},{"actions":[current]}]
    assert policy.supersede_document_previews(messages,"current")
    assert old["state"] == "superseded" and "not applied" in old["resultSummary"]
    assert current["state"] == "proposed"
    assert finished["state"] == "completed" and other["state"] == "proposed"
    assert not policy.supersede_document_previews(messages,"current")


def test_html_document_updates_require_the_current_matching_version():
    action = {"kind": "document_update", "state": "proposed", "courseCount": 1,
              "_sourceType": "html", "_applicabilityVersion": 1,
              "_targets": [{"id": 1, "version": "checked",
                            "changes": [{"field": "ielts_overall", "newValue": 6.5}]}]}
    assert not policy.supported(action)
    action["_applicabilityVersion"] = 2
    assert policy.supported(action)


def test_bulk_intake_repair_checks_remaining_rows_but_not_known_unsupported_sources():
    action = {"kind":"fix_fields","state":"proposed","courseCount":12,"_ids":list(range(12)),
        "fields":["intake_months"],"_sourceCheckedIds":[0],
        "repairPreview":[{"id":i,"status":"supported" if i==0 else "unverified"} for i in range(12)]}
    assert policy.supported(action)
    action["_sourceCheckedIds"].append(1)
    assert not policy.supported(action)


def test_large_generic_repairs_require_verified_source_evidence():
    action = {"kind":"fix_fields","state":"proposed","courseCount":12,"_ids":list(range(12)),
              "fields":["international_fee"],"_evidenceUrls":["https://www.example.edu/course"]}
    assert policy.supported(action)
    action.pop("_evidenceUrls")
    assert not policy.supported(action)


def test_explicit_run_without_preview_still_authorises_execution():
    assert execution_requested("Do not show a preview; run the pending merge now")


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly",[False,True])
async def test_large_batch_normalisation_is_persisted_and_readonly_remains_gated(store,monkeypatch,readonly):
    from app.config import settings
    from tests.test_assistant_intake_batches import prepare
    monkeypatch.setattr(settings,"scrape_assistant_auto_pending_user_ids","77")
    action = await prepare(store,monkeypatch,readonly=readonly)
    if readonly:
        assert action["requiresConfirmation"] and not action.get("automaticPending")
    else:
        assert action["courseCount"] == 12 and action["automaticPending"]
        assert choose_action([action]) == action


@pytest.mark.asyncio
async def test_large_batch_runs_and_stale_history_cannot_replay_it(store,monkeypatch):
    from sqlalchemy import select, func
    from app.config import settings
    from app.models import ScrapedCourse
    from app.models.user import User
    from app.models.audit import CourseAuditLog
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services import scrape_assistant as service
    from app.services.scrape_assistant_autorun import autorun
    from app.routers.scrape_assistant import get_assistant
    from tests.test_assistant_intake_batches import prepare
    monkeypatch.setattr(settings,"scrape_assistant_auto_pending_user_ids","77")
    action = await prepare(store,monkeypatch)
    old_preview = deepcopy(action)
    old_preview.update(id="old-preview",state="proposed")
    async with store() as db:
        user = await db.get(User,77)
        assert user is not None
        user.is_active = user.is_super_admin = True
        await db.commit()
    await autorun("eligibility","turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread,"eligibility")
        current = next(a for a in thread.messages[-1]["actions"] if a["id"] == action["id"])
        assert current["state"] == "completed"
        assert len([a for a in thread.messages[-1]["actions"] if a["kind"]=="undo_intake_merge"]) == 1
        rows = list((await db.execute(select(ScrapedCourse))).scalars())
        assert sum(r.status=="rejected" for r in rows) == 8
        assert all(r.status=="pending" for r in rows if r.id in {1,4,7,10,13})
        audits = await db.scalar(select(func.count()).select_from(CourseAuditLog))
        assert audits > 0
        thread.messages = [{"role":"assistant","content":"Old preview","actions":[old_preview]},*thread.messages]
        await db.commit()
        response = await get_assistant("source",db,{"id":77})
        assert response["messages"][0]["actions"][0]["state"] == "superseded"
    await autorun("eligibility","turn")
    async with store() as db:
        assert await db.scalar(select(func.count()).select_from(CourseAuditLog)) == audits
        assert await db.scalar(select(func.count()).select_from(ScrapedCourse).where(
            ScrapedCourse.status=="rejected")) == 8


@pytest.mark.asyncio
async def test_recovery_is_scheduled_once_for_the_current_turn():
    import asyncio
    from unittest.mock import AsyncMock
    service = SimpleNamespace(run_automatic_action=AsyncMock())
    policy.schedule(service,"thread","turn")
    policy.schedule(service,"thread","turn")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    service.run_automatic_action.assert_awaited_once_with("thread","turn")


@pytest.mark.asyncio
async def test_changed_or_already_merged_previews_are_superseded_not_replayed(store):
    from app.models import ScrapedCourse
    from app.services import scrape_assistant as service
    from app.services.assistant_staff_locations import version
    async with store() as db:
        row = await db.get(ScrapedCourse,1)
        action = {"kind":"merge_intakes","state":"proposed","_ids":[1],
                  "_versions":{"1":version(service,row,"pending")}}
        messages = [{"role":"assistant","actions":[action]}]
        assert not await policy.supersede_changed_merges(service,db,messages)
        row.status = "rejected"
        await db.flush()
        assert await policy.supersede_changed_merges(service,db,messages)
        assert action["state"] == "superseded"
        assert "not run again" in action["resultSummary"]
        assert row.status == "rejected"


from tests.test_scrape_prompt_assistant import store, assistant_postgres

"""Location complaints resolve pending rows and retain dated official proof."""
from datetime import datetime, timedelta, timezone
import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import assistant_saved_location_sources as saved
from app.services.snapshot_store import build_s3_key
from tests.test_assistant_saved_intakes import metadata
from tests.test_bedfordshire_intakes import URL, NAME, page
from tests.test_scrape_prompt_assistant import store, assistant_postgres


ROWS = [
    {"id": 1, "name": NAME, "url": URL, "location": "All locations", "status": "pending"},
    {"id": 2, "name": "Nursing MSc", "url": URL + "nursing/", "location": "All locations", "status": "pending"},
    {"id": 3, "name": "Business BA", "url": URL + "business/", "location": "Luton", "status": "pending"},
]


@pytest.mark.parametrize("prompt", [
    "what is this all locations fix this bug",
    "Fix the Course Location values showing All locations in this review",
    "please correct All locations for all Bedfordshire courses",
])
def test_plain_location_complaints_need_no_row_numbers(prompt):
    assert [r["id"] for r in saved.requested_rows(
        prompt, ROWS, "University of Bedfordshire")] == [1, 2]


@pytest.mark.parametrize("prompt", [
    "Only explain why All locations is shown",
    "Do not fix All locations",
    "Set all locations to Sydney",
    "Fix all location fees",
])
def test_readonly_literals_and_unmatched_names_never_broaden_scope(prompt):
    assert saved.requested_rows(prompt, ROWS) is None


def test_explicit_course_and_row_requests_stay_scoped():
    assert [r["id"] for r in saved.requested_rows(
        f"Fix All locations for {NAME}", ROWS)] == [1]
    assert [r["id"] for r in saved.requested_rows(
        "Fix All locations for row 2", ROWS)] == [2]
    assert saved.requested_rows("Fix All locations for row 9999", ROWS) == []
    assert saved.requested_rows(
        "Fix All locations for an imaginary unrelated course", ROWS) == []


def test_short_retry_keeps_the_actual_users_uninterrupted_location_scope():
    original = "what is this all locations fix this bug"
    messages = [
        {"role": "user", "content": original},
        {"role": "assistant", "content": "Source access failed"},
        {"role": "user", "content": "fix this bug"},
    ]
    assert saved.resolve_request("fix it", messages) == original


@pytest.mark.parametrize("intervening", [
    "Fix all course fees", "Check Nursing MSc",
    "Do not fix All locations", "Read-only comparison of All locations",
])
def test_other_topics_and_readonly_never_revive_old_location_repairs(intervening):
    assert saved.resolve_request("fix it", [
        {"role": "user", "content": "fix All locations"},
        {"role": "user", "content": intervening},
    ]) == "fix it"


@pytest.mark.asyncio
async def test_real_field_router_uses_saved_locations_before_a_live_source_gate(monkeypatch):
    from app.services.assistant_field_edits import handle_field_edit
    prepare = AsyncMock(return_value=True)
    monkeypatch.setattr(saved, "prepare_review", prepare)
    api = SimpleNamespace(inspect_source=AsyncMock(
        side_effect=AssertionError("Must not repeat blocked website checks")))
    ctx = {"samples": ROWS, "name": "University of Bedfordshire"}
    assert await handle_field_edit(
        api, "thread", "turn", "run", ctx, "what is this all locations fix this bug")
    assert [r["id"] for r in prepare.call_args.args[-1]] == [1, 2]
    api.inspect_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_clean_review_does_not_expand_back_to_all_valid_locations():
    from app.services.assistant_field_edits import handle_field_edit
    api = SimpleNamespace(now=service.now, save_research_reply=AsyncMock(),
                          inspect_source=AsyncMock(side_effect=AssertionError("No source request needed")))
    assert await handle_field_edit(
        api, "thread", "turn", "run", {"samples": ROWS[2:]},
        "fix All locations in this review")
    reply = api.save_research_reply.call_args.args[3]
    assert "No matching pending location placeholders" in reply["content"]
    assert not reply["actions"]
    api.inspect_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_captured_campus_is_owned_and_not_labelled_as_a_live_check(monkeypatch):
    from app.services import snapshot_store
    download = AsyncMock(return_value=page().encode())
    monkeypatch.setattr(snapshot_store, "download_snapshot", download)
    snapshot = SimpleNamespace(**metadata())
    fact = await saved.captured_location(snapshot, ROWS[0])
    assert fact["value"] == "Luton campus"
    assert fact["quote"] in page()
    assert fact["capturedAt"] == snapshot.fetched_at.isoformat()
    assert fact["sha256"] == hashlib.sha256(page().encode()).hexdigest()
    assert await saved.captured_location(snapshot, {**ROWS[0], "name": "Other course"}) is None


@pytest.mark.asyncio
async def test_supported_subset_has_preview_and_obeys_account_automation(monkeypatch):
    fact = {"value": "Luton campus", "quote": "official admission JSON",
            "url": URL, "capturedAt": datetime.now(timezone.utc).isoformat(), "snapshotId": 991}
    plans = [
        {**ROWS[0], "status": "supported", "_snapshot": fact,
         "changes": [{"field": "course_location", "oldValue": "All locations", "newValue": "Luton campus"}]},
        {**ROWS[1], "status": "unverified", "reason": "Distinct campus options", "changes": []},
    ]
    monkeypatch.setattr(saved, "saved_plans", AsyncMock(return_value=plans))
    api = SimpleNamespace(now=service.now, action_proposal=service.action_proposal,
                          save_research_reply=AsyncMock())
    ctx = {"job": {"id": "source"}, "university_id": 1, "home": "https://www.beds.ac.uk",
           "versions": {"1": "v", "2": "w"}, "config_version": "c"}
    assert await saved.prepare_review(api, "thread", "turn", "run", ctx, ROWS[:2])
    reply = api.save_research_reply.call_args.args[3]
    assert "1 course-specific location corrections" in reply["content"]
    assert "1 unverified" in reply["content"] and "not fresh website checks" in reply["content"]
    action = reply["actions"][0]
    assert action["_ids"] == [1] and action["fields"] == ["course_location"]
    assert action["requiresConfirmation"] and action["repairPreview"][0]["id"] == 1
    from app.services.assistant_pending_automation import prepare
    from app.services.scrape_assistant_autorun import choose_action
    prepare(reply, {"autoRun": True, "content": "fix all locations bug"},
            SimpleNamespace(scrape_assistant_auto_pending_user_ids="1"), 1)
    assert choose_action(reply["actions"]) is action
    assert action["automaticPending"] and not action["requiresConfirmation"]


@pytest.mark.asyncio
async def test_other_university_keeps_its_existing_source_path(monkeypatch):
    plans = AsyncMock()
    monkeypatch.setattr(saved, "saved_plans", plans)
    ctx = {"job": {"id": "source"}, "university_id": 1, "home": "https://other.ac.uk"}
    assert not await saved.prepare_review(None, "", "", "", ctx, ROWS)
    plans.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_copy_reports_its_age_without_reading_or_repeating_live_checks(store, monkeypatch):
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_location = "All locations"
        row.extraction_method = {"course_location": "location.heading"}
        db.add(PageSnapshot(**metadata(
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=28))))
        await db.commit()
    read = AsyncMock(side_effect=AssertionError("Expired bytes cannot authorize a repair"))
    monkeypatch.setattr(saved, "captured_location", read)
    plans = await saved.saved_plans(
        SimpleNamespace(AsyncSessionLocal=store), "source", 1, ROWS[:1])
    assert len(plans) == 1 and plans[0]["status"] == "unverified"
    assert not plans[0]["changes"]
    assert "24-hour automatic-repair limit" in plans[0]["reason"]
    read.assert_not_awaited()
    from app.services.assistant_field_edits import handle_field_edit
    api = SimpleNamespace(AsyncSessionLocal=store, now=service.now,
        save_research_reply=AsyncMock(),
        inspect_source=AsyncMock(side_effect=AssertionError("Do not retry the blocked live gate")))
    ctx = {"job": {"id": "source"}, "university_id": 1,
           "home": "https://www.beds.ac.uk", "samples": ROWS[:1],
           "name": "University of Bedfordshire"}
    assert await handle_field_edit(api, "thread", "turn", "run", ctx,
                                   "Fix All locations in this review")
    reply = api.save_research_reply.call_args.args[3]
    assert "24-hour automatic-repair limit" in reply["content"]
    assert not reply["actions"]
    api.inspect_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_failed_saved_source_does_not_hide_other_supported_courses(store, monkeypatch):
    other = ROWS[1]["url"]
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_location = "All locations"
        row.extraction_method = {"course_location": "location.heading"}
        db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="source",
            course_name=ROWS[1]["name"], course_website=other,
            course_location="All locations", status="pending"))
        db.add(PageSnapshot(**metadata()))
        db.add(PageSnapshot(**metadata(
            id=992, course_url=other, url_hash=hashlib.sha256(other.encode()).hexdigest()[:16],
            storage_path=build_s3_key(1, "source", other, "html"))))
        await db.commit()
    async def read(snapshot, row):
        if row["id"] == 2:
            raise RuntimeError("A single object could not be read")
        return {"value": "Luton campus", "quote": "owned campus fact", "url": URL,
                "capturedAt": snapshot.fetched_at.isoformat(), "snapshotId": snapshot.id}
    monkeypatch.setattr(saved, "captured_location", read)
    plans = await saved.saved_plans(SimpleNamespace(AsyncSessionLocal=store), "source", 1, ROWS[:2])
    assert [p["status"] for p in plans] == ["supported", "unverified"]
    assert plans[1]["changes"] == []
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).course_location == "All locations"
        assert (await db.get(ScrapedCourse, 2)).course_location == "All locations"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_status", [403, 200])
async def test_unreadable_sources_and_staff_edits_are_not_location_authority(store, monkeypatch, source_status):
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_location = "All locations"
        row.extraction_method = {"course_location": "staff"}
        db.add(PageSnapshot(**metadata(status_code=source_status)))
        await db.commit()
    read = AsyncMock(side_effect=AssertionError("A forbidden source must not be read"))
    monkeypatch.setattr(saved, "captured_location", read)
    plans = await saved.saved_plans(SimpleNamespace(AsyncSessionLocal=store), "source", 1, ROWS[:1])
    if source_status == 403:
        assert plans is None
    else:
        assert plans[0]["status"] == "unverified" and plans[0]["changes"] == []
    read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "body", "version", "expired", "staff"])
async def test_actual_approval_is_fenced_and_only_updates_pending_location(store, monkeypatch, tamper):
    from app.services import snapshot_store
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    from app.routers import scrape
    inspect = AsyncMock(side_effect=AssertionError("No live website request allowed"))
    queue = AsyncMock(side_effect=AssertionError("No discovery or re-extraction allowed"))
    monkeypatch.setattr(service, "inspect_source", inspect)
    monkeypatch.setattr(scrape, "start_bulk_fix_job", queue)
    download = AsyncMock(return_value=page().encode())
    monkeypatch.setattr(snapshot_store, "download_snapshot", download)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_name, row.course_website = NAME, URL
        row.course_location, row.study_mode = "All locations", "On Campus"
        row.international_fee, row.intake_months = 12345, ["January"]
        row.extraction_method = {"course_location": "location.heading", "intake_months": "staff"}
        if tamper == "staff":
            row.extraction_method = {**row.extraction_method, "course_location": "staff"}
        snapshot = PageSnapshot(**metadata())
        db.add(snapshot)
        await db.flush()
        await db.refresh(row)
        fact = await saved.captured_location(snapshot, ROWS[0])
        target = {
            "id": 1, "name": NAME, "url": URL, "status": "supported",
            "changes": [{"field": "course_location", "oldValue": "All locations",
                         "newValue": fact["value"]}],
            "source": {"url": URL, "quote": fact["quote"], "capturedAt": fact["capturedAt"],
                       "origin": "saved_official_html"}, "_snapshot": fact,
        }
        action = service.action_proposal({"kind": "fix_fields", "fields": ["course_location"]}, [1])
        action.update(_versions={"1": service.row_version(row)},
                      _configVersion=service.fingerprint({}),
                      _savedLocationTargets=[target], _savedLocationJobId="source",
                      repairPreview=[target])
        thread = ScrapeAssistantThread(
            id="saved-location", actor_id=7, job_id="source", turn_id="turn", status="ready",
            messages=[{"role": "assistant", "content": "Compare", "actions": [deepcopy(action)]}])
        db.add(thread)
        await db.commit()
        if tamper == "body":
            download.return_value = page().replace("Luton", "Bedford").encode()
        elif tamper == "version":
            row.course_location = "Staff campus"
            await db.commit()
        elif tamper == "expired":
            snapshot.fetched_at = datetime.now(timezone.utc) - timedelta(days=2)
            await db.commit()
        if tamper == "version":
            with pytest.raises(HTTPException):
                await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        else:
            result = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
            assert result["messages"][-1]["actions"][0]["state"] == ("completed" if tamper is None else "error")
        await db.refresh(row)
        assert row.course_location == ("Luton campus" if tamper is None
                                       else "Staff campus" if tamper == "version" else "All locations")
        assert row.international_fee == 12345 and row.intake_months == ["January"]
        assert row.study_mode == "On Campus" and row.status == "pending"
        assert row.extraction_method["intake_months"] == "staff"
        logs = (await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.field_key == "course_location"))).scalars().all()
        assert len(logs) == (1 if tamper is None else 0)
        evidence = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.field_key == "course_location"))).scalars().all()
        assert len(evidence) == (1 if tamper is None else 0)
    inspect.assert_not_awaited()
    queue.assert_not_awaited()

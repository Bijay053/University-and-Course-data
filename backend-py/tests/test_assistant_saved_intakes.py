from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import assistant_saved_intake_sources as saved
from app.services.snapshot_store import build_s3_key
from tests.test_bedfordshire_intakes import URL, NAME, page
from tests.test_scrape_prompt_assistant import store, assistant_postgres


def metadata(**updates):
    data = dict(id=991, university_id=1, scrape_job_id="source", course_url=URL,
                url_hash=hashlib.sha256(URL.encode()).hexdigest()[:16],
                snapshot_type="html", storage_path=build_s3_key(1, "source", URL, "html"),
                status_code=200, content_length=len(page().encode()), fetch_method="httpx",
                fetched_at=datetime.now(timezone.utc))
    data.update(updates)
    return data


@pytest.mark.parametrize("change", [
    {"university_id":2}, {"scrape_job_id":"other"}, {"status_code":403},
    {"snapshot_type":"ai_prompt"}, {"storage_path":"private/other"},
    {"url_hash":"wrong"}, {"content_length":4_000_000}, {"fetch_method":"wayback"},
    {"fetched_at":datetime.now(timezone.utc)-timedelta(days=2)},
    {"fetched_at":datetime.now(timezone.utc)+timedelta(hours=1)},
])
def test_foreign_stale_failed_and_unowned_copies_are_rejected(change):
    assert not saved.snapshot_valid(SimpleNamespace(**metadata(**change)), "source", 1, URL)


@pytest.mark.asyncio
async def test_capture_identity_hash_and_date_are_preserved_without_a_website_request(monkeypatch):
    from app.services import snapshot_store
    monkeypatch.setattr(snapshot_store, "download_snapshot", AsyncMock(return_value=page().encode()))
    snapshot = SimpleNamespace(**metadata())
    row = {"id":1,"name":NAME,"url":URL}
    fact = await saved.captured_fact(snapshot, row)
    assert fact["months"] == ["January", "September"]
    assert fact["capturedAt"] == snapshot.fetched_at.isoformat()
    assert fact["sha256"] == hashlib.sha256(page().encode()).hexdigest()
    assert await saved.captured_fact(snapshot, {**row,"name":"Another course"}) is None


@pytest.mark.asyncio
async def test_saved_review_exposes_partial_coverage_without_mutating_courses(monkeypatch):
    plans = [
        {"id":1,"name":NAME,"url":URL,"status":"supported","reason":"",
         "changes":[{"field":"intake_months","oldValue":[],"newValue":["January","September"]}],
         "source":{"url":URL,"quote":"literal JSON","capturedAt":"2026-11-23T01:00:00+00:00"},
         "_snapshot":{"snapshotId":991}},
        {"id":2,"name":"Unmatched course","url":URL+"other","status":"unverified",
         "changes":[],"reason":"Course variant could not be matched"},
    ]
    monkeypatch.setattr(saved, "saved_plans", AsyncMock(return_value=plans))
    api = SimpleNamespace(now=service.now,action_proposal=service.action_proposal,
                          save_research_reply=AsyncMock())
    ctx={"job":{"id":"source"},"university_id":1,"versions":{"1":"v","2":"w"},"config_version":"c"}
    rows=[{"id":p["id"],"name":p["name"],"url":p["url"]} for p in plans]
    assert await saved.prepare_saved_review(api,"thread","turn","run",ctx,rows)
    reply=api.save_research_reply.call_args.args[3]
    assert "1 changes proposed" in reply["content"] and "1 unverified" in reply["content"]
    assert "Unmatched course" in reply["content"] and "not fresh website checks" in reply["content"]
    action=reply["actions"][0]
    assert action["_ids"]==[1] and action["requiresConfirmation"]
    assert action["fields"]==["intake_months"]
    assert [p["id"] for p in action["repairPreview"]]==[1]
    from app.services.assistant_pending_automation import prepare
    from app.services.scrape_assistant_autorun import choose_action
    prepare(reply,{"autoRun":True,"content":"fix all course intakes"},
            SimpleNamespace(scrape_assistant_auto_pending_user_ids="1"),1)
    assert choose_action(reply["actions"]) is action
    assert action["automaticPending"] and not action["requiresConfirmation"]


@pytest.mark.asyncio
async def test_one_reader_error_remains_visible_without_aborting_other_saved_sources(store,monkeypatch):
    other=URL+"other"
    async with store() as db:
        db.add(PageSnapshot(**metadata()))
        db.add(PageSnapshot(**metadata(
            id=992,course_url=other,url_hash=hashlib.sha256(other.encode()).hexdigest()[:16],
            storage_path=build_s3_key(1,"source",other,"html"))))
        await db.commit()
    async def read(snapshot,row):
        if row["id"]==2:
            raise TypeError("simulated parser defect")
        return {"months":["January"],"quote":"January","url":URL,
                "capturedAt":snapshot.fetched_at.isoformat(),"snapshotId":991,
                "sha256":"hash","method":"intake:selected_start_option","selectedLocation":None}
    monkeypatch.setattr(saved,"captured_fact",read)
    plans=await saved.saved_plans(SimpleNamespace(AsyncSessionLocal=store),"source",1,[
        {"id":1,"name":NAME,"url":URL,"intakes":[]},
        {"id":2,"name":"Other","url":other,"intakes":[]}])
    assert plans[0]["status"]=="supported"
    assert plans[1]["status"]=="unverified" and "internal error" in plans[1]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "body", "second_body", "version", "expired"])
async def test_real_confirmation_is_audited_fenced_and_never_fetches_the_live_site(store,monkeypatch,tamper):
    from app.services import snapshot_store
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    from app.routers import scrape
    inspect=AsyncMock(side_effect=AssertionError("No live university access allowed"))
    queue=AsyncMock(side_effect=AssertionError("No discovery or generic re-extraction allowed"))
    monkeypatch.setattr(service,"inspect_source",inspect)
    monkeypatch.setattr(scrape,"start_bulk_fix_job",queue)
    download=AsyncMock(return_value=page().encode())
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.course_name,row.course_website=NAME,URL
        row.intake_months=[]
        row.international_fee=12345
        row.study_mode="On Campus"
        row.course_location="Luton"
        snapshot=PageSnapshot(**metadata())
        db.add(snapshot)
        await db.flush()
        await db.refresh(row)
        fact=await saved.captured_fact(snapshot,{"id":1,"name":NAME,"url":URL,"location":"Luton"})
        target={"id":1,"name":NAME,"url":URL,"status":"supported","reason":"",
                "changes":[{"field":"intake_months","oldValue":[],"newValue":fact["months"]}],
                "source":{"url":URL,"quote":fact["quote"],"capturedAt":fact["capturedAt"],
                          "origin":"saved_official_html","selectedLocation":fact["selectedLocation"]},
                "_snapshot":fact}
        action=service.action_proposal({"kind":"fix_fields","fields":["intake_months"]},[1])
        action.update(_versions={"1":service.row_version(row)},_configVersion=service.fingerprint({}),
                      _savedIntakeTargets=[target],_savedIntakeJobId="source",repairPreview=[target])
        if tamper=="second_body":
            second_url=URL+"another-offering"
            second_key=build_s3_key(1,"source",second_url,"html")
            second=ScrapedCourse(id=2,university_id=1,scrape_job_id="source",
                                 course_name=NAME,course_website=second_url,status="pending",
                                 intake_months=[],course_location="Luton")
            db.add(second)
            db.add(PageSnapshot(**metadata(
                id=992,course_url=second_url,
                url_hash=hashlib.sha256(second_url.encode()).hexdigest()[:16],
                storage_path=second_key,fetched_at=snapshot.fetched_at)))
            await db.flush()
            await db.refresh(second)
            target2=deepcopy(target)
            target2["id"]=2
            target2["url"]=second_url
            target2["source"]["url"]=second_url
            target2["_snapshot"].update(url=second_url,snapshotId=992)
            action.update(_ids=[1,2],courseCount=2,_savedIntakeTargets=[target,target2])
            action["_versions"]["2"]=service.row_version(second)
            async def changed_second_source(key):
                return page().replace("January","October").encode() if key==second_key else page().encode()
            download.side_effect=changed_second_source
        db.add(ScrapeAssistantThread(id="saved",actor_id=7,job_id="source",turn_id="turn",
                                    status="ready",messages=[{"role":"assistant","content":"Compare",
                                                              "actions":[deepcopy(action)]}]))
        if tamper=="body":
            download.return_value=page().replace("January","October").encode()
        elif tamper=="version":
            row.intake_months=["March"]
        elif tamper=="expired":
            snapshot.fetched_at=datetime.now(timezone.utc)-timedelta(days=2)
        await db.commit()
        if tamper=="version":
            from fastapi import HTTPException
            with pytest.raises(HTTPException):
                await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
        else:
            result=await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
            state=result["messages"][-1]["actions"][0]["state"]
            assert state==("completed" if tamper is None else "error")
            if tamper is None:
                saved_action=result["messages"][-1]["actions"][0]
                assert "January, September" in saved_action["courseResults"][0]["message"]
                assert "not a fresh website check" in saved_action["courseResults"][0]["message"]
                await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
    async with store() as db:
        record=await db.get(ScrapedCourse,1)
        assert record.international_fee==12345 and record.study_mode=="On Campus"
        assert record.status=="pending"
        assert record.intake_months==(["January","September"] if tamper is None else
                                     ["March"] if tamper=="version" else [])
        audits=(await db.execute(select(CourseAuditLog))).scalars().all()
        assert len(audits)==(1 if tamper is None else 0)
        selected=(await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.field_key=="intake_months",
            ScrapedFieldEvidence.selected.is_(True)))).scalars().all()
        assert len(selected)==(1 if tamper is None else 0)
        if tamper=="second_body":
            assert (await db.get(ScrapedCourse,2)).intake_months==[]
    inspect.assert_not_awaited()
    queue.assert_not_awaited()

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import scrape_assistant as service
from app.services import snapshot_store
from tests.test_assistant_retained_source import seed_snapshot, RAW, FAILURE, URL, PROMPT
from tests.test_scrape_prompt_assistant import store, assistant_postgres


async def prepare(store,monkeypatch):
    await seed_snapshot(store)
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        thread.status,thread.run_id="queued",None
        thread.messages=[{"role":"user","content":PROMPT,"_engine":"legacy","autoRun":True,
                          "createdAt":service.now().isoformat()}]
        await db.commit()
    ctx=await service.context("source")
    ctx["home"]="https://www.bradford.ac.uk"
    monkeypatch.setattr(service,"context",AsyncMock(return_value=ctx))
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    download=AsyncMock(return_value=RAW)
    inspect=AsyncMock(return_value=FAILURE)
    model=AsyncMock(return_value={"answer":"Research complete.","citations":[],"action":None})
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    monkeypatch.setattr(service,"inspect_source",inspect)
    monkeypatch.setattr(service,"chat_json",model)
    await service.investigate("eligibility","turn")
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        action=thread.messages[-1]["actions"][0]
        assert action["requiresConfirmation"] and action["state"]=="proposed"
        assert (await db.get(ScrapedCourse,1)).ielts_listening is None
    return action["id"], download, inspect, model


@pytest.mark.asyncio
@pytest.mark.parametrize("source_changed",[False,True])
async def test_approval_applies_retained_evidence_atomically_without_broken_refetch(store,monkeypatch,source_changed):
    from app.routers import scrape
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    action_id, download, inspect, model=await prepare(store,monkeypatch)
    if source_changed:
        download.return_value=RAW.replace(b"6.5",b"7.5").replace(b"6.0",b"7.0")
    queue=AsyncMock(side_effect=AssertionError("Do not re-fetch a page when the authorised retained proposal is applicable"))
    monkeypatch.setattr(scrape,"start_bulk_fix_job",queue)
    async with store() as db:
        # A second pending course must remain unaffected.
        db.add(ScrapedCourse(id=9001,university_id=1,scrape_job_id="source",
                            course_name="Another course",course_website=URL+"-other",
                            status="pending",ielts_overall=7.0))
        await db.commit()
        result=await confirm_action("source",action_id,ConfirmBody(confirm=True),db,{"id":77})
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        action=thread.messages[-1]["actions"][0]
        row=await db.get(ScrapedCourse,1)
        values=[getattr(row,k) for k in ("ielts_overall","ielts_listening","ielts_reading","ielts_writing","ielts_speaking")]
        assert row.status=="pending" and row.scrape_job_id=="source"
        other=await db.get(ScrapedCourse,9001)
        assert other.ielts_overall==7.0 and other.ielts_listening is None
        audits=(await db.execute(select(CourseAuditLog).where(CourseAuditLog.scraped_course_id==1))).scalars().all()
        if source_changed:
            assert action["state"]=="error"
            assert "no longer matches" in action["error"]
            assert values==[6.5,None,None,None,None] and not audits
        else:
            assert action["state"]=="completed" and not action.get("jobId")
            assert action["valuesChanged"]==4 and action["evidenceFieldsRefreshed"]==5
            assert values==[6.5,6.0,6.0,6.0,6.0]
            assert len(audits)==5
            proofs=(await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id==1,ScrapedFieldEvidence.selected.is_(True)
            ))).scalars().all()
            assert len(proofs)==5 and {p.page_type for p in proofs}=={"html"}
            assert all("no sub-test less than 6.0" in p.snippet for p in proofs)
            assert all('"sourceSnapshotId": 9000' in a.reason for a in audits)
    queue.assert_not_awaited()
    inspect.assert_awaited_once()
    model.assert_awaited_once()

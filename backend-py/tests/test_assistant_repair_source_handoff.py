from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_source_gate import (
    repair_probe_urls, require_reachable_sources, same_course_url,
)
from tests.test_cached_ielts_recovery import URL, TEXT
from tests.test_scrape_prompt_assistant import store, assistant_postgres, propose


@pytest.mark.parametrize("left,right,matches", [
    (URL,URL+"/",True),
    (URL+"#english",URL+"/#fees",True),
    (URL+"?option=standard",URL+"/?option=standard",True),
    (URL+"?option=standard",URL+"/?option=placement",False),
    (URL+"?value=x/",URL+"?value=x",False),
    (URL,URL.replace("www.bradford.ac.uk","other.edu"),False),
    (URL,URL+"/nursing",False),
    (URL.replace("https://","http://"),URL,False),
])
def test_only_the_same_course_path_variant_can_reuse_plan_evidence(left,right,matches):
    assert same_course_url(left,right) is matches


def test_supported_preview_beats_unchecked_stored_url_but_cannot_borrow_another_row():
    rows=[SimpleNamespace(id=59504,course_website=URL+"/")]
    action={"_evidenceUrls":[URL+"/"],"repairPreview":[
        {"id":59504,"status":"supported","source":{"url":URL}}]}
    assert repair_probe_urls(rows,action)==[URL]
    action["repairPreview"][0]["id"]=59505
    assert repair_probe_urls(rows,action)==[URL+"/"]
    assert repair_probe_urls(rows,{"_evidenceUrls":[URL+"/nursing"]})==[URL+"/"]


@pytest.mark.asyncio
async def test_failed_gate_names_the_checked_url_and_reason_without_queuing():
    async def denied(url,home):
        return {"verified":False,"error":"robots.txt verification failed (HTTP 403)"}
    with pytest.raises(ValueError) as exc:
        await require_reachable_sources([URL],URL,denied)
    assert URL in str(exc.value) and "HTTP 403" in str(exc.value)
    assert "not queued" in str(exc.value)


@pytest.mark.asyncio
async def test_real_confirmation_reuses_saved_source_and_reaches_queue_without_refetch(store,monkeypatch):
    """Replay the actual failure: row URL ends '/', approved evidence does not."""
    from app.models import ScrapedCourse
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.services import scrape_assistant as service
    from app.routers import scrape
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.course_website=URL+"/"
        row.ielts_overall=6.5
        row.ielts_reading=row.ielts_listening=row.ielts_writing=row.ielts_speaking=None
        before=service.row_version(row)
        action=await propose(db)
        action["fields"]=["ielts_overall"]
        action["_evidenceUrls"]=[URL]
        action["requiresConfirmation"]=True
        thread=await db.get(ScrapeAssistantThread,"ready")
        messages=deepcopy(thread.messages)
        messages[0]["actions"]=[action]
        thread.messages=[{"role":"user","content":"Fix IELTS on the checked course",
                          "createdAt":service.now().isoformat(),
                          "_sourceChecks":{URL:{"verified":True,"url":URL,
                              "text":TEXT,"facts":[],"_sourceScopeVersion":2}}}]+messages
        await db.commit()
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    inspect=AsyncMock(side_effect=AssertionError("Must reuse the checked URL, not refetch its slash variant"))
    monkeypatch.setattr(service,"inspect_source",inspect)
    queued=[]
    async def queue(body,db,actor):
        queued.append(body)
        assert body.ids==[1] and body.target_fields==["ielts_overall"]
        db.add(ScrapeRuntimeJob(runtime_job_id="handoff-fix",university_id=1,
                               job_type="bulk_fix",status="queued",total_found=1,current=0))
        await db.commit()
        return {"jobId":"handoff-fix"}
    monkeypatch.setattr(scrape,"start_bulk_fix_job",queue)
    async with store() as db:
        await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
        row=await db.get(ScrapedCourse,1)
        assert service.row_version(row)==before
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"ready")
        saved=next(a for m in thread.messages for a in m.get("actions",[]) if a["id"]==action["id"])
        assert saved["jobId"]=="handoff-fix" and saved["state"]=="running"
        await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
    assert len(queued)==1
    inspect.assert_not_awaited()

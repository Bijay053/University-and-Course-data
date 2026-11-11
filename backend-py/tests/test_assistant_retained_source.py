from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import ScrapedCourse
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import assistant_retained_source as retained
from app.services import scrape_assistant as service
from app.services import snapshot_store
from app.services.assistant_repair_proposals import _course_ielts
from tests.test_assistant_eligibility import seed_thread
from tests.test_assistant_incomplete_source import PROMPT, URL
from tests.test_scrape_prompt_assistant import store, assistant_postgres

NAME = "Foundation in Clinical Sciences and Medicine Leading to BSc Clinical Sciences"
BODY = NAME + (
    " - Undergraduate - University of Bradford Skip to content Entry requirements "
    "English language requirements 6.5 overall with no sub-test less than 6.0. "
    "If you do not meet the IELTS requirement, see the Language Centre."
)
RAW = (
    "You are a strict data extractor. Field hints: IELTS 9.0, all bands 9.0.\n"
    f"Course URL: {URL}/\n\nCourse page text (truncated):\n\"\"\"\n{BODY}\n\"\"\"\n"
).encode()
FAILURE = {
    "url":URL, "verified":False, "sourceType":"html", "httpStatus":200,
    "failureStage":"source_body", "restrictionKind":"incomplete_response",
    "error":"Official source returned HTTP 200, but its response body could not be read completely.",
}


async def seed_snapshot(store):
    await seed_thread(store)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        row.course_name, row.course_website = NAME, URL + "/"
        row.ielts_overall = 6.5
        row.ielts_listening = row.ielts_reading = row.ielts_writing = row.ielts_speaking = None
        db.add(PageSnapshot(
            id=9000, university_id=1, scrape_job_id="source", course_url=URL+"/",
            url_hash=snapshot_store.url_hash(URL+"/"), snapshot_type="ai_prompt",
            storage_path=snapshot_store.build_s3_key(1,"source",URL+"/","ai_prompt"),
            content_length=len(RAW), status_code=200, fetch_method="gemini",
            original_extraction={"call_type":"fallback","chunk":1},
            fetched_at=datetime.now(timezone.utc),
        ))
        await db.commit()


def test_only_page_data_not_template_hints_can_be_recovered():
    assert retained.recorded_page_text(RAW,URL+"/",len(RAW)) == BODY
    assert retained.recorded_page_text(RAW,URL+"?option=other",len(RAW)) is None
    assert retained.recorded_page_text(RAW,URL+"/",len(RAW)+1) is None
    assert retained.recorded_page_text(RAW[:-1],URL+"/",len(RAW)-1) is None
    assert retained.recorded_page_text(b"model answer IELTS 9",URL,20) is None


def test_retained_capture_age_is_not_reset_by_cache_reuse():
    now = datetime.now(timezone.utc)
    assert retained.recent_capture((now-timedelta(hours=1)).isoformat(), now)
    assert not retained.recent_capture((now-timedelta(days=2)).isoformat(), now)
    assert not retained.recent_capture((now+timedelta(hours=1)).isoformat(), now)
    assert not retained.recent_capture("invalid", now)
    assert not retained.recent_capture(None, now)


@pytest.mark.asyncio
async def test_same_review_retained_input_yields_five_scores_without_model_or_new_origin_request(store,monkeypatch):
    await seed_snapshot(store)
    download = AsyncMock(return_value=RAW)
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    result = await retained.retained_course_source(service,"eligibility","turn","owner",URL,FAILURE)
    assert result["verified"]
    assert result["sourceOrigin"] == "retained_scrape_page_text"
    assert result["_retainedSnapshotId"] == 9000
    assert {scores for scores,quote in _course_ielts(result)} == {(6.5,6.0,6.0,6.0,6.0)}
    assert "9.0" not in result["_fullText"]
    assert "saved course text" in result["title"]
    download.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"accessIssue":"robots_unavailable"}, {"accessIssue":"source_access_denied"},
    {"sourceType":"pdf"}, {"httpStatus":403}, {"httpStatus":429},
    {"failureStage":"robots"}, {"restrictionKind":"challenge"},
])
async def test_restrictions_and_other_failures_never_read_retained_objects(change):
    forbidden=AsyncMock(side_effect=AssertionError("Restrictions must not trigger alternate access"))
    assert await retained.retained_course_source(
        SimpleNamespace(AsyncSessionLocal=forbidden),"t","turn","owner",URL,{**FAILURE,**change}
    ) is None
    forbidden.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[
    {"fetched_at":datetime.now(timezone.utc)-timedelta(days=2)},
    {"storage_path":"universities/2/other/ai_prompt/unowned/chunk_1.txt.gz"},
    {"url_hash":"wrong-hash"}, {"content_length":len(RAW)+1},
    {"course_url":URL+"?option=other"}, {"snapshot_type":"staged_row"},
    {"status_code":403}, {"fetch_method":"model_response"},
])
async def test_stale_unowned_mismatched_and_non_source_snapshots_are_not_evidence(store,monkeypatch,change):
    await seed_snapshot(store)
    async with store() as db:
        snapshot=await db.get(PageSnapshot,9000)
        for key,value in change.items():
            setattr(snapshot,key,value)
        await db.commit()
    monkeypatch.setattr(snapshot_store,"download_snapshot",AsyncMock(return_value=RAW))
    assert await retained.retained_course_source(service,"eligibility","turn","owner",URL,FAILURE) is None


@pytest.mark.asyncio
async def test_replaced_turn_cannot_recover_evidence(store,monkeypatch):
    await seed_snapshot(store)
    download=AsyncMock(side_effect=AssertionError("Stale owner"))
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    assert await retained.retained_course_source(service,"eligibility","old-turn","owner",URL,FAILURE) is None
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_prompt_prepares_only_named_course_with_five_value_preview(store,monkeypatch):
    await seed_snapshot(store)
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        thread.status,thread.run_id="queued",None
        thread.messages=[{"role":"user","content":PROMPT,"_engine":"legacy","autoRun":True}]
        await db.commit()
    ctx=await service.context("source")
    ctx["home"]="https://www.bradford.ac.uk"
    ctx["samples"] += [{**ctx["samples"][0],"id":i,"name":f"Other course {i}",
                       "url":ctx["home"]+f"/other-{i}"} for i in range(2,140)]
    ctx["review_counts"]["pending"]=139
    monkeypatch.setattr(service,"context",AsyncMock(return_value=ctx))
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    monkeypatch.setattr(snapshot_store,"download_snapshot",AsyncMock(return_value=RAW))
    inspect=AsyncMock(return_value=FAILURE)
    monkeypatch.setattr(service,"inspect_source",inspect)
    monkeypatch.setattr(service,"chat_json",AsyncMock(return_value={
        "answer":"Bands unsupported. Repair all 139 courses.","citations":[],"action":None,
    }))
    await service.investigate("eligibility","turn")
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        assert thread.status=="ready",thread.error
        reply=thread.messages[-1]
        action=reply["actions"][0]
        assert action["_ids"]==[1] and action["courseCount"]==1
        assert action["requiresConfirmation"] is True
        assert [c["newValue"] for c in action["repairPreview"][0]["changes"]]==[6.5,6.0,6.0,6.0,6.0]
        assert "138 are not included" in reply["content"]
        assert "unsupported" not in reply["content"]
        assert any(a.get("label")=="Retained official course text checked" for a in thread.activity)
        row=await db.get(ScrapedCourse,1)
        assert row.ielts_listening is None and row.status=="pending"
    inspect.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_turn_reuses_recovered_source_without_refetch(store,monkeypatch):
    from app.services.assistant_source_cache import cached_source
    await seed_snapshot(store)
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    download=AsyncMock(return_value=RAW)
    inspect=AsyncMock(return_value=FAILURE)
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    first=await cached_source(service,"eligibility","turn","owner",URL,
                              "https://www.bradford.ac.uk",inspect,12)
    second=await cached_source(service,"eligibility","turn","owner",URL,
                               "https://www.bradford.ac.uk",inspect,12)
    assert first["verified"] and second["verified"] and second["_sourceCacheHit"]
    inspect.assert_awaited_once()
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_fresh_read_never_substitutes_retained_text(store,monkeypatch):
    from app.services.assistant_source_cache import cached_source
    await seed_snapshot(store)
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    download=AsyncMock(side_effect=AssertionError("Fresh means no retained source substitution"))
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    result=await cached_source(service,"eligibility","turn","owner",URL,
                               "https://www.bradford.ac.uk",AsyncMock(return_value=FAILURE),12,fresh=True)
    assert not result["verified"]
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_recorded_source_cannot_outlive_capture_age(store,monkeypatch):
    from copy import deepcopy
    from app.services.assistant_source_cache import cached_source
    await seed_snapshot(store)
    monkeypatch.setattr(service,"SOURCE_ACCESS_REGISTRY",False)
    download=AsyncMock(return_value=RAW)
    inspect=AsyncMock(return_value=FAILURE)
    monkeypatch.setattr(snapshot_store,"download_snapshot",download)
    result=await cached_source(service,"eligibility","turn","owner",URL,
                               "https://www.bradford.ac.uk",inspect,12)
    assert result["verified"]
    old=datetime.now(timezone.utc)-timedelta(days=2)
    async with store() as db:
        snapshot=await db.get(PageSnapshot,9000)
        snapshot.fetched_at=old
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        messages=deepcopy(thread.messages)
        messages[-1]["_sourceChecks"][URL]["sourceCapturedAt"]=old.isoformat()
        thread.messages=messages
        await db.commit()
    result=await cached_source(service,"eligibility","turn","owner",URL,
                               "https://www.bradford.ac.uk",inspect,12)
    assert not result["verified"]
    assert inspect.await_count==2
    download.assert_awaited_once()

"""Historical repair rollbacks must not expire the locked chat conversation."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import make_transient_to_detached

from app.models.scrape_assistant import ScrapeAssistantThread


@pytest.mark.asyncio
async def test_historical_repair_rollback_does_not_expire_chat_or_duplicate_dispatch(monkeypatch):
    from app import database
    from app.routers import scrape_assistant as router
    from app.services import ai_repair_workflow, review_counts
    factory = async_sessionmaker(class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    snapshot = ScrapeAssistantThread(
        id="thread", job_id="failed-job", actor_id=1, turn_id="current-turn",
        status="ready", phase="", error=None, run_id=None,
        updated_at=datetime.now(timezone.utc), activity=[],
        messages=[
            {"role":"assistant","content":"Old repair","turnId":"old-turn","createdAt":"old",
             "actions":[{"id":"old","kind":"repair_catalogue","state":"error",
                         "_sessionId":"old-session","courseCount":50}]},
            {"role":"user","content":"fix the scraper","turnId":"current-turn","createdAt":"now"},
            {"role":"assistant","content":"Repair started","turnId":"current-turn","createdAt":"now",
             "actions":[{"id":"new","kind":"repair_catalogue","state":"running",
                         "_sessionId":"new-session","courseCount":50}]},
        ],
    )
    make_transient_to_detached(snapshot)
    reconciled=[]
    async with factory() as caller:
        # Attach a real persistent ORM instance without connecting to a database.
        # Rolling back THIS session expires it, reproducing the production bug.
        thread=await caller.merge(snapshot,load=False)
        async def reconcile(job_id,session_id,db):
            assert db is not caller, "Repair lifecycle rollback must not release the chat transaction"
            await db.rollback()
            reconciled.append((job_id,session_id))
            return {"status":"failed" if session_id=="old-session" else "running",
                    "current_attempt":1,"autonomous":{"phase":"blocked" if session_id=="old-session" else "verifying"}}
        monkeypatch.setattr(ai_repair_workflow,"reconcile",reconcile)
        monkeypatch.setattr(caller,"commit",AsyncMock())
        async def get(model,key,**kwargs):
            return SimpleNamespace(university_id=100) if key=="failed-job" else None
        monkeypatch.setattr(caller,"get",get)
        monkeypatch.setattr(review_counts,"pending_counts",AsyncMock(return_value={"pending":0}))
        result=await router.response(thread,caller,"failed-job")
        assert not inspect(thread).expired_attributes
        assert result["turnId"]=="current-turn" and result["status"]=="ready"
        assert result["messages"][-1]["actions"][0]["state"]=="running"
        assert result["messages"][0]["actions"][0]["state"]=="error"
        assert reconciled==[("failed-job","old-session"),("failed-job","new-session")]
        assert "work is running" in result["phase"]
        caller.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,classification",[(403,"challenge"),(503,"network_failure")])
async def test_source_http_status_is_reported_without_request_headers_or_unsafe_fallback(monkeypatch,status,classification):
    import httpx
    from app.services.scraper import ai_repair_live as live
    from app.services import worker_fencing
    from app.services.scraper.config.schema import UniConfig
    base="https://www.example.edu"
    url=base+"/robots.txt"
    request=httpx.Request("GET",url)
    async def fetch(*args,**kwargs):
        raise httpx.HTTPStatusError("Do not expose arbitrary provider details",request=request,
                                  response=httpx.Response(status,request=request))
    monkeypatch.setattr(live,"_fetch_official",fetch)
    monkeypatch.setattr(worker_fencing,"reserve_live_fetch",AsyncMock(side_effect=lambda p,s,t:t))
    monkeypatch.setattr(worker_fencing,"refund_live_fetch",AsyncMock())
    cfg=UniConfig(slug="source-status",name="Example",base_url=base,scrape_url=base)
    evidence=live.LiveRepairEvidence({"scrape_url":base,"effective_config":cfg})
    result=await evidence.fetch(url)
    assert result["classification"]==classification
    assert f"HTTP {status}" in result["reason"]
    assert "provider details" not in result["reason"]
    assert evidence.pages_checked==1 and not result.get("_source_html")

import pytest
from app.services import scrape_assistant as service
from app.routers.scrape_assistant import confirm_action,ConfirmBody
from tests.test_scrape_prompt_assistant import store,assistant_postgres,propose

@pytest.mark.asyncio
async def test_no_bulk_job_dispatched_when_site_unreachable(store,monkeypatch):
    from app.routers import scrape
    async def blocked(url,home):return {"verified":False,"error":"robots.txt blocked"}
    async def forbidden_dispatch(*args,**kwargs):raise AssertionError("Must not dispatch a bulk fix")
    monkeypatch.setattr(service,"inspect_source",blocked)
    monkeypatch.setattr(scrape,"start_bulk_fix_job",forbidden_dispatch)
    async with store() as db:
        action=await propose(db)
        result=await confirm_action("source",action["id"],ConfirmBody(confirm=True),db,{"id":7})
        saved=result["messages"][-1]["actions"][0]
        assert saved["state"]=="error"
        assert "not queued" in saved["error"]

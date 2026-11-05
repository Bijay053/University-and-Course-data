import pytest
from app.services.scrape_agent_v2.tools import DataTools
from app.services.scrape_agent_v2 import web
from app.services import scrape_assistant as service
from tests.test_assistant_eligibility import seed_thread, snapshot
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest.mark.asyncio
async def test_status_question_cannot_fetch_even_if_model_asks(store,monkeypatch):
    await seed_thread(store)
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),"Is row 56064 removed?")
    tools.ctx["home"]="https://excelsia.edu.au/"
    async def forbidden(*args):raise AssertionError("Web must not be called")
    monkeypatch.setattr(web,"web_tool",forbidden)
    result=await tools.execute("fetch_page",{"url":tools.ctx["home"]},"web")
    assert not result["ok"] and "database-only" in result["reason"]


@pytest.mark.asyncio
async def test_robots_denial_is_cached_without_page_fetch(store,monkeypatch):
    await seed_thread(store)
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),"Check official pages")
    tools.ctx["home"]="https://excelsia.edu.au/"
    reads=[]
    async def request(url,**kwargs):
        reads.append(url)
        assert url.endswith("/robots.txt")
        assert kwargs["user_agent"].startswith(web.AGENT)
        return b"User-agent: *\nDisallow: /\n"
    async def throttle(*args):pass
    monkeypatch.setattr(web,"public_request",request)
    monkeypatch.setattr(web,"throttle",throttle)
    first=await tools.execute("fetch_page",{"url":tools.ctx["home"]},"web-1")
    second=await tools.execute("fetch_page",{"url":tools.ctx["home"]},"web-2")
    assert not first["ok"] and "robots.txt" in first["reason"]
    assert second["cached"] and len(reads)==1

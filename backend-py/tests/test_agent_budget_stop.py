from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import scrape_assistant as service
from app.services.scrape_agent_v2 import runtime,model
from app.services.scrape_agent_v2.spend import SpendLimit
from app.models.scrape_assistant import ScrapeAssistantThread
from tests.test_assistant_eligibility import seed_thread,snapshot
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_dollar_stop_reports_and_does_not_retry(store,monkeypatch):
    await seed_thread(store)
    monkeypatch.setattr(service,"context",AsyncMock(return_value=await snapshot(store)))
    client=SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(runtime,"_client",lambda:client)
    call=AsyncMock(side_effect=SpendLimit("Agent stopped: daily USD budget reached. Daily cap $100.00."))
    monkeypatch.setattr(model,"complete",call)
    await runtime.investigate(service,"eligibility","turn","owner")
    assert call.await_count==1
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        assert "$100.00" in thread.messages[-1]["content"]
        assert "spending safeguard" in thread.phase
    client.close.assert_awaited_once()

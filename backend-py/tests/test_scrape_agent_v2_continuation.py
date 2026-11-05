import pytest
from app.services import scrape_assistant as service
from app.services.scrape_agent_v2 import continuation
from app.models.scrape_assistant import ScrapeAssistantThread
from tests.test_assistant_eligibility import seed_thread
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_continuation_queues_only_current_owner(store,monkeypatch):
    await seed_thread(store)
    monkeypatch.setattr(continuation,"AsyncSessionLocal",store)
    from app.tasks.scrape_assistant import research_scrape
    published=[]
    monkeypatch.setattr(research_scrape,"apply_async",lambda **kw:published.append(kw))
    await continuation.enqueue("eligibility","turn","stale-owner")
    assert not published
    await continuation.enqueue("eligibility","turn","owner")
    assert len(published)==1
    async with store() as db:
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        assert thread.status=="queued" and thread.run_id is None
        assert thread.messages[-1]["_agentContinuations"]==1

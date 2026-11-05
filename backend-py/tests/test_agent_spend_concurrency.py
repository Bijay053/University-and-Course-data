import asyncio
from types import SimpleNamespace
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from app.models.agent_spend import AgentSpend
from app.services.scrape_agent_v2.spend import reserve,settle,SpendLimit
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overspend(store):
    async with store() as db:
        await db.run_sync(lambda session:AgentSpend.__table__.create(session.connection(),checkfirst=True))
        await db.commit()
    # Synthetic prices test arithmetic and database locking, not provider tariffs.
    settings=SimpleNamespace(scrape_agent_v2_daily_usd=.03,scrape_agent_v2_priced_model="synthetic",
        scrape_agent_v2_input_usd_per_million=1,scrape_agent_v2_output_usd_per_million=1)
    results=await asyncio.gather(*(reserve(store,settings,"test","synthetic",{}) for _ in range(10)),return_exceptions=True)
    reservations=[r for r in results if not isinstance(r,Exception)]
    assert len(reservations)==2
    assert all(isinstance(r,SpendLimit) for r in results if isinstance(r,Exception))
    async with store() as db:
        amount=(await db.execute(select(func.sum(AgentSpend.charged_usd)))).scalar_one()
        assert amount<=Decimal(".03")
    receipt,prices=reservations[0]
    await settle(store,receipt,prices,None)
    async with store() as db:
        row=await db.get(AgentSpend,receipt)
        assert row.charged_usd==row.reserved_usd and row.status=="reserved"
    await settle(store,receipt,prices,SimpleNamespace(input_tokens=100,output_tokens=10))
    async with store() as db:
        row=await db.get(AgentSpend,receipt)
        assert row.charged_usd==Decimal(".000135") and row.status=="settled"

import pytest
from sqlalchemy import select
from app.models import University
from app.models.evidence import ScrapedFieldEvidence
from app.services import scrape_assistant as service
from app.services.scrape_agent_v2.tools import DataTools
from app.services.scrape_agent_v2 import batch
from tests.test_assistant_eligibility import seed_thread,snapshot
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_batch_keeps_completed_rows_and_creates_owned_intake_evidence(store,monkeypatch):
    await seed_thread(store)
    ctx=await snapshot(store)
    tools=DataTools(service,"eligibility","turn","owner",ctx,"check all courses and fill missing intakes")
    reads=[]
    async def source(*args):
        reads.append(1)
        return {"verified":True,"_excelsiaIntake":{"name":ctx["samples"][0]["name"],
            "months":["February","July"],"quote":"Intake: February and July"}}
    monkeypatch.setattr(batch,"web_tool",source)
    first=await tools.execute("start_batch_check",{"row_ids":[1]},"batch-1")
    second=await tools.execute("start_batch_check",{"row_ids":[1]},"batch-1")
    assert first["checked"]==second["checked"]==1 and len(reads)==1
    async with store() as db:
        evidence=(await db.execute(select(ScrapedFieldEvidence))).scalars().all()
        assert len(evidence)==1 and evidence[0].field_key=="intake_months"


@pytest.mark.asyncio
async def test_saved_policy_requires_immediate_batch_application(store):
    await seed_thread(store)
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),"we don't need domestic-only courses")
    result=await tools.execute("save_rule",{},"rule-1")
    assert result["requires_tools"]==["start_batch_check"]
    async with store() as db:
        uni=await db.get(University,1)
        assert uni.scrape_config["assistant_rules"]["exclude_confirmed_domestic_only"]

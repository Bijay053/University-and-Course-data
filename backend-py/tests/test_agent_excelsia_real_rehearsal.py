"""Opt-in real-model/live-site rehearsal in disposable PostgreSQL, never AWS writes."""
import os,json
from pathlib import Path
import pytest
from sqlalchemy import select
from app.models import ScrapedCourse,University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.models.evidence import ScrapedFieldEvidence
from app.models.agent_spend import AgentSpend
from app.config import settings
from app.services import scrape_assistant as service
from app.services.ai.openai_client import _client
from app.services.scrape_agent_v2.model import complete
from app.services.scrape_agent_v2.loop import run_loop
from app.services.scrape_agent_v2.tools import DataTools
from app.services.scrape_agent_v2.runtime import SYSTEM
from tests.test_assistant_eligibility import seed_thread,snapshot
from tests.test_scrape_prompt_assistant import store,assistant_postgres

TARGETS=[
    (56052,"Bachelor of Business (Accounting)","business/bachelor-of-business-accounting"),
    (56026,"Excelsia.edu.au","business/graduate-diploma-of-business-administration"),
    (56053,"Master of Professional Accounting","business/master-of-professional-accounting"),
    (56063,"Graduate Certificate in Disability","counselling/graduate-certificate-in-disability"),
    (56073,"Master of Educational Leadership","education/master-of-educational-leadership"),
    (56042,"Excelsia.edu.au","social-work/graduate-certificate-in-aged-care"),
]


@pytest.mark.skipif(os.getenv("RUN_REAL_AGENT")!="1",reason="Explicit real-model/live-site opt-in required")
@pytest.mark.asyncio
async def test_requested_intakes_with_real_agent_and_official_pages(store):
    await seed_thread(store)
    prompt="Fill empty intakes only on rows 56052, 56026, 56053, 56063, 56073 and 56042 using verified official course evidence. Do not edit any other rows or fields. Do not invent months."
    ctx=await snapshot(store)
    ctx["home"]="https://excelsia.edu.au"
    ctx["job"]={"id":"source"}
    async with store() as db:
        await db.run_sync(lambda s:AgentSpend.__table__.create(s.connection(),checkfirst=True))
        university=await db.get(University,1)
        university.name="Excelsia"
        university.website=university.scrape_url=ctx["home"]
        for row_id,name,path in TARGETS:
            db.add(ScrapedCourse(id=row_id,university_id=1,scrape_job_id="source",status="pending",
                course_name=name,course_website=f"https://excelsia.edu.au/study/{path}/",intake_months=None))
        thread=await db.get(ScrapeAssistantThread,"eligibility")
        messages=list(thread.messages)
        messages[-1]={**messages[-1],"content":prompt}
        thread.messages=messages
        await db.commit()
    tools=DataTools(service,"eligibility","turn","owner",ctx,prompt)
    client=_client()
    async def model(messages,schemas):
        return await complete(client,messages,schemas,spend_context=(store,settings,"excelsia-rehearsal"))
    async def checkpoint(messages,tokens):
        trace=[{k:v for k,v in m.items() if k!="_response_items"} for m in messages if m["role"] in {"assistant","tool"}]
        (Path(__file__).resolve().parents[2]/"reports/excelsia-real-agent-trace.json").write_text(
            json.dumps({"tokens":tokens,"trace":trace},default=str,indent=2))
    try:
        result=await run_loop([{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],
                              tools,model,checkpoint,max_rounds=24,max_tokens=32000)
        async with store() as db:
            output=[]
            for row_id,name,path in TARGETS:
                row=await db.get(ScrapedCourse,row_id)
                proofs=(await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id==row_id))).scalars().all()
                output.append({"id":row_id,"name":name,"oldIntakes":None,"newIntakes":row.intake_months,
                    "evidence":[{"url":p.source_url,"field":p.field_key,"value":p.normalized_value,"quote":p.snippet} for p in proofs]})
            report={"realModel":True,"liveOfficialPages":True,"productionWrites":0,
                "database":"disposable rehearsal","rows":output,"agentResult":result}
            target=Path(__file__).resolve().parents[2]/"reports/excelsia-real-agent-rehearsal.json"
            # No private reasoning state in the user-facing report.
            for message in report["agentResult"].get("history",[]):
                message.pop("_response_items",None)
            target.write_text(json.dumps(report,default=str,indent=2))
            assert (await db.get(ScrapedCourse,1)).intake_months is None
            assert all(not row["newIntakes"] or row["evidence"] for row in output)
            assert any(row["evidence"] for row in output), "No verified source evidence was gathered; acceptance failed"
    finally:
        await client.close()


@pytest.mark.skipif(os.getenv("RUN_REAL_AGENT")!="1",reason="Explicit real-model/live-site opt-in required")
@pytest.mark.asyncio
async def test_real_model_official_cricos_tool(store):
    await seed_thread(store)
    async with store() as db:
        await db.run_sync(lambda s:AgentSpend.__table__.create(s.connection(),checkfirst=True))
        uni=await db.get(University,1)
        uni.name="Canberra Institute of Technology"
        uni.country="Australia"
        row=await db.get(ScrapedCourse,1)
        row.course_name="ACT Year 12 Certificate"
        row.cricos_code="078241E"
        await db.commit()
    prompt="Use verify_cricos to check the official registration for row 1. Do not change any data or infer current availability."
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),prompt)
    client=_client()
    async def model(messages,schemas):
        return await complete(client,messages,schemas,spend_context=(store,settings,"cricos-rehearsal"))
    async def checkpoint(*args):
        pass
    try:
        result=await run_loop([{"role":"system","content":SYSTEM},{"role":"user","content":prompt}],
                             tools,model,checkpoint,max_rounds=4)
        receipts=[json.loads(m["content"]) for m in result["history"] if m["role"]=="tool"]
        (Path(__file__).resolve().parents[2]/"reports/cricos-real-model-rehearsal.json").write_text(
            json.dumps({"realModel":True,"liveRegistry":True,"productionWrites":0,"receipts":receipts,
                        "answer":result["answer"]},default=str,indent=2))
        assert any(r.get("status")=="registration_matched" for r in receipts)
        assert all(not r.get("changed") for r in receipts)
    finally:
        await client.close()

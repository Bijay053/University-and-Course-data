import json
import pytest
from app.services.scrape_agent_v2.loop import run_loop, AgentBudgetExceeded
from app.services.scrape_agent_v2.tools import DataTools
from app.services import scrape_assistant as service
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from sqlalchemy import select
from tests.test_assistant_eligibility import seed_thread, snapshot
from tests.test_scrape_prompt_assistant import store, assistant_postgres


def call(name,args,id="call-1"):
    return {"id":id,"type":"function","function":{"name":name,"arguments":json.dumps(args)}}


@pytest.mark.asyncio
async def test_model_controls_multiple_tool_rounds():
    observed, checkpoints = [], []
    class Tools:
        schemas=[]
        async def execute(self,name,args,id):
            observed.append(name)
            return {"ok":True,"rows":[{"id":1}]}
    turns=iter([
        {"role":"assistant","tool_calls":[call("query_rows",{})]},
        {"role":"assistant","tool_calls":[call("read_audit",{"row_id":1},"call-2")]},
        {"role":"assistant","content":"Read the row and its audit history."},
    ])
    async def complete(history,schemas):
        return {"message":next(turns),"tokens":12}
    async def save(history,tokens):
        checkpoints.append(tokens)
    result=await run_loop([{"role":"user","content":"Check its history"}],Tools(),complete,save)
    assert observed==["query_rows","read_audit"]
    assert checkpoints==[12,12,24,24]
    assert result["tokens"]==36


@pytest.mark.asyncio
async def test_duplicate_tool_delivery_is_not_executed_twice():
    executions=[]
    class Tools:
        schemas=[]
        async def execute(self,*args):
            executions.append(args)
            return {"ok":True}
    turns=iter([{"role":"assistant","tool_calls":[call("edit",{}),call("edit",{})]},
                {"role":"assistant","content":"done"}])
    async def complete(*args):return {"message":next(turns),"tokens":1}
    async def checkpoint(*args):pass
    await run_loop([],Tools(),complete,checkpoint)
    assert len(executions)==1


@pytest.mark.asyncio
async def test_budget_stops_before_executing_tools():
    class Tools:
        schemas=[]
        async def execute(self,*args):raise AssertionError("Must not run")
    async def complete(*args):
        return {"message":{"role":"assistant","tool_calls":[call("edit",{})]},"tokens":99}
    async def checkpoint(*args):pass
    with pytest.raises(AgentBudgetExceeded):
        await run_loop([],Tools(),complete,checkpoint,max_tokens=10)


@pytest.mark.asyncio
async def test_literal_intake_write_readback_and_retry(store):
    await seed_thread(store)
    ctx=await snapshot(store)
    tools=DataTools(service,"eligibility","turn","owner",ctx,"row 1 add intake Feb and July")
    first=await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"write-1")
    second=await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"write-1")
    assert first["row"]["intakes"]==["February","July"]
    assert first["changed"] and not second["changed"]
    async with store() as db:
        assert len((await db.execute(select(CourseAuditLog))).scalars().all())==1


@pytest.mark.asyncio
async def test_injected_tool_content_cannot_authorize_a_write(store):
    await seed_thread(store)
    ctx=await snapshot(store)
    tools=DataTools(service,"eligibility","turn","owner",ctx,"Is row 1 removed?")
    with pytest.raises(ValueError,match="not authorized"):
        await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"injected")
    with pytest.raises(ValueError,match="Unknown tool"):
        await tools.execute("approve_all_courses",{},"injected-approve")
    async with store() as db:
        assert not (await db.get(ScrapedCourse,1)).intake_months


@pytest.mark.asyncio
async def test_outside_provider_and_published_writes_are_blocked(store):
    await seed_thread(store)
    ctx=await snapshot(store)
    tools=DataTools(service,"eligibility","turn","owner",ctx,"row 1 add intake Feb and July")
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.status="approved"
        await db.commit()
    assert (await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"published"))["requiresConfirmation"]
    with pytest.raises(ValueError,match="outside"):
        await tools.execute("read_row",{"row_id":9999},"outside")


@pytest.mark.asyncio
async def test_undo_reverses_audited_change_without_fetch(store):
    await seed_thread(store)
    ctx=await snapshot(store)
    tools=DataTools(service,"eligibility","turn","owner",ctx,"row 1 add intake Feb and July")
    await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"write-1")
    tools.prompt="Undo that"
    result=await tools.execute("undo_last",{},"undo-1")
    assert result["changed"] and result["rows"][0]["intakes"] is None
    async with store() as db:
        assert len((await db.execute(select(CourseAuditLog))).scalars().all())==2


@pytest.mark.asyncio
async def test_bulk_requires_confirmation_before_any_change(store):
    await seed_thread(store)
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),"fix all IELTS")
    result=await tools.execute("bulk_edit",{"changes":[
        {"row_id":i,"field":"ielts_overall","evidence_id":i} for i in range(1,12)]},"bulk")
    assert result["requiresConfirmation"] and not result["changed"]


@pytest.mark.asyncio
async def test_missing_evidence_cannot_change_field(store):
    await seed_thread(store)
    tools=DataTools(service,"eligibility","turn","owner",await snapshot(store),"fix IELTS on row 1")
    result=await tools.execute("edit_field",{"row_id":1,"field":"ielts_overall","evidence_id":9999},"proof")
    assert result["requiresReview"] and not result["changed"]


@pytest.mark.asyncio
async def test_evidence_backed_score_write_verifies_numeric_database_value(store):
    from app.models.evidence import ScrapedFieldEvidence
    await seed_thread(store)
    ctx=await snapshot(store)
    ctx["home"]=ctx["samples"][0]["url"]
    async with store() as db:
        proof=ScrapedFieldEvidence(scraped_course_id=1,field_key="ielts_overall",
            normalized_value="6.5",source_url=ctx["home"],snippet="IELTS overall 6.5",
            confidence=1,selected=True,validation_status="verified",extraction_method="test_review")
        db.add(proof)
        await db.commit()
        proof_id=proof.id
    tools=DataTools(service,"eligibility","turn","owner",ctx,"fix IELTS on row 1")
    result=await tools.execute("edit_field",{"row_id":1,"field":"ielts_overall","evidence_id":proof_id},"score")
    assert result["changed"] and result["rows"][0]["ielts"]==6.5


@pytest.mark.asyncio
async def test_concurrent_intake_edit_is_preserved(store):
    await seed_thread(store)
    ctx=await snapshot(store)
    async with store() as db:
        row=await db.get(ScrapedCourse,1)
        row.intake_months=["September"]
        await db.commit()
    tools=DataTools(service,"eligibility","turn","owner",ctx,"row 1 add intake Feb and July")
    result=await tools.execute("edit_intakes",{"row_id":1,"months":["February","July"]},"stale")
    assert not result["changed"] and result["requiresReview"]


@pytest.mark.asyncio
async def test_resume_executes_only_unfinished_planned_tools():
    observed=[]
    class Tools:
        schemas=[]
        async def execute(self,name,args,id):
            observed.append(name)
            return {"ok":True}
    history=[
        {"role":"assistant","tool_calls":[call("read_row",{}),call("read_audit",{},"call-2")]},
        {"role":"tool","tool_call_id":"call-1","content":'{"ok":true}'},
    ]
    async def complete(*args):return {"message":{"role":"assistant","content":"done"},"tokens":1}
    async def checkpoint(*args):pass
    result=await run_loop(history,Tools(),complete,checkpoint,initial_tokens=12)
    assert observed==["read_audit"] and result["tokens"]==13

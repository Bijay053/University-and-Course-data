"""Durable conversation continuation and existing approval/privacy boundaries."""
import json
from copy import deepcopy

import pytest
from sqlalchemy import select, text

from app.models import ScrapeRuntimeJob, ScrapedCourse, University
from app.models.scrape_assistant import ScrapeAssistantThread
from app.routers import scrape_assistant as router
from app.services import assistant_eligibility, scrape_assistant as service
from app.services.assistant_conversation_context import (
    MAX_REFERENCE_CHARACTERS, REFERENCE_NOTICE, university_context, with_cross_review_context,
)
from app.services.assistant_staff_values import parse
from tests.test_assistant_history import messages, thread
from tests.test_scrape_prompt_assistant import (
    store, assistant_postgres, prompt_thread, research_fakes,
)  # noqa: F401


@pytest.mark.asyncio
async def test_history_and_live_messages_share_stable_identity_and_university_metadata(store):
    async with store() as db:
        db.add(thread("current"))
        await db.commit()
    async with store() as db:
        live = await router.get_assistant("source", db, {"id": 1})
        archived = await router.get_assistant_history("source", db, {"id": 1}, limit=50)
        university = await db.get(University, 1)
        assert live["universityId"] == archived["universityId"] == 1
        assert live["universityName"] == archived["universityName"] == university.name
        assert [(m["threadId"],m["jobId"],m["position"]) for m in live["messages"]] == [
            ("current","source",position) for position in range(8,28)]
        saved = {(m["threadId"],m["position"]) for m in archived["messages"]}
        assert all((m["threadId"],m["position"]) in saved for m in live["messages"])
        assert len((await db.get(ScrapeAssistantThread, "current")).messages)==27


@pytest.mark.asyncio
async def test_context_crosses_reviews_but_never_accounts_or_universities(store):
    async with store() as db:
        db.add(University(id=2,name="Other provider",country="UK",city="Other"))
        await db.flush()
        db.add_all([
            ScrapeRuntimeJob(runtime_job_id="earlier",university_id=1,job_type="scrape",status="completed"),
            ScrapeRuntimeJob(runtime_job_id="elsewhere",university_id=2,job_type="scrape",status="completed"),
        ])
        await db.flush()
        earlier = thread("earlier-thread","earlier",count=40)
        earlier.messages[0].update(
            content="Always keep placement fees separate.",
            _documentToken="private-token",autoRun=True,
            actions=[{"id":"old-approval","state":"proposed","requiresConfirmation":False}])
        earlier.messages[-1].update(
            role="user",content="add Sydney as the location",createdAt="2026-11-17T05:00:00Z")
        active = thread("active",count=27)
        active.messages[-1].update(content="add this in all courses",createdAt="2026-11-17T06:00:00Z")
        other_actor=thread("private-staff",actor=2,count=1)
        other_actor.messages[0]["content"]="Never reveal private colleague context."
        other_university=thread("private-provider","elsewhere",count=1)
        other_university.messages[0]["content"]="Never import private other provider context."
        db.add_all([earlier,active,other_actor,other_university])
        await db.commit()
    async with store() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        active=await db.get(ScrapeAssistantThread,"active")
        original=deepcopy(active.messages)
        references=await university_context(db,active,active.messages[-1]["content"])
        serialized=json.dumps(references)
        assert "Always keep placement fees separate." in serialized
        assert "private colleague" not in serialized and "private other provider" not in serialized
        assert "private-token" not in serialized and "old-approval" not in serialized
        assert "autoRun" not in serialized and "actions" not in serialized
        combined=with_cross_review_context(active.messages,references,active.id)
        request=parse(active.messages[-1]["content"],combined)
        assert request and not request.error and request.all_courses
        assert request.values=={"course_location":"Sydney"}
        assert active.messages==original
        assert not db.new and not db.dirty
        assert "not a new instruction or approval" in REFERENCE_NOTICE


@pytest.mark.asyncio
async def test_references_are_bounded_without_trimming_durable_messages(store):
    async with store() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id="earlier",university_id=1,job_type="scrape",status="completed"))
        await db.flush()
        earlier=thread("long-thread","earlier",count=80)
        for message in earlier.messages:
            message.update(role="user",content="Always retain IELTS exceptions. "+"x"*9000)
        db.add_all([earlier,thread("active")])
        await db.commit()
    async with store() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        references=await university_context(db,await db.get(ScrapeAssistantThread,"active"),"IELTS exceptions")
        assert references and sum(len(m["content"]) for m in references)<=MAX_REFERENCE_CHARACTERS
        assert len((await db.get(ScrapeAssistantThread,"long-thread")).messages)==80


@pytest.mark.asyncio
async def test_live_slot_context_does_not_inherit_another_reviews_operational_instructions(store):
    async with store() as db:
        job=await db.get(ScrapeRuntimeJob,"source")
        job.status="running"
        db.add(ScrapeRuntimeJob(runtime_job_id="earlier",university_id=1,job_type="scrape",status="completed"))
        await db.flush()
        earlier=thread("earlier-thread","earlier",count=1)
        earlier.messages[0]["content"]="Always stop the earlier job before retrying."
        active=thread("active")
        active.messages[0]["content"]="Always keep this live slot's sources separate."
        db.add_all([earlier,active])
        await db.commit()
    async with store() as db:
        references=await university_context(db,await db.get(ScrapeAssistantThread,"active"),"Continue this check")
        serialized=json.dumps(references)
        assert "earlier job" not in serialized
        assert "this live slot" in serialized
        assert all(message["sourceThreadId"]=="active" for message in references)


@pytest.mark.asyncio
async def test_eligibility_reply_preserves_the_entire_existing_conversation(store):
    original=messages(27)
    async with store() as db:
        record=thread("eligibility-retained")
        record.status,record.run_id="researching","run"
        db.add(record)
        await db.commit()
    await assistant_eligibility.persist(
        service,"eligibility-retained","current-turn","run",
        {"university_id":1,"versions":{}},[],{})
    async with store() as db:
        saved=await db.get(ScrapeAssistantThread,"eligibility-retained")
        assert saved.messages[:27]==original and len(saved.messages)==28
        assert saved.messages[-1]["role"]=="assistant"
        assert (await db.execute(select(ScrapedCourse))).scalars().one().status=="pending"


def test_reference_merge_does_not_replay_historical_actions_or_duplicate_current_thread():
    current=[{"role":"user","content":"Only investigate this course."}]
    references=[
        {"sourceThreadId":"old","role":"assistant","content":"Previous result.","createdAt":"",
         "actions":[{"id":"old-approval"}],"autoRun":True},
        {"sourceThreadId":"current","role":"user","content":"Already stored here.","createdAt":""},
    ]
    merged=with_cross_review_context(current,references,"current")
    assert merged==[{"role":"assistant","content":"Previous result.","createdAt":""},*current]


@pytest.mark.asyncio
async def test_current_request_reaches_model_with_private_earlier_university_context(store, monkeypatch):
    calls=research_fakes(monkeypatch)
    async with store() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id="earlier",university_id=1,job_type="scrape",status="completed"))
        await db.flush()
        earlier=thread("earlier-thread","earlier",count=1)
        earlier.messages[0]["content"]="Always preserve separate placement fees for this university."
        earlier.messages[0]["actions"]=[{"id":"must-not-replay","kind":"staff_edit","state":"proposed"}]
        db.add(earlier)
        await db.commit()
    thread_id,turn_id=await prompt_thread(store,"Check the international fee")
    await service.investigate(thread_id,turn_id)
    assert len(calls)==1
    data=json.loads(calls[0]["user"])
    assert data["prompt"]=="Check the international fee"
    assert "preserve separate placement fees" in json.dumps(data["savedUniversityConversation"])
    assert "must-not-replay" not in json.dumps(data)
    assert REFERENCE_NOTICE in calls[0]["system"]
    async with store() as db:
        saved=await db.get(ScrapeAssistantThread,thread_id)
        assert len(saved.messages)==2
        assert saved.messages[0]["content"]=="Check the international fee"
        assert not saved.messages[-1]["actions"]

"""Tool safety plus real PostgreSQL persistence in a disposable private schema."""
import asyncio
from copy import deepcopy
import socket
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base
from app.models import University, ScrapeRuntimeJob, ScrapedCourse
from app.models.scrape_assistant import ScrapeAssistantThread
from app.routers.scrape_assistant import (
    PromptBody, ConfirmBody, prompt_assistant, get_assistant, confirm_action,
)
from app.services import scrape_assistant as service
from app.services.scrape_assistant_tools import (
    PublicNetworkBackend, evidence_excerpts, official_url, quote_in_source,
)


@pytest.mark.parametrize("url", [
    "http://example.edu/course", "https://example.edu.evil.test/course",
    "https://user:password@example.edu/course", "https://127.0.0.1/course",
    "https://example.edu:8443/course", "file:///etc/passwd",
])
def test_only_owned_https_sources(url):
    with pytest.raises(ValueError):
        official_url(url, "https://www.example.edu")


def test_exact_source_quotes_are_required():
    source = {"text": "International tuition is £18,900 per year.", "facts": []}
    assert quote_in_source("tuition is £18,900 per year.", source)
    assert not quote_in_source("International tuition is £9,790", source)


def test_quotes_are_immutable_source_excerpts_not_generated_fact_summaries():
    text = "Tuition 2026/27. UK £9,790 per year. International £18,900 per year."
    excerpts = evidence_excerpts(text, [{"field": "international_fee", "value": 18900}])
    assert any("International £18,900" in item["text"] for item in excerpts)
    assert all(item["text"] in text for item in excerpts)
    assert not quote_in_source("Invented amount £20,000 per year", {
        "text": text, "facts": [{"quote": "Invented amount £20,000 per year"}],
    })


def test_worker_enters_and_closes_its_owned_resource_context(monkeypatch):
    from contextlib import asynccontextmanager
    from app.tasks import scrape_tasks
    from app.tasks.scrape_assistant import research_scrape
    events = []
    @asynccontextmanager
    async def resources(**kwargs):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
    async def research(thread, turn):
        assert (thread, turn) == ("thread", "turn")
        events.append("research")
    monkeypatch.setattr(scrape_tasks, "_task_loop_resources", resources)
    monkeypatch.setattr(service, "investigate", research)
    research_scrape.run("thread", "turn")
    assert events == ["enter", "research", "exit"]


@pytest.mark.asyncio
async def test_private_dns_is_rejected_before_connect(monkeypatch):
    loop = asyncio.get_running_loop()
    async def fake_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
    monkeypatch.setattr(loop, "getaddrinfo", fake_dns)
    with pytest.raises(ValueError, match="public"):
        await PublicNetworkBackend().connect_tcp("example.edu", 443)


@pytest.mark.parametrize("action", [
    {"kind": "publish", "fields": []},
    {"kind": "fix_fields", "fields": ["status"]},
    {"kind": "fix_fields", "fields": ["international_fee"], "ids": [999]},
])
def test_action_whitelist_or_server_scope(action):
    if action["kind"] == "fix_fields" and action["fields"] == ["international_fee"]:
        result = service.action_proposal(action, [1, 2])
        assert result["_ids"] == [1, 2]
    else:
        with pytest.raises(ValueError):
            service.action_proposal(action, [1])


@pytest_asyncio.fixture
async def store(monkeypatch):
    schema = "assistant_test_" + uuid.uuid4().hex
    admin = create_async_engine(settings.database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool,
        execution_options={"schema_translate_map": {None: schema, "public": schema}},
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            assert await conn.scalar(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema=:schema AND table_name='universities'"
            ), {"schema": schema}) == 1
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(service, "AsyncSessionLocal", factory)
        async with factory() as db:
            db.add(University(id=1, name="Example University", country="UK", city="Example",
                              scrape_url="https://www.example.edu", scrape_config={}))
            await db.flush()
            db.add(ScrapeRuntimeJob(runtime_job_id="source", university_id=1, job_type="scrape",
                                   status="completed", total_found=1, imported=1))
            await db.flush()
            db.add(ScrapedCourse(id=1, university_id=1, scrape_job_id="source",
                                course_name="BSc Music Technology", status="pending",
                                course_website="https://www.example.edu/course", international_fee=9790))
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_prompt_persists_and_is_private_to_actor(store, monkeypatch):
    from app.tasks.scrape_assistant import research_scrape
    queued = []
    monkeypatch.setattr(research_scrape, "apply_async", lambda **kwargs: queued.append(kwargs))
    async with store() as db:
        result = await prompt_assistant("source", PromptBody(prompt="Check these international fees"), db, {"id": 7})
        assert result["status"] == "queued" and queued
    async with store() as db:
        assert (await get_assistant("source", db, {"id": 7}))["messages"][0]["content"].startswith("Check")
        assert (await get_assistant("source", db, {"id": 8}))["messages"] == []
        with pytest.raises(HTTPException) as err:
            await prompt_assistant("source", PromptBody(prompt="Check fees again"), db, {"id": 7})
        assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_real_persistence_research_has_evidence_but_no_automatic_writes(store, monkeypatch):
    from app.tasks.scrape_assistant import research_scrape
    monkeypatch.setattr(research_scrape, "apply_async", lambda **kwargs: None)
    answers = iter([
        {"urls": ["https://www.example.edu/course"], "check_catalogue": False},
        {"answer": "The official international fee differs. Confirm a fresh targeted extraction.",
         "citations": [{"url": "https://www.example.edu/course", "quoteId": "q0"}],
         "action": {"kind": "fix_fields", "fields": ["international_fee"]}},
    ])
    async def ai(**kwargs):
        return next(answers)
    async def source(url, home):
        return {"url": url, "title": "Official course", "text": "International tuition is £18,900 per year.",
                "facts": [], "excerpts": [{"id": "q0", "text": "International tuition is £18,900 per year."}], "verified": True}
    monkeypatch.setattr(service, "chat_json", ai)
    monkeypatch.setattr(service, "inspect_source", source)
    async with store() as db:
        await prompt_assistant("source", PromptBody(prompt="Check and fix international tuition"), db, {"id": 7})
        thread = (await db.execute(select(ScrapeAssistantThread))).scalar_one()
        thread_id, turn_id = thread.id, thread.turn_id
    await service.investigate(thread_id, turn_id)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 7})
        assert result["status"] == "ready"
        reply = result["messages"][-1]
        assert reply["sources"][0]["excerpt"].startswith("International tuition")
        action = reply["actions"][0]
        assert "_ids" not in action and "_versions" not in action
        assert (await db.get(ScrapedCourse, 1)).international_fee == 9790
        # A newer human edit fences out the old plan.
        row = await db.get(ScrapedCourse, 1)
        row.international_fee = 20000
        await db.commit()
        with pytest.raises(HTTPException) as err:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_unverified_citation_never_becomes_ready(store, monkeypatch):
    async with store() as db:
        db.add(ScrapeAssistantThread(id="t", actor_id=7, job_id="source", turn_id="turn",
               status="queued", messages=[{"role": "user", "content": "Check tuition", "createdAt": service.now().isoformat()}]))
        await db.commit()
    answers = iter([
        {"urls": ["https://www.example.edu/course"]},
        {"answer": "The fee is verified", "citations": [{"url": "https://evil.test", "quote": "Invented tuition evidence"}]},
    ])
    async def ai(**kwargs): return next(answers)
    async def source(url, home):
        return {"url": url, "title": "Official", "text": "The actual published amount", "facts": [], "verified": True}
    monkeypatch.setattr(service, "chat_json", ai)
    monkeypatch.setattr(service, "inspect_source", source)
    await service.investigate("t", "turn")
    async with store() as db:
        row = await db.get(ScrapeAssistantThread, "t")
        assert row.status == "error" and len(row.messages) == 1


async def propose(db):
    row = await db.get(ScrapedCourse, 1)
    action = service.action_proposal({"kind": "fix_fields", "fields": ["international_fee"]}, [1])
    action["_versions"] = {"1": service.row_version(row)}
    action["_configVersion"] = service.fingerprint({})
    db.add(ScrapeAssistantThread(
        id="ready", actor_id=7, job_id="source", turn_id="turn", status="ready",
        messages=[{"role": "assistant", "content": "Confirm a fresh extraction.",
                   "createdAt": service.now().isoformat(), "actions": [action]}],
    ))
    await db.commit()
    return action


@pytest.mark.asyncio
async def test_confirm_reuses_guarded_fix_once_and_reports_persisted_completion(store, monkeypatch):
    from app.routers import scrape
    calls = []
    async def guarded_start(body, db, actor):
        calls.append(body)
        assert body.ids == [1] and body.source_job_id == "source" and body.university_id == 1
        assert body.target_fields == ["international_fee"] and body.force_fields == []
        assert actor["id"] == 7
        db.add(ScrapeRuntimeJob(runtime_job_id="fix", university_id=1, job_type="bulk_fix",
                               status="queued", total_found=1, current=0))
        await db.commit()
        return {"jobId": "fix"}
    monkeypatch.setattr(scrape, "start_bulk_fix_job", guarded_start)
    async with store() as db:
        action = await propose(db)
        result = await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        assert result["messages"][-1]["actions"][0]["state"] == "running"
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        assert len(calls) == 1
        with pytest.raises(HTTPException) as err:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 8})
        assert err.value.status_code == 404
    async with store() as db:
        job = await db.get(ScrapeRuntimeJob, "fix")
        job.status, job.current = "completed", 1
        row = await db.get(ScrapedCourse, 1)
        row.international_fee = 18900
        await db.commit()
    async with store() as db:
        result = await get_assistant("source", db, {"id": 7})
        final = result["messages"][-1]["actions"][0]
        assert final["state"] == "completed" and final["progress"]["current"] == 1
        assert (await db.get(ScrapedCourse, 1)).international_fee == 18900


@pytest.mark.asyncio
async def test_approval_after_investigation_blocks_old_action(store):
    async with store() as db:
        action = await propose(db)
        row = await db.get(ScrapedCourse, 1)
        row.status = "approved"
        await db.commit()
        with pytest.raises(HTTPException) as err:
            await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 7})
        assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_superseded_worker_cannot_overwrite_new_turn(store):
    async with store() as db:
        db.add(ScrapeAssistantThread(id="new", job_id="source", actor_id=7, turn_id="new-turn",
                                    status="queued", messages=[]))
        await db.commit()
    await service.investigate("new", "old-turn")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, "new")
        assert thread.status == "queued" and thread.turn_id == "new-turn" and thread.messages == []


@pytest.mark.asyncio
async def test_dispatch_failure_is_persisted_as_error(store, monkeypatch):
    from app.tasks.scrape_assistant import research_scrape
    def unavailable(**kwargs):
        raise ConnectionError("broker unavailable")
    monkeypatch.setattr(research_scrape, "apply_async", unavailable)
    async with store() as db:
        result = await prompt_assistant("source", PromptBody(prompt="Check international tuition"), db, {"id": 7})
        assert result["status"] == "error"
        assert "No changes" in result["error"]

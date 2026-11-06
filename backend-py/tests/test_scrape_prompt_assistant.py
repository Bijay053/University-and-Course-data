"""Tool safety plus real PostgreSQL persistence in a disposable private schema."""
import asyncio
from copy import deepcopy
import socket
from types import SimpleNamespace
import uuid

import pytest


@pytest.mark.asyncio
async def test_document_upload_is_disabled_until_storage_is_ready(store, monkeypatch):
    from app.config import settings
    from app.routers.scrape_assistant import document_upload, DocumentUploadBody
    monkeypatch.setattr(settings, "assistant_document_uploads_enabled", False)
    async with store() as db:
        with pytest.raises(HTTPException) as error:
            await document_upload("source", DocumentUploadBody(
                filename="requirements.pdf", size=100, contentType="application/pdf",
            ), db, {"id": 1})
        assert error.value.status_code == 503
        assert "storage" in error.value.detail


@pytest.mark.asyncio
async def test_document_confirmation_persists_audit_and_rejects_stale_replay(store, monkeypatch):
    from app.services.assistant_document_review import apply
    from app.services import assistant_staff_edits
    from app.models.audit import CourseAuditLog
    from app.models.evidence import ScrapedFieldEvidence
    monkeypatch.setattr(assistant_staff_edits, "permissions", lambda actor: None)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        action = {"id": "doc-proof", "_turnId": "turn", "_source": "https://www.example.edu/english.pdf",
                  "_provenance": "official-document", "_targets": [{"id": 1, "version": service.row_version(row),
                    "changes": [{"field": "ielts_reading", "newValue": 5.5,
                                 "quote": "IELTS no component below 5.5", "page": 1}]}]}
        await apply(db, SimpleNamespace(turn_id="turn"), action, 1, {"id": 1})
        await db.commit()
    async with store() as db:
        assert (await db.get(ScrapedCourse, 1)).ielts_reading == 5.5
        audits = (await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.action == "assistant_document_update"))).scalars().all()
        assert len(audits) == 1 and "english.pdf" in audits[0].reason
        proofs = (await db.execute(select(ScrapedFieldEvidence).where(
            ScrapedFieldEvidence.scraped_course_id == 1,
            ScrapedFieldEvidence.field_key == "ielts_reading"))).scalars().all()
        assert len(proofs) == 1 and proofs[0].selected
        with pytest.raises(ValueError, match="changed"):
            await apply(db, SimpleNamespace(turn_id="turn"), action, 1, {"id": 1})
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

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
from tests.test_hsu_facts import hsu_postgres as assistant_postgres

# The imported module-scoped fixture owns a socket-only disposable cluster;
# each store still owns its schema. No application/shared database is used.


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
async def store(monkeypatch, assistant_postgres):
    # Legacy confirmation tests isolate dispatch; automatic orchestration has
    # explicit tests below that restore the real dispatcher.
    async def no_automatic_dispatch(*args):
        return None
    monkeypatch.setattr(service, "run_automatic_action", no_automatic_dispatch)
    schema = "assistant_test_" + uuid.uuid4().hex
    admin = create_async_engine("postgresql+asyncpg://", connect_args=assistant_postgres, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        "postgresql+asyncpg://", poolclass=NullPool,
        execution_options={"schema_translate_map": {None: schema, "public": schema}},
        connect_args={**assistant_postgres, "server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            assert await conn.scalar(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema=:schema AND table_name='universities'"
            ), {"schema": schema}) == 1
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(service, "AsyncSessionLocal", factory)
        from app.services import scrape_assistant_runner
        monkeypatch.setattr(scrape_assistant_runner, "schedule", lambda *args: True)
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
    from app.services import scrape_assistant_runner
    queued = []
    monkeypatch.setattr(scrape_assistant_runner, "schedule", lambda *args: queued.append(args) or True)
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
@pytest.mark.parametrize("alternative_readable", [True, False])
@pytest.mark.parametrize("update_requested", [True, False])
async def test_lsbu_pdf_fallback_keeps_course_context_and_exact_source_urls(store, monkeypatch, alternative_readable, update_requested):
    from app.services import assistant_document_review
    import json
    from app.services.assistant_official_sources import LSBU_ENGLISH_SOURCES
    from app.tasks.scrape_assistant import research_scrape
    monkeypatch.setattr(research_scrape, "apply_async", lambda **kwargs: None)
    original_context = service.context
    async def context(job):
        result = await original_context(job)
        result["home"] = "https://www.lsbu.ac.uk"
        return result
    monkeypatch.setattr(service, "context", context)
    requested = "https://www.lsbu.ac.uk/_media_old/pdf/guidelines/Eng-lang-requirements.pdf.pdf.pdf"
    seen = []
    async def source(url, home):
        seen.append(url)
        if alternative_readable and url == LSBU_ENGLISH_SOURCES[0]:
            return {
                "url": url, "title": "Official English guidance", "sourceType": "pdf", "verified": True, "facts": [],
                "text": "General English guidance except health and postgraduate research.",
                "excerpts": [{"id": "p1-q0", "text": "General English guidance except health and postgraduate research."}],
            }
        return {
            "url": url, "sourceType": "pdf", "verified": False,
            "accessIssue": "robots_unavailable", "failureStage": "robots",
            "error": "robots.txt could not be verified (HTTP 403).",
        }
    async def answer(**kwargs):
        assert alternative_readable, "No model call is needed for wholly blocked sources"
        payload = json.loads(kwargs["user"])
        assert payload["sampleSize"] > 0
        return {
            "answer": "The alternative guidance was read. Course applicability remains unchecked.",
            "citations": [{"url": LSBU_ENGLISH_SOURCES[0], "quoteId": "p1-q0"}],
            "action": None,
        }
    monkeypatch.setattr(service, "inspect_source", source)
    monkeypatch.setattr(assistant_document_review, "inspect_source", source)
    monkeypatch.setattr(service, "chat_json", answer)
    errors = []
    investigate = service._investigate
    async def checked_investigation(*args):
        try:
            return await investigate(*args)
        except Exception as exc:
            errors.append(exc)
            raise
    monkeypatch.setattr(service, "_investigate", checked_investigation)
    prompt = (requested + "\nupdate each band ielts for all courses" if update_requested
              else "Read English guidance " + requested + "; do not update anything")
    thread_id, turn_id = await prompt_thread(store, prompt)
    await service.investigate(thread_id, turn_id)
    assert not errors, repr(errors[0]) if errors else ""
    if update_requested and alternative_readable:
        assert {requested, LSBU_ENGLISH_SOURCES[0]} <= set(seen)
    else:
        assert {requested, *LSBU_ENGLISH_SOURCES} <= set(seen)
    if not update_requested:
        assert len(seen) == 3
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
        assert result["status"] == "ready"
        reply = result["messages"][-1]
        assert not reply["actions"]
        if not update_requested:
            assert bool(reply["sources"]) is alternative_readable
        assert "edit form" not in reply["content"]
        if not alternative_readable:
            assert result["phase"] == "Source access blocked — 0 courses updated"
            assert reply["sourceOutcome"] == "access_verification_failed"
            assert reply.get("coursesUpdated", 0) == 0
            assert reply["activity"][-1]["status"] == "error"
            assert not any(event["label"] == "Official PDF unavailable" for event in reply["activity"])


@pytest.mark.asyncio
async def test_real_persistence_research_has_evidence_but_no_automatic_writes(store, monkeypatch):
    from app.tasks.scrape_assistant import research_scrape
    monkeypatch.setattr(research_scrape, "apply_async", lambda **kwargs: None)
    answers = iter([
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
    async def reachable(url,home):
        return {"verified":True,"url":url}
    monkeypatch.setattr(service,"inspect_source",reachable)
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
    from app.services import scrape_assistant_runner
    def unavailable(*args):
        raise ConnectionError("broker unavailable")
    monkeypatch.setattr(scrape_assistant_runner, "schedule", unavailable)
    async with store() as db:
        result = await prompt_assistant("source", PromptBody(prompt="Check international tuition"), db, {"id": 7})
        assert result["status"] == "error"
        assert "No changes" in result["error"]


async def prompt_thread(store, prompt="Check the international fee"):
    async with store() as db:
        result = await prompt_assistant("source", PromptBody(prompt=prompt), db, {"id": 1})
        thread = await db.get(ScrapeAssistantThread, result["threadId"])
        return thread.id, thread.turn_id


def research_fakes(monkeypatch):
    calls = []
    async def ai(**kwargs):
        calls.append(kwargs)
        return {"answer": "The bounded checks are complete. No course changes were applied.",
                "citations": [], "action": None}
    async def source(url, home):
        return {"url": url, "title": "Official source", "verified": True, "text": "Official source",
                "facts": [], "excerpts": [{"id": "q0", "text": "Official source"}]}
    monkeypatch.setattr(service, "chat_json", ai)
    monkeypatch.setattr(service, "inspect_source", source)
    async def empty_catalogue(*args):
        return {"courses": [], "sources": [], "available": False}
    monkeypatch.setattr(service, "inspect_comparison", empty_catalogue)
    return calls


@pytest.mark.asyncio
async def test_known_sources_use_one_model_call_and_persist_activity(store, monkeypatch):
    calls = research_fakes(monkeypatch)
    thread_id, turn_id = await prompt_thread(store)
    await service.investigate(thread_id, turn_id)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
    assert result["status"] == "ready"
    assert len(calls) == 1
    assert result["queuedAt"] <= result["startedAt"] <= result["updatedAt"]
    assert any(item["label"] == "Official page checked" and item["durationMs"] >= 0
               for item in result["activity"])
    assert result["activity"][-1]["label"] == "Answer ready"
    assert not any(item["status"] == "running" for item in result["activity"])
    assert result["turnId"] == turn_id
    assert result["messages"][-1]["turnId"] == turn_id
    assert result["messages"][-1]["activity"] == result["activity"]


def test_chat_prompt_no_longer_has_the_2000_character_cap():
    assert PromptBody(prompt="x" * 12000).prompt == "x" * 12000
    assert PromptBody(prompt="yes").prompt == "yes"
    assert "maxLength" not in PromptBody.model_json_schema()["properties"]["prompt"]


def test_failed_turn_keeps_its_real_work_when_next_prompt_starts():
    thread = SimpleNamespace(
        turn_id="old-turn", error="Source check failed; no correction was applied.",
        messages=[{"role": "user", "content": "Check this source", "createdAt": service.now().isoformat()}],
        activity=[{**service.event("Checking official source", status="running"), "_internal": "private"}],
    )
    service.archive_activity(thread)
    reply = thread.messages[-1]
    assert reply["role"] == "assistant" and reply["turnId"] == "old-turn"
    assert reply["content"] == thread.error
    assert reply["activity"][0]["status"] == "error"
    assert "_internal" not in reply["activity"][0]
    service.archive_activity(thread)
    assert len(thread.messages) == 2


@pytest.mark.asyncio
async def test_next_long_prompt_preserves_previous_turn_activity(store, monkeypatch):
    from app.services import scrape_assistant_runner
    research_fakes(monkeypatch)
    thread_id, turn_id = await prompt_thread(store)
    await service.investigate(thread_id, turn_id)
    monkeypatch.setattr(scrape_assistant_runner, "schedule", lambda *_: True)
    long_prompt = "Check official tuition and IELTS. " * 200
    async with store() as db:
        result = await prompt_assistant("source", PromptBody(prompt=long_prompt), db, {"id": 1})
    assert result["messages"][-1]["content"] == long_prompt.strip()
    assert result["messages"][-1]["turnId"] != turn_id
    assert result["messages"][-2]["activity"][-1]["label"] == "Answer ready"
    assert result["messages"][-2]["turnId"] == turn_id
    assert result["activity"][0]["label"] == "Request received"


@pytest.mark.asyncio
async def test_page_and_catalogue_checks_are_concurrent(store, monkeypatch):
    research_fakes(monkeypatch)
    source_started, catalogue_started = asyncio.Event(), asyncio.Event()
    async def source(url, home):
        source_started.set()
        await catalogue_started.wait()
        return {"url": url, "verified": False, "error": "Page was unavailable"}
    async def catalogue(*args):
        catalogue_started.set()
        await source_started.wait()
        return {"available": True, "observed": 12, "note": "Bounded observation, not proof of eligibility."}
    monkeypatch.setattr(service, "inspect_source", source)
    monkeypatch.setattr(service, "inspect_comparison", catalogue)
    thread_id, turn_id = await prompt_thread(store, "Why are all courses not listed?")
    await asyncio.wait_for(service.investigate(thread_id, turn_id), timeout=8)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
    assert result["status"] == "ready"
    assert any(item["label"] == "Catalogue check finished" for item in result["activity"])
    assert any(item["status"] == "error" and item["label"] == "Official page unavailable"
               for item in result["activity"])


@pytest.mark.asyncio
async def test_duplicate_deliveries_claim_only_one_investigation(store, monkeypatch):
    calls = research_fakes(monkeypatch)
    thread_id, turn_id = await prompt_thread(store)
    await asyncio.gather(service.investigate(thread_id, turn_id), service.investigate(thread_id, turn_id))
    assert len(calls) == 1
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        assert thread.status == "ready"
        assert len([m for m in thread.messages if m["role"] == "assistant"]) == 1


@pytest.mark.asyncio
async def test_cancelled_api_research_requeues_with_fenced_ownership(store, monkeypatch):
    research_fakes(monkeypatch)
    started = asyncio.Event()
    async def blocking_source(url, home):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "inspect_source", blocking_source)
    thread_id, turn_id = await prompt_thread(store)
    task = asyncio.create_task(service.investigate(thread_id, turn_id))
    await asyncio.wait_for(started.wait(), timeout=8)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        assert thread.status == "queued" and thread.run_id is None
        assert not any(item["status"] == "running" for item in thread.activity)
    assert await service.claim(thread_id, turn_id, "new-owner")
    with pytest.raises(ValueError, match="superseded"):
        await service.record_activity(thread_id, turn_id, "old-owner", service.event("Stale result"))
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        assert thread.run_id == "new-owner"
        assert not any(item["label"] == "Stale result" for item in thread.activity)


@pytest.mark.asyncio
async def test_opening_a_lost_queued_request_schedules_it_again(store, monkeypatch):
    from app.services import scrape_assistant_runner
    thread_id, turn_id = await prompt_thread(store)
    scheduled = []
    monkeypatch.setattr(scrape_assistant_runner, "schedule", lambda *args: scheduled.append(args))
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
    assert result["status"] == "queued" and result["error"] is None
    assert scheduled == [(thread_id, turn_id)]


@pytest.mark.asyncio
async def test_api_runner_is_bounded_and_does_not_duplicate_local_tasks(monkeypatch):
    from app.services import scrape_assistant_runner as runner
    blocked = asyncio.Event()
    calls = []
    async def research(*args):
        calls.append(args)
        await blocked.wait()
    monkeypatch.setattr(service, "investigate", research)
    try:
        assert runner.schedule("one", "turn")
        assert runner.schedule("one", "turn")
        assert runner.schedule("two", "turn")
        assert not runner.schedule("three", "turn")
        await asyncio.sleep(0)
        assert len(calls) == 2
        await runner.shutdown()
        assert not runner._tasks
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_binary_documents_are_not_claimed_as_verified_html(monkeypatch):
    from app.services import scrape_assistant_tools as tools
    async def pdf(*args, **kwargs):
        return b"%PDF-1.7\n" + b"x" * 1000
    monkeypatch.setattr(tools, "public_request", pdf)
    result = await tools.inspect_source("https://www.example.edu/fees.pdf", "https://www.example.edu")
    assert not result["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("readable", [True, False])
async def test_pdf_activity_and_page_citations_survive_reload_without_value_writes(store, monkeypatch, readable):
    from app.services import scrape_assistant_tools as tools
    from tests.helpers.assistant_pdf_fixture import make_pdf
    pdf_url = "https://www.example.edu/guidance.pdf"
    async def fetch(url):
        return make_pdf([
            "International tuition for Bachelor of Example 2026",
            "Annual GBP 18,900. Full Course GBP 56,700. Domestic GBP 9,790.",
            "IELTS overall 6.5, with no component below 6.0.",
        ] if readable else None)
    monkeypatch.setattr(tools, "public_request", fetch)
    async def ai(**kwargs):
        import json
        payload = json.loads(kwargs["user"])
        source = payload["sources"][0]
        assert source["sourceType"] == "pdf"
        assert not source.get("facts")
        return {
            "answer": "PDF research complete; values have not been updated.",
            "citations": [{"url": pdf_url, "quoteId": source["excerpts"][0]["id"]}] if readable else [],
            "action": None,
            # Unsupported generated numbers must never be persisted to rows.
            "international_fee": 1, "ielts_overall": 9,
        }
    monkeypatch.setattr(service, "chat_json", ai)
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        before = (row.international_fee, row.ielts_overall)
    thread_id, turn_id = await prompt_thread(store, f"Read fee and English guidance {pdf_url}; do not update anything")
    await service.investigate(thread_id, turn_id)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
        assert result["status"] == "ready"
        label = "Official PDF checked" if readable else "Official PDF unavailable"
        activity = next(item for item in result["activity"] if item["label"] == label)
        assert activity["url"] == pdf_url
        assert activity["status"] == ("completed" if readable else "error")
        assert ("Read 1/1 PDF pages" if readable else "no readable text") in activity["detail"]
        reply = result["messages"][-1]
        assert any(item["label"] == label for item in reply["activity"])
        if readable:
            assert reply["sources"][0]["page"] == 1
            assert "-p1-" in reply["sources"][0]["quoteId"]
            assert "Annual GBP 18,900" in reply["sources"][0]["excerpt"]
        else:
            assert reply["sources"] == []
        assert reply["actions"] == []
        row = await db.get(ScrapedCourse, 1)
        assert (row.international_fee, row.ielts_overall) == before


@pytest.mark.asyncio
async def test_confirmed_repair_keeps_verified_user_course_url(store, monkeypatch):
    research_fakes(monkeypatch)
    async def ai(**kwargs):
        return {"answer": "Confirm the bounded repair for this missing course.",
                "citations": [{"url": "https://www.example.edu/course", "quoteId": "q0"}],
                "action": {"kind": "repair_catalogue", "fields": []}}
    monkeypatch.setattr(service, "chat_json", ai)
    thread_id, turn_id = await prompt_thread(store, "https://www.example.edu/course is missing; fix it")
    await service.investigate(thread_id, turn_id)
    from app.routers import scrape
    from app.services import ai_repair_workflow
    received = []
    async def start(job, db, actor, evidence):
        received.append(evidence)
        return {"session_id": "new-repair"}
    async def load(*args):
        return {"status": "queued", "autonomous": {"phase": "queued"}}
    monkeypatch.setattr(scrape, "start_ai_repair", start)
    monkeypatch.setattr(ai_repair_workflow, "load", load)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
        action = result["messages"][-1]["actions"][0]
        assert "_evidenceUrls" not in action
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 1})
    assert received == [["https://www.example.edu/course"]]


@pytest.mark.asyncio
async def test_old_failed_repair_exposes_its_actual_failure(store, monkeypatch):
    from app.services import ai_repair_workflow
    thread_id, turn_id = await prompt_thread(store)
    async def load(*args):
        return {"status": "failed", "error": "Accepted live validation is required",
                "autonomous": {"phase": "blocked"},
                "live_probe": {"accepted": False, "course_pages": 0,
                               "samples": [{"reason": "HTTP 403; access not verified"}]}}
    monkeypatch.setattr(ai_repair_workflow, "load", load)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        thread.status = "ready"
        thread.messages = [{"role": "assistant", "content": "Earlier failed repair", "actions": [{
            "id": "failed-action", "kind": "repair_catalogue", "state": "error",
            "_sessionId": "old", "jobId": "source", "courseCount": 0,
        }]}]
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
        assert "HTTP 403" in result["messages"][0]["actions"][0]["error"]


def test_followup_uses_last_course_url_but_new_broad_question_does_not():
    messages = [{"role": "user", "content": "https://www.example.edu/old"},
                {"role": "user", "content": "https://www.example.edu/recent is missing"}]
    assert service.requested_course_urls("fix this", messages, "https://www.example.edu") == [
        "https://www.example.edu/recent"]
    assert service.requested_course_urls("where is result", messages, "https://www.example.edu") == [
        "https://www.example.edu/recent"]
    assert service.requested_course_urls("why this is missing from list add this", messages, "https://www.example.edu") == [
        "https://www.example.edu/recent"]
    assert not service.requested_course_urls("Why are all courses missing?", messages, "https://www.example.edu")
    assert service.requested_course_urls("https://www.example.edu/new", messages, "https://www.example.edu") == [
        "https://www.example.edu/new"]


@pytest.mark.parametrize("switch", [
    "not only this course all course it is mentuin unverified fix this",
    "Fix IELTS for all courses",
    "still lots of courses ielts is unverified, veify it",
    "many courses still show unverified",
    "verify the remaining courses",
    "Check every pending course",
    "New topic: check fees",
    "Now check this Master of Psychology",
])
def test_topic_switch_and_followups_do_not_resurrect_old_course(switch):
    messages = [
        {"role": "user", "content": "https://www.example.edu/bachelor-of-counselling/"},
        {"role": "assistant", "content": "Bachelor of Counselling is approved."},
        {"role": "user", "content": switch},
        {"role": "assistant", "content": "For Bachelor of Counselling..."},
        {"role": "user", "content": "do it i need verified data only"},
    ]
    assert not service.requested_course_urls(switch, messages[:3], "https://www.example.edu")
    assert not service.requested_course_urls(messages[-1]["content"], messages, "https://www.example.edu")
    assert not service.requested_course_urls("where is result", messages, "https://www.example.edu")


@pytest.mark.asyncio
async def test_review_wide_followup_passes_current_scope_not_stale_course_to_model(store, monkeypatch):
    import json
    research_fakes(monkeypatch)
    original = service.chat_json
    seen = []
    async def model(**kwargs):
        data = json.loads(kwargs["user"])
        seen.append(data)
        assert data["reviewWideScope"] is True
        assert data["requestedCourseUrls"] == []
        assert data["sampledRows"]
        assert "Counselling" not in json.dumps(data["recentConversation"])
        return await original(**kwargs)
    monkeypatch.setattr(service, "chat_json", model)
    tid, turn = await prompt_thread(store, "do it i need verified data only")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, tid)
        thread.messages = [
            {"role": "user", "content": "https://www.example.edu/bachelor-of-counselling/"},
            {"role": "assistant", "content": "Bachelor of Counselling is approved"},
            {"role": "user", "content": "not only this course all course it is mentuin unverified fix this"},
            {"role": "assistant", "content": "For Bachelor of Counselling..."},
        ] + thread.messages
        await db.commit()
    assert await service.claim(tid, turn, "scope-test")
    await service._investigate(tid, turn, "scope-test")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, tid)
        assert thread.status == "ready", thread.error
        assert seen
        assert all("bachelor-of-counselling" not in source["url"] for source in thread.messages[-1]["sources"])


@pytest.mark.asyncio
@pytest.mark.parametrize("record_kind",["published","pending"])
async def test_already_registered_course_does_not_trigger_unrelated_repair(store, monkeypatch,record_kind):
    from app.models.course import Course
    url = "https://www.example.edu/already-approved"
    async with store() as db:
        if record_kind=="published":
            db.add(Course(id=17, university_id=1, name="Already approved degree", course_website=url,
                          status="active", approval_status="approved"))
        else:
            db.add(ScrapeRuntimeJob(runtime_job_id="other-review",university_id=1,job_type="scrape",status="completed"))
            await db.flush()
            db.add(ScrapedCourse(id=17,university_id=1,scrape_job_id="other-review",status="pending",
                                course_name="Already approved degree",course_website=url))
        await db.commit()
    research_fakes(monkeypatch)
    async def ai(**kwargs):
        assert "Already approved degree" in kwargs["user"]
        return {"answer": "Incorrect model proposal must not run.", "citations": [{"url": url, "quoteId": "q0"}],
                "action": {"kind": "repair_catalogue", "fields": []}}
    monkeypatch.setattr(service, "chat_json", ai)
    thread_id, turn_id = await prompt_thread(store, f"{url} is missing; fix it")
    await service.investigate(thread_id, turn_id)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
    assert result["status"] == "ready"
    assert result["messages"][-1]["actions"]==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_row", [False, True])
async def test_add_this_keeps_requested_course_and_creates_confirmable_targeted_review(store, monkeypatch, matching_row):
    import json
    from app.models.course import Course
    from app.routers import scrape
    from app.services import scrape_assistant_runner
    url = "https://www.example.edu/counselling"
    async with store() as db:
        db.add(Course(id=17, university_id=1, name="Bachelor of Counselling", course_website=url,
                      status="active", approval_status="approved"))
        await db.commit()
    research_fakes(monkeypatch)
    seen = []
    async def source(value, home):
        seen.append(value)
        return {"url": value, "title": "Bachelor of Counselling", "verified": True,
                "text": "Bachelor of Counselling", "facts": [],
                "excerpts": [{"id": "q0", "text": "Bachelor of Counselling"}]}
    async def ai(**kwargs):
        context = json.loads(kwargs["user"])
        assert context["requestedCourseUrls"] == [url]
        assert context["sampledRows"] == []
        return {"answer": "Cannot add because it is approved.", "citations": [], "action": None}
    monkeypatch.setattr(service, "inspect_source", source)
    monkeypatch.setattr(service, "chat_json", ai)
    thread_id, turn = await prompt_thread(store, url + " this course is missing")
    await service.investigate(thread_id, turn)
    monkeypatch.setattr(scrape_assistant_runner, "schedule", lambda *_: True)
    async with store() as db:
        reply = await prompt_assistant("source", PromptBody(prompt="why this is missing from list add this"), db, {"id": 1})
    await service.investigate(thread_id, reply["turnId"])
    async with store() as db:
        reply = await get_assistant("source", db, {"id": 1})
    action = reply["messages"][-1]["actions"][0]
    assert action["kind"] == "review_courses"
    assert "Bachelor of Counselling" in reply["messages"][-1]["content"]
    assert "Nothing has been added yet" in reply["messages"][-1]["content"]
    assert set(seen) == {url}
    calls = []
    async def start(body, db):
        calls.append(body)
        db.add(ScrapeRuntimeJob(runtime_job_id="requested-review", university_id=1,
                               job_type="targeted", status="queued"))
        await db.flush()
        return SimpleNamespace(job_id="requested-review")
    monkeypatch.setattr(scrape, "start_scrape", start)
    async with store() as db:
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 1})
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 1})
        assert len(calls) == 1 and calls[0].course_urls == [url]
        assert calls[0].retry_source_job_id == "source"
        job = await db.get(ScrapeRuntimeJob, "requested-review")
        job.status = "completed"
        if matching_row:
            db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="requested-review", status="pending",
                                course_name="Bachelor of Counselling", course_website=url))
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
        assert result["messages"][-1]["actions"][0]["state"] == ("completed" if matching_row else "error")


@pytest.mark.asyncio
async def test_specific_url_cannot_fix_unrelated_open_review_rows(store, monkeypatch):
    research_fakes(monkeypatch)
    url = "https://www.example.edu/a-different-course"
    async def ai(**kwargs):
        return {"answer": "Fix the fee.", "citations": [{"url": url, "quoteId": "q0"}],
                "action": {"kind": "fix_fields", "fields": ["international_fee"]}}
    monkeypatch.setattr(service, "chat_json", ai)
    thread_id, turn = await prompt_thread(store, url + " correct its tuition")
    await service.investigate(thread_id, turn)
    async with store() as db:
        reply = await get_assistant("source", db, {"id": 1})
    assert reply["status"] == "ready"
    action = reply["messages"][-1]["actions"][0]
    assert action["kind"] == "review_courses" and action["state"] == "proposed"
    assert action["fields"] == [] and action["courseCount"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["running", "verified", "missing"])
async def test_footer_reports_confirmed_work_not_stale_research_status(store, case):
    await prompt_thread(store)
    url = "https://www.example.edu/counselling"
    async with store() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id="result-job", university_id=1, job_type="targeted",
                               status="running" if case == "running" else "completed"))
        await db.flush()
        if case == "verified":
            db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="result-job", status="pending",
                                course_name="Bachelor of Counselling", course_website=url))
        thread = (await db.execute(select(ScrapeAssistantThread))).scalar_one()
        thread.status = "ready"
        thread.phase = "Investigation ready — no changes applied"
        thread.messages = [{"role": "assistant", "content": "Confirmed action", "actions": [{
            "id": "operation", "kind": "review_courses", "state": "running",
            "jobId": "result-job", "_requestedUrls": [url], "fields": [], "courseCount": 1,
        }]}]
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
    assert "no changes applied" not in result["phase"].lower()
    action = result["messages"][0]["actions"][0]
    assert action["state"] == {"running": "running", "verified": "completed", "missing": "error"}[case]
    if case == "verified":
        assert "Verified: 1 requested course" in result["phase"]
    if case == "missing":
        assert "needs attention" in result["phase"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,kind,checkpoint,expected", [
    ("domestic_only", "skipped", "skipped", "international_ineligible"),
    ("online_only", "skipped", "skipped", "online_only"),
    ("part_time_only", "skipped", "skipped", "international_ineligible"),
    ("challenge_page", "extract_error", "error", "blocked_fetch"),
    ("fetch_failed", "extract_error", "error", "blocked_fetch"),
    ("extract_exception", "extract_error", "error", "incomplete_extraction"),
    ("no_international_fee", "skipped", "skipped", "incomplete_extraction"),
    ("", "stage_error", "error", "incomplete_extraction"),
    ("", None, "error", "incomplete_extraction"),
    ("", None, "skipped", "excluded"),
    ("", None, "staged", "unverified"),
    ("", None, None, "unverified"),
    ("blocked_path", "extract_gate_drop", "skipped", "excluded"),
    ("", "extract_course_detail_filter", "skipped", "excluded"),
    ("rejected: domestic_only", "skipped", "skipped", "international_ineligible"),
    ("rejected: online_only", "skipped", "skipped", "online_only"),
    ("rejected: part_time_only", "skipped", "skipped", "international_ineligible"),
    ("rejected: listed_but_ineligible_online_only", "skipped", "skipped", "online_only"),
    ("rejected: no_international_fee", "skipped", "skipped", "incomplete_extraction"),
])
@pytest.mark.parametrize("job_status", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("partial", [True, False])
@pytest.mark.parametrize("action_kind", ["review_courses", "recover_catalogue"])
async def test_recovery_inline_outcomes_are_persisted_and_url_scoped(store, reason, kind, checkpoint, expected, job_status, partial, action_kind):
    from app.models.scrape_runtime import ScrapeRuntimeLog
    await prompt_thread(store)
    urls = ["https://www.example.edu/counselling", "https://www.example.edu/criminology"]
    async with store() as db:
        target = ScrapeRuntimeJob(
            runtime_job_id="result-job", university_id=1, job_type="targeted",
            status=job_status, imported=9, skipped=7, errors=5,
            discovered_config={"autonomousVerification": {
                "completed_urls": urls,
                "url_outcomes": {urls[1]: checkpoint} if checkpoint else {},
            }},
        )
        db.add(target)
        await db.flush()
        db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="result-job", status="pending",
                            course_name="Counselling", course_website=urls[0]))
        # Old failed attempts cannot override a verified pending row.
        db.add(ScrapeRuntimeLog(runtime_job_id="result-job", sequence=1, event="status",
                               payload={"kind": "skipped", "url": urls[0], "reason": "online_only"}))
        if kind:
            db.add(ScrapeRuntimeLog(runtime_job_id="result-job", sequence=2, event="status",
                                   payload={"kind": kind, "reason": reason, **(
                                       {"dropped_sample": [urls[1]]} if kind == "extract_course_detail_filter"
                                       else {"url": urls[1]}
                                   )}))
        thread = (await db.execute(select(ScrapeAssistantThread))).scalar_one()
        thread.status = "ready"
        thread.messages = [{"role": "assistant", "content": "Checking", "actions": [{
            "id": "operation", "kind": action_kind, "state": "running",
            "jobId": "result-job", "_requestedUrls": urls if partial else urls[1:],
            "fields": [], "courseCount": 2 if partial else 1,
        }]}]
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
        action = result["messages"][0]["actions"][0]
        assert action["state"] == "error"
        assert action["jobId"] == "result-job"
        assert ("Partial recovery: 1 of 2" if partial else "No recovery verified for 1") in action["error"]
        assert [item["status"] for item in action["courseResults"]] == (["recovered", expected] if partial else [expected])
        assert "_requestedUrls" not in action
        assert (target.imported, target.skipped, target.errors) == (9, 7, 5)
        restored = await get_assistant("source", db, {"id": 1})
        assert restored["messages"][0]["actions"][0] == action


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,automatic", [(True, True), (False, True), (True, False)])
async def test_automatic_dispatch_authorization_and_no_duplicate_alternatives(store, monkeypatch, allowed, automatic):
    from app.models.user import User
    from app.services.scrape_assistant_autorun import autorun
    from app.routers import scrape_assistant as routes
    thread_id, turn = await prompt_thread(store)
    async with store() as db:
        db.add(User(id=1, email="fixture@example.test", password_hash="fixture-only", is_super_admin=allowed))
        thread = await db.get(ScrapeAssistantThread, thread_id)
        thread.status = "ready"
        thread.messages = [
            {"role": "user", "content": "Run the catalogue check", "autoRun": automatic},
            {"role": "assistant", "content": "Starting", "actions": [
                {"id": "recover", "kind": "recover_catalogue", "state": "proposed"},
                {"id": "scan", "kind": "scan_catalogue", "state": "proposed"}]}]
        await db.commit()
    calls = []
    async def dispatch(job_id, action_id, body, db, actor):
        calls.append(action_id)
        assert body.confirm is True and body.courseUrls is None
        thread = await db.get(ScrapeAssistantThread, thread_id)
        messages = deepcopy(thread.messages)
        messages[-1]["actions"][1]["state"] = "running"
        thread.messages = messages
        await db.commit()
    monkeypatch.setattr(routes, "confirm_action", dispatch)
    await autorun(thread_id, "stale-turn")
    assert not calls
    await autorun(thread_id, turn)
    if allowed and automatic:
        await autorun(thread_id, turn)
        assert calls == ["scan"]
        async with store() as db:
            thread = await db.get(ScrapeAssistantThread, thread_id)
            assert thread.messages[-1]["actions"][0]["state"] == "superseded"
    else:
        assert calls == []


@pytest.mark.asyncio
async def test_new_prompt_dispatches_after_research_without_another_user_click(store, monkeypatch):
    from app.models.user import User
    from app.services.scrape_assistant_autorun import autorun
    from app.routers import scrape_assistant as routes
    research_fakes(monkeypatch)
    async def answer(**kwargs):
        return {"answer": "I will recheck the fee.", "citations": [{"url": "https://www.example.edu/course", "quoteId": "q0"}],
                "action": {"kind": "fix_fields", "fields": ["international_fee"]}}
    monkeypatch.setattr(service, "chat_json", answer)
    monkeypatch.setattr(service, "run_automatic_action", autorun)
    async with store() as db:
        db.add(User(id=1, email="fixture@example.test", password_hash="fixture-only", is_super_admin=True))
        await db.commit()
    calls = []
    async def dispatch(job_id, action_id, body, db, actor):
        calls.append((job_id, body.confirm, actor["id"]))
    monkeypatch.setattr(routes, "confirm_action", dispatch)
    thread_id, turn = await prompt_thread(store, "Fix the international fee")
    await service.investigate(thread_id, turn)
    assert calls == [("source", True, 1)]


@pytest.mark.parametrize("prompt,expected", [
    ("Fix the fees", True), ("Find missing courses and run it", True),
    ("Only explain the differences", False), ("Compare but don't change anything", False),
    ("Research this but do not apply any changes", False), ("Do a read-only check", False),
    ("Fix pending fees; do not publish anything", True),
])
def test_explicit_read_only_requests_never_authorize_execution(prompt, expected):
    from app.services.scrape_assistant_autorun import execution_requested
    assert execution_requested(prompt) is expected


@pytest.mark.asyncio
async def test_new_research_status_does_not_reuse_historical_completed_action(store):
    thread_id, turn = await prompt_thread(store, "Check IELTS")
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        thread.status = "ready"
        thread.phase = "Current research complete"
        thread.messages = [
            {"role": "assistant", "content": "Earlier work", "actions": [
                {"id": "old", "kind": "fix_fields", "state": "completed",
                 "fields": [], "courseCount": 1, "resultSummary": "OLD RESULT"}]},
            {"role": "user", "content": "Check IELTS", "autoRun": True, "createdAt": service.now().isoformat()},
            {"role": "assistant", "content": "Current comparison", "actions": []},
        ]
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
        assert result["phase"] == "Current research complete"


@pytest.mark.asyncio
async def test_thread_links_to_matching_pending_review_not_approved_history(store):
    await prompt_thread(store, "https://www.example.edu/course is missing")
    async with store() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id="fresh-review", university_id=1, job_type="targeted",
                               status="completed"))
        await db.flush()
        db.add(ScrapedCourse(id=2, university_id=1, scrape_job_id="fresh-review", status="pending",
                            course_name="BSc Music Technology", course_website="https://www.example.edu/course"))
        await db.commit()
        thread = (await db.execute(select(ScrapeAssistantThread))).scalar_one()
        thread.messages = list(thread.messages) + [{"role": "user", "content": "https://www.example.edu/another-course",
                                                   "createdAt": thread.messages[0]["createdAt"]}]
        await db.commit()
        result = await get_assistant("source", db, {"id": 1})
    assert result["relatedReviews"] == [{"jobId": "fresh-review", "courseName": "BSc Music Technology", "status": "pending"}]


async def named_inventory(store):
    from app.models.course import Course
    async with store() as db:
        db.add(University(id=2, name="Different University", country="UK", city="Elsewhere"))
        db.add(ScrapeRuntimeJob(runtime_job_id="other-review", university_id=1,
                               job_type="scrape", status="completed"))
        await db.flush()
        db.add_all([
            Course(id=20, university_id=1, name="Master of Counselling and Psychotherapy",
                   course_website="https://www.example.edu/master", status="active", approval_status="approved"),
            Course(id=21, university_id=2, name="Bachelor of Nursing",
                   course_website="https://www.example.edu/wrong-university", status="active", approval_status="approved"),
            ScrapedCourse(id=20, university_id=1, scrape_job_id="other-review", status="pending",
                          course_name="Graduate Certificate of Counselling",
                          course_website="https://www.example.edu/certificate"),
            ScrapedCourse(id=21, university_id=1, scrape_job_id="source", status="pending",
                          course_name="Graduate Diploma of Counselling",
                          course_website="https://www.example.edu/diploma"),
        ])
        await db.commit()


async def named_turn(store, prompt):
    tid, turn = await prompt_thread(store, prompt)
    await service.investigate(tid, turn)
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, tid)
        assert thread.status == "ready", thread.error
        assert (await db.get(ScrapedCourse, 1)).international_fee == 9790
        return thread.messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,path", [
    ("Check Master of Counselling and Psychotherapy fees", "master"),
    ("Graduate Certificate of Counselling", "certificate"),
    ("Check IELTS for Graduate Diploma of Counselling", "diploma"),
])
async def test_named_courses_resolve_across_this_university_inventory(store, monkeypatch, prompt, path):
    import json
    await named_inventory(store)
    calls = research_fakes(monkeypatch)
    reply = await named_turn(store, prompt)
    data = json.loads(calls[-1]["user"])
    assert data["requestedCourseUrls"] == [f"https://www.example.edu/{path}"]
    assert all(row["id"] != 1 for row in data["sampledRows"])
    assert reply["resolvedCourses"][0]["url"].endswith("/" + path)
    assert not reply["actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [
    "No, I meant Graduate Certificate of Counselling, not Graduate Diploma of Counselling",
    "Not Graduate Diploma of Counselling, Graduate Certificate of Counselling",
    "Graduate Certificate of Counselling",
])
async def test_name_corrections_and_title_only_followups_replace_old_target(store, monkeypatch, correction):
    import json
    await named_inventory(store)
    calls = research_fakes(monkeypatch)
    await named_turn(store, "https://www.example.edu/course check this course")
    await named_turn(store, "Graduate Diploma of Counselling")
    reply = await named_turn(store, correction)
    assert reply["resolvedCourses"][0]["url"] == "https://www.example.edu/certificate"
    assert json.loads(calls[-1]["user"])["requestedCourseUrls"] == ["https://www.example.edu/certificate"]
    await named_turn(store, "Check it again")
    assert json.loads(calls[-1]["user"])["requestedCourseUrls"] == ["https://www.example.edu/certificate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["2", "Graduate Certificate of Counselling"])
async def test_ambiguous_subject_requires_choice_before_research(store, monkeypatch, selection):
    import json
    await named_inventory(store)
    calls = research_fakes(monkeypatch)
    first = await named_turn(store, "Check Counselling")
    assert len(first["courseChoices"]) == 3
    assert not first["actions"] and not first["sources"] and not calls
    # A vague follow-up must not quietly choose the first option.
    again = await named_turn(store, "do it")
    assert again["courseChoices"] == first["courseChoices"]
    assert not calls
    reply = await named_turn(store, selection)
    expected = first["courseChoices"][1]["url"] if selection == "2" else "https://www.example.edu/certificate"
    assert reply["resolvedCourses"][0]["url"] == expected
    assert json.loads(calls[-1]["user"])["requestedCourseUrls"] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["Bachelor of Nursing", "Master of Unknown Studies", "Quantum Computing"])
async def test_unmatched_names_never_use_another_school_or_open_review(store, monkeypatch, prompt):
    await named_inventory(store)
    calls = research_fakes(monkeypatch)
    reply = await named_turn(store, prompt)
    assert "could not match" in reply["content"]
    assert not reply["actions"] and not reply["sources"] and not calls
    followup = await named_turn(store, "IELTS?")
    assert "could not match" in followup["content"]
    assert not followup["actions"] and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_names_can_resolve_from_verified_official_catalogue_only(store, monkeypatch, verified):
    import json
    calls = research_fakes(monkeypatch)
    async def catalogue(*args):
        return {"sources": [{"url": "https://www.example.edu", "status": "checked" if verified else "unverified"}],
                "courses": [{"title": "Bachelor of Physics", "url": "https://www.example.edu/physics"},
                            {"title": "Bachelor of Physics", "url": "https://attacker.example/physics"}]}
    monkeypatch.setattr(service, "inspect_comparison", catalogue)
    reply = await named_turn(store, "Bachelor of Physics")
    if verified:
        assert reply["resolvedCourses"] == [{"title": "Bachelor of Physics", "url": "https://www.example.edu/physics"}]
        assert json.loads(calls[-1]["user"])["requestedCourseUrls"] == ["https://www.example.edu/physics"]
    else:
        assert not calls and "could not match" in reply["content"]
    assert not reply["actions"]


@pytest.mark.asyncio
async def test_named_correction_never_reimports_existing_course(store, monkeypatch):
    await named_inventory(store)
    research_fakes(monkeypatch)
    reply = await named_turn(store, "Fix Graduate Certificate of Counselling IELTS")
    assert not reply["actions"]
    assert reply["resolvedCourses"][0]["url"]=="https://www.example.edu/certificate"


@pytest.mark.asyncio
async def test_ambiguous_selection_retains_original_request_without_autoconfirmation(store, monkeypatch):
    await named_inventory(store)
    research_fakes(monkeypatch)
    choice = await named_turn(store, "Fix Counselling IELTS")
    index = next(i for i, item in enumerate(choice["courseChoices"], 1) if item["url"].endswith("/certificate"))
    clarification = await named_turn(store, "IELTS?")
    assert clarification["courseChoices"] == choice["courseChoices"]
    reply = await named_turn(store, str(index))
    assert not reply["actions"]
    assert reply["resolvedCourses"][0]["url"]=="https://www.example.edu/certificate"

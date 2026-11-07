"""Real isolated PostgreSQL registry tests plus transport-level restriction tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from tests.test_scrape_prompt_assistant import store, assistant_postgres
from app.services import university_source_access as registry
from app.services import scrape_assistant_tools as tools
from app.services.assistant_source_cache import cached_source

HOME = "https://www.example.edu"
URL = HOME + "/requirements.pdf"


@pytest.mark.parametrize("change", [
    {"method": "captcha_bypass"},
    {"alternatives": ["https://foreign.example/requirements.pdf"]},
    {"alternatives": ["http://www.example.edu/requirements.pdf"]},
    {"alternatives": [URL]},
    {"cookie": "not-allowed"},
])
def test_policy_rejects_evasion_foreign_sources_and_credentials(change):
    with pytest.raises(ValueError):
        registry.validate_policy(change, HOME, URL)


def test_expired_failure_does_not_become_document_evidence():
    now = datetime.now(timezone.utc)
    record = SimpleNamespace(url=URL, policy={}, observation={
        "verified": False, "accessIssue": "robots_unavailable",
        "checkedAt": (now - timedelta(seconds=121)).isoformat(),
    })
    assert registry.refusal(record, now) is None
    record.observation["checkedAt"] = now.isoformat()
    assert registry.refusal(record, now)["verified"] is False
    record.observation["verified"] = True
    assert registry.refusal(record, now) is None


@pytest.mark.asyncio
async def test_shared_observations_policy_audit_and_last_success(store):
    async with store() as db:
        await registry.observe(db, URL, HOME, {
            "verified": True, "sourceType": "pdf", "text": "Do not persist document contents",
            "cookies": "never", "authorization": "never",
        })
        await db.commit()
    async with store() as db:
        record = await registry.entry(db, URL, HOME)
        success = record.last_success_at
        assert success
        assert "text" not in record.observation and "cookies" not in record.observation
        await registry.set_policy(db, URL, HOME, {
            "method": "authorization_required",
            "alternatives": [HOME + "/alternative.pdf"],
            "authorization_reference": "Awaiting university authorisation",
        }, 7)
        await registry.observe(db, URL, HOME, {
            "verified": False, "accessIssue": "robots_unavailable",
            "httpStatus": 403, "failureStage": "robots",
            "restrictionKind": "cloudflare_challenge", "sourceRequestAttempted": False,
        })
        await db.commit()
    async with store() as db:
        record = await registry.entry(db, URL, HOME)
        assert record.last_success_at == success
        assert record.history[0]["actorId"] == 7
        assert record.policy["alternatives"] == [HOME + "/alternative.pdf"]
        assert registry.refusal(record)["_accessRegistryHit"]
        # Undo is an explicit, audited policy change, not deletion of history.
        await registry.set_policy(db, URL, HOME, record.history[0]["before"], 7)
        await db.commit()
        assert len(record.history) == 2


@pytest.mark.asyncio
async def test_cross_session_policy_stops_even_forced_fetch(store):
    async with store() as db:
        await registry.set_policy(db, URL, HOME, {"method": "authorized_upload"}, 1)
        await db.commit()
    service = SimpleNamespace(AsyncSessionLocal=store, SOURCE_ACCESS_REGISTRY=True)
    reader = AsyncMock()
    result = await cached_source(service, "missing-thread", "turn", "run", URL, HOME,
                                 reader, 2, fresh=True)
    assert not result["verified"] and not result["sourceRequestAttempted"]
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_admin_routes_persist_and_list_policy(store):
    from app.models import University
    from app.routers.source_access import PolicyRequest, update_access, list_access
    async with store() as db:
        provider = await db.get(University, 1)
        provider.website = HOME
        await db.commit()
        response = await update_access(1, PolicyRequest(
            url=URL, policy=registry.AccessPolicy(method="authorization_required"),
        ), db, {"id": 7})
        assert response["history"][-1]["actorId"] == 7
    async with store() as db:
        response = await list_access(1, db, {"id": 7})
        assert response["sources"][0]["policy"]["method"] == "authorization_required"


@pytest.mark.asyncio
async def test_route_permissions_are_enforced(store):
    from fastapi import FastAPI
    from app.dependencies import get_db, get_current_user
    from app.routers.source_access import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async def db():
        async with store() as session:
            yield session
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/universities/1/source-access")).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {"id": 7, "permissions": ["scraping.view"]}
        result = await client.put("/api/universities/1/source-access", json={
            "url": URL, "policy": {"method": "official_https"},
        })
        assert result.status_code == 403


@pytest.mark.asyncio
async def test_registry_policy_precedes_existing_conversation_success(store):
    from app.models.scrape_assistant import ScrapeAssistantThread
    async with store() as db:
        db.add(ScrapeAssistantThread(
            id="cached-thread", job_id="source", actor_id=9, turn_id="turn",
            run_id="run", status="researching",
            messages=[{"role": "user", "_sourceChecks": {URL: {
                "verified": True, "text": "Previously read evidence",
                "_sourceScopeVersion": 2, "_sourceCheckedAt": 1,
            }}}],
        ))
        await registry.set_policy(db, URL, HOME, {"method": "authorization_required"}, 7)
        await db.commit()
    reader = AsyncMock()
    result = await cached_source(SimpleNamespace(AsyncSessionLocal=store, SOURCE_ACCESS_REGISTRY=True),
                                 "cached-thread", "turn", "run", URL, HOME, reader, 2)
    assert result["verified"] is False and result["_accessRegistryHit"]
    reader.assert_not_called()


@pytest.mark.asyncio
async def test_reader_records_failure_without_repeating_across_users(store):
    service = SimpleNamespace(AsyncSessionLocal=store, SOURCE_ACCESS_REGISTRY=True)
    reader = AsyncMock(return_value={
        "url": URL, "verified": False, "accessIssue": "robots_unavailable",
        "failureStage": "robots", "httpStatus": 403, "sourceRequestAttempted": False,
    })
    await cached_source(service, "first-user", "turn", "run", URL, HOME, reader, 2)
    result = await cached_source(service, "second-user", "turn", "run", URL, HOME, reader, 2)
    assert result["_accessRegistryHit"]
    assert reader.await_count == 1


@pytest.mark.asyncio
async def test_failure_expiry_retries_and_providers_are_isolated(store):
    other_home = "https://second.example.edu"
    other = other_home + "/requirements.pdf"
    async with store() as db:
        await registry.observe(db, URL, HOME, {"verified": False, "accessIssue": "robots_unavailable"})
        record = await registry.entry(db, URL, HOME)
        record.observation = {**record.observation, "checkedAt": (
            datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat()}
        await registry.set_policy(db, other, other_home, {"method": "authorized_upload"}, 1)
        await db.commit()
    reader = AsyncMock(return_value={"url": URL, "verified": True, "sourceType": "pdf"})
    assert (await registry.guarded_read(store, URL, HOME, reader, 2))["verified"]
    assert not (await registry.guarded_read(store, other, other_home, reader, 2))["verified"]
    assert reader.await_count == 1


@pytest.mark.asyncio
async def test_simultaneous_sessions_do_not_duplicate_network_check(store):
    import asyncio
    started, finish = asyncio.Event(), asyncio.Event()
    async def reader(url, home):
        started.set()
        await finish.wait()
        return {"url": url, "verified": False, "accessIssue": "robots_unavailable"}
    first = asyncio.create_task(registry.guarded_read(store, URL, HOME, reader, 5))
    try:
        await asyncio.wait_for(started.wait(), 2)
        second = await registry.guarded_read(store, URL, HOME, AsyncMock(), 2)
        assert second["failureStage"] == "access_in_progress"
        assert second["sourceRequestAttempted"] is False
    finally:
        finish.set()
        await first
    # The transaction lock was released and the failure is now reusable.
    again = await registry.guarded_read(store, URL, HOME, AsyncMock(), 2)
    assert again["_accessRegistryHit"]


@pytest.mark.asyncio
async def test_schema_checker_matches_real_isolated_storage(store):
    from app.services.source_access_schema import require_source_access_schema
    async with store() as db:
        await require_source_access_schema(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("home", [HOME, "https://second.example.edu"])
async def test_registered_official_pdf_alternative_full_approval_flow(store, monkeypatch, home):
    import json
    from sqlalchemy import select, func
    from app.models import ScrapedCourse, User
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.services import scrape_assistant as service, assistant_document_review
    from app.services.scrape_agent_v2 import model
    from app.services.ai import openai_client
    from tests.test_scrape_prompt_assistant import prompt_thread
    from tests.helpers.assistant_pdf_fixture import make_pdf
    from app.routers.scrape_assistant import get_assistant, confirm_action, ConfirmBody
    primary, alternative = home + "/requirements.pdf", home + "/approved.pdf"
    quote = "Bachelor of Arts requires IELTS overall 6.5 with no component below 5.5."
    raw = make_pdf([quote])
    original_context = service.context
    async def context(job):
        ctx = await original_context(job)
        ctx["home"] = home
        return ctx
    monkeypatch.setattr(service, "context", context)
    async with store() as db:
        db.add(User(id=1, email="registry-test@example.test", password_hash="unused",
                    is_active=True, is_super_admin=True))
        row = await db.get(ScrapedCourse, 1)
        row.course_name, row.degree_level = "Bachelor of Arts", "Undergraduate"
        await registry.set_policy(db, primary, home, {"alternatives": [alternative]}, 1)
        await db.commit()
    calls = []
    async def source(url, owner):
        calls.append(url)
        if url == primary:
            return {"url": url, "verified": False, "failureStage": "robots",
                    "accessIssue": "robots_unavailable", "httpStatus": 403}
        assert url == alternative
        return await tools.inspect_source(url, owner, raw_body=raw)
    async def complete(client, messages, *args, **kwargs):
        courses = json.loads(messages[-1]["content"])["courses"]
        return {"message": {"content": json.dumps({"rows": [{
            "id": r["id"], "matched": True,
            "applicability": {"guidanceQuote": quote, "courseQuote": r["courseEvidence"], "page": 1},
            "changes": [{"field": field, "newValue": 6.5 if field == "ielts_overall" else 5.5,
                         "quote": quote, "page": 1} for field in (
                             "ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking")],
        } for r in courses]})}}
    monkeypatch.setattr(assistant_document_review, "inspect_source", source)
    monkeypatch.setattr(model, "complete", complete)
    monkeypatch.setattr(openai_client, "_client", lambda: SimpleNamespace(close=AsyncMock()))
    thread_id, turn_id = await prompt_thread(store, primary + "\nupdate each band ielts for all courses")
    await service.investigate(thread_id, turn_id)
    assert calls == [primary, alternative]
    async with store() as db:
        reply = (await get_assistant("source", db, {"id": 1}))["messages"][-1]
        assert reply["documentReview"]["proposed"] == 1
        action = reply["actions"][0]
        assert action["requiresConfirmation"] is True
        row = await db.get(ScrapedCourse, 1)
        assert row.ielts_overall is None
        await confirm_action("source", action["id"], ConfirmBody(confirm=True), db, {"id": 1})
    async with store() as db:
        row = await db.get(ScrapedCourse, 1)
        assert row.ielts_overall == 6.5
        assert all(getattr(row, f"ielts_{part}") == 5.5 for part in ("listening", "reading", "writing", "speaking"))
        assert row.international_fee == 9790
        assert await db.scalar(select(func.count()).select_from(ScrapedCourse)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cloudflare", [True, False])
async def test_robots_challenge_is_distinct_from_pdf_parse_failure(monkeypatch, cloudflare):
    original = httpx.AsyncClient
    calls = []
    def response(request):
        calls.append(str(request.url))
        return httpx.Response(403, headers={"cf-mitigated": "challenge"} if cloudflare else {})
    monkeypatch.setattr(tools.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(response)))
    result = await tools.inspect_source(URL, HOME)
    assert calls == [HOME + "/robots.txt"]
    assert result["failureStage"] == "robots"
    assert result["restrictionKind"] == ("cloudflare_challenge" if cloudflare else "robots_http_failure")
    assert result["sourceRequestAttempted"] is False

"""Private schema tests; never resume a real provider."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from tests.test_scrape_prompt_assistant import store, assistant_postgres
from app.models import University, CourseAuditLog
from app.models.user import User
from app.models.scrape_assistant import ScrapeAssistantThread
from app.routers.scrape_assistant import (
    assistant_import_status, resume_assistant_imports, ResumeImportsBody,
    confirm_action, ConfirmBody,
)

OWNER = {"id": 7, "is_super_admin": True}
STAFF = {"id": 8, "permissions": ["scraping.trigger"]}


async def pause(db):
    uni = await db.get(University, 1)
    uni.scrape_config = {"assistant_imports_paused": True, "preserved": "yes"}
    await db.commit()


@pytest.mark.asyncio
async def test_status_resume_audit_and_idempotence(store):
    async with store() as db:
        db.add(User(id=7, email="owner@example.test", password_hash="unused-test", is_active=True, is_super_admin=True))
        await pause(db)
        assert (await assistant_import_status("source", db, STAFF))["paused"]
        assert not (await assistant_import_status("source", db, STAFF))["canResume"]
        with pytest.raises(HTTPException) as error:
            await resume_assistant_imports("source", ResumeImportsBody(
                consolidationComplete=True, reason="Consolidation reviewed"), db, STAFF)
        assert error.value.status_code == 403
        with pytest.raises(HTTPException) as error:
            await resume_assistant_imports("source", ResumeImportsBody(
                consolidationComplete=False, reason="Consolidation reviewed"), db, OWNER)
        assert error.value.status_code == 422
        assert (await assistant_import_status("source", db, OWNER))["paused"]
        for _ in range(2):
            result = await resume_assistant_imports("source", ResumeImportsBody(
                consolidationComplete=True, reason="All duplicate groups reviewed and resolved"), db, OWNER)
            assert not result["paused"]
        logs = (await db.execute(select(CourseAuditLog).where(
            CourseAuditLog.action == "assistant_imports_resumed"))).scalars().all()
        assert len(logs) == 1
        assert logs[0].actor == "7" and "University 1" in logs[0].reason
        assert (await db.get(University, 1)).scrape_config["preserved"] == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scan_catalogue", "recover_catalogue", "review_courses", "repair_catalogue"])
async def test_pause_cannot_be_bypassed_by_confirming_existing_proposal(store, kind):
    async with store() as db:
        await pause(db)
        db.add(ScrapeAssistantThread(id="paused", job_id="source", actor_id=7,
            turn_id="turn", status="ready", messages=[{"role": "assistant", "actions": [
                {"id": "import", "state": "proposed", "kind": kind, "courseCount": 1}]}]))
        await db.commit()
        with pytest.raises(HTTPException) as error:
            await confirm_action("source", "import", ConfirmBody(confirm=True), db, OWNER)
        assert error.value.status_code == 409
        assert "consolidation" in error.value.detail
        await db.rollback()
        assert (await db.get(ScrapeAssistantThread, "paused")).messages[0]["actions"][0]["state"] == "proposed"


@pytest.mark.asyncio
async def test_http_authentication_and_permission_gates(store):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from app.routers.scrape_assistant import router
    from app.dependencies import get_current_user
    from app.database import get_db
    app = FastAPI()
    app.include_router(router)
    async def database():
        async with store() as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/jobs/source/assistant/import-status")).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {"id": 8, "permissions": []}
        assert (await client.get("/jobs/source/assistant/import-status")).status_code == 403
        assert (await client.post("/jobs/source/assistant/resume-imports", json={
            "consolidationComplete": True, "reason": "Reviewed all duplicates"})).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: STAFF
        assert (await client.get("/jobs/source/assistant/import-status")).status_code == 200
        assert (await client.post("/jobs/source/assistant/resume-imports", json={
            "consolidationComplete": True, "reason": "Reviewed all duplicates"})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["demoted", "deactivated", "deleted"])
async def test_old_admin_token_cannot_resume_after_revocation(store, monkeypatch, revocation):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from jose import jwt
    from types import SimpleNamespace
    from app import dependencies
    from app.routers.scrape_assistant import router
    from app.database import get_db
    # Test-only signing identity, no application credential is read.
    secret = "isolated-test-signing-key"
    monkeypatch.setattr(dependencies, "settings", SimpleNamespace(session_secret=secret))
    token = jwt.encode({**OWNER, "otp_verified": True}, secret, algorithm="HS256")
    async with store() as db:
        await pause(db)
        if revocation != "deleted":
            db.add(User(id=7, email="revoked@example.test", password_hash="unused-test",
                        is_active=revocation != "deactivated", is_super_admin=revocation != "demoted"))
        await db.commit()
    app = FastAPI()
    app.include_router(router)
    async def database():
        async with store() as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as client:
        status = await client.get("/jobs/source/assistant/import-status")
        assert status.status_code == 200
        assert status.json()["canResume"] is False
        result = await client.post("/jobs/source/assistant/resume-imports", json={
            "consolidationComplete": True, "reason": "Reviewed all duplicates"})
        assert result.status_code == 403
    async with store() as db:
        assert (await db.get(University, 1)).scrape_config["assistant_imports_paused"] is True
        assert not (await db.execute(select(CourseAuditLog))).scalars().all()

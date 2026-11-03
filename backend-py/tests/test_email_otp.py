"""Real Redis challenge semantics; no real accounts or email delivery."""
import asyncio
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jose import jwt
from redis.asyncio import Redis

from app.config import settings
from app.dependencies import get_current_user
from app.routers import auth
from app.services import login_otp


@pytest.fixture
def portal(monkeypatch):
    prefix = f"test:login-otp:{uuid4().hex}:"
    monkeypatch.setattr(login_otp, "PREFIX", prefix)
    monkeypatch.setattr(settings, "session_secret", "test-only-login-otp-signing-secret")
    user = SimpleNamespace(
        id=777, email="person@example.test", full_name="Test User",
        is_active=True, is_super_admin=False, password_hash="test-password-hash",
        role=None, permissions=[],
    )
    sent = []
    delivery = {"ok": True}

    async def load(db, email):
        return user if email.lower() == user.email.lower() else None

    async def mail(to, subject, body):
        sent.append(re.search(r"\b[0-9]{6}\b", body).group())
        return delivery["ok"]

    async def db():
        yield None

    monkeypatch.setattr(auth, "_load_user_by_email", load)
    monkeypatch.setattr(auth, "verify_password", lambda password, hashed: password == "test-password")
    monkeypatch.setattr(auth, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(auth, "send_email", mail)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[auth.get_db] = db

    @app.get("/protected")
    async def protected(payload=Depends(get_current_user)):
        return {"ok": True}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, user=user, sent=sent, delivery=delivery)

    async def purge():
        async with Redis.from_url(settings.redis_url, decode_responses=True) as redis:
            keys = [key async for key in redis.scan_iter(match=prefix + "*")]
            if keys:
                await redis.delete(*keys)
    asyncio.run(purge())


def start(portal):
    return portal.client.post("/api/auth/login", json={
        "email": portal.user.email, "password": "test-password",
    })


def verify(portal, token, code):
    return portal.client.post("/api/auth/verify-otp", json={"challenge_token": token, "code": code})


def test_password_only_never_authenticates_and_code_is_required(portal):
    response = start(portal)
    assert response.status_code == 200
    body = response.json()
    assert body["otp_required"] is True
    assert "token" not in body and "user" not in body
    assert portal.client.get("/protected").status_code == 401
    assert portal.client.get("/protected", headers={
        "Authorization": "Bearer " + body["challenge_token"],
    }).status_code == 401
    verified = verify(portal, body["challenge_token"], portal.sent[0])
    assert verified.status_code == 200
    token = verified.json()["token"]
    assert jwt.decode(token, settings.session_secret, algorithms=["HS256"])["otp_verified"] is True
    assert "HttpOnly" in verified.headers["set-cookie"]
    assert portal.client.get("/protected", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert verify(portal, body["challenge_token"], portal.sent[0]).status_code == 401


def test_five_incorrect_attempts_revoke_the_challenge(portal):
    token = start(portal).json()["challenge_token"]
    wrong = "000000" if portal.sent[0] != "000000" else "111111"
    for _ in range(5):
        assert verify(portal, token, wrong).status_code == 401
    assert verify(portal, token, portal.sent[0]).status_code == 401


def test_delivery_failure_issues_no_session_or_usable_challenge(portal):
    portal.delivery["ok"] = False
    response = start(portal)
    assert response.status_code == 503
    assert "token" not in response.json()
    assert portal.client.get("/protected").status_code == 401


def test_legacy_password_only_jwt_is_rejected(portal):
    token = jwt.encode({"sub": "777", "email": portal.user.email}, settings.session_secret, algorithm="HS256")
    assert portal.client.get("/protected", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert portal.client.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).json()["user"] is None


@pytest.mark.parametrize("change", ["password", "disabled"])
def test_account_changes_cancel_pending_login(portal, change):
    token = start(portal).json()["challenge_token"]
    if change == "password":
        portal.user.password_hash = "new-password-hash"
    else:
        portal.user.is_active = False
    assert verify(portal, token, portal.sent[0]).status_code == 401


def test_resend_cooldown_and_new_code_invalidates_old_challenge(portal):
    old = start(portal).json()["challenge_token"]
    assert start(portal).status_code == 429

    async def release():
        async with Redis.from_url(settings.redis_url) as redis:
            await redis.delete(login_otp.PREFIX + "cooldown:" + login_otp.stamp(portal.user.email))
    asyncio.run(release())
    new = start(portal).json()["challenge_token"]
    assert verify(portal, old, portal.sent[0]).status_code == 401
    assert verify(portal, new, portal.sent[1]).status_code == 200


def test_expiry_hash_only_storage_and_atomic_consumption(portal):
    token = start(portal).json()["challenge_token"]

    async def check():
        async with Redis.from_url(settings.redis_url, decode_responses=True) as redis:
            key = login_otp.PREFIX + "challenge:" + token
            data = await redis.hgetall(key)
            assert data["digest"] != portal.sent[0]
            assert data["password_stamp"] != portal.user.password_hash
            assert 0 < await redis.ttl(key) <= 600
        outcomes = await asyncio.gather(
            login_otp.consume(token, portal.sent[0]), login_otp.consume(token, portal.sent[0]),
            return_exceptions=True,
        )
        assert sum(isinstance(value, dict) for value in outcomes) == 1
        assert sum(isinstance(value, HTTPException) and value.status_code == 401 for value in outcomes) == 1
    asyncio.run(check())

    async def expire():
        new, code = await login_otp.issue(SimpleNamespace(
            id=888, email="other@example.test", password_hash="test-only-hash",
        ))
        async with Redis.from_url(settings.redis_url) as redis:
            await redis.delete(login_otp.PREFIX + "challenge:" + new)
        with pytest.raises(HTTPException) as error:
            await login_otp.consume(new, code)
        assert error.value.status_code == 401
    asyncio.run(expire())

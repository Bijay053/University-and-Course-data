"""JWT cookie session login matching the Node behaviour:
POST /api/auth/login   { email, password }   -> sets `session` cookie
POST /api/auth/logout                         -> clears cookie
GET  /api/auth/me                             -> { user } | { user: null }

The Node API used a static admin credential. We preserve that for parity
during cutover; a real user table can be wired in afterwards.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from jose import jwt

from app.config import settings
from app.dependencies import get_optional_user
from app.schemas.auth import LoginBody, LoginResponse, MeResponse

router = APIRouter()

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@university-portal.local")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Bijay@12345")
SESSION_DAYS = 7


def _issue_token(user: dict) -> str:
    payload = {
        **user,
        "exp": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginBody, response: Response) -> LoginResponse:
    if body.email.lower() != ADMIN_EMAIL.lower() or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    user = {"email": ADMIN_EMAIL, "role": "admin", "name": "Admin"}
    token = _issue_token(user)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS in production
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
    )
    return LoginResponse(user=user)


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(user: dict | None = Depends(get_optional_user)) -> MeResponse:
    if not user:
        return MeResponse(user=None)
    return MeResponse(user={k: v for k, v in user.items() if k not in {"exp", "iat"}})

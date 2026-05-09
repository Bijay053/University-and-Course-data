"""JWT cookie session login backed by the ``users`` table.

Endpoints:

  POST /api/auth/login              { email, password }   -> sets `session` cookie
  POST /api/auth/logout                                   -> clears cookie
  GET  /api/auth/me                                       -> { user, permissions, is_super_admin }
  POST /api/auth/forgot-password    { email }             -> always 200 (no enumeration)
  POST /api/auth/reset-password     { token, new_password }
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db, get_optional_user
from app.models.user import PasswordResetToken, User, UserPermission
from app.schemas.auth import (
    ForgotPasswordBody,
    GenericOk,
    LoginBody,
    LoginResponse,
    MeResponse,
    ResetPasswordBody,
)
from app.services.email import _is_configured as smtp_is_configured
from app.services.email import send_email
from app.services.passwords import hash_password, verify_password

log = logging.getLogger("uniportal.auth")
router = APIRouter()

SESSION_DAYS = 7
RESET_TOKEN_TTL_HOURS = 2


def _payload_for(user: User, permissions: list[str]) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.full_name or user.email.split("@")[0],
        "is_super_admin": user.is_super_admin,
        "permissions": permissions,
        # role kept for backwards compat with existing UI
        "role": "admin" if user.is_super_admin else "user",
    }


def _issue_token(payload: dict) -> str:
    body = {
        **payload,
        "exp": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(body, settings.session_secret, algorithm="HS256")


async def _load_user_by_email(db: AsyncSession, email: str) -> User | None:
    res = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return res.scalar_one_or_none()


def _perm_keys(user: User) -> list[str]:
    return sorted(p.permission_key for p in user.permissions)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginBody, response: Response, db: Annotated[AsyncSession, Depends(get_db)]
) -> LoginResponse:
    user = await _load_user_by_email(db, body.email)
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    perms = _perm_keys(user)
    payload = _payload_for(user, perms)
    token = _issue_token(payload)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        # Cookie must be HTTPS-only in production. Local dev (Replit preview
        # proxy or `http://127.0.0.1`) should set COOKIE_SECURE=0 (or leave
        # APP_ENV unset). Anything other than the dev override defaults to
        # secure=True so we can't accidentally ship insecure cookies.
        secure=os.environ.get("COOKIE_SECURE", "1" if os.environ.get("APP_ENV", "").lower() == "production" else "0") == "1",
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
    )
    return LoginResponse(
        user={k: v for k, v in payload.items() if k not in ("permissions", "is_super_admin")},
        permissions=perms,
        is_super_admin=user.is_super_admin,
    )


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
async def me(
    user: dict | None = Depends(get_optional_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    if not user:
        return MeResponse(user=None)
    # Re-read from DB so is_super_admin / permissions are always up-to-date,
    # even if the browser still holds an old JWT cookie.
    db_user = await _load_user_by_email(db, user.get("email", ""))
    if not db_user or not db_user.is_active:
        return MeResponse(user=None)
    perms = _perm_keys(db_user)
    safe = {
        "id": db_user.id,
        "email": db_user.email,
        "name": db_user.full_name,
        "role": "admin" if db_user.is_super_admin else "user",
    }
    return MeResponse(
        user=safe,
        permissions=perms,
        is_super_admin=db_user.is_super_admin,
    )


def _build_reset_url(request: Request, token: str) -> str:
    # Prefer the host the user is currently on (works for dev, prod, custom domains).
    host = request.headers.get("origin") or f"{request.url.scheme}://{request.url.netloc}"
    base = host.rstrip("/")
    return f"{base}/reset-password?token={token}"


@router.post("/forgot-password", response_model=GenericOk)
async def forgot_password(
    body: ForgotPasswordBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GenericOk:
    """Always returns 200 to avoid leaking which emails exist."""
    user = await _load_user_by_email(db, body.email)
    debug_url: str | None = None
    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        db.add(PasswordResetToken(token=token, user_id=user.id, expires_at=expires))
        await db.commit()

        reset_url = _build_reset_url(request, token)
        body_text = (
            f"Hi {user.full_name or user.email},\n\n"
            f"We received a request to reset your password. Click the link below "
            f"to set a new one (the link expires in {RESET_TOKEN_TTL_HOURS} hours):\n\n"
            f"{reset_url}\n\n"
            f"If you didn't request this, you can safely ignore this email.\n"
        )
        sent = await send_email(user.email, "Reset your password", body_text)
        if not sent and not smtp_is_configured():
            # Surface link in dev so the operator can still complete the flow.
            debug_url = reset_url
            log.warning("Reset link (SMTP not configured): %s", reset_url)

    return GenericOk(
        ok=True,
        message="If an account exists for that address, a reset link has been sent.",
        debug_reset_url=debug_url,
    )


@router.post("/reset-password", response_model=GenericOk)
async def reset_password(
    body: ResetPasswordBody, db: Annotated[AsyncSession, Depends(get_db)]
) -> GenericOk:
    res = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token == body.token)
    )
    row = res.scalar_one_or_none()
    if not row or row.used_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or already-used reset token")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Reset token has expired")
    user_res = await db.execute(select(User).where(User.id == row.user_id))
    user = user_res.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Account no longer accepts resets")
    user.password_hash = hash_password(body.new_password)
    user.updated_at = datetime.now(timezone.utc)
    row.used_at = datetime.now(timezone.utc)
    await db.commit()
    return GenericOk(ok=True, message="Password updated. You can now sign in.")


# ---------------------------------------------------------------------------
# Bootstrap helper -- called from app.main lifespan to ensure the env-default
# admin always exists. Idempotent.
# ---------------------------------------------------------------------------
async def ensure_admin_user() -> None:
    from sqlalchemy import select as _select

    from app.database import AsyncSessionLocal
    from app.models.user import UserPermission
    from app.permissions import ALL_KEYS

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@university-portal.local")
    admin_password = os.environ.get("ADMIN_PASSWORD", "Bijay@12345")
    admin_name = os.environ.get("ADMIN_NAME", "Admin")
    async with AsyncSessionLocal() as db:
        existing = await _load_user_by_email(db, admin_email)
        if existing:
            # Make sure they keep super-admin powers even if the row was edited.
            if not existing.is_super_admin or not existing.is_active:
                existing.is_super_admin = True
                existing.is_active = True
                existing.updated_at = datetime.now(timezone.utc)
                await db.commit()
            admin_id = existing.id
        else:
            admin = User(
                email=admin_email,
                full_name=admin_name,
                password_hash=hash_password(admin_password),
                is_active=True,
                is_super_admin=True,
            )
            db.add(admin)
            await db.commit()
            await db.refresh(admin)
            admin_id = admin.id
            log.info("Bootstrapped initial admin user %s", admin_email)

        # Grant every registered permission key explicitly. Super-admin already
        # bypasses checks, but this keeps the Permissions UI showing all boxes
        # ticked and protects any future code path that reads the granted set
        # directly instead of using `user_has_permission`.
        rows = (
            await db.execute(
                _select(UserPermission.permission_key).where(
                    UserPermission.user_id == admin_id
                )
            )
        ).scalars().all()
        already_granted = set(rows)
        missing = ALL_KEYS - already_granted
        for key in missing:
            db.add(UserPermission(user_id=admin_id, permission_key=key))
        if missing:
            await db.commit()
            log.info("Granted %d new permission(s) to admin user", len(missing))

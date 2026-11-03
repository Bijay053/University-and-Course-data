"""JWT cookie session login backed by the ``users`` table.

Endpoints:

   POST /api/auth/login              { email, password }   -> email OTP challenge, never a session
   POST /api/auth/verify-otp         { challenge_token, code } -> verified session
  POST /api/auth/logout                                   -> clears cookie
  GET  /api/auth/me                                       -> { user, permissions, is_super_admin }
  POST /api/auth/forgot-password    { email }             -> generic acknowledgement or global delivery-unavailable error
  POST /api/auth/reset-password     { token, new_password }
"""
from __future__ import annotations

import logging
import hmac
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
from app.models.user import PasswordResetToken, User
from app.schemas.auth import (
    ForgotPasswordBody,
    GenericOk,
    LoginBody,
    LoginResponse,
    LoginChallengeResponse,
    VerifyLoginBody,
    MeResponse,
    ResetPasswordBody,
)
from app.services.email import _is_configured as smtp_is_configured
from app.services.email import send_email
from app.services.passwords import hash_password, verify_password
from app.services import login_otp
from app.services.email_templates import login_code_email, password_reset_email

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
        "otp_verified": True,
    }
    return jwt.encode(body, settings.session_secret, algorithm="HS256")


async def _load_user_by_email(db: AsyncSession, email: str) -> User | None:
    res = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return res.scalar_one_or_none()


def _perm_keys(user: User) -> list[str]:
    role_perms = {p.permission_key for p in user.role.permissions} if user.role else set()
    user_perms = {p.permission_key for p in user.permissions}
    return sorted(role_perms | user_perms)


@router.post("/login", response_model=LoginChallengeResponse)
async def login(
    body: LoginBody, request: Request, response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LoginChallengeResponse:
    if request.headers.get("x-portal-login-flow") != "email-otp-v1":
        # Old open tabs treat ANY successful login response as an authenticated
        # session. Reject them before sending a code or consuming a rate limit.
        raise HTTPException(
            409, "The sign-in page has been updated for email verification. "
            "Please reload this page, then sign in again.",
            headers={"Cache-Control": "no-store"},
        )
    await login_otp.check_login_rate(request.client.host if request.client else "unknown")
    user = await _load_user_by_email(db, body.email)
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    if not smtp_is_configured():
        raise HTTPException(503, "Sign-in email delivery is unavailable. Please contact the portal administrator.")
    challenge = await login_otp.issue(user)
    if challenge.code is not None:
        message = login_code_email(
            challenge.code, user.full_name or "", expires_minutes=login_otp.TTL // 60,
        )
        sent = await send_email(
            user.email, message.subject, message.text, html_body=message.html,
        )
        if not sent:
            await login_otp.discard(challenge.token)
            raise HTTPException(503, "Could not send your verification email. Please try again later.")
        await login_otp.mark_delivered(challenge.token)
    response.delete_cookie("session", path="/")
    local, _, domain = user.email.partition("@")
    return LoginChallengeResponse(
        challenge_token=challenge.token, masked_email=f"{local[:1]}***@{domain}",
        expires_in=challenge.expires_in, resend_after=challenge.resend_after,
    )


@router.post("/verify-otp", response_model=LoginResponse)
async def verify_login_otp(
    body: VerifyLoginBody, request: Request, response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LoginResponse:
    proof = await login_otp.consume(body.challenge_token, body.code)
    user = await _load_user_by_email(db, proof["email"])
    if (
        not user or not user.is_active or user.id != proof["user_id"]
        or not hmac.compare_digest(login_otp.stamp(user.password_hash), proof["password_stamp"])
    ):
        raise HTTPException(401, "Your account changed. Please start sign-in again.")
    perms = _perm_keys(user)
    payload = _payload_for(user, perms)
    token = _issue_token(payload)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https",
        max_age=SESSION_DAYS * 24 * 3600,
        path="/",
    )
    return LoginResponse(
        user={k: v for k, v in payload.items() if k not in ("permissions", "is_super_admin")},
        permissions=perms,
        is_super_admin=user.is_super_admin,
        token=token,
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

    # Belt-and-suspenders: the configured admin account must always be
    # super-admin.  If the DB row has drifted (e.g. manual edit, failed
    # startup bootstrap), heal it now so the UI doesn't end up blank.
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@university-portal.local")
    is_admin_account = db_user.email.lower() == admin_email.lower()
    if is_admin_account and not db_user.is_super_admin:
        db_user.is_super_admin = True
        db_user.updated_at = datetime.now(timezone.utc)
        try:
            await db.commit()
            log.info("Healed is_super_admin for admin account %s via /me", db_user.email)
        except Exception:  # noqa: BLE001
            await db.rollback()
            log.exception("Could not heal is_super_admin for %s — using in-memory override", db_user.email)

    is_super = db_user.is_super_admin or is_admin_account
    perms = _perm_keys(db_user)
    safe = {
        "id": db_user.id,
        "email": db_user.email,
        "name": db_user.full_name,
        "role": "admin" if is_super else "user",
    }
    return MeResponse(
        user=safe,
        permissions=perms,
        is_super_admin=is_super,
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
    """Never expose reset credentials or whether an account exists."""
    # Check before account lookup/token creation so this error is identical for
    # existing, inactive and unknown addresses.
    if not smtp_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset email is currently unavailable. Please contact the portal administrator.",
        )
    user = await _load_user_by_email(db, body.email)
    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        reset_row = PasswordResetToken(token=token, user_id=user.id, expires_at=expires)
        db.add(reset_row)
        await db.commit()

        reset_url = _build_reset_url(request, token)
        message = password_reset_email(
            reset_url, user.full_name or "", expires_hours=RESET_TOKEN_TTL_HOURS,
        )
        sent = await send_email(user.email, message.subject, message.text, html_body=message.html)
        if not sent:
            # Do not leave a usable reset credential for an undelivered email.
            # Keep the public acknowledgement identical to unknown accounts:
            # a transport failure must not become an account-existence oracle.
            await db.delete(reset_row)
            await db.commit()
            log.warning("Password reset email was not delivered")

    return GenericOk(
        ok=True,
        message="If an active account uses this address, check its inbox for reset instructions. "
        "If no email arrives, contact the portal administrator.",
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
    from sqlalchemy import text as _text

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
                log.info("Restored is_super_admin=true for admin account %s", admin_email)
            # Belt-and-suspenders: also issue a raw SQL UPDATE in case the ORM
            # session has a stale view or the column type caused a comparison
            # mismatch.  This is idempotent and cheap.
            await db.execute(
                _text(
                    "UPDATE users SET is_super_admin = true, is_active = true,"
                    " updated_at = now() AT TIME ZONE 'utc'"
                    " WHERE lower(email) = lower(:email)"
                    "   AND (NOT is_super_admin OR NOT is_active)"
                ),
                {"email": admin_email},
            )
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

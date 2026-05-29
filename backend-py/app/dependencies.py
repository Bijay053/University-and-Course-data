"""Reusable FastAPI dependencies (auth, db re-export)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt

from app.config import settings
from app.database import get_db  # noqa: F401  (re-export for routers)


def _resolve_token(session: str | None, authorization: str | None) -> str | None:
    """Return the JWT from the session cookie, falling back to a Bearer token header."""
    if session:
        return session
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return None


async def get_current_user(
    session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Validate the session cookie (or Bearer token) and return the decoded
    user payload, or raise 401.
    """
    token = _resolve_token(session, authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc
    exp = payload.get("exp")
    if exp and datetime.now(timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return payload


async def get_optional_user(
    session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict | None:
    """Same as ``get_current_user`` but never raises; returns None if no/invalid token."""
    token = _resolve_token(session, authorization)
    if not token:
        return None
    try:
        return jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except JWTError:
        return None

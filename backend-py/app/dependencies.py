"""Reusable FastAPI dependencies (auth, db re-export)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from jose import JWTError, jwt

from app.config import settings
from app.database import get_db  # noqa: F401  (re-export for routers)


async def get_current_user(session: Annotated[str | None, Cookie()] = None) -> dict:
    """Validate the session cookie set by ``/api/auth/login`` and return the
    decoded user payload (or raise 401).
    """
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(session, settings.session_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        ) from exc
    exp = payload.get("exp")
    if exp and datetime.now(timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return payload


async def get_optional_user(session: Annotated[str | None, Cookie()] = None) -> dict | None:
    """Same as ``get_current_user`` but never raises; returns None if no/invalid cookie."""
    if not session:
        return None
    try:
        return jwt.decode(session, settings.session_secret, algorithms=["HS256"])
    except JWTError:
        return None

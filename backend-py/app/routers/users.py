"""Admin CRUD for users + per-user dynamic permissions.

All endpoints require ``users.manage`` (super admins always pass).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models.user import User, UserPermission
from app.permissions import ALL_KEYS, registry_payload, require_permission
from app.schemas.auth import (
    PermissionsUpdateBody,
    UserCreateBody,
    UserOut,
    UserUpdateBody,
)
from app.services.passwords import hash_password

router = APIRouter()


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        is_active=u.is_active,
        is_super_admin=u.is_super_admin,
    )


@router.get("/permissions/registry")
async def permissions_registry(
    _: Annotated[dict, Depends(get_current_user)],
) -> list[dict]:
    """Any authenticated user may read the registry (so the frontend can
    decide what to show in their own profile / who-am-I context)."""
    return registry_payload()


@router.get("/users", response_model=list[UserOut])
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> list[UserOut]:
    res = await db.execute(select(User).order_by(User.id.asc()))
    return [_to_out(u) for u in res.scalars().all()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> UserOut:
    bad = [k for k in body.permissions if k not in ALL_KEYS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown permission keys: {bad}")
    # Reject duplicates case-insensitively before opening a transaction.
    existing = await db.execute(
        select(User).where(func.lower(User.email) == body.email.lower())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    user = User(
        email=body.email,
        full_name=body.full_name or "",
        password_hash=hash_password(body.password),
        is_active=True,
        is_super_admin=body.is_super_admin,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email already in use") from exc
    for key in set(body.permissions):
        db.add(UserPermission(user_id=user.id, permission_key=key))
    await db.commit()
    await db.refresh(user)
    return _to_out(user)


async def _load(db: AsyncSession, user_id: int) -> User:
    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("users.manage"))],
) -> UserOut:
    user = await _load(db, user_id)
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.is_active is not None:
        if user.id == actor.get("id") and body.is_active is False:
            raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
        user.is_active = body.is_active
    if body.is_super_admin is not None:
        if user.id == actor.get("id") and body.is_super_admin is False:
            raise HTTPException(
                status_code=400, detail="You cannot remove your own super-admin status"
            )
        user.is_super_admin = body.is_super_admin
    if body.new_password:
        user.password_hash = hash_password(body.new_password)
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    return _to_out(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("users.manage"))],
) -> None:
    user = await _load(db, user_id)
    if user.id == actor.get("id"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    await db.delete(user)
    await db.commit()


@router.get("/users/{user_id}/permissions")
async def get_user_permissions(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> dict:
    user = await _load(db, user_id)
    return {
        "user_id": user.id,
        "is_super_admin": user.is_super_admin,
        "permissions": sorted(p.permission_key for p in user.permissions),
    }


@router.put("/users/{user_id}/permissions")
async def set_user_permissions(
    user_id: int,
    body: PermissionsUpdateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> dict:
    bad = [k for k in body.permissions if k not in ALL_KEYS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown permission keys: {bad}")
    user = await _load(db, user_id)
    desired = set(body.permissions)
    current = {p.permission_key: p for p in user.permissions}
    # Remove permissions no longer desired.
    for key, row in current.items():
        if key not in desired:
            await db.delete(row)
    # Add newly granted permissions.
    for key in desired - set(current):
        db.add(UserPermission(user_id=user.id, permission_key=key))
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    return {"user_id": user.id, "permissions": sorted(desired)}

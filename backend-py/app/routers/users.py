"""Admin CRUD for users, roles, and per-user dynamic permissions.

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
from app.models.user import Role, RolePermission, User, UserPermission
from app.permissions import ALL_KEYS, registry_payload, require_permission
from app.schemas.auth import (
    PermissionsUpdateBody,
    RoleCreateBody,
    RoleOut,
    RoleUpdateBody,
    UserCreateBody,
    UserOut,
    UserUpdateBody,
)
from app.services.passwords import hash_password

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _role_out(r: Role) -> RoleOut:
    return RoleOut(
        id=r.id,
        name=r.name,
        description=r.description,
        permissions=sorted(p.permission_key for p in r.permissions),
    )


def _to_out(u: User) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        is_active=u.is_active,
        is_super_admin=u.is_super_admin,
        role_id=u.role_id,
        role_name=u.role.name if u.role else None,
    )


async def _load_user(db: AsyncSession, user_id: int) -> User:
    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _load_role(db: AsyncSession, role_id: int) -> Role:
    res = await db.execute(select(Role).where(Role.id == role_id))
    role = res.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


# ---------------------------------------------------------------------------
# Permissions registry (any authenticated user may read)
# ---------------------------------------------------------------------------

@router.get("/permissions/registry")
async def permissions_registry(
    _: Annotated[dict, Depends(get_current_user)],
) -> list[dict]:
    return registry_payload()


# ---------------------------------------------------------------------------
# Roles CRUD
# ---------------------------------------------------------------------------

@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> list[RoleOut]:
    res = await db.execute(select(Role).order_by(Role.name.asc()))
    return [_role_out(r) for r in res.scalars().all()]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    body: RoleCreateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> RoleOut:
    bad = [k for k in body.permissions if k not in ALL_KEYS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown permission keys: {bad}")
    role = Role(name=body.name.strip(), description=body.description.strip())
    db.add(role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A role with that name already exists")
    for key in set(body.permissions):
        db.add(RolePermission(role_id=role.id, permission_key=key))
    await db.commit()
    await db.refresh(role)
    return _role_out(role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: int,
    body: RoleUpdateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> RoleOut:
    role = await _load_role(db, role_id)
    if body.name is not None:
        role.name = body.name.strip()
    if body.description is not None:
        role.description = body.description.strip()
    if body.permissions is not None:
        bad = [k for k in body.permissions if k not in ALL_KEYS]
        if bad:
            raise HTTPException(status_code=400, detail=f"Unknown permission keys: {bad}")
        desired = set(body.permissions)
        current = {p.permission_key: p for p in role.permissions}
        for key, row in current.items():
            if key not in desired:
                await db.delete(row)
        for key in desired - set(current):
            db.add(RolePermission(role_id=role.id, permission_key=key))
    role.updated_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A role with that name already exists")
    await db.refresh(role)
    return _role_out(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> None:
    role = await _load_role(db, role_id)
    await db.delete(role)
    await db.commit()


# ---------------------------------------------------------------------------
# Users CRUD
# ---------------------------------------------------------------------------

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
    if body.role_id is not None:
        await _load_role(db, body.role_id)
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
        role_id=body.role_id,
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


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdateBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    actor: Annotated[dict, Depends(require_permission("users.manage"))],
) -> UserOut:
    user = await _load_user(db, user_id)
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
    if body.role_id is not None:
        await _load_role(db, body.role_id)
        user.role_id = body.role_id
    elif "role_id" in body.model_fields_set and body.role_id is None:
        user.role_id = None
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
    user = await _load_user(db, user_id)
    if user.id == actor.get("id"):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    await db.delete(user)
    await db.commit()


# ---------------------------------------------------------------------------
# Per-user individual permissions (on top of role)
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/permissions")
async def get_user_permissions(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(require_permission("users.manage"))],
) -> dict:
    user = await _load_user(db, user_id)
    role_perms = sorted(p.permission_key for p in user.role.permissions) if user.role else []
    user_perms = sorted(p.permission_key for p in user.permissions)
    effective = sorted(set(role_perms) | set(user_perms))
    return {
        "user_id": user.id,
        "is_super_admin": user.is_super_admin,
        "role_id": user.role_id,
        "role_permissions": role_perms,
        "user_permissions": user_perms,
        "effective_permissions": effective,
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
    user = await _load_user(db, user_id)
    desired = set(body.permissions)
    current = {p.permission_key: p for p in user.permissions}
    for key, row in current.items():
        if key not in desired:
            await db.delete(row)
    for key in desired - set(current):
        db.add(UserPermission(user_id=user.id, permission_key=key))
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    role_perms = sorted(p.permission_key for p in user.role.permissions) if user.role else []
    effective = sorted(set(role_perms) | desired)
    return {
        "user_id": user.id,
        "role_permissions": role_perms,
        "user_permissions": sorted(desired),
        "effective_permissions": effective,
    }

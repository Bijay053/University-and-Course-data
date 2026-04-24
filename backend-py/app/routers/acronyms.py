"""Settings endpoints — reference data the admin UI uses for dropdowns.

Bug J fix: full CRUD on /academic-levels matching Node's response shape.
The React Settings → Academic Level Options page calls this endpoint
exactly the way Node served it — envelope `{options: [...]}`, camelCase
`sortOrder` / `createdAt`, plus a sibling `/reorder` for drag-handle
saves. Without the POST/PATCH/DELETE the admin UI showed
"405 Method Not Allowed" toasts on every action.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AcademicLevelOption, CourseAcronymOption

router = APIRouter()


# Mirror Node's seed list verbatim so a fresh DB renders the same dropdown
# the admin team is used to. We only insert when the table is empty so
# deliberate deletions are not resurrected.
_SEED_ACADEMIC_LEVELS: list[tuple[str, int]] = [
    ("High School Certificate", 1),
    ("Diploma / Advanced Diploma", 2),
    ("Bachelor's degree", 3),
    ("Bachelor's degree with Honours", 4),
    ("Graduate Certificate / Diploma", 5),
    ("Master's degree", 6),
    ("Master's degree or equivalent qualification in a relevant field", 7),
    ("Doctorate / PhD", 8),
    ("Associate Degree or Equivalent", 9),
]
_seeded = False


async def _ensure_seeded(db: AsyncSession) -> None:
    global _seeded
    if _seeded:
        return
    count = (
        await db.execute(select(func.count(AcademicLevelOption.id)))
    ).scalar_one() or 0
    if count == 0:
        for name, sort_order in _SEED_ACADEMIC_LEVELS:
            db.add(AcademicLevelOption(name=name, sort_order=sort_order))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
    _seeded = True


def _opt_to_dict(r: AcademicLevelOption) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "sortOrder": r.sort_order,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/academic-levels")
async def academic_levels_list(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    await _ensure_seeded(db)
    rows = (
        await db.execute(
            select(AcademicLevelOption).order_by(
                AcademicLevelOption.sort_order, AcademicLevelOption.id
            )
        )
    ).scalars().all()
    return {"options": [_opt_to_dict(r) for r in rows]}


@router.post("/academic-levels")
async def academic_levels_create(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    sort_order_raw = body.get("sortOrder")
    if sort_order_raw in (None, ""):
        next_sort = (
            await db.execute(
                select(func.coalesce(func.max(AcademicLevelOption.sort_order), 0) + 1)
            )
        ).scalar_one()
        sort_order = int(next_sort or 1)
    else:
        try:
            sort_order = int(sort_order_raw)
        except (TypeError, ValueError):
            sort_order = 0

    # Mirror Node's "ON CONFLICT (name) DO UPDATE SET sort_order = EXCLUDED.sort_order"
    # so re-adding a deleted-then-re-typed name updates the order rather
    # than 500-ing on the unique constraint.
    stmt = (
        pg_insert(AcademicLevelOption)
        .values(name=name, sort_order=sort_order)
        .on_conflict_do_update(
            index_elements=[AcademicLevelOption.name],
            set_={"sort_order": sort_order},
        )
        .returning(AcademicLevelOption)
    )
    try:
        row = (await db.execute(stmt)).scalar_one()
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e.orig)) from e
    return {"option": _opt_to_dict(row)}


@router.patch("/academic-levels/{opt_id}")
async def academic_levels_update(
    opt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    row = await db.get(AcademicLevelOption, opt_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    changed = False
    if isinstance(body.get("name"), str):
        new_name = body["name"].strip()
        if new_name:
            row.name = new_name
            changed = True
    if body.get("sortOrder") not in (None, ""):
        try:
            row.sort_order = int(body["sortOrder"])
            changed = True
        except (TypeError, ValueError):
            pass
    if not changed:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e.orig)) from e
    await db.refresh(row)
    return {"option": _opt_to_dict(row)}


@router.delete("/academic-levels/{opt_id}")
async def academic_levels_delete(
    opt_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await db.get(AcademicLevelOption, opt_id)
    if not row:
        return {"success": True, "deleted": 0}
    await db.delete(row)
    await db.commit()
    return {"success": True, "deleted": 1}


@router.post("/academic-levels/reorder")
async def academic_levels_reorder(
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not items:
        return {"success": True, "updated": 0}
    updated = 0
    # Single transaction so a partial reorder never leaves the list
    # half-renumbered (would corrupt the dropdown order in the admin UI).
    for item in items:
        try:
            opt_id = int(item.get("id"))
            sort_order = int(item.get("sortOrder"))
        except (TypeError, ValueError, AttributeError):
            continue
        row = await db.get(AcademicLevelOption, opt_id)
        if row is None:
            continue
        row.sort_order = sort_order
        updated += 1
    await db.commit()
    return {"success": True, "updated": updated}


@router.get("/acronyms")
async def acronyms(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    rows = (
        await db.execute(select(CourseAcronymOption).order_by(CourseAcronymOption.acronym))
    ).scalars().all()
    return [{"id": r.id, "acronym": r.acronym, "note": r.note} for r in rows]

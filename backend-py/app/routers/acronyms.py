"""Settings endpoints — reference data the admin UI uses for dropdowns."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AcademicLevelOption, CourseAcronymOption

router = APIRouter()


@router.get("/academic-levels")
async def academic_levels(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    rows = (
        await db.execute(
            select(AcademicLevelOption).order_by(
                AcademicLevelOption.sort_order, AcademicLevelOption.name
            )
        )
    ).scalars().all()
    return [{"id": r.id, "name": r.name, "sort_order": r.sort_order} for r in rows]


@router.get("/acronyms")
async def acronyms(db: Annotated[AsyncSession, Depends(get_db)]) -> list[dict]:
    rows = (
        await db.execute(select(CourseAcronymOption).order_by(CourseAcronymOption.acronym))
    ).scalars().all()
    return [{"id": r.id, "acronym": r.acronym, "note": r.note} for r in rows]

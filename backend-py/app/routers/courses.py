"""Course CRUD endpoints (read-heavy; writes guarded by auth)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, University
from app.schemas.course import CourseCreate, CourseListResponse, CourseRead, CourseUpdate

router = APIRouter()


@router.get("/courses")
async def list_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    university_id: int | None = None,
    q: str | None = None,
    degree_level: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> CourseListResponse:
    stmt = select(Course).join(University, Course.university_id == University.id)
    if university_id:
        stmt = stmt.where(Course.university_id == university_id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Course.name).like(like), func.lower(Course.description).like(like))
        )
    if degree_level:
        stmt = stmt.where(Course.degree_level == degree_level)
    if status_filter:
        stmt = stmt.where(Course.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(Course.updated_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    # LEFT JOIN fees + add camelCase fields the UI expects
    course_ids = [r.id for r in rows]
    fees_map: dict = {}
    if course_ids:
        from sqlalchemy import text
        fee_rows = (await db.execute(text("""
            SELECT DISTINCT ON (course_id) course_id, international_fee, fee_term, fee_year, currency
            FROM fees WHERE course_id = ANY(:ids)
            ORDER BY course_id, created_at DESC
        """), {"ids": course_ids})).all()
        for fr in fee_rows:
            fees_map[fr.course_id] = {
                "international_fee": fr.international_fee,
                "internationalFee": fr.international_fee,
                "fee_term": fr.fee_term,
                "feeTerm": fr.fee_term,
                "fee_year": fr.fee_year,
                "feeYear": fr.fee_year,
                "currency": fr.currency,
            }

    course_aliases = {
        "university_id": "universityId",
        "degree_level": "degreeLevel",
        "study_mode": "studyMode",
        "course_location": "courseLocation",
        "course_website": "courseWebsite",
        "duration_term": "durationTerm",
        "study_load": "studyLoad",
        "international_eligible": "internationalEligible",
        "on_campus_available": "onCampusAvailable",
        "delivery_mode": "deliveryMode",
        "student_market": "studentMarket",
        "eligibility_status": "eligibilityStatus",
        "approval_status": "approvalStatus",
        "approval_score": "approvalScore",
        "approved_at": "approvedAt",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
    }
    out = []
    for r in rows:
        d = {col.name: getattr(r, col.name, None) for col in r.__table__.columns}
        from datetime import datetime as _dt
        for k, v in list(d.items()):
            if isinstance(v, _dt):
                d[k] = v.isoformat()
        for snake, camel in course_aliases.items():
            if snake in d:
                d[camel] = d[snake]
        # Add camelCase aliases for course fields
        d["universityId"] = d.get("university_id")
        d["degreeLevel"] = d.get("degree_level")
        d["studyMode"] = d.get("study_mode")
        d["courseLocation"] = d.get("course_location")
        d["courseWebsite"] = d.get("course_website")
        d["durationTerm"] = d.get("duration_term")
        # Merge in fees (always include keys, even null)
        d.update(fees_map.get(r.id, {
            "international_fee": None, "internationalFee": None,
            "fee_term": None, "feeTerm": None,
            "fee_year": None, "feeYear": None,
            "currency": None,
        }))
        out.append(d)
    return JSONResponse(content={"data": out, "total": int(total), "page": page, "limit": limit})


@router.get("/courses/{course_id}", response_model=CourseRead)
async def get_course(course_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> CourseRead:
    c = await db.get(Course, course_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    return CourseRead.model_validate(c)


@router.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
async def create_course(
    body: CourseCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> CourseRead:
    # Bug #1 fix here too -- case-insensitive uniqueness within a university.
    dupe = (
        await db.execute(
            select(Course).where(
                Course.university_id == body.university_id,
                func.lower(Course.name) == body.name.lower(),
            )
        )
    ).scalar_one_or_none()
    if dupe:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Course '{dupe.name}' already exists for this university",
        )
    c = Course(**body.model_dump(exclude_none=True))
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return CourseRead.model_validate(c)


@router.patch("/courses/{course_id}", response_model=CourseRead)
async def update_course(
    course_id: int,
    body: CourseUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> CourseRead:
    c = await db.get(Course, course_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    await db.commit()
    await db.refresh(c)
    return CourseRead.model_validate(c)


@router.delete("/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> None:
    c = await db.get(Course, course_id)
    if not c:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    await db.delete(c)
    await db.commit()

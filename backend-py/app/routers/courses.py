"""Course CRUD endpoints (read-heavy; writes guarded by auth)."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, University
from app.schemas.course import CourseCreate, CourseListResponse, CourseRead, CourseUpdate

router = APIRouter()


def _f(v: object) -> object:
    """Convert Decimal → float so JSONResponse can serialize it."""
    return float(v) if isinstance(v, Decimal) else v


@router.get("/courses")
async def list_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    # B4: the OpenAPI spec declares these params in camelCase
    # (universityId, degreeLevel, studyMode, search, subCategory)
    # but FastAPI by default matches function parameter names verbatim.
    # Without the aliases below the client's ?universityId=11 was
    # silently dropped and the endpoint returned ALL 353 courses
    # instead of the 8 belonging to that university — that's the
    # "fake 353" the UI was showing in the Courses tab header.
    university_id: int | None = Query(default=None, alias="universityId"),
    q: str | None = Query(default=None, alias="search"),
    degree_level: str | None = Query(default=None, alias="degreeLevel"),
    study_mode: str | None = Query(default=None, alias="studyMode"),
    category: str | None = None,
    sub_category: str | None = Query(default=None, alias="subCategory"),
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
    if study_mode:
        stmt = stmt.where(Course.study_mode == study_mode)
    if category:
        stmt = stmt.where(Course.category == category)
    if sub_category:
        stmt = stmt.where(Course.sub_category == sub_category)
    if status_filter:
        stmt = stmt.where(Course.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(Course.updated_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()

    # LEFT JOIN fees + add camelCase fields the UI expects
    course_ids = [r.id for r in rows]
    fees_map: dict = {}
    eng_map: dict = {}
    if course_ids:
        from sqlalchemy import text
        fee_rows = (await db.execute(text("""
            SELECT DISTINCT ON (course_id) course_id, international_fee, fee_term, fee_year, currency
            FROM fees WHERE course_id = ANY(:ids)
            ORDER BY course_id, created_at DESC
        """), {"ids": course_ids})).all()
        for fr in fee_rows:
            fees_map[fr.course_id] = {
                "international_fee": _f(fr.international_fee),
                "internationalFee": _f(fr.international_fee),
                "fee_term": fr.fee_term,
                "feeTerm": fr.fee_term,
                "fee_year": _f(fr.fee_year),
                "feeYear": _f(fr.fee_year),
                "currency": fr.currency,
            }

        # B7: surface english_requirements rows on each course so the
        # UI's English Proficiency tab renders. The tab filters
        # `c.ieltsOverall || c.pteOverall || c.toeflOverall || ...`
        # — those camelCase fields didn't exist on the response, so
        # the tab was always empty even when 29 rows existed in the
        # english_requirements table for ASA's 8 courses.
        #
        # The table has multiple rows per course (one per test_type:
        # IELTS / PTE / TOEFL / Other), so we bucket by normalized
        # test_type. If the same course has duplicate rows for the
        # same test_type (shouldn't, but no UNIQUE constraint), the
        # newest wins via ORDER BY created_at DESC + first-write-wins.
        eng_rows = (await db.execute(text("""
            SELECT course_id, test_type, test_name,
                   listening, speaking, writing, reading, overall
            FROM english_requirements
            WHERE course_id = ANY(:ids)
            ORDER BY course_id, created_at DESC, id DESC
        """), {"ids": course_ids})).all()
        for er in eng_rows:
            bucket = eng_map.setdefault(er.course_id, {})
            tt = (er.test_type or "").upper().strip()
            # B7: use startswith so real-world variants like
            # "IELTS Academic", "TOEFL iBT", "PTE Academic" still
            # bucket correctly. Exact equality dropped them on prod.
            if tt.startswith("IELTS") and "ielts" not in bucket:
                bucket["ielts"] = er
            elif tt.startswith("PTE") and "pte" not in bucket:
                bucket["pte"] = er
            elif tt.startswith("TOEFL") and "toefl" not in bucket:
                bucket["toefl"] = er
            elif (
                not tt.startswith(("IELTS", "PTE", "TOEFL"))
                and "other" not in bucket
            ):
                bucket["other"] = er

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
            elif isinstance(v, Decimal):
                d[k] = float(v)
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

        # B7: surface english bands. Always emit ALL keys (with None
        # fallback) so the UI doesn't have to guard against undefined.
        # NOTE: asyncpg returns NUMERIC columns as Python Decimal objects.
        # Wrap every band value through _f() so they become plain floats
        # before JSONResponse tries to serialize the dict.
        b = eng_map.get(r.id, {})
        i = b.get("ielts"); p = b.get("pte"); t = b.get("toefl"); o = b.get("other")
        d["ieltsListening"] = _f(i.listening) if i else None
        d["ieltsSpeaking"]  = _f(i.speaking)  if i else None
        d["ieltsWriting"]   = _f(i.writing)   if i else None
        d["ieltsReading"]   = _f(i.reading)   if i else None
        d["ieltsOverall"]   = _f(i.overall)   if i else None
        d["pteListening"]   = _f(p.listening) if p else None
        d["pteSpeaking"]    = _f(p.speaking)  if p else None
        d["pteWriting"]     = _f(p.writing)   if p else None
        d["pteReading"]     = _f(p.reading)   if p else None
        d["pteOverall"]     = _f(p.overall)   if p else None
        d["toeflListening"] = _f(t.listening) if t else None
        d["toeflSpeaking"]  = _f(t.speaking)  if t else None
        d["toeflWriting"]   = _f(t.writing)   if t else None
        d["toeflReading"]   = _f(t.reading)   if t else None
        d["toeflOverall"]   = _f(t.overall)   if t else None
        # Other-test name falls back to test_type so e.g. test_type
        # "Cambridge" with no test_name still labels the row.
        d["otherEnglishTestName"] = (o.test_name if o else None) or (o.test_type if o else None)
        d["otherEnglishListening"] = _f(o.listening) if o else None
        d["otherEnglishSpeaking"]  = _f(o.speaking)  if o else None
        d["otherEnglishWriting"]   = _f(o.writing)   if o else None
        d["otherEnglishReading"]   = _f(o.reading)   if o else None
        d["otherEnglishOverall"]   = _f(o.overall)   if o else None
        # Safety pass: catch any remaining Decimal or datetime values
        # that may have been introduced after the initial conversion loop.
        from datetime import datetime as _dt2
        for k, v in list(d.items()):
            if isinstance(v, Decimal):
                d[k] = float(v)
            elif isinstance(v, _dt2):
                d[k] = v.isoformat()
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

"""University CRUD endpoints. Path layout mirrors the Node API exactly."""
from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, University
from app.schemas.course import CourseListResponse, CourseRead
from app.schemas.university import (
    UniversityCreate,
    UniversityListResponse,
    UniversityRead,
    UniversityUpdate,
)

router = APIRouter()


def _to_read(u: University, course_count: int = 0) -> UniversityRead:
    return UniversityRead.model_validate(
        {
            **{c.name: getattr(u, c.name) for c in u.__table__.columns},
            "course_count": course_count,
        }
    )


@router.get("/universities")
async def list_universities(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    country: str | None = None,
    city: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> UniversityListResponse:
    stmt = select(University, func.count(Course.id).label("course_count")).outerjoin(
        Course, Course.university_id == University.id
    )
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(University.name).like(like),
                func.lower(University.country).like(like),
                func.lower(University.city).like(like),
            )
        )
    if country:
        stmt = stmt.where(func.lower(University.country) == country.lower())
    if city:
        stmt = stmt.where(func.lower(University.city) == city.lower())
    stmt = stmt.group_by(University.id).order_by(
        desc(University.featured), desc(University.featured_priority), University.name
    )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).all()

    # Build response manually with both snake and camelCase keys
    aliases = {
        "scrape_url": "scrapeUrl",
        "fee_page_url": "feePageUrl",
        "requirements_page_url": "requirementsPageUrl",
        "academic_requirements_page_url": "academicRequirementsPageUrl",
        "scholarship_page_url": "scholarshipPageUrl",
        "logo_url": "logoUrl",
        "course_count": "courseCount",
        "featured_priority": "featuredPriority",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
    }
    out = []
    for u, cc in rows:
        d = {col.name: getattr(u, col.name, None) for col in u.__table__.columns}
        from datetime import datetime as _dt
        for k, v in list(d.items()):
            if isinstance(v, _dt):
                d[k] = v.isoformat()
        d["course_count"] = int(cc)
        for snake, camel in aliases.items():
            if snake in d:
                d[camel] = d[snake]
        out.append(d)
    return JSONResponse(content={
        "data": out,
        "total": int(total),
        "page": page,
        "limit": limit,
    })


@router.get("/universities/{uni_id}")
async def get_university(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> UniversityRead:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    cc_stmt = select(func.count(Course.id)).where(Course.university_id == uni_id)
    cc = (await db.execute(cc_stmt)).scalar_one()
    return _to_read(u, int(cc)).model_dump()


@router.get("/universities/{uni_id}/courses", response_model=CourseListResponse)
async def get_university_courses(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> CourseListResponse:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    stmt = select(Course).where(Course.university_id == uni_id)
    if status_filter:
        stmt = stmt.where(Course.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(desc(Course.updated_at)).offset((page - 1) * limit).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return CourseListResponse(
        data=[CourseRead.model_validate(r) for r in rows],
        total=int(total),
        page=page,
        limit=limit,
    )


@router.post("/universities", response_model=UniversityRead, status_code=status.HTTP_201_CREATED)
async def create_university(
    body: UniversityCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> UniversityRead:
    # Bug #1: case-insensitive name match -- prevents "Monash" / "monash" duplicates.
    existing_stmt = select(University).where(func.lower(University.name) == body.name.lower())
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "id": existing.id,
                "name": existing.name,
                "website": existing.website,
                "message": f"University '{existing.name}' already exists",
            },
        )
    # Bug #2: website URL uniqueness -- prevents duplicate universities that
    # happen to be spelled differently but share the same domain.
    if body.website:
        website_str = str(body.website)
        url_stmt = select(University).where(
            or_(
                University.website == website_str,
                University.scrape_url == website_str,
            )
        )
        existing_by_url = (await db.execute(url_stmt)).scalar_one_or_none()
        if existing_by_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "id": existing_by_url.id,
                    "name": existing_by_url.name,
                    "website": existing_by_url.website,
                    "message": f"A university with website '{website_str}' already exists",
                },
            )
    payload = body.model_dump(exclude_none=True)
    for url_key in (
        "website",
        "scrape_url",
    ):
        if url_key in payload and payload[url_key] is not None:
            payload[url_key] = str(payload[url_key])
    u = University(**payload)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return _to_read(u, 0)


@router.patch("/universities/{uni_id}", response_model=UniversityRead)
async def update_university(
    uni_id: int,
    body: UniversityUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> UniversityRead:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    payload = body.model_dump(exclude_none=True)
    if "name" in payload:
        dupe_stmt = select(University.id).where(
            func.lower(University.name) == payload["name"].lower(), University.id != uni_id
        )
        if (await db.execute(dupe_stmt)).first():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Name already in use")
    for k, v in payload.items():
        setattr(u, k, str(v) if hasattr(v, "unicode_string") else v)
    await db.commit()
    await db.refresh(u)
    return _to_read(u)


@router.patch("/universities/{uni_id}/featured")
async def update_university_featured(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[dict, Body(...)],
) -> dict:
    """Toggle the featured flag (and optional priority) used by the public
    Course Search ranking. Mirrors Node ``router.patch
    ("/universities/:id/featured", ...)`` so the React detail-page
    star button works without changes."""
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    payload = body if isinstance(body, dict) else {}
    u.featured = bool(payload.get("featured"))
    raw_priority = payload.get("featuredPriority")
    try:
        u.featured_priority = int(raw_priority) if raw_priority is not None else 0
    except (TypeError, ValueError):
        u.featured_priority = 0
    await db.commit()
    await db.refresh(u)
    return {c.name: getattr(u, c.name) for c in u.__table__.columns}


@router.post("/universities/bulk-import", status_code=status.HTTP_200_OK)
async def bulk_import_universities(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Bug #6 fix: CSV bulk import for universities.

    CSV must have a header row including at least: name, country, city.
    Optional columns: website, scrape_url, featured, featured_priority.
    Each row is validated through ``UniversityCreate`` so the same rules
    apply (no 'Unknown', dedupe by lowercase name).
    """
    if file.content_type and "csv" not in file.content_type and "text" not in file.content_type:
        raise HTTPException(status_code=400, detail="File must be a CSV")

    MAX_BYTES = 5 * 1024 * 1024  # 5 MB hard cap (~50k rows)
    raw = await file.read(MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not UTF-8 text") from None

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    headers = {h.strip().lower() for h in reader.fieldnames if h}
    required = {"name", "country", "city"}
    missing = required - headers
    if missing:
        raise HTTPException(
            status_code=400, detail=f"Missing columns: {', '.join(sorted(missing))}"
        )

    created = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for line_no, row in enumerate(reader, start=2):  # header is line 1
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not any(clean.values()):
            continue

        body_payload: dict[str, Any] = {
            "name": clean.get("name", ""),
            "country": clean.get("country", ""),
            "city": clean.get("city", ""),
        }
        for opt in ("website", "scrape_url"):
            if clean.get(opt):
                body_payload[opt] = clean[opt]
        if clean.get("featured"):
            body_payload["featured"] = clean["featured"].lower() in {"1", "true", "yes", "y"}
        if clean.get("featured_priority"):
            try:
                body_payload["featured_priority"] = int(clean["featured_priority"])
            except ValueError:
                pass

        try:
            body = UniversityCreate(**body_payload)
        except ValidationError as ve:
            errors.append({"line": line_no, "name": body_payload.get("name"), "error": ve.errors()[0]["msg"]})
            continue

        existing_stmt = select(University.id).where(
            func.lower(University.name) == body.name.lower()
        )
        if (await db.execute(existing_stmt)).first():
            skipped += 1
            continue

        payload = body.model_dump(exclude_none=True)
        for url_key in ("website", "scrape_url"):
            if url_key in payload and payload[url_key] is not None:
                payload[url_key] = str(payload[url_key])
        db.add(University(**payload))
        created += 1

    try:
        await db.commit()
    except Exception as exc:  # IntegrityError, disconnect, etc.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Bulk import failed at commit: {exc.__class__.__name__}",
        ) from exc
    return {"created": created, "skipped": skipped, "errors": errors}


@router.delete("/universities/{uni_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_university(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> None:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    await db.delete(u)
    await db.commit()


def _to_camel_uni(u) -> dict:
    """Add camelCase aliases UI expects: scrapeUrl, feePageUrl, etc."""
    if hasattr(u, '__table__'):
        d = {c.name: getattr(u, c.name, None) for c in u.__table__.columns}
    elif isinstance(u, dict):
        d = dict(u)
    else:
        return u
    # Convert datetimes to iso
    from datetime import datetime
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    # Add camelCase aliases
    aliases = {
        'scrape_url': 'scrapeUrl',
        'fee_page_url': 'feePageUrl',
        'requirements_page_url': 'requirementsPageUrl',
        'academic_requirements_page_url': 'academicRequirementsPageUrl',
        'scholarship_page_url': 'scholarshipPageUrl',
        'logo_url': 'logoUrl',
        'course_count': 'courseCount',
        'featured_priority': 'featuredPriority',
        'created_at': 'createdAt',
        'updated_at': 'updatedAt',
    }
    for snake, camel in aliases.items():
        if snake in d:
            d[camel] = d[snake]
    return d

"""University CRUD endpoints. Path layout mirrors the Node API exactly."""
from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, University
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


@router.get("/universities", response_model=UniversityListResponse)
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

    return UniversityListResponse(
        data=[_to_read(u, cc) for u, cc in rows],
        total=int(total),
        page=page,
        limit=limit,
    )


@router.get("/universities/{uni_id}", response_model=UniversityRead)
async def get_university(uni_id: int, db: Annotated[AsyncSession, Depends(get_db)]) -> UniversityRead:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    cc_stmt = select(func.count(Course.id)).where(Course.university_id == uni_id)
    cc = (await db.execute(cc_stmt)).scalar_one()
    return _to_read(u, int(cc))


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
            detail=f"University '{existing.name}' already exists",
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

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
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

    await db.commit()
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

"""Public course search endpoints. Reads from the existing materialized view
``course_search_view`` so the user-facing search behaviour stays identical
to Node. The view is created/refreshed by the existing Drizzle migrations.

View columns we rely on (verified live):
    id, course_name, university_id, university_name, university_country,
    university_city, degree_level, course_location, duration, duration_term,
    international_fee, ielts_overall, intakes, search_tsv
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.search import (
    SearchCourseResponse,
    SearchCourseRow,
    SearchOptionsResponse,
    SearchStatsResponse,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/courses")
async def search_courses(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    country: str | None = None,
    city: str | None = None,
    university_id: int | None = None,
    degree_level: str | None = None,
    intake_month: str | None = None,
    max_fee: float | None = None,
    max_ielts: float | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> SearchCourseResponse:
    where: list[str] = []
    params: dict = {}
    if q:
        # search_tsv is a precomputed tsvector on the MV; fall back to ILIKE on course_name.
        where.append(
            "(c.search_tsv @@ plainto_tsquery('english', :q) OR lower(c.course_name) ILIKE :ql)"
        )
        params["q"] = q
        params["ql"] = f"%{q.lower()}%"
    if country:
        where.append("lower(c.university_country) = lower(:country)")
        params["country"] = country
    if city:
        where.append("lower(c.university_city) = lower(:city)")
        params["city"] = city
    if university_id:
        where.append("c.university_id = :uid")
        params["uid"] = university_id
    if degree_level:
        where.append("c.degree_level = :dl")
        params["dl"] = degree_level
    if intake_month:
        where.append(":im = ANY(c.intakes)")
        params["im"] = intake_month
    if max_fee is not None:
        where.append("(c.international_fee IS NULL OR c.international_fee <= :max_fee)")
        params["max_fee"] = max_fee
    if max_ielts is not None:
        where.append("(c.ielts_overall IS NULL OR c.ielts_overall <= :max_ielts)")
        params["max_ielts"] = max_ielts

    where_sql = " AND ".join(where) if where else "TRUE"

    rank_select = (
        "ts_rank(c.search_tsv, plainto_tsquery('english', :q)) AS rank" if q else "NULL AS rank"
    )

    base_sql = f"""
        SELECT c.id           AS course_id,
               c.course_name,
               c.university_id,
               c.university_name,
               c.degree_level,
               c.course_location,
               c.duration,
               c.duration_term,
               c.international_fee,
               c.ielts_overall,
               c.intakes      AS intake_months,
               {rank_select}
        FROM course_search_view c
        WHERE {where_sql}
        ORDER BY {"rank DESC NULLS LAST, " if q else ""}c.course_name
        LIMIT :limit OFFSET :offset
    """
    count_sql = f"SELECT COUNT(*) FROM course_search_view c WHERE {where_sql}"

    params["limit"] = limit
    params["offset"] = (page - 1) * limit

    try:
        rows = (await db.execute(text(base_sql), params)).mappings().all()
        total = (await db.execute(text(count_sql), params)).scalar_one()
    except Exception as exc:
        # Surface DB errors in logs (don't silently mask) but never 500 the search page.
        log.error("search_courses SQL failed: %s", exc)
        return SearchCourseResponse(results=[], total=0, page=page, limit=limit)

    aliases = {
        "course_id": "courseId",
        "course_name": "courseName",
        "course_location": "courseLocation",
        "university_id": "universityId",
        "university_name": "universityName",
        "degree_level": "degreeLevel",
        "duration_term": "durationTerm",
        "international_fee": "internationalFee",
        "ielts_overall": "ieltsOverall",
        "intake_months": "intakeMonths",
    }
    out = []
    for r in rows:
        d = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
        for snake, camel in aliases.items():
            if snake in d:
                d[camel] = d[snake]
        # Ensure internationalFee always exists (UI calls .toLocaleString)
        if d.get("internationalFee") is None:
            d["internationalFee"] = 0
            d["international_fee"] = 0
        out.append(d)
    return JSONResponse(content={
        "results": out,
        "total": int(total or 0),
        "page": page,
        "limit": limit,
    })


@router.get("/options", response_model=SearchOptionsResponse)
async def search_options(db: Annotated[AsyncSession, Depends(get_db)]) -> SearchOptionsResponse:
    try:
        countries = (
            (
                await db.execute(
                    text(
                        "SELECT DISTINCT country FROM universities "
                        "WHERE country IS NOT NULL AND lower(country) <> 'unknown' "
                        "ORDER BY country"
                    )
                )
            )
            .scalars()
            .all()
        )
        cities = (
            (
                await db.execute(
                    text(
                        "SELECT DISTINCT city FROM universities "
                        "WHERE city IS NOT NULL AND lower(city) <> 'unknown' "
                        "ORDER BY city"
                    )
                )
            )
            .scalars()
            .all()
        )
        unis = (
            await db.execute(text("SELECT id, name FROM universities ORDER BY name"))
        ).mappings().all()
        degree_levels = (
            (
                await db.execute(
                    text(
                        "SELECT DISTINCT degree_level FROM courses "
                        "WHERE degree_level IS NOT NULL ORDER BY degree_level"
                    )
                )
            )
            .scalars()
            .all()
        )
        intake_months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    except Exception as exc:
        log.error("search_options SQL failed: %s", exc)
        return SearchOptionsResponse()

    return SearchOptionsResponse(
        countries=list(countries),
        cities=list(cities),
        universities=[dict(u) for u in unis],
        degree_levels=list(degree_levels),
        intake_months=intake_months,
    )


@router.get("/stats", response_model=SearchStatsResponse)
async def search_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> SearchStatsResponse:
    try:
        total_unis = (await db.execute(text("SELECT COUNT(*) FROM universities"))).scalar_one()
        total_courses = (
            await db.execute(
                text("SELECT COUNT(*) FROM courses WHERE status = 'active'")
            )
        ).scalar_one()
        countries = (
            await db.execute(text("SELECT COUNT(DISTINCT country) FROM universities"))
        ).scalar_one()
        avg_fee = (
            await db.execute(
                text(
                    "SELECT AVG(international_fee) FROM fees WHERE international_fee IS NOT NULL"
                )
            )
        ).scalar_one()
    except Exception as exc:
        log.error("search_stats SQL failed: %s", exc)
        return SearchStatsResponse()

    return SearchStatsResponse(
        total_universities=int(total_unis or 0),
        total_courses=int(total_courses or 0),
        countries=int(countries or 0),
        average_fee=float(avg_fee) if avg_fee is not None else None,
    )

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
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
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
    # B5: the /search UI sends ~14 filter params that the previous
    # implementation silently ignored. The MV `course_search_view`
    # actually has the columns we need for most of them (location,
    # intakes, fee, duration_years, per-exam overall scores,
    # category/sub_category) — they were just never wired. The ones
    # that DO require a join (per-band English bands beyond overall,
    # academic/grading filters, country_residence, other_exam) stay
    # accepted-but-noop for now so the UI keeps building URLs without
    # 422s and we can ship them in a follow-up. See bottom of handler.
    location: str | None = None,
    intakes: str | None = None,         # CSV: "Spring,Fall"
    fee_min: float | None = None,
    fee_max: float | None = None,
    duration_years_min: float | None = None,
    duration_years_max: float | None = None,
    english_exam: str | None = None,    # IELTS|PTE|TOEFL|CAE|DUOLINGO
    english_overall: float | None = None,
    category: str | None = None,
    sub_category: str | None = None,
    sort: str | None = None,            # relevance|fee_asc|fee_desc|duration|name
    # Accepted-but-noop (require academic_requirements/english_requirements join):
    english_reading: float | None = None,        # noqa: ARG001
    english_writing: float | None = None,        # noqa: ARG001
    english_listening: float | None = None,      # noqa: ARG001
    english_speaking: float | None = None,       # noqa: ARG001
    country_residence: str | None = None,        # noqa: ARG001
    highest_qualification: str | None = None,    # noqa: ARG001
    grading_scheme: str | None = None,           # noqa: ARG001
    grading_out_of: str | None = None,           # noqa: ARG001
    grading_score: str | None = None,            # noqa: ARG001
    other_exam: str | None = None,               # noqa: ARG001
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

    # B5: location is a free-text city/region match. UI feeds either a
    # university_city OR a course_location string ("Sydney Campus"),
    # so OR them with case-insensitive ILIKE on both columns.
    if location and location.strip():
        where.append(
            "(lower(c.university_city) ILIKE :loc OR lower(c.course_location) ILIKE :loc)"
        )
        params["loc"] = f"%{location.strip().lower()}%"

    # B5: intakes CSV → array overlap. Empty tokens after split are
    # ignored so "?intakes=" is a no-op rather than a "match nothing" bug.
    if intakes:
        intake_list = [s.strip() for s in intakes.split(",") if s.strip()]
        if intake_list:
            where.append("c.intakes && (:intake_list)::text[]")
            params["intake_list"] = intake_list

    # B5: fee_min/fee_max are the slider's two ends. NULLs always
    # match the lower bound (so "fee_min=0" doesn't drop unpriced
    # courses) but only match the upper bound when the slider is at
    # its max — handled UI-side by omitting the param entirely.
    if fee_min is not None and fee_min > 0:
        where.append("(c.international_fee IS NOT NULL AND c.international_fee >= :fee_min)")
        params["fee_min"] = fee_min
    if fee_max is not None:
        where.append("(c.international_fee IS NULL OR c.international_fee <= :fee_max)")
        params["fee_max"] = fee_max

    # B5: duration_years already pre-computed on the MV (real column
    # `duration_years`). NULL is treated as "matches" so courses with
    # missing duration data aren't silently dropped.
    if duration_years_min is not None and duration_years_min > 0:
        where.append("(c.duration_years IS NULL OR c.duration_years >= :dy_min)")
        params["dy_min"] = duration_years_min
    if duration_years_max is not None:
        where.append("(c.duration_years IS NULL OR c.duration_years <= :dy_max)")
        params["dy_max"] = duration_years_max

    # B5: english_exam picks which of the per-exam overall columns to
    # filter on. The MV has overall scores for all five tests
    # (ielts/pte/toefl/cae/duolingo). english_overall is the user's
    # achievable score → keep courses requiring AT MOST that.
    if english_exam and english_overall is not None:
        col_map = {
            "IELTS": "c.ielts_overall",
            "PTE": "c.pte_overall",
            "TOEFL": "c.toefl_overall",
            "CAE": "c.cae_overall",
            "DUOLINGO": "c.duolingo_overall",
        }
        col = col_map.get(english_exam.upper().strip())
        if col:
            where.append(f"({col} IS NULL OR {col} <= :eng_overall)")
            params["eng_overall"] = english_overall

    if category:
        where.append("c.category = :cat")
        params["cat"] = category
    if sub_category:
        where.append("c.sub_category = :sub_cat")
        params["sub_cat"] = sub_category

    where_sql = " AND ".join(where) if where else "TRUE"

    # B5: sort param. Default behaviour (no sort) preserves the
    # legacy "relevance when q present, alphabetical otherwise" order.
    sort_clause = (
        ("rank DESC NULLS LAST, " if q else "") + "c.course_name"
    )
    if sort:
        s = sort.strip().lower()
        if s == "fee_asc":
            sort_clause = "c.international_fee ASC NULLS LAST, c.course_name"
        elif s == "fee_desc":
            sort_clause = "c.international_fee DESC NULLS LAST, c.course_name"
        elif s in ("duration", "duration_asc"):
            sort_clause = "c.duration_years ASC NULLS LAST, c.course_name"
        elif s == "duration_desc":
            sort_clause = "c.duration_years DESC NULLS LAST, c.course_name"
        elif s in ("name", "alpha"):
            sort_clause = "c.course_name ASC"
        # else fall through to relevance/alpha default

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
               c.pte_overall,
               c.toefl_overall,
               c.cae_overall,
               c.duolingo_overall,
               pte_er.listening AS pte_listening,
               pte_er.writing  AS pte_writing,
               c.intakes      AS intake_months,
               {rank_select}
        FROM course_search_view c
        LEFT JOIN english_requirements pte_er
               ON pte_er.course_id = c.id AND pte_er.test_type = 'PTE'
        WHERE {where_sql}
        ORDER BY {sort_clause}
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

        # Required by UI: id (alias for course_id)
        d["id"] = d.get("course_id")

        # Required by UI: result.intakes (always array)
        d["intakes"] = d.get("intake_months") or []

        # Required by UI: nested university object
        d["university"] = {
            "id": d.get("university_id"),
            "name": d.get("university_name") or "",
            "city": d.get("uni_city") or "",
            "country": d.get("uni_country") or "",
            "featured": bool(d.get("uni_featured") or False),
            "logo_url": d.get("uni_logo_url"),
            "logoUrl": d.get("uni_logo_url"),
        }

        # Required by UI: english_requirements nested object.
        # pte_overall / toefl_overall / cae_overall / duolingo_overall all
        # live in course_search_view alongside ielts_overall — previously
        # they were hardcoded to None here, which prevented the search card
        # from showing PTE/TOEFL badges even when data existed.
        d["english_requirements"] = {
            "ielts_overall": d.get("ielts_overall"),
            "pte_overall": d.get("pte_overall"),
            "pte_listening": d.get("pte_listening"),
            "pte_writing": d.get("pte_writing"),
            "toefl_overall": d.get("toefl_overall"),
            "cae_overall": d.get("cae_overall"),
            "duolingo_overall": d.get("duolingo_overall"),
        }

        # Currency / fee_term / fee_yearly — UI reads them on the result
        d.setdefault("currency", "AUD")
        d.setdefault("fee_term", "Year")
        if d.get("international_fee") is None:
            d["international_fee"] = 0
            d["internationalFee"] = 0
        d.setdefault("international_fee_yearly", d.get("international_fee") or 0)
        d.setdefault("internationalFeeYearly", d.get("international_fee") or 0)

        # Optional fields UI checks (with falsy guards but better defined)
        d.setdefault("category", None)
        d.setdefault("course_url", d.get("course_website"))
        d.setdefault("courseUrl", d.get("course_website"))
        out.append(d)
    # Build facets — UI expects {facets: {intakes, degree_levels, locations, universities}}
    # Each facet item: {name, count}. Use simple aggregate query.
    from collections import Counter
    intake_counter: Counter = Counter()
    degree_counter: Counter = Counter()
    location_counter: Counter = Counter()
    uni_counter: Counter = Counter()
    for d in out:
        for m in (d.get("intake_months") or []):
            if m: intake_counter[m] += 1
        if d.get("degree_level"): degree_counter[d["degree_level"]] += 1
        if d.get("course_location"): location_counter[d["course_location"]] += 1
        if d.get("university_name"): uni_counter[d["university_name"]] += 1

    facets = {
        "intakes": [{"name": k, "count": v} for k, v in intake_counter.most_common()],
        "degreeLevels": [{"name": k, "count": v} for k, v in degree_counter.most_common()],
        "degree_levels": [{"name": k, "count": v} for k, v in degree_counter.most_common()],
        "locations": [{"name": k, "count": v} for k, v in location_counter.most_common()],
        "universities": [{"name": k, "count": v} for k, v in uni_counter.most_common()],
    }
    return JSONResponse(content=jsonable_encoder({
        "results": out,
        "total": int(total or 0),
        "page": page,
        "limit": limit,
        "facets": facets,
    }))


@router.get("/compare")
async def search_compare(
    db: Annotated[AsyncSession, Depends(get_db)],
    ids: str = Query(..., description="CSV of course ids, max 5 — `?ids=1,2,3`"),
) -> JSONResponse:
    """Course-comparison payload for the `/compare` UI page.

    Ports Node ``GET /api/search/compare`` (``artifacts/api-server/src/
    routes/search.ts:644``). The UI calls this with up to 5 course ids and
    expects ``{courses: [...]}`` where each course bundles the materialised
    view row plus its full english + academic requirement lists.

    Without this endpoint the React Compare page (``pages/compare.tsx:77``)
    receives 404 and shows ``error: "Not Found"`` — the only P0 missing
    endpoint identified in MIGRATION_AUDIT.md.
    """
    raw = [s.strip() for s in (ids or "").split(",") if s.strip()]
    if not raw:
        return JSONResponse(
            status_code=400,
            content={"error": "ids_required", "message": "Provide ids=1,2,3 (max 5)"},
        )
    # Mirror Node's tolerant `map(Number).filter(Number.isInteger && >0)`
    # behaviour: silently drop non-numeric tokens and only return
    # ``ids_invalid`` when *nothing* parsed. ``?ids=1,abc`` therefore
    # succeeds with course 1 — the architect review caught this divergence.
    int_ids: list[int] = []
    for s in raw:
        try:
            n = int(s)
        except ValueError:
            continue
        if n > 0:
            int_ids.append(n)
    if not int_ids:
        return JSONResponse(status_code=400, content={"error": "ids_invalid"})
    if len(int_ids) > 5:
        return JSONResponse(
            status_code=400,
            content={"error": "too_many_ids", "message": "Compare supports at most 5 courses"},
        )

    try:
        mv_rows = (
            await db.execute(
                text("SELECT * FROM course_search_view WHERE id = ANY(:ids)"),
                {"ids": int_ids},
            )
        ).mappings().all()
        eng_rows = (
            await db.execute(
                text(
                    "SELECT course_id, test_type, test_name, overall, "
                    "listening, reading, writing, speaking "
                    "FROM english_requirements WHERE course_id = ANY(:ids)"
                ),
                {"ids": int_ids},
            )
        ).mappings().all()
        acad_rows = (
            await db.execute(
                text(
                    "SELECT course_id, academic_level, academic_score, "
                    "score_type, academic_country "
                    "FROM academic_requirements WHERE course_id = ANY(:ids)"
                ),
                {"ids": int_ids},
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001 — match Node's 500 fallback
        log.error("search_compare SQL failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "compare_failed", "message": str(exc)},
        )

    eng_by_course: dict[int, list[dict]] = {}
    for r in eng_rows:
        d = dict(r)
        eng_by_course.setdefault(int(d["course_id"]), []).append(d)
    acad_by_course: dict[int, list[dict]] = {}
    for r in acad_rows:
        d = dict(r)
        acad_by_course.setdefault(int(d["course_id"]), []).append(d)

    # Preserve the request order — UI renders columns left-to-right in this order.
    by_id = {int(r["id"]): dict(r) for r in mv_rows}
    courses: list[dict] = []
    for cid in int_ids:
        r = by_id.get(cid)
        if not r:
            continue
        # ``international_fee_yearly`` is read straight from the view
        # column when present. Node does the same — its
        # ``r.international_fee_yearly == null ? null : Number(...)`` line
        # reduces to ``null`` whenever the view didn't compute the value,
        # so we mirror that exactly instead of inventing a yearly figure
        # from the raw fee (which would be wrong for Full Course / Total
        # / Trimester fee terms).
        intl_fee = r.get("international_fee")
        intl_fee_yearly_raw = r.get("international_fee_yearly")
        intl_fee_yearly = (
            None if intl_fee_yearly_raw is None else float(intl_fee_yearly_raw)
        )
        courses.append(
            {
                "id": r.get("id"),
                "course_name": r.get("course_name"),
                "university": {
                    "id": r.get("university_id"),
                    "name": r.get("university_name"),
                    "logo_url": r.get("logo_url"),
                    "city": r.get("university_city"),
                    "country": r.get("university_country"),
                    "website": r.get("university_website"),
                },
                "course_location": r.get("course_location"),
                "degree_level": r.get("degree_level"),
                "category": r.get("category"),
                "sub_category": r.get("sub_category"),
                "duration": r.get("duration"),
                "duration_term": r.get("duration_term"),
                "duration_years": r.get("duration_years"),
                "study_mode": r.get("study_mode"),
                "intakes": r.get("intakes") or [],
                "international_fee": intl_fee,
                "international_fee_yearly": intl_fee_yearly,
                "currency": r.get("currency"),
                "fee_term": r.get("fee_term"),
                "application_fee": r.get("application_fee"),
                "course_url": r.get("course_website"),
                "english_requirements": eng_by_course.get(cid, []),
                "academic_requirements": acad_by_course.get(cid, []),
            }
        )

    return JSONResponse(content=jsonable_encoder({"courses": courses}))


@router.get("/options")
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
        return JSONResponse(content={"countries": [], "cities": [], "universities": [], "degree_levels": [], "degreeLevels": [], "intake_months": [], "intakeMonths": []})

    return JSONResponse(content=jsonable_encoder({
        "countries": list(countries),
        "cities": list(cities),
        "universities": [dict(u) for u in unis],
        "degree_levels": list(degree_levels), "degreeLevels": list(degree_levels),
        "intake_months": intake_months, "intakeMonths": intake_months,
    }))


@router.get("/stats")
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
        return JSONResponse(content={"total_universities": 0, "totalUniversities": 0, "total_courses": 0, "totalCourses": 0, "universities_with_courses": 0, "universitiesWithCourses": 0, "countries": 0, "average_fee": 0, "averageFee": 0})

    tu = int(total_unis or 0)
    tc = int(total_courses or 0)
    co = int(countries or 0)
    af = float(avg_fee) if avg_fee is not None else 0
    # Count unis that actually have courses (not just total registered)
    uwc = (await db.execute(text(
        "SELECT COUNT(DISTINCT university_id) FROM courses WHERE status = 'active'"
    ))).scalar_one()
    uwc = int(uwc or 0)
    return JSONResponse(content={
        "total_universities": tu, "totalUniversities": tu,
        "total_courses": tc, "totalCourses": tc,
        "universities_with_courses": uwc, "universitiesWithCourses": uwc,
        "countries": co,
        "average_fee": af, "averageFee": af,
    })

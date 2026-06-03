"""University CRUD endpoints. Path layout mirrors the Node API exactly."""
from __future__ import annotations

import copy
import csv
import io
import re
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import yaml as _yaml_mod

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import json

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.models import Course, University
from app.permissions import require_permission
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
    search: str | None = None,
    country: str | None = None,
    city: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> UniversityListResponse:
    stmt = select(University, func.count(Course.id).label("course_count")).outerjoin(
        Course, Course.university_id == University.id
    )
    if search:
        like = f"%{search.lower()}%"
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


@router.get("/universities/cert-dashboard")
async def get_cert_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return cert-status summary counts + per-university health-score rows.

    Health score uses the same 40/30/30 formula as the scrape-agent config
    endpoint so numbers are consistent across the product.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT
                    u.id,
                    u.name,
                    u.country,
                    u.scrape_url,
                    COALESCE(u.certification_status, 'draft') AS certification_status,
                    u.last_certified_score,
                    u.last_certified_at,
                    j.runtime_job_id,
                    j.total_found,
                    j.imported,
                    j.created_at            AS last_scrape_at,
                    j.avg_completeness,
                    sj.scrape_config
                FROM universities u
                LEFT JOIN LATERAL (
                    SELECT
                        rj.runtime_job_id,
                        rj.total_found,
                        rj.imported,
                        rj.created_at,
                        (
                            SELECT ROUND(AVG(sc.completeness)::numeric, 1)
                            FROM scraped_courses sc
                            WHERE sc.scrape_job_id = rj.runtime_job_id
                              AND sc.completeness IS NOT NULL
                        ) AS avg_completeness
                    FROM scrape_runtime_jobs rj
                    WHERE rj.university_id = u.id
                    ORDER BY rj.created_at DESC
                    LIMIT 1
                ) j ON true
                LEFT JOIN LATERAL (
                    SELECT scrape_config
                    FROM scraping_jobs
                    WHERE university_id = u.id
                    ORDER BY id DESC
                    LIMIT 1
                ) sj ON true
                ORDER BY u.name
                """
            )
        )
    ).mappings().all()

    universities_out = []
    summary: dict[str, int] = {
        "certified": 0, "testing": 0, "needs_review": 0, "failed": 0, "draft": 0,
    }

    for r in rows:
        cert_status = r["certification_status"] or "draft"
        summary[cert_status] = summary.get(cert_status, 0) + 1

        admin_cfg: dict = {}
        sc = r["scrape_config"]
        if sc and isinstance(sc, dict):
            admin_cfg = sc.get("admin", {})

        min_expected = int(admin_cfg.get("_min_expected_courses") or 0)
        found = int(r["total_found"] or 0)
        imported = int(r["imported"] or 0)
        avg_comp = float(r["avg_completeness"] or 0)

        score_found = (
            40 * min(found / max(min_expected, 1), 1.0)
            if min_expected
            else (40 if found >= 10 else 40 * found / 10)
        )
        score_comp = 30 * min(avg_comp / 100.0, 1.0)
        score_stage = 30 * min(imported / max(found, 1), 1.0) if found else 0
        current_score = round(score_found + score_comp + score_stage)

        last_cert = r["last_certified_score"]
        score_drop = (int(last_cert) - current_score) if last_cert is not None else None

        universities_out.append({
            "id": r["id"],
            "name": r["name"],
            "country": r["country"],
            "scrape_url": r["scrape_url"],
            "certification_status": cert_status,
            "last_certified_score": last_cert,
            "last_certified_at": r["last_certified_at"].isoformat() if r["last_certified_at"] else None,
            "current_health_score": current_score,
            "score_drop": score_drop,
            "last_scrape_at": r["last_scrape_at"].isoformat() if r["last_scrape_at"] else None,
            "staged_courses": imported,
            "total_found": found,
        })

    return {"summary": summary, "universities": universities_out}


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
    _u: Annotated[dict, Depends(require_permission("universities.create"))],
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
    # Default scrape_url to website so the scraper can find newly-created unis.
    if not payload.get("scrape_url") and payload.get("website"):
        payload["scrape_url"] = payload["website"]
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
    _user: Annotated[dict, Depends(require_permission("universities.edit"))],
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
    _u: Annotated[dict, Depends(require_permission("universities.edit"))],
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
    _user: Annotated[dict, Depends(require_permission("bulk.import"))],
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


_CERT_STATUSES = ("draft", "testing", "certified", "needs_review", "failed")


@router.get("/universities/{uni_id}/certification-status")
async def get_certification_status(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return current certification status + last certified score/date."""
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")
    return {
        "university_id": uni_id,
        "certification_status": u.certification_status,
        "last_certified_score": u.last_certified_score,
        "last_certified_at": u.last_certified_at.isoformat() if u.last_certified_at else None,
        "available_statuses": list(_CERT_STATUSES),
    }


@router.patch("/universities/{uni_id}/certification-status")
async def update_certification_status(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Manually set the certification status for a university.

    Body: { "status": "certified" | "draft" | "testing" | "needs_review" | "failed",
            "score": <int, optional — current certification score> }
    """
    from datetime import datetime, timezone

    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")
    new_status: str = body.get("status", "")
    if new_status not in _CERT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{new_status}'. Must be one of: {', '.join(_CERT_STATUSES)}",
        )
    u.certification_status = new_status
    if new_status == "certified":
        score = body.get("score")
        if score is not None:
            u.last_certified_score = int(score)
        u.last_certified_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "university_id": uni_id,
        "certification_status": u.certification_status,
        "last_certified_score": u.last_certified_score,
        "last_certified_at": u.last_certified_at.isoformat() if u.last_certified_at else None,
    }


@router.delete("/universities/{uni_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_university(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(require_permission("universities.delete"))],
) -> None:
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")
    await db.delete(u)
    await db.commit()


@router.delete(
    "/universities/{uni_id}/central-cache",
    status_code=status.HTTP_200_OK,
    summary="Invalidate central-page cache for a university",
)
async def invalidate_university_central_cache(
    uni_id: int,
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Delete all central_page_cache rows for this university.

    Forces the next scrape to re-fetch and re-parse the fee and English
    requirements pages from source, then re-cache the fresh results.
    Use after a university updates their fee schedule or English requirements.
    """
    from app.services.scraper.central_pages import invalidate_central_cache

    deleted = await invalidate_central_cache(uni_id)
    return {"university_id": uni_id, "rows_deleted": deleted, "status": "invalidated"}


@router.post(
    "/universities/add-by-url",
    status_code=status.HTTP_201_CREATED,
    summary="One-click pipeline: URL → probe → create university → queue scrape",
)
async def add_university_by_url(
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Accept a single URL and run the full autonomous onboarding pipeline.

    Steps
    -----
    1. Quick HTTP fetch to extract university name from ``<title>`` tag.
    2. Infer country and city from TLD / existing data.
    3. Create the university record (or return existing if website matches).
    4. Dispatch ``probe_and_configure`` Celery task.
    5. Return ``{university_id, task_id, name, country, city, message}``.

    The caller should navigate to ``/universities/{university_id}`` and poll
    the probe status.  No YAML, no manual config — zero-touch onboarding.

    Payload
    -------
    ``{"url": "https://www.example.edu.au"}``
    """
    from datetime import datetime, timezone
    from urllib.parse import urlparse

    import re as _re
    import httpx as _httpx

    url: str = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="url is required",
        )
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    root_url = f"{parsed.scheme}://{parsed.netloc}"

    # ── Step 1: Infer metadata from URL and HTML <title> ──────────────────
    name: str = ""
    country: str = "Unknown"
    city: str = "Unknown"

    # Country from TLD
    tld_country: dict[str, str] = {
        ".edu.au": "Australia", ".ac.au": "Australia",
        ".ac.uk": "United Kingdom", ".co.uk": "United Kingdom",
        ".edu.nz": "New Zealand", ".ac.nz": "New Zealand",
        ".edu": "United States",
        ".ca": "Canada",
        ".ie": "Ireland",
        ".de": "Germany",
        ".nl": "Netherlands",
        ".sg": "Singapore",
        ".my": "Malaysia",
        ".hk": "Hong Kong",
    }
    for suffix, cntry in tld_country.items():
        if hostname.endswith(suffix):
            country = cntry
            break

    # Name + city from page HTML
    _UNI_KEYWORDS = _re.compile(
        r"\b(university|université|universität|universiteit|institute|"
        r"institution|college|school\s+of|academy|polytechnic|wānanga|"
        r"teknoloji|teknologi|tec\b|tecnológico)\b",
        _re.I,
    )

    # Small hostname → city lookup for universities whose homepages use
    # a marketing phrase as the page title rather than their real name.
    _HOSTNAME_CITY: dict[str, str] = {
        "waikato.ac.nz":     "Hamilton",
        "auckland.ac.nz":    "Auckland",
        "aut.ac.nz":         "Auckland",
        "victoria.ac.nz":    "Wellington",
        "vuw.ac.nz":         "Wellington",
        "massey.ac.nz":      "Palmerston North",
        "lincoln.ac.nz":     "Christchurch",
        "canterbury.ac.nz":  "Christchurch",
        "otago.ac.nz":       "Dunedin",
        "anu.edu.au":        "Canberra",
        "unimelb.edu.au":    "Melbourne",
        "sydney.edu.au":     "Sydney",
        "unsw.edu.au":       "Sydney",
        "uts.edu.au":        "Sydney",
        "mq.edu.au":         "Sydney",
        "uws.edu.au":        "Sydney",
        "westernsydney.edu.au": "Sydney",
        "uq.edu.au":         "Brisbane",
        "qut.edu.au":        "Brisbane",
        "griffith.edu.au":   "Brisbane",
        "bond.edu.au":       "Gold Coast",
        "monash.edu":        "Melbourne",
        "deakin.edu.au":     "Melbourne",
        "rmit.edu.au":       "Melbourne",
        "latrobe.edu.au":    "Melbourne",
        "swin.edu.au":       "Melbourne",
        "uwa.edu.au":        "Perth",
        "curtin.edu.au":     "Perth",
        "murdoch.edu.au":    "Perth",
        "ecu.edu.au":        "Perth",
        "adelaide.edu.au":   "Adelaide",
        "unisa.edu.au":      "Adelaide",
        "flinders.edu.au":   "Adelaide",
        "csu.edu.au":        "Bathurst",
        "newcastle.edu.au":  "Newcastle",
        "uon.edu.au":        "Newcastle",
        "une.edu.au":        "Armidale",
        "uow.edu.au":        "Wollongong",
        "canberra.edu.au":   "Canberra",
        "uts.edu.au":        "Sydney",
        "jcu.edu.au":        "Townsville",
        "cdu.edu.au":        "Darwin",
        "federation.edu.au": "Ballarat",
        "usq.edu.au":        "Toowoomba",
        "unisq.edu.au":      "Toowoomba",
        "scu.edu.au":        "Lismore",
    }

    try:
        async with _httpx.AsyncClient(
            follow_redirects=True, timeout=10.0, verify=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UniPortalBot/1.0)"},
        ) as client:
            resp = await client.get(root_url)
            if resp.status_code < 400:
                html = resp.text

                # ── Name from <title> ────────────────────────────────────────
                title_m = _re.search(r"<title[^>]*>([^<]+)</title>", html, _re.I)
                if title_m:
                    raw_title = title_m.group(1).strip()
                    segments = [s.strip() for s in _re.split(r"\s*[|–—:]\s*", raw_title) if s.strip()]
                    if segments:
                        # Prefer the segment that looks like an institution name
                        preferred = next(
                            (s for s in segments if _UNI_KEYWORDS.search(s)),
                            None,
                        )
                        if preferred is None:
                            # Fall back to the longest segment
                            preferred = max(segments, key=len)
                        name = preferred[:200]

                # ── City from JSON-LD structured data ────────────────────────
                for ld_block in _re.finditer(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    html, _re.S | _re.I,
                ):
                    import json as _json
                    try:
                        ld = _json.loads(ld_block.group(1))
                        # ld may be a list or a dict
                        items = ld if isinstance(ld, list) else [ld]
                        for item in items:
                            addr = item.get("address") or {}
                            locality = (
                                addr.get("addressLocality")
                                or item.get("addressLocality")
                                or ""
                            )
                            if locality:
                                city = locality.strip()
                                break
                    except Exception:
                        pass
                    if city != "Unknown":
                        break

                # ── City from <meta name="geo.placename"> ───────────────────
                if city == "Unknown":
                    geo_m = _re.search(
                        r'<meta[^>]+name=["\']geo\.placename["\'][^>]+content=["\']([^"\']+)["\']',
                        html, _re.I,
                    )
                    if geo_m:
                        city = geo_m.group(1).strip()

                # ── City from <meta property="og:locality"> ──────────────────
                if city == "Unknown":
                    og_m = _re.search(
                        r'<meta[^>]+property=["\']og:locality["\'][^>]+content=["\']([^"\']+)["\']',
                        html, _re.I,
                    )
                    if og_m:
                        city = og_m.group(1).strip()

    except Exception:
        pass  # Best-effort; we'll fall back to hostname-derived name

    # ── City fallback: hostname lookup ────────────────────────────────────────
    if city == "Unknown":
        # Strip www. prefix then try progressively shorter domain suffixes
        _stripped = hostname.removeprefix("www.")
        _host_parts = _stripped.split(".")
        for _n in range(len(_host_parts), 0, -1):
            _candidate = ".".join(_host_parts[-_n:])
            if _candidate in _HOSTNAME_CITY:
                city = _HOSTNAME_CITY[_candidate]
                break

    if not name:
        # Fallback: capitalise hostname parts
        parts = hostname.removeprefix("www.").split(".")
        name = " ".join(p.capitalize() for p in parts[:2])

    # ── Step 2: Check for existing university with same website ───────────
    existing = (await db.execute(
        select(University).where(University.website.ilike(f"%{hostname}%"))
    )).scalar_one_or_none()

    if existing:
        # Return existing — don't duplicate
        return {
            "university_id": existing.id,
            "task_id": None,
            "name": existing.name,
            "country": existing.country or country,
            "city": existing.city or city,
            "already_exists": True,
            "message": (
                f"University '{existing.name}' already exists "
                f"(id={existing.id}). Navigate there to re-probe or scrape."
            ),
        }

    # ── Step 3: Create university record ──────────────────────────────────
    uni = University(
        name=name,
        country=country,
        city=city,
        website=root_url,
        scrape_url=root_url,
        probe_status="pending",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(uni)
    await db.commit()
    await db.refresh(uni)

    # ── Step 4: Dispatch probe_and_configure ──────────────────────────────
    task_id: str | None = None
    try:
        from app.tasks.scrape_tasks import probe_and_configure
        task = probe_and_configure.delay(uni.id)
        task_id = task.id
        # Mark as probing
        from sqlalchemy import update as _upd
        await db.execute(
            _upd(University)
            .where(University.id == uni.id)
            .values(probe_status="probing", probe_updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    except Exception as exc:
        # Non-fatal: university is created; user can trigger probe manually
        import logging as _log
        _log.getLogger(__name__).warning(
            "add-by-url: probe dispatch failed for uni_id=%d: %s", uni.id, exc
        )

    return {
        "university_id": uni.id,
        "task_id": task_id,
        "name": name,
        "country": country,
        "city": city,
        "already_exists": False,
        "message": (
            "University created and probe queued. "
            "The system will detect the platform, generate a scrape config, "
            "and queue the first scrape automatically."
        ),
    }


@router.post(
    "/universities/{uni_id}/probe",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger autonomous site probe + auto-config generation",
)
async def trigger_probe(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Dispatch a background Celery task that:

    1. Probes the university website (Cloudflare detection, SPA detection,
       search-API fingerprinting, sitemap check, Wayback CDX).
    2. Calls Gemini to analyse the probe result + a sample course page.
    3. Generates a UniConfig-compatible dict and stores it in
       ``university.scrape_config["auto_config"]``.
    4. Sets ``probe_status`` to ``"configured"`` (or ``"failed"``).

    Returns immediately with ``status="probing"``.  Poll
    ``GET /api/universities/{id}/probe-result`` to check completion.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")

    probe_url = u.scrape_url or u.website
    if not probe_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="University has no scrape_url or website configured",
        )

    # Mark as probing immediately so the UI can show a spinner
    await db.execute(
        update(University)
        .where(University.id == uni_id)
        .values(probe_status="probing", probe_updated_at=datetime.now(timezone.utc))
    )
    await db.commit()

    # Dispatch async Celery task
    from app.tasks.scrape_tasks import probe_and_configure

    task = probe_and_configure.delay(uni_id)
    return {
        "status": "probing",
        "university_id": uni_id,
        "task_id": task.id,
        "probe_url": str(probe_url),
    }


@router.get(
    "/universities/{uni_id}/probe-result",
    summary="Get probe status and site intelligence for a university",
)
async def get_probe_result(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Return the latest probe status, site profile, and auto-generated config.

    ``probe_status`` values:
    - ``"none"``        — never probed
    - ``"probing"``     — Celery task dispatched, not yet complete
    - ``"configured"``  — probe complete, auto_config written to scrape_config
    - ``"failed"``      — probe or config generation failed
    """
    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="University not found")

    # Include the auto_config portion of scrape_config for inspection
    auto_config: dict | None = None
    if u.scrape_config and isinstance(u.scrape_config, dict):
        raw = u.scrape_config.get("auto_config")
        if raw:
            # Strip internal metadata keys for cleaner response
            auto_config = {k: v for k, v in raw.items() if not k.startswith("_")}

    return {
        "university_id": uni_id,
        "probe_status": u.probe_status or "none",
        "probe_updated_at": u.probe_updated_at.isoformat() if u.probe_updated_at else None,
        "probe_result": u.probe_result,
        "auto_config": auto_config,
    }


# ── AI Diagnostics ────────────────────────────────────────────────────────────

@router.post("/universities/{uni_id}/diagnose")
async def diagnose_university(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(get_current_user)],
) -> dict[str, Any]:
    """Three-phase diagnostic analysis for a university's scraping config.

    Phase 1 — Analyse the last completed scrape job (field completion, patterns).
    Phase 2 — Probe the live site to detect available data sources.
    Phase 3 — Cross-correlate to produce root-cause analysis and actionable fixes.
    """
    from app.services.scraper.diagnostics import run_diagnostics
    return await run_diagnostics(uni_id, db)


# ── Scrape Fix Agent endpoints ────────────────────────────────────────────────

@router.get("/universities/{uni_id}/agent-config")
async def get_agent_config(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return the admin UI-writable config layer plus effective summary for the Scrape Fix Agent."""
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = u.scrape_config or {}
    admin_cfg: dict = sc.get("admin_config") or {}

    # Read the last job stats for health score
    job_row = (await db.execute(
        text("""
            SELECT runtime_job_id, total_found, imported, skipped, errors,
                   (SELECT ROUND(AVG(completeness), 1)
                    FROM scraped_courses
                    WHERE scrape_job_id = j.runtime_job_id
                      AND completeness IS NOT NULL) AS avg_completeness
            FROM scrape_runtime_jobs j
            WHERE j.university_id = :uid
            ORDER BY j.created_at DESC
            LIMIT 1
        """),
        {"uid": uni_id},
    )).mappings().first()

    job_stats = dict(job_row) if job_row else {}

    # Derive health score (0-100):
    #   40 pts: found-to-expected ratio
    #   30 pts: average completeness
    #   30 pts: staged-to-found ratio (no errors/rejects)
    min_expected = int((admin_cfg.get("_min_expected_courses") or 0))
    found = int(job_stats.get("total_found") or 0)
    imported = int(job_stats.get("imported") or 0)
    avg_comp = float(job_stats.get("avg_completeness") or 0)

    score_found = 40 * min(found / max(min_expected, 1), 1.0) if min_expected else (40 if found >= 10 else 40 * found / 10)
    score_comp  = 30 * min(avg_comp / 100.0, 1.0)
    score_stage = 30 * min(imported / max(found, 1), 1.0) if found else 0
    health_score = round(score_found + score_comp + score_stage)

    recipe_cfg: dict = sc.get("recipe") or {}

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "scrape_url": u.scrape_url or "",
        "admin_config": admin_cfg,
        "recipe": recipe_cfg,
        "health_score": health_score,
        "latest_job_id": job_stats.get("runtime_job_id"),
        "has_rollback": "_prev_admin_config" in sc,
        "job_stats": {
            "total_found": found,
            "imported": imported,
            "skipped": int(job_stats.get("skipped") or 0),
            "errors": int(job_stats.get("errors") or 0),
            "avg_completeness_pct": round(avg_comp, 1),
            "min_expected_courses": min_expected,
        },
    }


@router.get("/universities/{uni_id}/filter-impact")
async def get_filter_impact(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Simulate the university's current effective URL filter against known course URLs.

    Uses the last 200 course_website values from scraped_courses as a proxy
    for the URLs that will be discovered on the next scrape.  Returns drop
    statistics and sample kept/dropped URLs so the operator can see whether
    the current config is safe before triggering a scrape.
    """
    import re as _re_fi

    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    # Load effective config (YAML + DB merged)
    allow_pats: list[str] = []
    mc_patterns: list[str] = []
    block_pats: list[str] = []
    try:
        from app.services.scraper.config.loader import get_config_for_host as _gcfh
        from urllib.parse import urlparse as _up
        _h = _up(u.scrape_url or "").hostname or ""
        _uc = _gcfh(
            hostname=_h, name=u.name or "",
            scrape_url=u.scrape_url or "",
            university_id=u.id,
            db_scrape_config=dict(u.scrape_config or {}),
        )
        allow_pats = list(_uc.discovery.allow_url_patterns or [])
        mc_patterns = list(_uc.discovery.must_contain or [])
        block_pats = list(_uc.discovery.block_url_patterns or [])
    except Exception:
        pass

    has_filters = bool(allow_pats or mc_patterns or block_pats)

    # Fetch known course URLs
    from app.models import ScrapedCourse as _SC2
    url_rows = (await db.execute(
        select(_SC2.course_website)
        .where(_SC2.university_id == uni_id)
        .where(_SC2.course_website.isnot(None))
        .limit(200)
    )).scalars().all()
    known_urls = [u2 for u2 in url_rows if u2]

    if not known_urls:
        return {
            "ok": True,
            "has_filters": has_filters,
            "total_urls": 0,
            "after_filter": 0,
            "dropped": 0,
            "drop_rate_pct": 0,
            "kept_samples": [],
            "dropped_samples": [],
            "filter_config": {
                "allow_url_patterns": allow_pats,
                "must_contain": mc_patterns,
                "block_url_patterns": block_pats,
            },
            "message": "No historical course URLs found — cannot simulate filter impact.",
        }

    compiled_allow = [_re_fi.compile(p, _re_fi.IGNORECASE) for p in allow_pats if p]
    compiled_block = [_re_fi.compile(p, _re_fi.IGNORECASE) for p in block_pats if p]
    mc_lower = [m.lower() for m in mc_patterns if m]

    passing: list[str] = []
    dropped_urls: list[str] = []
    for url in known_urls:
        ok = True
        ul = url.lower()
        if compiled_allow and not any(pat.search(url) for pat in compiled_allow):
            ok = False
        if ok and mc_lower and not any(m in ul for m in mc_lower):
            ok = False
        if ok and compiled_block and any(pat.search(url) for pat in compiled_block):
            ok = False
        (passing if ok else dropped_urls).append(url)

    drop_rate = len(dropped_urls) / len(known_urls) if known_urls else 0

    return {
        "ok": True,
        "has_filters": has_filters,
        "total_urls": len(known_urls),
        "after_filter": len(passing),
        "dropped": len(dropped_urls),
        "drop_rate_pct": round(drop_rate * 100),
        "kept_samples": passing[:50],
        "dropped_samples": dropped_urls[:50],
        "filter_config": {
            "allow_url_patterns": allow_pats,
            "must_contain": mc_patterns,
            "block_url_patterns": block_pats,
        },
        "status": (
            "critical" if drop_rate >= 0.70 else
            "warning" if drop_rate >= 0.20 else
            "ok"
        ),
        # Historical pts contribution (0-30) for composite safety score
        "historical_pts": (
            0 if len(known_urls) == 0 else
            0 if drop_rate >= 0.70 else
            int(30 * (1 - drop_rate)) if drop_rate >= 0.20 else
            30
        ),
    }


@router.post("/universities/{uni_id}/test-discovery")
async def post_test_discovery(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
    fast_only: bool = Query(False, description="Skip browser fallback even when HTTP fails"),
) -> dict:
    """Live seed-URL test: fetch each configured seed page, count course-link candidates,
    then apply current URL filters to those links.

    Does NOT run a full BFS — just fetches seed pages (max 5, 8s each) and counts
    <a href> links that look like course detail pages.  Returns per-seed results,
    aggregate stats, and an AI Safety Score (0-100) combining:
      - Historical URL simulation (30 pts)
      - Live seed test pass-rate (40 pts)
      - Config sanity check (30 pts)
    """
    import re as _re_td
    import httpx as _httpx
    from html.parser import HTMLParser as _HTMLParser
    from urllib.parse import urljoin as _urljoin

    class _LinkExtractor(_HTMLParser):
        def __init__(self, base: str):
            super().__init__()
            self.links: list[tuple[str, str]] = []
            self._base = base
            self._cur: list[str] = []
            self._in_a = False
            self._href = ""

        def handle_starttag(self, tag: str, attrs: list):
            if tag == "a":
                self._in_a = True
                self._cur = []
                for k, v in attrs:
                    if k == "href" and v:
                        self._href = v

        def handle_data(self, data: str):
            if self._in_a:
                self._cur.append(data.strip())

        def handle_endtag(self, tag: str):
            if tag == "a" and self._in_a:
                href = self._href
                text = " ".join(t for t in self._cur if t)
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    full = _urljoin(self._base, href).split("?")[0].split("#")[0]
                    self.links.append((full, text))
                self._in_a = False
                self._href = ""
                self._cur = []

    _COURSE_URL_HINTS = (
        "/course", "/programme", "/program", "/study/", "/degree/",
        "/bachelor", "/master", "/doctor", "/phd", "/mba",
        "/graduate", "/undergraduate", "/postgraduate",
        "/diploma", "/certificate", "/associate",
    )

    def _quick_course(url: str, text: str) -> bool:
        lurl = url.lower()
        if any(h in lurl for h in _COURSE_URL_HINTS):
            return True
        if text:
            tl = text.lower()
            for kw in ("bachelor", "master", "doctor", "phd", "diploma",
                       "certificate", "mba", "graduate", "honours"):
                if kw in tl:
                    return True
        return False

    _LISTING_HINTS_TD = (
        "/courses", "/programmes", "/programs", "/find-a-course",
        "/browse-courses", "/search-courses", "/study-areas",
        "/our-courses", "/all-courses", "/course-search",
        "courses.html", "courses.aspx",
    )
    _CATEGORY_HINTS_TD = (
        "/faculty/", "/faculties/", "/school/", "/schools/",
        "/department/", "/departments/", "/subject/",
        "/discipline/", "/area-of-study/", "/college/",
    )

    def _classify_url_type(url: str) -> str:
        lurl = url.lower().split("?")[0].rstrip("/")
        # Listing: broad course-index pages
        if any(lurl.endswith(h.rstrip("/")) or h in lurl for h in _LISTING_HINTS_TD):
            return "listing"
        # Category: faculty/school/department hub pages
        if any(h in lurl for h in _CATEGORY_HINTS_TD):
            return "category"
        # Course: individual course-detail URL patterns
        if _quick_course(url, ""):
            return "course"
        return "other"

    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    # Load effective config
    allow_pats: list[str] = []
    mc_patterns: list[str] = []
    block_pats: list[str] = []
    seed_urls_cfg: list[str] = []
    try:
        from app.services.scraper.config.loader import get_config_for_host as _gcfh3
        from urllib.parse import urlparse as _up3
        _h = _up3(u.scrape_url or "").hostname or ""
        _uc = _gcfh3(
            hostname=_h, name=u.name or "",
            scrape_url=u.scrape_url or "",
            university_id=u.id,
            db_scrape_config=dict(u.scrape_config or {}),
        )
        allow_pats = list(_uc.discovery.allow_url_patterns or [])
        mc_patterns = list(_uc.discovery.must_contain or [])
        block_pats = list(_uc.discovery.block_url_patterns or [])
        seed_urls_cfg = list(_uc.discovery.seed_urls or [])
    except Exception:
        pass

    if not seed_urls_cfg and u.scrape_url:
        seed_urls_cfg = [u.scrape_url]
    if not seed_urls_cfg:
        return {"ok": False, "error": "No seed URLs configured"}

    compiled_allow = [_re_td.compile(p, _re_td.IGNORECASE) for p in allow_pats if p]
    compiled_block = [_re_td.compile(p, _re_td.IGNORECASE) for p in block_pats if p]
    mc_lower = [m.lower() for m in mc_patterns if m]

    def _passes(url: str) -> bool:
        ul = url.lower()
        if compiled_allow and not any(pat.search(url) for pat in compiled_allow):
            return False
        if mc_lower and not any(m in ul for m in mc_lower):
            return False
        if compiled_block and any(pat.search(url) for pat in compiled_block):
            return False
        return True

    # Historical URL pts (0-30) — reuse scraped_courses
    from app.models import ScrapedCourse as _SC3
    _url_rows = (await db.execute(
        select(_SC3.course_website)
        .where(_SC3.university_id == uni_id)
        .where(_SC3.course_website.isnot(None))
        .limit(200)
    )).scalars().all()
    _known_urls = [x for x in _url_rows if x]
    if not _known_urls:
        historical_pts = 0
    else:
        _hist_passing = sum(1 for u2 in _known_urls if _passes(u2))
        _hist_drop = 1 - _hist_passing / len(_known_urls)
        historical_pts = (
            0 if _hist_drop >= 0.70 else
            int(30 * (1 - _hist_drop)) if _hist_drop >= 0.20 else
            30
        )

    # Fetch each seed URL
    seed_results = []
    all_raw_set: set[str] = set()
    all_pass_set: set[str] = set()
    warnings: list[str] = []

    async with _httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; UniPortalBot/1.0)"},
        follow_redirects=True,
        timeout=8.0,
    ) as _client:
        for seed_url in seed_urls_cfg[:5]:
            try:
                resp = await _client.get(seed_url)
                parser = _LinkExtractor(seed_url)
                try:
                    parser.feed(resp.text)
                except Exception:
                    pass
                seen: set[str] = set()
                candidates: list[str] = []
                for link_url, text in parser.links:
                    if link_url in seen:
                        continue
                    seen.add(link_url)
                    if _quick_course(link_url, text):
                        candidates.append(link_url)

                passing = [u2 for u2 in candidates if _passes(u2)]
                dropped = [u2 for u2 in candidates if not _passes(u2)]
                drop_r = len(dropped) / len(candidates) if candidates else 0

                all_raw_set.update(candidates)
                all_pass_set.update(passing)

                # Classify ALL raw candidates by page type so operator sees
                # full breakdown: "26 raw (12 course, 8 listing, 6 other)"
                _raw_type_groups: dict[str, list[str]] = {
                    "course": [], "listing": [], "category": [], "other": []
                }
                for _cu in candidates:
                    _raw_type_groups[_classify_url_type(_cu)].append(_cu)

                # Classify passing URLs by page type
                _type_groups: dict[str, list[str]] = {
                    "course": [], "listing": [], "category": [], "other": []
                }
                for _pu in passing:
                    _type_groups[_classify_url_type(_pu)].append(_pu)
                _classified_passing = {k: v[:8] for k, v in _type_groups.items() if v}

                sr: dict = {
                    "seed_url": seed_url,
                    "status_code": resp.status_code,
                    "raw_candidates": len(candidates),
                    "raw_course_count": len(_raw_type_groups["course"]),
                    "raw_listing_count": len(_raw_type_groups["listing"]),
                    "raw_category_count": len(_raw_type_groups["category"]),
                    "raw_other_count": len(_raw_type_groups["other"]),
                    "after_filter": len(passing),
                    "dropped": len(dropped),
                    "drop_rate_pct": round(drop_r * 100),
                    "sample_passing": passing[:6],
                    "sample_dropped": dropped[:6],
                    "classified_passing": _classified_passing,
                    "course_count": len(_type_groups["course"]),
                    "listing_count": len(_type_groups["listing"]),
                    "category_count": len(_type_groups["category"]),
                    "ok": resp.status_code < 400,
                }

                if len(candidates) < 5:
                    w = (
                        f"'{seed_url}' returned only {len(candidates)} course-link "
                        f"candidate(s) — the URL may be incorrect, or the page uses "
                        f"JavaScript rendering (links not in HTML source)."
                    )
                    warnings.append(w)
                    sr["warning"] = w
                elif drop_r >= 0.70:
                    w = (
                        f"'{seed_url}' found {len(candidates)} course links but "
                        f"the current filter dropped {len(dropped)} ({round(drop_r*100)}%)."
                    )
                    warnings.append(w)
                    sr["warning"] = w

                seed_results.append(sr)
            except Exception as exc:
                err = str(exc)[:120]
                seed_results.append({
                    "seed_url": seed_url,
                    "status_code": 0,
                    "raw_candidates": 0,
                    "after_filter": 0,
                    "dropped": 0,
                    "drop_rate_pct": 0,
                    "sample_passing": [],
                    "sample_dropped": [],
                    "ok": False,
                    "error": err,
                    "warning": f"Could not fetch '{seed_url}': {err[:80]}",
                })
                warnings.append(f"Could not fetch '{seed_url}': {err[:80]}")

    total_raw = len(all_raw_set)
    total_passing = len(all_pass_set)
    total_dropped = total_raw - total_passing
    agg_drop = total_dropped / total_raw if total_raw > 0 else 0

    # Seed pts (0-40)
    if total_raw == 0:
        seed_pts = 0
    elif agg_drop >= 0.70:
        seed_pts = 0
    elif agg_drop >= 0.20:
        seed_pts = int(40 * (1 - agg_drop))
    elif total_raw < 5:
        seed_pts = 15  # found too few to be confident
    else:
        seed_pts = 40

    # ── Browser fallback ────────────────────────────────────────────────────
    # If any seed returned 403 or < 5 candidates, automatically fall back to
    # Playwright browser discovery (30 s time-budget, max 50 courses per seed)
    # unless fast_only=True was requested.
    browser_used = False
    browser_total_raw: set[str] = set()
    browser_total_pass: set[str] = set()

    _needs_browser = (not fast_only) and any(
        (not sr["ok"] or sr.get("raw_candidates", 0) < 5) for sr in seed_results
    )

    if _needs_browser:
        try:
            from app.services.scraper.config.schema import (
                UniConfig as _UC_br,
                DiscoveryConfig as _DC_br,
            )
            from app.services.scraper.config.context import set_uni_config as _set_cfg_br
            from app.services.scraper.browser_discover_generic import (
                browser_discover_generic as _bdg,
            )
            from urllib.parse import urlparse as _up_br

            _br_host = (_up_br(u.scrape_url or "").hostname or "").replace("www.", "")
            _test_cfg = _UC_br(
                slug=_br_host.replace(".", "_") or "test",
                name=u.name or "",
                base_url=u.scrape_url or "",
                scrape_url=u.scrape_url or "",
                discovery=_DC_br(
                    browser_time_budget_s=30,
                    browser_early_stop_courses=50,
                ),
            )
            _set_cfg_br(_test_cfg)

            for _sr in seed_results:
                if _sr.get("ok") and _sr.get("raw_candidates", 0) >= 5:
                    _sr["browser_test"] = {"skipped": True, "reason": "HTTP test succeeded"}
                    continue
                try:
                    _blinks = await _bdg(_sr["seed_url"], max_courses=50)
                    _b_all = [item["url"] for item in _blinks]
                    _b_pass = [_u2 for _u2 in _b_all if _passes(_u2)]
                    _b_drop = [_u2 for _u2 in _b_all if not _passes(_u2)]
                    _b_drop_r = len(_b_drop) / len(_b_all) if _b_all else 0

                    browser_total_raw.update(_b_all)
                    browser_total_pass.update(_b_pass)
                    browser_used = True

                    _bsr: dict = {
                        "raw_candidates": len(_b_all),
                        "after_filter": len(_b_pass),
                        "dropped": len(_b_drop),
                        "drop_rate_pct": round(_b_drop_r * 100),
                        "sample_passing": _b_pass[:6],
                        "sample_dropped": _b_drop[:6],
                        "ok": True,
                    }
                    if len(_b_all) < 5:
                        _bsr["warning"] = (
                            f"Browser also found only {len(_b_all)} course link(s) from "
                            f"'{_sr['seed_url']}' — this seed URL may be incorrect."
                        )
                    elif _b_drop_r >= 0.70:
                        _bw = (
                            f"Browser: '{_sr['seed_url']}' found {len(_b_all)} links but "
                            f"filter dropped {len(_b_drop)} ({round(_b_drop_r * 100)}%)."
                        )
                        _bsr["warning"] = _bw
                        if _bw not in warnings:
                            warnings.append(_bw)
                    _sr["browser_test"] = _bsr

                    # Upgrade the "JS rendered" warning once browser succeeds
                    if len(_b_all) >= 5:
                        _real_diag = (
                            f"'{_sr['seed_url']}' requires browser rendering "
                            f"(HTTP {_sr.get('status_code', '?')}) — "
                            f"browser found {len(_b_all)} course link(s)."
                        )
                        _sr["warning"] = _real_diag
                        for _wi, _wv in enumerate(warnings):
                            if _sr["seed_url"] in _wv:
                                warnings[_wi] = _real_diag
                                break

                except Exception as _be:
                    _sr["browser_test"] = {"ok": False, "error": str(_be)[:120]}

        except Exception as _br_outer:
            warnings.append(f"Browser test unavailable: {str(_br_outer)[:100]}")

    # ── Final aggregates (prefer browser data where available) ──────────────
    if browser_used and browser_total_raw:
        _merged_raw = all_raw_set | browser_total_raw
        _merged_pass = all_pass_set | browser_total_pass
        total_raw = len(_merged_raw)
        total_passing = len(_merged_pass)
    else:
        total_raw = len(all_raw_set)
        total_passing = len(all_pass_set)
    total_dropped = total_raw - total_passing
    agg_drop = (total_raw - total_passing) / total_raw if total_raw > 0 else 0

    # Seed pts (0-40)
    if total_raw == 0:
        seed_pts = 0
    elif agg_drop >= 0.70:
        seed_pts = 0
    elif agg_drop >= 0.20:
        seed_pts = int(40 * (1 - agg_drop))
    elif total_raw < 5:
        seed_pts = 15
    else:
        seed_pts = 40

    # Config pts (0-30)
    has_filters = bool(allow_pats or mc_patterns or block_pats)
    if not has_filters:
        config_pts = 30
    elif total_raw > 0 and agg_drop < 0.20:
        config_pts = 20
    elif total_raw > 0 and agg_drop < 0.50:
        config_pts = 10
    else:
        config_pts = 0

    safety_score = historical_pts + seed_pts + config_pts

    return {
        "ok": True,
        "seed_results": seed_results,
        "total_raw": total_raw,
        "total_passing": total_passing,
        "total_dropped": total_dropped,
        "agg_drop_rate_pct": round(agg_drop * 100),
        "warnings": warnings,
        "has_filters": has_filters,
        "browser_fallback_used": browser_used,
        "fast_only": fast_only,
        "safety_score": safety_score,
        "safety_score_breakdown": {
            "historical_pts": historical_pts,
            "seed_pts": seed_pts,
            "config_pts": config_pts,
        },
        "safety_level": (
            "safe" if safety_score >= 90 else
            "warning" if safety_score >= 70 else
            "dangerous"
        ),
        "agg_status": (
            "critical" if agg_drop >= 0.70 or (0 < total_raw < 5) else
            "warning" if agg_drop >= 0.20 or total_raw < 10 else
            "ok"
        ),
        "filter_config": {
            "allow_url_patterns": allow_pats,
            "must_contain": mc_patterns,
            "block_url_patterns": block_pats,
        },
    }


@router.post("/universities/{uni_id}/full-validation")
async def post_full_validation(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Test up to 5 URLs through the full filter→classify→extract pipeline (read-only).

    Takes a JSON body ``{"urls": ["https://...", ...]}`` (max 5 URLs).
    For each URL fetches the page, applies the current effective filter config,
    classifies the page type, and probes for course-data keywords to estimate
    how extractable the page is.  Nothing is written to the database.
    """
    import httpx as _httpx_fv
    import re as _re_fv
    from urllib.parse import urlparse as _up_fv

    urls: list[str] = [
        u for u in (body.get("urls") or [])
        if isinstance(u, str) and u.strip()
    ][:5]
    if not urls:
        return {"ok": False, "error": "No URLs provided"}

    u_obj: University | None = await db.get(University, uni_id)
    if not u_obj:
        raise HTTPException(status_code=404, detail="University not found")

    allow_pats: list[str] = []
    block_pats: list[str] = []
    mc_patterns: list[str] = []
    try:
        from app.services.scraper.config.loader import get_config_for_host as _gcfh_fv
        _h_fv = (_up_fv(u_obj.scrape_url or "").hostname or "")
        _uc_fv = _gcfh_fv(
            hostname=_h_fv, name=u_obj.name or "",
            scrape_url=u_obj.scrape_url or "",
            university_id=u_obj.id,
            db_scrape_config=dict(u_obj.scrape_config or {}),
        )
        allow_pats = list(_uc_fv.discovery.allow_url_patterns or [])
        block_pats = list(_uc_fv.discovery.block_url_patterns or [])
        mc_patterns = list(_uc_fv.discovery.must_contain or [])
    except Exception:
        pass

    c_allow = [_re_fv.compile(p, _re_fv.IGNORECASE) for p in allow_pats if p]
    c_block = [_re_fv.compile(p, _re_fv.IGNORECASE) for p in block_pats if p]
    c_mc = [m.lower() for m in mc_patterns if m]

    def _fv_passes(url: str) -> bool:
        ul = url.lower()
        if c_allow and not any(pat.search(url) for pat in c_allow):
            return False
        if c_mc and not any(m in ul for m in c_mc):
            return False
        if c_block and any(pat.search(url) for pat in c_block):
            return False
        return True

    _LISTING_P_FV = ("/courses", "/programmes", "/programs", "/find-a-course", "/study")
    _CATEGORY_P_FV = ("/faculty/", "/school/", "/department/", "/discipline/", "/area-of-study/")

    # Field extraction regexes — used to simulate what the extractor would find
    _H1_RE_FV = _re_fv.compile(r'<h1[^>]*>(.*?)</h1>', _re_fv.IGNORECASE | _re_fv.DOTALL)
    _TITLE_RE_FV = _re_fv.compile(r'<title[^>]*>(.*?)</title>', _re_fv.IGNORECASE | _re_fv.DOTALL)
    _FEE_RE_FV = _re_fv.compile(
        r'(?:NZD|AUD|USD|GBP|£|\$)\s*[\d,]+|[\d,]+\s*(?:NZD|AUD|USD|per\s+year)',
        _re_fv.IGNORECASE,
    )
    _ENGLISH_RE_FV = _re_fv.compile(
        r'\b(?:IELTS|PTE Academic|PTE|TOEFL|TOEIC|Cambridge English|Duolingo)\b',
        _re_fv.IGNORECASE,
    )
    _INTAKE_RE_FV = _re_fv.compile(
        r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b',
        _re_fv.IGNORECASE,
    )
    _DURATION_RE_FV = _re_fv.compile(r'\d+(?:\.\d+)?\s*(?:year|month|semester|week)s?', _re_fv.IGNORECASE)
    _DEGREE_RE_FV = _re_fv.compile(
        r'\b(?:bachelor|master|doctorate|doctor|phd|diploma|certificate|graduate certificate|postgraduate diploma)\b',
        _re_fv.IGNORECASE,
    )

    def _fv_classify(url: str, has_degree_kw: bool) -> str:
        lurl = url.lower().split("?")[0]
        if any(lurl.rstrip("/").endswith(h.rstrip("/")) or h in lurl for h in _LISTING_P_FV):
            return "listing"
        if any(h in lurl for h in _CATEGORY_P_FV):
            return "category"
        degree_url_kws = {"bachelor", "master", "doctor", "phd", "diploma", "certificate", "degree", "graduate"}
        if any(kw in lurl for kw in degree_url_kws):
            return "course"
        if has_degree_kw:
            return "course"
        return "unknown"

    def _strip_tags(s: str) -> str:
        return _re_fv.sub(r'<[^>]+>', '', s).strip()

    results = []
    async with _httpx_fv.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (compatible; UniPortalBot/1.0)"},
        follow_redirects=True,
        timeout=12.0,
    ) as _cl:
        for url in urls:
            passes = _fv_passes(url)
            blocked_by: str | None = None
            if c_allow and not any(pat.search(url) for pat in c_allow):
                blocked_by = "allow_url_patterns"
            elif c_mc and not any(m in url.lower() for m in c_mc):
                blocked_by = "must_contain"
            elif c_block and any(pat.search(url) for pat in c_block):
                blocked_by = "block_url_patterns"

            try:
                resp_fv = await _cl.get(url)
                raw_html = resp_fv.text[:80000]

                # ── Extract individual fields ──────────────────────────────
                # Course name: h1 > title tag
                _h1m = _H1_RE_FV.search(raw_html[:30000])
                _h1_text = _strip_tags(_h1m.group(1)) if _h1m else ""
                if not _h1_text or len(_h1_text) < 4:
                    _titlem = _TITLE_RE_FV.search(raw_html[:5000])
                    _h1_text = _strip_tags(_titlem.group(1)).split("|")[0].strip() if _titlem else ""
                course_name_extracted = len(_h1_text) >= 4

                fee_match = _FEE_RE_FV.search(raw_html)
                fee_extracted = bool(fee_match)
                fee_value = fee_match.group(0)[:30] if fee_match else None

                english_match = _ENGLISH_RE_FV.search(raw_html)
                english_extracted = bool(english_match)
                english_value = english_match.group(0)[:20] if english_match else None

                intake_extracted = bool(_INTAKE_RE_FV.search(raw_html))
                duration_extracted = bool(_DURATION_RE_FV.search(raw_html))
                degree_match = _DEGREE_RE_FV.search(raw_html[:40000])
                degree_level_extracted = bool(degree_match)

                # Classify page type using both URL and content signals
                page_type = _fv_classify(url, degree_level_extracted)

                # Completeness simulation (6 key fields)
                _fields = {
                    "course_name": course_name_extracted,
                    "fee": fee_extracted,
                    "english_requirement": english_extracted,
                    "intake": intake_extracted,
                    "duration": duration_extracted,
                    "degree_level": degree_level_extracted,
                }
                fields_found = sum(_fields.values())
                completeness_pct = round(fields_found / len(_fields) * 100)

                # Will stage?  needs: passes filter + course page + ≥4/6 fields
                will_stage = passes and page_type == "course" and fields_found >= 4

                # Rejection reason
                rejection_reason: str | None = None
                if not will_stage:
                    if not passes:
                        rejection_reason = f"Blocked by URL filter ({blocked_by or 'filter'})"
                    elif page_type != "course":
                        rejection_reason = f"Not a course page (classified as '{page_type}')"
                    else:
                        missing = [k.replace("_", " ") for k, v in _fields.items() if not v]
                        rejection_reason = (
                            f"Too few fields extracted ({fields_found}/6). "
                            f"Missing: {', '.join(missing[:3])}"
                            + ("…" if len(missing) > 3 else "")
                        )

                results.append({
                    "url": url,
                    "passes_filter": passes,
                    "blocked_by": blocked_by,
                    "status_code": resp_fv.status_code,
                    "page_type": page_type,
                    # Field-level extraction
                    "course_name_extracted": course_name_extracted,
                    "course_name_value": _h1_text[:80] if course_name_extracted else None,
                    "fee_extracted": fee_extracted,
                    "fee_value": fee_value,
                    "english_extracted": english_extracted,
                    "english_value": english_value,
                    "intake_extracted": intake_extracted,
                    "duration_extracted": duration_extracted,
                    "degree_level_extracted": degree_level_extracted,
                    "fields_found": fields_found,
                    "fields_total": len(_fields),
                    "completeness_pct": completeness_pct,
                    # Staging decision
                    "will_stage": will_stage,
                    "rejection_reason": rejection_reason,
                    "ok": resp_fv.status_code < 400,
                    "text_length": len(resp_fv.text),
                })
            except Exception as _exc_fv:
                results.append({
                    "url": url,
                    "passes_filter": passes,
                    "blocked_by": blocked_by,
                    "status_code": 0,
                    "page_type": "unknown",
                    "course_name_extracted": False,
                    "course_name_value": None,
                    "fee_extracted": False,
                    "fee_value": None,
                    "english_extracted": False,
                    "english_value": None,
                    "intake_extracted": False,
                    "duration_extracted": False,
                    "degree_level_extracted": False,
                    "fields_found": 0,
                    "fields_total": 6,
                    "completeness_pct": 0,
                    "will_stage": False,
                    "rejection_reason": f"Fetch error: {str(_exc_fv)[:80]}",
                    "ok": False,
                    "text_length": 0,
                    "error": str(_exc_fv)[:120],
                })

    course_results = [r for r in results if r["page_type"] == "course" and r["ok"]]
    avg_comp = (
        round(sum(r["completeness_pct"] for r in course_results) / len(course_results))
        if course_results else 0
    )
    will_stage_count = sum(1 for r in results if r.get("will_stage"))
    return {
        "ok": True,
        "results": results,
        "summary": {
            "total": len(results),
            "passed_filter": sum(1 for r in results if r["passes_filter"]),
            "course_pages": sum(1 for r in results if r["page_type"] == "course"),
            "listing_pages": sum(1 for r in results if r["page_type"] == "listing"),
            "will_stage": will_stage_count,
            "avg_course_completeness_pct": avg_comp,
        },
    }


@router.put("/universities/{uni_id}/agent-config")
async def put_agent_config(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Write admin UI rules to DB scrape_config.admin_config (Scrape Fix Agent write layer).

    The body is a partial config matching the UniConfig schema structure, e.g.::

        {
          "discovery": {"must_contain": ["/courses/"], "bfs_page_budget": 80},
          "extraction": {"filters": {"online_only": {"enabled": true}}},
          "_min_expected_courses": 100
        }

    Values are stored in ``scrape_config.admin_config`` and merged into the
    scraper's config chain between auto_config and the per-uni YAML override.
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(u.scrape_config or {})
    sc["admin_config"] = body

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {"ok": True, "university_id": uni_id, "admin_config": body}


# ── YAML ↔ Recipe Editor helpers ─────────────────────────────────────────────

_UNIS_YAML_DIR = Path(__file__).parent.parent.parent / "scraper_config" / "unis"


def _slug_for_uni(scrape_url: str) -> str | None:
    """Return the YAML slug for a university given its scrape_url.

    1. Extracts hostname, strips www.
    2. Fast-path: if ``{first_segment}.yaml`` exists, return it.
    3. Scan all YAML files and return the stem of the first file whose
       content contains the bare hostname.
    Returns None if no matching YAML file exists.
    """
    if not scrape_url:
        return None
    try:
        parsed = urlparse(scrape_url.strip())
        host = parsed.netloc or urlparse("https://" + scrape_url.strip()).netloc
        host = re.sub(r"^www\.", "", (host or "").lower().split(":")[0])
        if not host:
            return None
        first = host.split(".")[0]
        if first and (_UNIS_YAML_DIR / f"{first}.yaml").exists():
            return first
        for f in sorted(_UNIS_YAML_DIR.glob("*.yaml")):
            try:
                if host in f.read_text(encoding="utf-8", errors="replace"):
                    return f.stem
            except OSError:
                pass
    except Exception:
        pass
    return None


def _yaml_to_recipe(yaml_data: dict) -> dict:
    """Convert a parsed YAML config dict into recipe-editor compatible fields."""
    recipe: dict = {}
    disc = yaml_data.get("discovery") or {}
    ext  = yaml_data.get("extraction") or {}

    # Discovery
    if disc.get("seed_urls"):
        recipe["seed_urls"] = list(disc["seed_urls"])
    if disc.get("block_url_patterns"):
        recipe["block_url_patterns"] = list(disc["block_url_patterns"])
    if disc.get("allow_url_patterns"):
        recipe["must_contain"] = list(disc["allow_url_patterns"])
    for key in ("bfs_page_budget", "max_candidates", "expected_min_courses",
                "expected_max_courses", "browser_time_budget_s", "browser_early_stop_courses"):
        if disc.get(key) is not None:
            recipe[key] = disc[key]

    # course_name cleanup
    cn = ext.get("course_name") or {}
    if cn.get("remove_after"):
        recipe["course_name_remove_after"] = list(cn["remove_after"])
    if cn.get("remove_year_suffix"):
        recipe["course_name_remove_year_suffix"] = True
    if cn.get("remove_patterns"):
        recipe["course_name_remove_patterns"] = list(cn["remove_patterns"])

    # fees
    fees = ext.get("fees") or {}
    if fees.get("default_currency"):
        recipe["fee_currency"] = fees["default_currency"]
    if fees.get("fee_year") is not None:
        recipe["fee_year"] = fees["fee_year"]
    if fees.get("prefer_international"):
        recipe["fee_prefer_international"] = bool(fees["prefer_international"])
    if fees.get("fee_url_suffix"):
        recipe["fee_url_suffix"] = fees["fee_url_suffix"]
    if fees.get("reject_keywords"):
        recipe["fee_reject_keywords"] = list(fees["reject_keywords"])
    if fees.get("follow_links"):
        recipe["fee_follow_links"] = list(fees["follow_links"])
    if fees.get("rules_undergraduate"):
        recipe["fee_rules_undergraduate"] = list(fees["rules_undergraduate"])
    if fees.get("rules_postgraduate"):
        recipe["fee_rules_postgraduate"] = list(fees["rules_postgraduate"])

    # english
    eng = ext.get("english") or {}
    if eng.get("course_english_priority"):
        recipe["course_english_priority"] = True
    ielts_vals = {k: eng.get(k, "") for k in ("overall_regex", "band_regex", "source_xpath")}
    if any(ielts_vals.values()):
        recipe["ielts"] = ielts_vals
    if eng.get("follow_links"):
        recipe["follow_links"] = list(eng["follow_links"])
    if eng.get("degree_level_defaults"):
        recipe["degree_level_defaults"] = dict(eng["degree_level_defaults"])
    if eng.get("band_mapping"):
        recipe["band_mapping"] = dict(eng["band_mapping"])
    if eng.get("band_reference_url"):
        recipe["band_reference_url"] = eng["band_reference_url"]

    # location
    loc = ext.get("location") or {}
    if loc.get("replace"):
        recipe["location_replace"] = dict(loc["replace"])
    if loc.get("allowed_values"):
        recipe["location_allowed_values"] = list(loc["allowed_values"])
    if loc.get("reject_values"):
        recipe["location_reject_values"] = list(loc["reject_values"])

    # study_mode
    sm = ext.get("study_mode") or {}
    if sm.get("from_location"):
        recipe["study_mode_from_location"] = True
    if sm.get("online_keywords"):
        recipe["study_mode_online_keywords"] = list(sm["online_keywords"])

    # intake
    intake = ext.get("intake") or {}
    intake_recipe: dict = {}
    if intake.get("xpath"):
        intake_recipe["xpath"] = intake["xpath"]
    if intake.get("regex"):
        intake_recipe["regex"] = intake["regex"]
    if intake.get("month_map"):
        intake_recipe["month_map"] = dict(intake["month_map"])
    if intake_recipe:
        recipe["intake"] = {"xpath": "", "regex": "", "month_map": {}, **intake_recipe}

    # browser actions
    raw_actions = ext.get("actions") or []
    actions: list[dict] = []
    for a in raw_actions:
        if not isinstance(a, dict):
            continue
        if "click_text"   in a: actions.append({"action_type": "click_text",        "value": str(a["click_text"])})
        elif "click_css"  in a: actions.append({"action_type": "click_css",         "value": str(a["click_css"])})
        elif "expand_text" in a: actions.append({"action_type": "expand_text",       "value": str(a["expand_text"])})
        elif "scroll_to"  in a: actions.append({"action_type": "scroll_to",         "value": str(a["scroll_to"])})
        elif "wait_for"   in a:
            wf = a["wait_for"]
            if isinstance(wf, dict):
                if "text"     in wf: actions.append({"action_type": "wait_for_text",     "value": str(wf["text"])})
                elif "selector" in wf: actions.append({"action_type": "wait_for_selector", "value": str(wf["selector"])})
    if actions:
        recipe["actions"] = actions

    # quality gates
    quality = ext.get("quality") or {}
    if quality.get("minimum_completeness") is not None:
        recipe["minimum_completeness"] = quality["minimum_completeness"]
    if quality.get("required_fields"):
        recipe["required_fields"] = list(quality["required_fields"])
    if quality.get("block_publish_if"):
        recipe["block_publish_if"] = list(quality["block_publish_if"])

    # JSON/REST API discovery (generic_search_api)
    gsa = disc.get("generic_search_api") or {}
    if gsa.get("url"):
        api_block: dict = {
            "endpoint":            gsa["url"],
            "method":              gsa.get("method") or "GET",
            "root_path":           gsa.get("root_path") or "",
            "count_path":          "",
            "course_url_template": gsa.get("course_url_template") or "",
            "query_params":        dict(gsa.get("params") or {}),
            "headers":             dict(gsa.get("headers") or {}),
            "fields":              {},
        }
        # enabled / browser mode
        if gsa.get("enabled") is False:
            api_block["enabled"] = False
        if gsa.get("fetch_via_browser"):
            api_block["fetch_via_browser"] = True
        if gsa.get("browser_seed_url"):
            api_block["browser_seed_url"] = gsa["browser_seed_url"]
        # URL normalization
        if gsa.get("base_url"):
            api_block["base_url"] = gsa["base_url"]
        if gsa.get("normalize_relative_urls") is False:
            api_block["normalize_relative_urls"] = False
        # URL field hints
        if gsa.get("url_fields"):
            api_block["url_fields"] = list(gsa["url_fields"])
        if gsa.get("title_fields"):
            api_block["title_fields"] = list(gsa["title_fields"])
        # URL filters within this API provider
        if gsa.get("allow_url_patterns"):
            api_block["api_allow_url_patterns"] = list(gsa["allow_url_patterns"])
        if gsa.get("block_url_patterns"):
            api_block["api_block_url_patterns"] = list(gsa["block_url_patterns"])
        # POST body
        if gsa.get("body") is not None:
            api_block["body"] = gsa["body"]
        if gsa.get("body_pagination"):
            bp = gsa["body_pagination"]
            api_block["body_pagination"] = dict(bp) if isinstance(bp, dict) else bp.model_dump(exclude_none=True)
        # Additional URLs (UG+PG split endpoints)
        if gsa.get("additional_urls"):
            api_block["additional_urls"] = list(gsa["additional_urls"])
        # Pagination — query-string only (when no body_pagination)
        has_body_pag = bool(gsa.get("body_pagination"))
        if gsa.get("page_size") and not has_body_pag:
            api_block["pagination"] = {
                "type":       "offset",
                "page_param": gsa.get("page_number_param") or gsa.get("offset_param") or "start",
                "size_param": gsa.get("page_size_param") or "rows",
                "page_size":  gsa["page_size"],
                "page_start": 0,
                "max_pages":  gsa.get("max_pages") or 20,
            }
        # page_size + max_pages as standalone (used with body_pagination)
        if gsa.get("page_size") and has_body_pag:
            api_block["page_size"] = gsa["page_size"]
        if gsa.get("max_pages") and gsa["max_pages"] != 20:
            api_block["max_pages"] = gsa["max_pages"]
        recipe["api"] = api_block

    return recipe


def _recipe_to_yaml_patch(existing_yaml: dict, recipe: dict) -> dict:
    """Patch an existing parsed YAML config dict with recipe-editor fields.

    Only touches keys that the recipe editor manages.  YAML-only keys
    (``use_stealth_browser``, ``browser_wait_strategy``, ``max_parallel_fetch``,
    ``filters``, ``text_cleaning``, etc.) are left untouched.
    Empty lists / falsy scalars remove the key so the YAML stays clean.
    """
    out  = copy.deepcopy(existing_yaml)
    disc = out.setdefault("discovery", {})
    ext  = out.setdefault("extraction", {})

    def _set_or_del(d: dict, key: str, val: Any) -> None:
        if val:
            d[key] = val
        elif key in d:
            del d[key]

    # Discovery
    _set_or_del(disc, "seed_urls",           recipe.get("seed_urls") or [])
    _set_or_del(disc, "block_url_patterns",  recipe.get("block_url_patterns") or [])
    _set_or_del(disc, "allow_url_patterns",  recipe.get("must_contain") or [])
    for key in ("bfs_page_budget", "max_candidates", "expected_min_courses",
                "expected_max_courses", "browser_time_budget_s", "browser_early_stop_courses"):
        if recipe.get(key) is not None:
            disc[key] = recipe[key]

    # course_name — patch recipe-managed keys, keep YAML-only keys (e.g. strip_title_suffixes)
    cn_after = recipe.get("course_name_remove_after") or []
    cn_year  = bool(recipe.get("course_name_remove_year_suffix"))
    cn_pats  = recipe.get("course_name_remove_patterns") or []
    _RECIPE_CN_KEYS = {"remove_after", "remove_year_suffix", "remove_patterns"}
    cn = ext.setdefault("course_name", {})
    _set_or_del(cn, "remove_after",       cn_after)
    _set_or_del(cn, "remove_year_suffix", cn_year or None)
    _set_or_del(cn, "remove_patterns",    cn_pats)
    if not cn:
        del ext["course_name"]

    # fees — preserve YAML-only keys (prefer_annual_over_total, prefer_year_one_over_total, etc.)
    fees = ext.setdefault("fees", {})
    fc = recipe.get("fee_currency") or ""
    if fc and fc != "AUD":
        fees["default_currency"] = fc
    elif "default_currency" in fees and not fc:
        del fees["default_currency"]
    if recipe.get("fee_year") is not None:
        fees["fee_year"] = recipe["fee_year"]
    _set_or_del(fees, "prefer_international", recipe.get("fee_prefer_international") or None)
    _set_or_del(fees, "fee_url_suffix",        recipe.get("fee_url_suffix") or "")
    _set_or_del(fees, "reject_keywords",       recipe.get("fee_reject_keywords") or [])
    _set_or_del(fees, "follow_links",          recipe.get("fee_follow_links") or [])
    _set_or_del(fees, "rules_undergraduate",   recipe.get("fee_rules_undergraduate") or [])
    _set_or_del(fees, "rules_postgraduate",    recipe.get("fee_rules_postgraduate") or [])
    if not fees:
        del ext["fees"]

    # english — preserve YAML-only keys (trust_vision_ocr, default_ielts, etc.)
    eng = ext.setdefault("english", {})
    _set_or_del(eng, "course_english_priority", bool(recipe.get("course_english_priority")) or None)
    ielts = recipe.get("ielts") or {}
    for field in ("overall_regex", "band_regex", "source_xpath"):
        _set_or_del(eng, field, ielts.get(field) or "")
    _set_or_del(eng, "follow_links",             recipe.get("follow_links") or [])
    _set_or_del(eng, "degree_level_defaults",    recipe.get("degree_level_defaults") or {})
    _set_or_del(eng, "band_mapping",             recipe.get("band_mapping") or {})
    _set_or_del(eng, "band_reference_url",       recipe.get("band_reference_url") or "")
    if not eng:
        del ext["english"]

    # location
    loc_replace  = recipe.get("location_replace") or {}
    loc_allowed  = recipe.get("location_allowed_values") or []
    loc_reject   = recipe.get("location_reject_values") or []
    if loc_replace or loc_allowed or loc_reject:
        loc = ext.setdefault("location", {})
        _set_or_del(loc, "replace",        loc_replace)
        _set_or_del(loc, "allowed_values", loc_allowed)
        _set_or_del(loc, "reject_values",  loc_reject)
    elif "location" in ext:
        loc = ext["location"]
        _RECIPE_LOC_KEYS = {"replace", "allowed_values", "reject_values"}
        ext["location"] = {k: v for k, v in loc.items() if k not in _RECIPE_LOC_KEYS} or None
        if not ext["location"]:
            del ext["location"]

    # study_mode
    sm_from_loc = bool(recipe.get("study_mode_from_location"))
    sm_keywords = recipe.get("study_mode_online_keywords") or []
    if sm_from_loc or sm_keywords:
        sm = ext.setdefault("study_mode", {})
        _set_or_del(sm, "from_location",    sm_from_loc or None)
        _set_or_del(sm, "online_keywords",  sm_keywords)
    elif "study_mode" in ext:
        del ext["study_mode"]

    # intake — only patch recipe-managed keys, keep YAML-only keys
    intake_cfg = recipe.get("intake") or {}
    if intake_cfg.get("xpath") or intake_cfg.get("regex") or intake_cfg.get("month_map"):
        intake = ext.setdefault("intake", {})
        _set_or_del(intake, "xpath",      intake_cfg.get("xpath") or "")
        _set_or_del(intake, "regex",      intake_cfg.get("regex") or "")
        _set_or_del(intake, "month_map",  intake_cfg.get("month_map") or {})

    # browser actions
    recipe_actions = recipe.get("actions") or []
    yaml_actions: list[dict] = []
    for a in recipe_actions:
        at  = a.get("action_type") or ""
        val = a.get("value") or ""
        if at == "click_text":          yaml_actions.append({"click_text": val})
        elif at == "click_css":         yaml_actions.append({"click_css": val})
        elif at == "expand_text":       yaml_actions.append({"expand_text": val})
        elif at == "scroll_to":         yaml_actions.append({"scroll_to": val})
        elif at == "wait_for_text":     yaml_actions.append({"wait_for": {"text": val}})
        elif at == "wait_for_selector": yaml_actions.append({"wait_for": {"selector": val}})
    _set_or_del(ext, "actions", yaml_actions)

    # quality gates
    min_comp   = recipe.get("minimum_completeness")
    req_fields = recipe.get("required_fields") or []
    block_if   = recipe.get("block_publish_if") or []
    if (min_comp is not None and min_comp != 85) or req_fields or block_if:
        qual = ext.setdefault("quality", {})
        if min_comp is not None and min_comp != 85:
            qual["minimum_completeness"] = min_comp
        elif "minimum_completeness" in qual:
            del qual["minimum_completeness"]
        _set_or_del(qual, "required_fields", req_fields)
        _set_or_del(qual, "block_publish_if", block_if)
    elif "quality" in ext:
        qual = ext["quality"]
        _RECIPE_QUAL_KEYS = {"minimum_completeness", "required_fields", "block_publish_if"}
        ext["quality"] = {k: v for k, v in qual.items() if k not in _RECIPE_QUAL_KEYS} or None
        if not ext["quality"]:
            del ext["quality"]

    # JSON/REST API discovery — write back to generic_search_api
    api = recipe.get("api") or {}
    if api.get("endpoint"):
        gsa = disc.setdefault("generic_search_api", {})
        gsa["url"] = api["endpoint"]
        method = api.get("method") or "GET"
        if method and method != "GET":
            gsa["method"] = method
        elif "method" in gsa and gsa["method"] == "GET":
            del gsa["method"]
        # enabled
        if api.get("enabled") is False:
            gsa["enabled"] = False
        elif "enabled" in gsa and gsa["enabled"] is False and api.get("enabled") is not False:
            del gsa["enabled"]
        # browser mode
        if api.get("fetch_via_browser"):
            gsa["fetch_via_browser"] = True
        elif "fetch_via_browser" in gsa:
            del gsa["fetch_via_browser"]
        _set_or_del(gsa, "browser_seed_url", api.get("browser_seed_url") or "")
        # URL normalization
        _set_or_del(gsa, "base_url", api.get("base_url") or "")
        if api.get("normalize_relative_urls") is False:
            gsa["normalize_relative_urls"] = False
        elif "normalize_relative_urls" in gsa and gsa["normalize_relative_urls"] is False:
            del gsa["normalize_relative_urls"]
        # params / headers / paths
        _set_or_del(gsa, "params",              dict(api.get("query_params") or {}) or None)
        _set_or_del(gsa, "headers",             dict(api.get("headers") or {}) or None)
        _set_or_del(gsa, "root_path",           api.get("root_path") or "")
        _set_or_del(gsa, "course_url_template", api.get("course_url_template") or "")
        # URL fields
        if api.get("url_fields"):
            gsa["url_fields"] = list(api["url_fields"])
        elif "url_fields" in gsa:
            del gsa["url_fields"]
        if api.get("title_fields"):
            gsa["title_fields"] = list(api["title_fields"])
        elif "title_fields" in gsa:
            del gsa["title_fields"]
        # URL filters
        if api.get("api_allow_url_patterns"):
            gsa["allow_url_patterns"] = list(api["api_allow_url_patterns"])
        elif "allow_url_patterns" in gsa:
            del gsa["allow_url_patterns"]
        if api.get("api_block_url_patterns"):
            gsa["block_url_patterns"] = list(api["api_block_url_patterns"])
        elif "block_url_patterns" in gsa:
            del gsa["block_url_patterns"]
        # POST body
        if api.get("body") is not None:
            gsa["body"] = api["body"]
        elif "body" in gsa:
            del gsa["body"]
        bp = api.get("body_pagination") or {}
        if bp.get("current_path"):
            gsa["body_pagination"] = {k: v for k, v in bp.items() if v}
        elif "body_pagination" in gsa:
            del gsa["body_pagination"]
        # Additional URLs
        if api.get("additional_urls"):
            gsa["additional_urls"] = list(api["additional_urls"])
        elif "additional_urls" in gsa:
            del gsa["additional_urls"]
        # page_size / max_pages (standalone — used with body_pagination)
        if api.get("page_size"):
            gsa["page_size"] = int(api["page_size"])
        elif "page_size" in gsa and not api.get("pagination"):
            del gsa["page_size"]
        if api.get("max_pages") and api["max_pages"] != 20:
            gsa["max_pages"] = int(api["max_pages"])
        elif "max_pages" in gsa and not api.get("pagination"):
            del gsa["max_pages"]
        # query-string pagination (only when body_pagination is absent)
        pag = api.get("pagination") or {}
        if pag.get("page_size") and not bp.get("current_path"):
            gsa["page_size"]      = int(pag["page_size"])
            gsa["max_pages"]      = int(pag.get("max_pages") or 20)
            _set_or_del(gsa, "page_size_param",  pag.get("size_param") or "")
            _set_or_del(gsa, "offset_param",     pag.get("page_param") or "")
    elif "generic_search_api" in disc:
        del disc["generic_search_api"]

    # Clean up empty top-level sections
    if not disc:
        out.pop("discovery", None)
    if not ext:
        out.pop("extraction", None)

    return out


def _load_yaml_recipe(scrape_url: str) -> tuple[str | None, dict]:
    """Load and parse the YAML file for a university.

    Returns ``(slug, yaml_as_recipe_dict)`` — slug is None if no file found.
    """
    slug = _slug_for_uni(scrape_url)
    if not slug:
        return None, {}
    path = _UNIS_YAML_DIR / f"{slug}.yaml"
    if not path.exists():
        return slug, {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = _yaml_mod.safe_load(raw) or {}
        return slug, _yaml_to_recipe(data)
    except Exception:
        return slug, {}


def _write_yaml_from_recipe(slug: str, recipe: dict) -> None:
    """Patch the YAML file for *slug* with fields from the recipe editor.

    Preserves YAML-only keys (use_stealth_browser, browser_wait_strategy, …).
    Creates the file if it does not yet exist.
    """
    path = _UNIS_YAML_DIR / f"{slug}.yaml"
    existing: dict = {}
    if path.exists():
        try:
            existing = _yaml_mod.safe_load(
                path.read_text(encoding="utf-8", errors="replace")
            ) or {}
        except Exception:
            existing = {}

    updated = _recipe_to_yaml_patch(existing, recipe)
    path.write_text(
        _yaml_mod.dump(updated, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ── Recipe endpoints ──────────────────────────────────────────────────────────

@router.get("/universities/{uni_id}/recipe")
async def get_recipe(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return the advanced scraping recipe for a university.

    Merges the per-university YAML config (base) with any recipe values
    previously saved via the recipe editor (DB overlay wins field-by-field).
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = u.scrape_config or {}
    db_recipe: dict = sc.get("recipe") or {}

    # Load YAML and convert to recipe fields
    slug, yaml_recipe = _load_yaml_recipe(u.scrape_url or "")

    # Merge: YAML is the base; DB recipe overrides field-by-field
    merged = {**yaml_recipe, **db_recipe}

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "scrape_url": u.scrape_url or "",
        "recipe": merged,
        "yaml_slug": slug,
    }


@router.put("/universities/{uni_id}/recipe")
async def put_recipe(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Save the advanced scraping recipe for a university.

    The body should be the full recipe dict.  It is stored at
    scrape_config.recipe and read by the orchestrator at scrape-start time.
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(u.scrape_config or {})
    sc["recipe"] = body

    # ── Translate Recipe Editor extraction fields → admin_config ──────────────
    # These fields are stored in scrape_config.admin_config so the loader merges
    # them into UniConfig at scrape time (highest priority, above per-uni YAML).
    # This lets operators configure extraction behaviour without a code deploy.
    english_patch: dict = {}
    fee_patch: dict = {}
    extraction_patch: dict = {}
    discovery_patch: dict = {}

    if body.get("follow_links"):
        english_patch["follow_links"] = body["follow_links"]
    if body.get("band_mapping"):
        english_patch["band_mapping"] = body["band_mapping"]
    if body.get("band_reference_url"):
        english_patch["band_reference_url"] = body["band_reference_url"]
    if body.get("fee_reject_keywords"):
        fee_patch["reject_keywords"] = body["fee_reject_keywords"]
    if body.get("fee_prefer_international") is not None:
        fee_patch["prefer_international"] = bool(body["fee_prefer_international"])
    if body.get("fee_follow_links") is not None:
        fee_patch["follow_links"] = body["fee_follow_links"]
    if body.get("actions"):
        extraction_patch["actions"] = body["actions"]
    if body.get("url_rewrites"):
        extraction_patch["url_rewrites"] = body["url_rewrites"]
    if body.get("browser_time_budget_s") is not None:
        discovery_patch["browser_time_budget_s"] = int(body["browser_time_budget_s"])
    if body.get("browser_early_stop_courses") is not None:
        discovery_patch["browser_early_stop_courses"] = int(body["browser_early_stop_courses"])
    if body.get("max_candidates") is not None:
        discovery_patch["max_candidates"] = int(body["max_candidates"])
    if body.get("bfs_page_budget") is not None:
        discovery_patch["bfs_page_budget"] = int(body["bfs_page_budget"])

    if english_patch:
        extraction_patch["english"] = english_patch
    if fee_patch:
        extraction_patch["fees"] = fee_patch

    if extraction_patch or discovery_patch:
        admin_cfg: dict = dict(sc.get("admin_config") or {})
        if extraction_patch:
            existing_extraction = dict(admin_cfg.get("extraction") or {})
            for k, v in extraction_patch.items():
                if isinstance(v, dict) and isinstance(existing_extraction.get(k), dict):
                    existing_extraction[k] = {**existing_extraction[k], **v}
                else:
                    existing_extraction[k] = v
            admin_cfg["extraction"] = existing_extraction
        if discovery_patch:
            admin_cfg["discovery"] = {**dict(admin_cfg.get("discovery") or {}), **discovery_patch}
        sc["admin_config"] = admin_cfg

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    # ── Also write recipe fields back to the per-university YAML file ─────────
    yaml_slug: str | None = None
    yaml_write_error: str | None = None
    try:
        slug = _slug_for_uni(u.scrape_url or "")
        if slug:
            _write_yaml_from_recipe(slug, body)
            yaml_slug = slug
    except Exception as _ye:
        yaml_write_error = str(_ye)
        import logging as _log
        _log.getLogger(__name__).warning("put_recipe: YAML write failed for uni %s: %s", uni_id, _ye)

    return {
        "ok": True,
        "university_id": uni_id,
        "recipe": body,
        "yaml_slug": yaml_slug,
        **({"yaml_write_error": yaml_write_error} if yaml_write_error else {}),
    }


@router.post("/universities/{uni_id}/recipe/simulate")
async def simulate_recipe(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Preview how a recipe would transform recent staged courses — no re-scrape.

    Applies the supplied recipe rules in-memory against the most recent staged
    courses for this university and returns per-course before/after diffs so
    operators can validate their config before saving and re-scraping.

    Body: ``{"recipe": { ... }}``
    """
    import copy

    from app.models.scraped_course import ScrapedCourse
    from app.services.scraper.recipe_rules import apply_recipe_rules

    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    recipe: dict = body.get("recipe") or {}

    rows = (
        await db.execute(
            select(ScrapedCourse)
            .where(ScrapedCourse.university_id == uni_id)
            .order_by(ScrapedCourse.id.desc())
            .limit(15)
        )
    ).scalars().all()

    if not rows:
        return {
            "total": 0,
            "changed": 0,
            "samples": [],
            "message": "No staged courses found for this university.",
        }

    # Fields that recipe rules can transform — (payload_key, display_label)
    WATCH: list[tuple[str, str]] = [
        ("course_name", "Course Name"),
        ("degree_level", "Degree Level"),
        ("annual_tuition_fee", "Fee Amount"),
        ("fee_term", "Fee Term"),
        ("ielts_overall", "IELTS Overall"),
        ("ielts_reading", "IELTS Reading"),
        ("ielts_writing", "IELTS Writing"),
        ("ielts_listening", "IELTS Listening"),
        ("ielts_speaking", "IELTS Speaking"),
        ("course_location", "Location"),
        ("study_mode", "Study Mode"),
    ]

    samples = []
    changed_count = 0

    for sc in rows:
        before: dict = {
            "course_name": sc.course_name,
            "name": sc.course_name,
            "degree_level": sc.degree_level,
            "annual_tuition_fee": float(sc.international_fee) if sc.international_fee is not None else None,
            "fee_term": sc.fee_term,
            "ielts_overall": sc.ielts_overall,
            "ielts_reading": sc.ielts_reading,
            "ielts_writing": sc.ielts_writing,
            "ielts_listening": sc.ielts_listening,
            "ielts_speaking": sc.ielts_speaking,
            "location": sc.course_location,
            "course_location": sc.course_location,
            "study_mode": sc.study_mode,
            "duration": float(sc.duration) if sc.duration is not None else None,
            "duration_term": sc.duration_term,
        }

        after = copy.deepcopy(before)
        apply_recipe_rules(after, recipe)

        changes = []
        for field_key, field_label in WATCH:
            b_val = before.get(field_key)
            a_val = after.get(field_key)
            if b_val != a_val:
                changes.append(
                    {
                        "field": field_label,
                        "before": str(b_val) if b_val is not None else None,
                        "after": str(a_val) if a_val is not None else None,
                    }
                )

        if changes:
            changed_count += 1
            samples.append({"id": sc.id, "name": sc.course_name, "changes": changes})

    return {"total": len(rows), "changed": changed_count, "samples": samples}


@router.post("/universities/{uni_id}/recipe/test")
async def test_recipe(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Lightweight recipe discovery test — no staging, no Gemini, no DB writes.

    Body fields (all optional — uses saved recipe + YAML defaults as fallback):
      seed_urls           list[str]   Course listing page URLs to test
      must_contain        list[str]   URL substrings that discovered links must contain
      block_url_patterns  list[str]   Regex patterns to drop from discovered URLs
      expected_min_courses int | null Warn/fail if fewer courses found
      json_api            dict | null JSON API config {endpoint, root_path, course_url_template, ...}
      time_limit_s        int         Max seconds to spend (default 60)

    Returns:
      status              PASS / WARN / FAIL
      raw_found           Total course links before filters (deduped)
      after_filter_count  Links surviving filters
      seed_results        Per-seed-URL breakdown [{url, raw_found, status, sample_urls}]
      api_result          JSON API result (if configured)
      dropped_samples     First 10 dropped URLs
      kept_samples        First 10 surviving URLs
      warnings            Non-fatal issues
      recommendations     Actionable next steps
      elapsed_s           Seconds taken
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    scrape_url: str = u.scrape_url or ""
    if not scrape_url:
        raise HTTPException(status_code=400, detail="University has no scrape_url configured")

    # Merge: saved recipe → request body (body fields win)
    sc: dict = u.scrape_config or {}
    saved_recipe: dict = sc.get("recipe") or {}

    seed_urls: list[str] = body.get("seed_urls") or saved_recipe.get("seed_urls") or []
    must_contain: list[str] = body.get("must_contain") or saved_recipe.get("must_contain") or []
    block_url_patterns: list[str] = (
        body.get("block_url_patterns") or saved_recipe.get("block_url_patterns") or []
    )
    expected_min: int | None = (
        body.get("expected_min_courses") or saved_recipe.get("expected_min_courses") or None
    )
    json_api_cfg: dict | None = body.get("json_api") or saved_recipe.get("api") or None
    time_limit_s: int = int(body.get("time_limit_s") or 60)
    time_limit_s = max(10, min(time_limit_s, 120))  # clamp 10–120 s

    # Also pull seed_urls from the YAML config if nothing else is configured
    if not seed_urls:
        try:
            from app.services.scraper.config.loader import load_uni_config
            _ucfg = await load_uni_config(uni_id, db)
            yaml_seeds = list(getattr(getattr(_ucfg, "discovery", None), "seed_urls", []) or [])
            if yaml_seeds:
                seed_urls = yaml_seeds
            if not expected_min:
                expected_min = getattr(getattr(_ucfg, "discovery", None), "expected_min_courses", None)
            if not must_contain:
                must_contain = list(getattr(getattr(_ucfg, "discovery", None), "must_contain", []) or [])
        except Exception:
            pass

    from app.services.scraper.recipe_tester import test_recipe as _test
    result = await _test(
        scrape_url=scrape_url,
        seed_urls=seed_urls,
        must_contain=must_contain,
        block_url_patterns=block_url_patterns,
        expected_min_courses=expected_min,
        json_api_cfg=json_api_cfg,
        time_limit_s=time_limit_s,
    )

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "scrape_url": scrape_url,
        **result,
    }


@router.post("/universities/{uni_id}/recipe/test-api")
async def test_json_api_endpoint(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Standalone JSON API endpoint test — no staging, no Gemini, no DB writes.

    Tests the JSON API config from the recipe editor directly via the backend
    (avoids CORS issues with university APIs). Fetches page 1 and optionally
    page 2 to verify pagination, reads total count from count_path, extracts
    sample course names.

    Body: the ``api`` object from the recipe editor, e.g.:
      {
        "endpoint": "https://...",
        "method": "GET",
        "query_params": {"category": "Course", "s": "{GUID}"},
        "root_path": "Results",
        "count_path": "TotalCount",
        "course_url_template": "https://uni.edu/courses/{Url}",
        "pagination": {"type": "offset", "page_param": "p", "page_start": 1, "page_size": 20},
        "fields": {"course_name": "Title"},
        "headers": {}
      }

    Returns:
      status         'ok' | 'no_endpoint' | 'timeout' | 'http_NNN' | 'bad_root_path'
      http_status    HTTP response code
      total_from_api Total from count_path if configured
      page1_count    Items on page 1
      page2_count    Items on page 2 (pagination test)
      sample_names   Up to 8 course names from page 1
      all_keys       Top-level JSON keys in first item
      warnings       Non-fatal issues
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    # Accept either full recipe body (with api key) or the api object directly
    api_cfg: dict = body.get("api") or body

    from app.services.scraper.recipe_tester import test_json_api_standalone
    result = await test_json_api_standalone(api_cfg)
    return result


@router.post("/universities/{uni_id}/auto-repair-filter")
async def auto_repair_filter_from_discovery(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Clear a URL filter that is blocking 100% of discovered links.

    Called by the Test Discovery panel when it detects raw_discovered > 0 AND
    total_passing == 0.  Unlike the job-based version in scrape.py this endpoint
    does NOT trigger a new scrape — it only patches the config so the operator can
    immediately re-run Test Discovery and see the effect.

    Body::

        {
          "recipe_patch": {"discovery": {"allow_url_patterns": []}},
          "filter_cleared": "allow_url_patterns"
        }
    """
    import json as _json
    from sqlalchemy import text as _text

    recipe_patch: dict = body.get("recipe_patch") or {}
    filter_cleared: str = body.get("filter_cleared") or "unknown"

    if not recipe_patch:
        return {"status": "no_patch", "message": "No recipe_patch provided — nothing to apply."}

    uni_row = (await db.execute(
        _text("SELECT id, name, scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not uni_row:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(uni_row.get("scrape_config") or {})
    existing: dict = dict(sc.get("admin_config") or {})

    # Save rollback snapshot
    if existing:
        sc["_prev_admin_config"] = existing

    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    sc["admin_config"] = _deep_merge(existing, recipe_patch)

    await db.execute(
        _text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": _json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    import logging as _logging
    _logging.getLogger(__name__).info(
        "auto_repair_filter(discovery): cleared %s for uni %s → saved admin_config",
        filter_cleared, uni_id,
    )

    return {
        "status": "ok",
        "filter_cleared": filter_cleared,
        "has_rollback": bool(existing),
        "message": (
            f"Cleared '{filter_cleared}' filter. "
            "Re-run Test Discovery to confirm URLs now pass."
        ),
    }


# ── Feature: Effective Config Viewer ─────────────────────────────────────────

@router.get("/universities/{uni_id}/effective-config")
async def get_effective_config(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return the final merged config with per-field source attribution.

    Builds the config step-by-step through the priority chain and annotates
    every leaf value with the source layer that last set it:
      system_default | defaults_yaml | db_legacy | db_auto | yaml | admin_config
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    from app.services.scraper.config.loader import (
        _load_yaml_file, _deep_merge, _translate_db_scrape_config,
        _extract_auto_config, _extract_admin_config,
        _DEFAULTS_FILE, _UNIS_DIR, _hostname_to_slug,
    )
    from urllib.parse import urlparse as _up2

    sc: dict = dict(u.scrape_config or {})
    _h2 = _up2(u.scrape_url or "").hostname or ""
    slug = _hostname_to_slug(_h2) if _h2 else (u.name or "").lower().split()[0]

    layers: list[tuple[str, dict]] = []

    # 1 defaults.yaml
    d = _load_yaml_file(_DEFAULTS_FILE)
    if d:
        layers.append(("defaults_yaml", d))

    # 2 legacy DB uniPages
    db_t = _translate_db_scrape_config(sc)
    if db_t:
        layers.append(("db_legacy", db_t))

    # 3 auto_config
    auto = _extract_auto_config(sc)
    if auto:
        layers.append(("db_auto", auto))

    # 4 per-uni YAML
    uni_id_yaml = _UNIS_DIR / f"{slug}_{uni_id}.yaml"
    yaml_path = uni_id_yaml if uni_id_yaml.exists() else _UNIS_DIR / f"{slug}.yaml"
    y = _load_yaml_file(yaml_path)
    if y:
        layers.append(("yaml", y))

    # 5 admin_config (highest priority)
    adm = _extract_admin_config(sc)
    if adm:
        layers.append(("admin_config", adm))

    # Build annotated tree: walk each layer and record which source set each leaf
    def _annotate(base_ann: dict, layer_dict: dict, source: str) -> dict:
        result = dict(base_ann)
        for k, v in layer_dict.items():
            if isinstance(v, dict) and isinstance(result.get(k), dict) and not isinstance(result[k], dict):
                result[k] = _annotate(result.get(k, {}), v, source)
            elif isinstance(v, dict) and isinstance(result.get(k, {}), dict):
                result[k] = _annotate(result.get(k, {}), v, source)
            else:
                result[k] = {"value": v, "source": source}
        return result

    annotated: dict = {}
    for src_name, layer_data in layers:
        annotated = _annotate(annotated, layer_data, src_name)

    # Also produce a flat "overrides only" summary for the admin_config panel
    admin_flat: list[dict] = []

    def _flatten_admin(d2: dict, prefix: str = "") -> None:
        for k, v in d2.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and not k.startswith("_"):
                _flatten_admin(v, path)
            else:
                admin_flat.append({"path": path, "value": v})

    if adm:
        _flatten_admin(adm)

    has_yaml = bool(y)
    yaml_slug = yaml_path.name if yaml_path.exists() else None

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "slug": slug,
        "yaml_slug": yaml_slug,
        "has_yaml": has_yaml,
        "layers_present": [l[0] for l in layers],
        "annotated_config": annotated,
        "admin_config_raw": adm,
        "admin_overrides_flat": admin_flat,
        "has_admin_overrides": bool(adm),
    }


@router.delete("/universities/{uni_id}/admin-config")
async def clear_admin_config(
    uni_id: int,
    body: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Clear admin_config overrides for a university.

    Body (optional):
      { "keys": ["extraction.filters.online_only"] }  → clear specific dotted paths
      {}                                               → clear ALL admin_config
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(u.scrape_config or {})
    keys_to_clear: list[str] = body.get("keys") or []

    if not keys_to_clear:
        # Clear everything — preserve _prev_admin_config for rollback
        old = sc.get("admin_config") or {}
        if old:
            sc["_prev_admin_config"] = old
        sc["admin_config"] = {}
        cleared = "all"
    else:
        adm: dict = dict(sc.get("admin_config") or {})
        sc["_prev_admin_config"] = copy.deepcopy(adm)
        for dotted_key in keys_to_clear:
            parts = dotted_key.split(".")
            node = adm
            for p in parts[:-1]:
                if isinstance(node.get(p), dict):
                    node = node[p]
                else:
                    node = None
                    break
            if node is not None and parts[-1] in node:
                del node[parts[-1]]
        sc["admin_config"] = adm
        cleared = keys_to_clear

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {"ok": True, "university_id": uni_id, "cleared": cleared}


# ── Feature: Rejection Log ────────────────────────────────────────────────────

@router.get("/universities/{uni_id}/rejection-log")
async def get_rejection_log(
    uni_id: int,
    job_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Return every skipped/rejected URL and course for a scrape job.

    If job_id is omitted, uses the most recent completed job for this university.
    Reads scrape_runtime_logs rows with kind='skipped' and aggregates by reason.
    """
    # Resolve job_id
    if not job_id:
        row = (await db.execute(
            text("""
                SELECT runtime_job_id FROM scrape_runtime_jobs
                WHERE university_id = :uid AND status IN ('completed','done','failed')
                ORDER BY created_at DESC LIMIT 1
            """),
            {"uid": uni_id},
        )).mappings().first()
        if not row:
            return {"university_id": uni_id, "job_id": None, "rejections": [], "summary": {}, "total": 0}
        job_id = row["runtime_job_id"]

    rows = (await db.execute(
        text("""
            SELECT payload, created_at
            FROM scrape_runtime_logs
            WHERE runtime_job_id = :jid
              AND payload->>'kind' = 'skipped'
            ORDER BY sequence ASC
            LIMIT 1000
        """),
        {"jid": job_id},
    )).mappings().all()

    REASON_LABELS: dict[str, dict] = {
        "online_only": {
            "label": "Online-only filter",
            "description": "Study mode was detected as 'Online'. If this is wrong, add the host to _STUDY_MODE_RULE_SUPPRESSED_HOSTS or set extraction.filters.online_only.enabled: false.",
            "config_key": "extraction.filters.online_only.enabled",
            "severity": "warning",
        },
        "domestic_only": {
            "label": "Domestic-only course",
            "description": "The course was flagged as domestic-only (no international pricing). Set extraction.filters.domestic_only.enabled: false to disable this filter.",
            "config_key": "extraction.filters.domestic_only.enabled",
            "severity": "warning",
        },
        "category_landing_page": {
            "label": "Category/landing page",
            "description": "URL looks like a subject category or faculty index page, not an individual course. Add discovery.allow_url_patterns to restrict to course-level URLs.",
            "config_key": "discovery.allow_url_patterns",
            "severity": "info",
        },
        "generic_category_page": {
            "label": "Generic category page",
            "description": "URL matched a generic category pattern (no degree-level qualifier in the title). Add discovery.allow_url_patterns to restrict to course-level URLs.",
            "config_key": "discovery.allow_url_patterns",
            "severity": "info",
        },
        "no_international_fee": {
            "label": "No international fee",
            "description": "No international fee was found. If fees are on a central page, set extraction.fees.central_page.",
            "config_key": "extraction.fees.central_page",
            "severity": "warning",
        },
        "missing_mandatory_field": {
            "label": "Missing mandatory field",
            "description": "A field listed in staging.reject_if_missing was absent.",
            "config_key": "extraction.staging.reject_if_missing",
            "severity": "critical",
        },
        "duplicate": {
            "label": "Duplicate course",
            "description": "A course with the same name was already staged in this run.",
            "config_key": None,
            "severity": "info",
        },
        "recently_rejected": {
            "label": "Recently rejected (within 7d)",
            "description": "This URL was manually rejected in the last 7 days and is being suppressed automatically.",
            "config_key": None,
            "severity": "info",
        },
    }

    rejections: list[dict] = []
    summary: dict[str, int] = {}

    for r in rows:
        p = r["payload"] or {}
        reason_raw: str = (p.get("reason") or "unknown").strip()
        # Normalise: strip "rejected: " prefix (actual DB format is "rejected: domestic_only")
        _rn = reason_raw.lower()
        if _rn.startswith("rejected:"):
            _rn = _rn[len("rejected:"):].strip()
        elif _rn.startswith("recently rejected"):
            _rn = "recently_rejected"
        reason_key = _rn.replace(" ", "_").replace("-", "_")[:40]
        meta = REASON_LABELS.get(reason_key, {
            "label": reason_raw.replace("_", " ").title(),
            "description": "The course or URL was rejected by the staging gate.",
            "config_key": None,
            "severity": "info",
        })
        # Extract name/url from the event text in the payload
        event_text: str = p.get("message") or p.get("event") or ""
        url: str = p.get("url") or ""
        course_name: str = p.get("name") or ""
        if not course_name and event_text:
            import re as _re2
            m2 = _re2.search(r"skipped (.+?):", event_text)
            if m2:
                course_name = m2.group(1).strip()

        rejections.append({
            "reason": reason_key,
            "reason_label": meta["label"],
            "description": meta["description"],
            "config_key": meta["config_key"],
            "severity": meta["severity"],
            "course_name": course_name,
            "url": url,
            "ts": r["created_at"].isoformat() if r["created_at"] else None,
        })
        summary[reason_key] = summary.get(reason_key, 0) + 1

    summary_display = [
        {
            "reason": k,
            "reason_label": REASON_LABELS.get(k, {"label": k.replace("_"," ").title()})["label"],
            "count": v,
            "severity": REASON_LABELS.get(k, {"severity": "info"})["severity"],
            "config_key": REASON_LABELS.get(k, {"config_key": None})["config_key"],
        }
        for k, v in sorted(summary.items(), key=lambda x: -x[1])
    ]

    return {
        "university_id": uni_id,
        "job_id": job_id,
        "total": len(rejections),
        "summary": summary_display,
        "rejections": rejections,
    }


# ── Feature: Extraction Debugger ─────────────────────────────────────────────

# Human-readable field labels for the UI
_FIELD_LABELS: dict[str, str] = {
    "course_name": "Course Name", "degree_level": "Degree Level",
    "category": "Category", "sub_category": "Sub-category",
    "study_mode": "Study Mode", "course_location": "Location",
    "duration": "Duration", "duration_term": "Duration Term",
    "international_fee": "Int'l Fee", "fee_term": "Fee Term", "currency": "Currency",
    "ielts_overall": "IELTS Overall", "ielts_listening": "IELTS Listening",
    "ielts_speaking": "IELTS Speaking", "ielts_writing": "IELTS Writing",
    "ielts_reading": "IELTS Reading",
    "pte_overall": "PTE Overall", "toefl_overall": "TOEFL Overall",
    "cambridge_overall": "Cambridge Overall", "duolingo_overall": "Duolingo Overall",
    "intake_months": "Intakes", "academic_level": "Academic Level",
    "academic_score": "Academic Score", "description": "Description",
    "other_requirement": "Other Requirement", "cricos_code": "CRICOS Code",
    "scholarship": "Scholarship", "student_market": "Student Market",
    "eligibility_status": "Eligibility", "international_eligible": "Intl Eligible",
}


@router.get("/universities/{uni_id}/scraped-courses")
async def list_scraped_courses(
    uni_id: int,
    job_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Return courses from the last scrape job with completeness + extraction methods."""
    if not job_id:
        row = (await db.execute(
            text("""
                SELECT runtime_job_id FROM scrape_runtime_jobs
                WHERE university_id = :uid AND status IN ('completed','done','failed')
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": uni_id},
        )).mappings().first()
        if not row:
            return {"university_id": uni_id, "job_id": None, "courses": []}
        job_id = row["runtime_job_id"]

    # scraped_courses.scrape_job_id IS the runtime_job_id string directly
    courses = (await db.execute(
        text("""
            SELECT id, course_name, status, completeness, auto_publish_status,
                   study_mode, degree_level, international_fee, ielts_overall,
                   course_website, extraction_method
            FROM scraped_courses
            WHERE university_id = :uid
              AND scrape_job_id = :jid
            ORDER BY completeness DESC NULLS LAST, course_name ASC
            LIMIT 200
        """), {"uid": uni_id, "jid": job_id},
    )).mappings().all()

    return {
        "university_id": uni_id,
        "job_id": job_id,
        "courses": [
            {
                "id": c["id"],
                "course_name": c["course_name"],
                "status": c["status"],
                "completeness": c["completeness"],
                "auto_publish_status": c["auto_publish_status"],
                "study_mode": c["study_mode"],
                "degree_level": c["degree_level"],
                "international_fee": c["international_fee"],
                "ielts_overall": c["ielts_overall"],
                "course_website": c["course_website"],
                "extraction_method": c["extraction_method"],
            }
            for c in courses
        ],
    }


@router.get("/universities/{uni_id}/scraped-courses/{course_id}/extraction-trace")
async def get_extraction_trace(
    uni_id: int,
    course_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Return the full extraction pipeline trace for one staged course.

    For each field: lists all candidate evidence rows (raw → method → normalised),
    marks which was selected, and shows the final value actually stored.
    """
    # Load the course
    course_row = (await db.execute(
        text("""
            SELECT id, course_name, study_mode, degree_level, category, sub_category,
                   course_location, duration, duration_term, international_fee,
                   fee_term, currency, ielts_overall, ielts_listening, ielts_speaking,
                   ielts_writing, ielts_reading, pte_overall, toefl_overall,
                   cambridge_overall, duolingo_overall, intake_months, academic_level,
                   academic_score, description, other_requirement, cricos_code,
                   completeness, status, auto_publish_status, course_website,
                   extraction_method, eligibility_status, international_eligible,
                   student_market, scholarship
            FROM scraped_courses
            WHERE id = :cid AND university_id = :uid
        """), {"cid": course_id, "uid": uni_id},
    )).mappings().first()
    if not course_row:
        raise HTTPException(status_code=404, detail="Course not found")

    # Load all evidence rows for this course
    evidence_rows = (await db.execute(
        text("""
            SELECT field_key, candidate_value, normalized_value, extraction_method,
                   confidence, selected, raw_text, snippet, source_url, page_type,
                   decision_score, validation_status
            FROM scraped_field_evidence
            WHERE scraped_course_id = :cid
            ORDER BY field_key, selected DESC, confidence DESC NULLS LAST
        """), {"cid": course_id},
    )).mappings().all()

    # Build per-field pipeline map from evidence
    fields_with_evidence: set[str] = set()
    evidence_by_field: dict[str, list[dict]] = {}
    for ev in evidence_rows:
        fk = ev["field_key"]
        fields_with_evidence.add(fk)
        evidence_by_field.setdefault(fk, []).append({
            "candidate_value": ev["candidate_value"],
            "normalized_value": ev["normalized_value"],
            "extraction_method": ev["extraction_method"],
            "confidence": ev["confidence"],
            "selected": ev["selected"],
            "snippet": (ev["snippet"] or "")[:300],
            "source_url": ev["source_url"],
            "page_type": ev["page_type"],
            "validation_status": ev["validation_status"],
        })

    # Build final-values dict from scraped_courses columns
    course_d = dict(course_row)
    _FINAL_FIELDS = [
        "course_name", "degree_level", "category", "sub_category", "study_mode",
        "course_location", "duration", "duration_term", "international_fee", "fee_term",
        "currency", "ielts_overall", "ielts_listening", "ielts_speaking", "ielts_writing",
        "ielts_reading", "pte_overall", "toefl_overall", "cambridge_overall",
        "duolingo_overall", "intake_months", "academic_level", "academic_score",
        "description", "other_requirement", "cricos_code", "scholarship",
        "student_market", "eligibility_status", "international_eligible",
    ]

    extraction_method_map: dict[str, str] = course_d.get("extraction_method") or {}

    pipeline: list[dict] = []
    all_fields = set(_FINAL_FIELDS) | fields_with_evidence
    for field in sorted(all_fields):
        final_val = course_d.get(field)
        if final_val is None and field not in fields_with_evidence:
            continue  # skip fully absent fields with no evidence
        method = extraction_method_map.get(field)
        ev_list = evidence_by_field.get(field, [])
        # Find the selected (winning) evidence
        selected_ev = next((e for e in ev_list if e["selected"]), None)
        # Determine if the value was actually used
        final_display = (
            json.dumps(final_val) if isinstance(final_val, (list, dict))
            else str(final_val) if final_val is not None else None
        )
        pipeline.append({
            "field_key": field,
            "field_label": _FIELD_LABELS.get(field, field.replace("_", " ").title()),
            "final_value": final_display,
            "extraction_method": method or (selected_ev["extraction_method"] if selected_ev else None),
            "confidence": selected_ev["confidence"] if selected_ev else None,
            "snippet": selected_ev["snippet"] if selected_ev else None,
            "source_url": selected_ev["source_url"] if selected_ev else None,
            "candidates_count": len(ev_list),
            "candidates": ev_list[:8],  # cap for response size
            "has_conflict": len(ev_list) > 1 and not selected_ev,
            "missing": final_val is None,
        })

    return {
        "university_id": uni_id,
        "course_id": course_id,
        "course_name": course_d["course_name"],
        "completeness": course_d["completeness"],
        "status": course_d["status"],
        "course_website": course_d["course_website"],
        "pipeline": pipeline,
    }


# ── Feature: Discovery Debugger ───────────────────────────────────────────────

@router.get("/universities/{uni_id}/discovery-stats")
async def get_discovery_stats(
    uni_id: int,
    job_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Return URL filter stats for a scrape job.

    Reads scrape_runtime_logs events:
      block_url_filter / extract_block_url_filter — block patterns
      extract_allow_url_filter                   — allow-list filter
      must_contain_filter / extract_must_contain_filter — substring filter
      page_classified                            — BFS page classification
    """
    if not job_id:
        row = (await db.execute(
            text("""
                SELECT runtime_job_id FROM scrape_runtime_jobs
                WHERE university_id = :uid AND status IN ('completed','done','failed')
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": uni_id},
        )).mappings().first()
        if not row:
            return {"university_id": uni_id, "job_id": None, "events": [], "summary": {}}
        job_id = row["runtime_job_id"]

    rows = (await db.execute(
        text("""
            SELECT payload
            FROM scrape_runtime_logs
            WHERE runtime_job_id = :jid
              AND payload->>'kind' IN (
                'block_url_filter','extract_block_url_filter',
                'extract_allow_url_filter',
                'must_contain_filter','extract_must_contain_filter',
                'page_classified','discovery_failed'
              )
            ORDER BY sequence ASC
            LIMIT 500
        """), {"jid": job_id},
    )).mappings().all()

    events: list[dict] = []
    summary = {
        "total_blocked_by_block_patterns": 0,
        "total_blocked_by_allow_patterns": 0,
        "total_blocked_by_must_contain": 0,
        "pages_classified": 0,
        "pattern_breakdown": {},
        "blocked_samples": [],
        "allow_dropped_samples": [],
        "must_contain_dropped_samples": [],
    }

    for r in rows:
        p = r["payload"] or {}
        kind = p.get("kind", "")
        events.append({
            "kind": kind,
            "phase": p.get("phase", ""),
            "dropped": p.get("dropped", 0),
            "kept": p.get("kept", 0),
            "drop_pct": p.get("drop_pct"),
            "message": p.get("message", ""),
            "dropped_sample": p.get("dropped_sample", []),
            "pattern_breakdown": p.get("pattern_breakdown", {}),
        })
        if kind in ("block_url_filter", "extract_block_url_filter"):
            d = p.get("dropped", 0)
            summary["total_blocked_by_block_patterns"] += d
            for pat, cnt in (p.get("pattern_breakdown") or {}).items():
                summary["pattern_breakdown"][pat] = summary["pattern_breakdown"].get(pat, 0) + cnt
            summary["blocked_samples"].extend(p.get("dropped_sample") or [])
        elif kind == "extract_allow_url_filter":
            summary["total_blocked_by_allow_patterns"] += p.get("dropped", 0)
            summary["allow_dropped_samples"].extend(p.get("dropped_sample") or [])
        elif kind in ("must_contain_filter", "extract_must_contain_filter"):
            summary["total_blocked_by_must_contain"] += p.get("dropped", 0)
            summary["must_contain_dropped_samples"].extend(p.get("dropped_sample") or [])
        elif kind == "page_classified":
            summary["pages_classified"] += 1

    # Trim sample lists
    summary["blocked_samples"] = list(dict.fromkeys(summary["blocked_samples"]))[:20]
    summary["allow_dropped_samples"] = list(dict.fromkeys(summary["allow_dropped_samples"]))[:20]
    summary["must_contain_dropped_samples"] = list(dict.fromkeys(summary["must_contain_dropped_samples"]))[:20]
    # Sort pattern breakdown
    summary["pattern_breakdown"] = dict(
        sorted(summary["pattern_breakdown"].items(), key=lambda x: -x[1])
    )

    return {
        "university_id": uni_id,
        "job_id": job_id,
        "summary": summary,
        "events": events,
    }


@router.post("/universities/{uni_id}/test-url")
async def test_url_against_config(
    uni_id: int,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Test a URL against the university's current config patterns.

    Body: { "url": "https://example.edu/courses/master-of-science" }
    Returns: { accepted, blocked_by, matched_pattern, reason }
    """
    import re as _re3
    url: str = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")

    u = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    from app.services.scraper.config.loader import get_config_for_host
    from urllib.parse import urlparse as _up3

    hostname = _up3(u.scrape_url or "").hostname or ""
    cfg = get_config_for_host(
        hostname=hostname,
        name=u.name or "",
        scrape_url=u.scrape_url or "",
        university_id=int(u.id),
        db_scrape_config=dict(u.scrape_config or {}),
    )

    disc = cfg.discovery
    block_patterns: list[str] = list(disc.block_url_patterns or [])
    allow_patterns: list[str] = list(disc.allow_url_patterns or [])
    must_contain: list[str] = list(disc.must_contain or [])

    test_url_lower = url.lower()

    # Check block patterns first
    for pat in block_patterns:
        try:
            if _re3.search(pat, url, _re3.IGNORECASE):
                return {
                    "accepted": False,
                    "blocked_by": "block_url_patterns",
                    "matched_pattern": pat,
                    "reason": f"URL matches block pattern: {pat}",
                    "block_patterns": block_patterns,
                    "allow_patterns": allow_patterns,
                    "must_contain": must_contain,
                }
        except _re3.error:
            pass

    # Check allow patterns (if any — URL must match at least one)
    if allow_patterns:
        matched_allow = None
        for pat in allow_patterns:
            try:
                if _re3.search(pat, url, _re3.IGNORECASE):
                    matched_allow = pat
                    break
            except _re3.error:
                pass
        if not matched_allow:
            return {
                "accepted": False,
                "blocked_by": "allow_url_patterns",
                "matched_pattern": None,
                "reason": "URL does not match any allow_url_patterns (whitelist is active)",
                "block_patterns": block_patterns,
                "allow_patterns": allow_patterns,
                "must_contain": must_contain,
            }

    # Check must_contain (URL must contain ALL substrings)
    for sub in must_contain:
        if sub.lower() not in test_url_lower:
            return {
                "accepted": False,
                "blocked_by": "must_contain",
                "matched_pattern": sub,
                "reason": f"URL is missing required substring: {sub}",
                "block_patterns": block_patterns,
                "allow_patterns": allow_patterns,
                "must_contain": must_contain,
            }

    return {
        "accepted": True,
        "blocked_by": None,
        "matched_pattern": None,
        "reason": "URL passes all filters",
        "block_patterns": block_patterns,
        "allow_patterns": allow_patterns,
        "must_contain": must_contain,
    }


@router.post("/universities/{uni_id}/ai-root-cause")
async def ai_root_cause_analysis(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """
    Gather all 7 debugger data sources for a university and run AI root-cause analysis.

    Returns structured diagnosis: issue_summary, root_cause_category, confidence,
    evidence[], fix_recommendation, fix_yaml_snippet, safe_fix, risk_label,
    developer_required, developer_note.
    """
    import os as _os
    from collections import Counter as _Counter

    # ── 1. University + scrape_config ────────────────────────────────────────
    uni: University | None = await db.get(University, uni_id)
    if not uni:
        raise HTTPException(status_code=404, detail="University not found")

    sc_dict: dict = dict(uni.scrape_config or {})
    admin_cfg: dict = sc_dict.get("admin_config") or {}
    scrape_url: str = getattr(uni, "scrape_url", "") or ""

    # ── 2. Last 2 scrape jobs ────────────────────────────────────────────────
    jobs_res = await db.execute(
        text("""
            SELECT runtime_job_id, status, total_found, imported, errors, skipped,
                   total_gemini_cost_usd, cost_ceiling_hit, error_message,
                   EXTRACT(EPOCH FROM (completed_at - started_at))::int AS duration_s,
                   created_at
            FROM scrape_runtime_jobs
            WHERE university_id = :uni_id
            ORDER BY created_at DESC
            LIMIT 2
        """),
        {"uni_id": uni_id},
    )
    jobs = [dict(r._mapping) for r in jobs_res]
    last_job_id: str | None = jobs[0]["runtime_job_id"] if jobs else None

    # ── 3. Scraped courses summary (last job) ────────────────────────────────
    _job_filter = "AND scrape_job_id = :job_id" if last_job_id else ""
    _job_params: dict = {"uni_id": uni_id}
    if last_job_id:
        _job_params["job_id"] = last_job_id

    courses_res = await db.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
                COUNT(*) FILTER (WHERE status = 'approved') AS approved,
                COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                COUNT(*) FILTER (WHERE auto_publish_status = 'review') AS in_review,
                ROUND(AVG(completeness))   AS avg_completeness,
                COUNT(*) FILTER (WHERE completeness < 70) AS low_completeness_count
            FROM scraped_courses
            WHERE university_id = :uni_id
              {_job_filter}
        """),
        _job_params,
    )
    courses_summary = dict(courses_res.mappings().first() or {})

    lc_res = await db.execute(
        text(f"""
            SELECT course_name, completeness, auto_publish_status, status,
                   international_fee, ielts_overall, study_mode, academic_level
            FROM scraped_courses
            WHERE university_id = :uni_id
              AND completeness < 70
              {_job_filter}
            ORDER BY completeness ASC NULLS FIRST
            LIMIT 5
        """),
        _job_params,
    )
    low_completeness_samples = [dict(r._mapping) for r in lc_res]

    # ── 4. Rejection / block log (last job) ──────────────────────────────────
    rejection_rows: list[dict] = []
    if last_job_id:
        rej_res = await db.execute(
            text("""
                SELECT
                    payload->>'kind'    AS kind,
                    payload->>'url'     AS url,
                    payload->>'reason'  AS reason,
                    payload->>'pattern' AS pattern
                FROM scrape_runtime_logs
                WHERE runtime_job_id = :job_id
                  AND payload->>'kind' IN (
                      'block_url_filter','extract_block_url_filter',
                      'extract_allow_url_filter','must_contain_filter',
                      'extract_must_contain_filter','domestic_only_filter',
                      'rejected_course'
                  )
                LIMIT 200
            """),
            {"job_id": last_job_id},
        )
        rejection_rows = [dict(r._mapping) for r in rej_res]

    rejection_agg: dict[str, int] = _Counter(
        f"{r.get('kind', '?')}:{r.get('reason') or r.get('pattern') or '?'}"
        for r in rejection_rows
    )
    top_rejections = sorted(rejection_agg.items(), key=lambda x: -x[1])[:10]

    # ── 5. Discovery event summary (last job) ────────────────────────────────
    discovery_agg: dict[str, int] = {}
    if last_job_id:
        disc_res = await db.execute(
            text("""
                SELECT payload->>'kind' AS kind, COUNT(*) AS cnt
                FROM scrape_runtime_logs
                WHERE runtime_job_id = :job_id
                  AND payload->>'kind' IN (
                      'block_url_filter','extract_allow_url_filter',
                      'must_contain_filter','page_classified'
                  )
                GROUP BY payload->>'kind'
            """),
            {"job_id": last_job_id},
        )
        discovery_agg = {r.kind: int(r.cnt) for r in disc_res}

    # ── 6. Scrape run alerts (last job) ─────────────────────────────────────
    alerts: list[dict] = []
    if last_job_id:
        alt_res = await db.execute(
            text("""
                SELECT rule_id, severity, message, acknowledged
                FROM scrape_run_alerts
                WHERE scrape_run_id = :job_id
                ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END
                LIMIT 10
            """),
            {"job_id": last_job_id},
        )
        alerts = [dict(r._mapping) for r in alt_res]

    # ── 7. Config history (last 3 versions) ──────────────────────────────────
    config_history: list[dict] = []
    slug_for_hist: str | None = None
    if scrape_url:
        from urllib.parse import urlparse as _up_h
        _host_h = (_up_h(scrape_url).hostname or "")
        _bare_h = re.sub(r"^www\.", "", _host_h).split(".")[0]
        if _bare_h:
            slug_for_hist = _bare_h
    if slug_for_hist:
        hist_res = await db.execute(
            text("""
                SELECT id, slug, saved_at, saved_by,
                       LEFT(yaml_content, 300) AS yaml_preview
                FROM scraper_config_history
                WHERE slug = :slug
                ORDER BY saved_at DESC
                LIMIT 3
            """),
            {"slug": slug_for_hist},
        )
        config_history = [dict(r._mapping) for r in hist_res]

    # ── 8. Effective merged config (key fields) ──────────────────────────────
    eff_cfg_summary: dict = {}
    try:
        from app.services.scraper.config.loader import get_config_for_host as _gcfh
        from urllib.parse import urlparse as _up_e
        _host_e = (_up_e(scrape_url).hostname or "") if scrape_url else ""
        if _host_e:
            _eff = _gcfh(
                hostname=_host_e,
                name=getattr(uni, "name", ""),
                scrape_url=scrape_url,
                university_id=uni_id,
                db_scrape_config=sc_dict,
            )
            _disc = _eff.discovery
            _extr = _eff.extraction
            _dom = (
                _extr.filters.domestic_only
                if _extr and _extr.filters and _extr.filters.domestic_only
                else None
            )
            eff_cfg_summary = {
                "block_url_patterns":    list(_disc.block_url_patterns or []),
                "allow_url_patterns":    list(_disc.allow_url_patterns or []),
                "must_contain":          list(_disc.must_contain or []),
                "domestic_only_enabled": getattr(_dom, "enabled", False),
                "domestic_only_text":    getattr(_dom, "text_must_appear_in", None),
                "bfs_page_budget":       getattr(_disc, "bfs_page_budget", None),
                "seed_urls":             list((_disc.seed_urls or []))[:3],
            }
    except Exception as _e:
        eff_cfg_summary = {"load_error": str(_e)}

    # ── 9. Historical URL simulation against CURRENT config ──────────────────
    # Simulates the current active filter against historical course URLs so the
    # AI sees whether the *current* config's filter is the problem — even if the
    # last scrape ran with a different (older) config.
    hist_sim_summary: dict = {}
    try:
        import re as _re_sim

        _sim_allow = [
            _re_sim.compile(p, _re_sim.IGNORECASE)
            for p in eff_cfg_summary.get("allow_url_patterns", [])
            if p
        ]
        _sim_block = [
            _re_sim.compile(p, _re_sim.IGNORECASE)
            for p in eff_cfg_summary.get("block_url_patterns", [])
            if p
        ]
        _sim_mc = [m.lower() for m in eff_cfg_summary.get("must_contain", []) if m]

        def _sim_passes(url: str) -> bool:
            ul = url.lower()
            if _sim_allow and not any(pat.search(url) for pat in _sim_allow):
                return False
            if _sim_mc and not any(m in ul for m in _sim_mc):
                return False
            if _sim_block and any(pat.search(url) for pat in _sim_block):
                return False
            return True

        _hist_url_res = await db.execute(
            text(
                "SELECT DISTINCT course_website FROM scraped_courses "
                "WHERE university_id = :uid AND course_website IS NOT NULL LIMIT 200"
            ),
            {"uid": uni_id},
        )
        _hist_urls = [r[0] for r in _hist_url_res if r[0]]
        if _hist_urls:
            _hist_pass = sum(1 for _hu in _hist_urls if _sim_passes(_hu))
            _hist_pass_pct = round(_hist_pass / len(_hist_urls) * 100)
            hist_sim_summary = {
                "total": len(_hist_urls),
                "passing": _hist_pass,
                "blocked": len(_hist_urls) - _hist_pass,
                "pass_pct": _hist_pass_pct,
                "has_filters": bool(_sim_allow or _sim_block or _sim_mc),
            }
    except Exception as _sim_e:
        hist_sim_summary = {"error": str(_sim_e)[:100]}

    # ── Build context document ───────────────────────────────────────────────
    lines: list[str] = []

    lines.append(f"=== UNIVERSITY ===\nName: {uni.name}\nScrape URL: {scrape_url}\nUni ID: {uni_id}")

    if jobs:
        j = jobs[0]
        lines.append(
            f"\n=== LAST SCRAPE JOB ===\n"
            f"Status: {j.get('status','?')}  |  "
            f"Found: {j.get('total_found',0)}  Imported: {j.get('imported',0)}  "
            f"Errors: {j.get('errors',0)}  Skipped: {j.get('skipped',0)}\n"
            f"Gemini cost: ${float(j.get('total_gemini_cost_usd') or 0):.4f}  "
            f"Cost ceiling hit: {j.get('cost_ceiling_hit',False)}\n"
            f"Duration: {j.get('duration_s','?')}s  |  "
            f"Error message: {j.get('error_message') or 'None'}"
        )
    else:
        lines.append("\n=== LAST SCRAPE JOB ===\nNo scrape jobs found for this university.")

    cs = courses_summary
    lines.append(
        f"\n=== SCRAPED COURSES SUMMARY ===\n"
        f"Total: {cs.get('total',0)}  Pending: {cs.get('pending',0)}  "
        f"Approved: {cs.get('approved',0)}  Rejected: {cs.get('rejected',0)}  "
        f"In review: {cs.get('in_review',0)}\n"
        f"Avg completeness: {cs.get('avg_completeness',0)}%  "
        f"Low (<70%): {cs.get('low_completeness_count',0)} courses"
    )

    if low_completeness_samples:
        lines.append("\n=== LOW COMPLETENESS COURSES (sample) ===")
        for s in low_completeness_samples:
            lines.append(
                f"  - {s.get('course_name','?')}  {s.get('completeness',0)}%  "
                f"fee={s.get('international_fee')}  ielts={s.get('ielts_overall')}  "
                f"mode={s.get('study_mode')}  level={s.get('academic_level')}"
            )

    if top_rejections:
        lines.append("\n=== REJECTION/BLOCK LOG (aggregated, last job) ===")
        for kind, count in top_rejections:
            lines.append(f"  - {kind}: {count} times")

    if discovery_agg:
        lines.append("\n=== DISCOVERY EVENT COUNTS (last job) ===")
        for k, v in discovery_agg.items():
            lines.append(f"  - {k}: {v}")

    if alerts:
        lines.append("\n=== SCRAPE RUN ALERTS (last job) ===")
        for a in alerts:
            lines.append(
                f"  - [{(a.get('severity') or '?').upper()}] "
                f"{a.get('rule_id','?')}: {a.get('message','?')}  "
                f"(acknowledged={a.get('acknowledged',False)})"
            )

    lines.append(f"\n=== EFFECTIVE MERGED CONFIG (key fields) ===\n{json.dumps(eff_cfg_summary, indent=2)}")

    if admin_cfg:
        lines.append(f"\n=== ADMIN OVERRIDES (admin_config) ===\n{json.dumps(admin_cfg, indent=2)}")
    else:
        lines.append("\n=== ADMIN OVERRIDES ===\nNone active.")

    if config_history:
        lines.append("\n=== RECENT CONFIG HISTORY (last 3 versions) ===")
        for h in config_history:
            lines.append(f"  Version {h['id']} saved {h['saved_at']} by {h.get('saved_by') or 'system'}:")
            lines.append(f"  {h['yaml_preview']!r}")

    # Staleness note — config saved after the last scrape ran
    _last_job_ts = str(jobs[0]["created_at"])[:19] if jobs else None
    _cfg_saved_ts = str(config_history[0]["saved_at"])[:19] if config_history else None
    _config_is_stale = bool(_last_job_ts and _cfg_saved_ts and _cfg_saved_ts > _last_job_ts)
    if _config_is_stale:
        lines.append(
            f"\n=== CONFIG STALENESS WARNING ===\n"
            f"The last scrape ran at {_last_job_ts} but the config was last saved at {_cfg_saved_ts} — "
            f"AFTER the scrape. All scrape statistics above reflect the OLD config.\n"
            f"The CURRENT FILTER SIMULATION below shows what the current config actually does. "
            f"Weight that evidence heavily and be explicit that the job data is stale."
        )

    # Current filter simulation (always include when historical URLs are available)
    if hist_sim_summary and "error" not in hist_sim_summary and hist_sim_summary.get("total", 0) > 0:
        _pct = hist_sim_summary["pass_pct"]
        _verdict = (
            f"IMPORTANT: {_pct}% of historical course URLs PASS the current filter — "
            "the filter is NOT causing the problem. Root cause is in extraction, staging gate, or "
            "JS rendering, NOT in URL filtering."
            if _pct >= 60 else
            f"IMPORTANT: Only {_pct}% of historical course URLs pass the current filter — "
            "the current filter is likely blocking too many courses."
        )
        lines.append(
            f"\n=== CURRENT FILTER SIMULATION (current config vs {hist_sim_summary['total']} historical URLs) ===\n"
            f"  Passing: {hist_sim_summary['passing']}  Blocked: {hist_sim_summary['blocked']}  ({_pct}% pass rate)\n"
            f"  {_verdict}"
        )

    context_doc = "\n".join(lines)

    # ── Call Gemini ──────────────────────────────────────────────────────────
    api_key = _os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    try:
        from google import genai as _gai
        from google.genai import types as _gtypes
        _gc = _gai.Client(api_key=api_key)
    except Exception as _exc:
        raise HTTPException(status_code=503, detail=f"Gemini client error: {_exc}") from _exc

    prompt = f"""You are an expert university scraper diagnostic system. Analyse the real operational data below and identify the root cause of any issues.

STRICT RULES:
1. Base your analysis ONLY on the provided data — do not invent or guess.
2. If the data shows healthy operation (good import counts, completeness ≥85%, no critical alerts), set root_cause_category to "healthy" and say so.
3. Every evidence item MUST quote an exact value from the data context.
4. safe_fix rules — only suggest ONE of these two actions, or null:
   a. "clear_admin_override": an admin_config key is causing the problem → key = dot-notation path of that key
   b. "set_admin_override": a small config change (in admin_config) can fix it without code changes → key = dot-notation path, value = the new value
   Never suggest safe_fix for issues that require code changes.
5. risk_label = "developer_required" when the fix needs: changing Python extractors/regex, adding a new provider, fixing a runtime exception, or changing discovery browser logic.
6. fix_yaml_snippet: only include YAML that belongs in the per-uni YAML config file (discovery or extraction section). Keep it under 10 lines. null if not applicable.
7. CURRENT FILTER SIMULATION is ground truth for the active config. If it shows ≥60% pass rate, the filter is working — do NOT set root_cause_category to "filtering". Focus on extraction (missing selectors, JS rendering), staging gate (completeness threshold), or config conflicts instead.
8. If CONFIG STALENESS WARNING is present, the job statistics reflect an old config. Rely on CURRENT FILTER SIMULATION for filter behaviour and be explicit that the job data is stale.

OPERATIONAL DATA:
{context_doc}

Return ONLY a valid JSON object — no markdown fences, no prose, just the object:
{{
  "issue_summary": "<1-2 sentence plain-English summary, or 'No significant issues detected'>",
  "root_cause_category": "<discovery|filtering|extraction|config_conflict|api|pdf|browser|staging_gate|healthy>",
  "confidence": "<high|medium|low>",
  "evidence": [
    {{"type": "<job_stat|rejection|config|alert|extraction|discovery>", "label": "<short label>", "value": "<exact value from data>", "source": "<e.g. last job stats, admin_config, scrape_run_alerts>"}}
  ],
  "fix_recommendation": "<plain-English recommended fix>",
  "fix_yaml_snippet": null,
  "safe_fix": null,
  "risk_label": "<low|medium|developer_required>",
  "developer_required": false,
  "developer_note": null
}}

Include 3-8 evidence items. safe_fix and fix_yaml_snippet may be null. developer_note is required when developer_required is true."""

    try:
        _resp = await _gc.aio.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=_gtypes.GenerateContentConfig(max_output_tokens=2048),
        )
        raw_text = (getattr(_resp, "text", "") or "").strip()
    except Exception as _exc2:
        raise HTTPException(status_code=502, detail=f"Gemini API error: {_exc2}") from _exc2

    # Strip markdown fences if model added them
    _clean = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE).strip()
    _clean = re.sub(r"\s*```$", "", _clean, flags=re.MULTILINE).strip()

    try:
        result: dict = json.loads(_clean)
    except json.JSONDecodeError:
        _m = re.search(r"\{.*\}", _clean, re.DOTALL)
        if _m:
            try:
                result = json.loads(_m.group())
            except Exception:
                raise HTTPException(status_code=502, detail=f"Gemini returned unparseable JSON: {raw_text[:300]}")
        else:
            raise HTTPException(status_code=502, detail=f"Gemini returned non-JSON: {raw_text[:300]}")

    result["university_id"]        = uni_id
    result["university_name"]      = uni.name
    result["last_job_id"]          = last_job_id
    result["last_job_created_at"]  = _last_job_ts
    result["config_last_saved_at"] = _cfg_saved_ts
    result["config_is_stale"]      = _config_is_stale
    result["filter_sim"]           = (
        hist_sim_summary if hist_sim_summary and "error" not in hist_sim_summary else None
    )
    result["context_used"]    = [
        "university_info", "last_scrape_job", "scraped_courses_summary",
        "rejection_log", "discovery_events", "scrape_run_alerts",
        "effective_config", "admin_overrides",
    ] + (["config_history"] if config_history else [])

    return result


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

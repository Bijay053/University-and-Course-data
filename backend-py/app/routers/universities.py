"""University CRUD endpoints. Path layout mirrors the Node API exactly."""
from __future__ import annotations

import csv
import io
from typing import Annotated, Any

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

    # Name from <title> or hostname
    try:
        async with _httpx.AsyncClient(
            follow_redirects=True, timeout=8.0, verify=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UniPortalBot/1.0)"},
        ) as client:
            resp = await client.get(root_url)
            if resp.status_code < 400:
                # Extract <title>
                title_m = _re.search(r"<title[^>]*>([^<]+)</title>", resp.text, _re.I)
                if title_m:
                    raw_title = title_m.group(1).strip()
                    # Take the first segment before | — or :
                    name = _re.split(r"\s*[|–—:]\s*", raw_title)[0].strip()[:200]
    except Exception:
        pass  # Best-effort; we'll fall back to hostname-derived name

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
        "kept_samples": passing[:8],
        "dropped_samples": dropped_urls[:8],
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

                sr: dict = {
                    "seed_url": seed_url,
                    "status_code": resp.status_code,
                    "raw_candidates": len(candidates),
                    "after_filter": len(passing),
                    "dropped": len(dropped),
                    "drop_rate_pct": round(drop_r * 100),
                    "sample_passing": passing[:6],
                    "sample_dropped": dropped[:6],
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


@router.put("/universities/{uni_id}/recipe")
async def put_recipe_config(
    uni_id: int,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Save the operator Data Cleaning Recipe for a university.

    The recipe is a set of no-code rules applied to every extracted course
    payload BEFORE it is staged — covering fee term overrides, IELTS component
    mapping, course name cleanup, location filtering, and study mode derivation.

    Stored at ``scrape_config.recipe`` (JSONB) alongside ``admin_config``.
    Applied by ``recipe_rules.apply_recipe_rules()`` in the orchestrator.

    Example body::

        {
          "fee_source_urls": ["https://uni.edu.au/fees/international"],
          "fee_term": "Annual",
          "fee_prevent_full_course_rollup": true,
          "ielts_component_mapping": {"6.0": 5.5, "6.5": 6.0, "7.0": 6.5},
          "course_name_remove_after": ["|", " - Southern Cross University"],
          "location_allowed_values": ["Gold Coast", "Lismore", "Online"],
          "location_reject_values": ["How to Apply", "Teaching period"]
        }
    """
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = dict(u.scrape_config or {})
    sc["recipe"] = body

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {"ok": True, "university_id": uni_id, "recipe": body}


@router.get("/universities/{uni_id}/recipe")
async def get_recipe(
    uni_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return the advanced scraping recipe for a university."""
    u: University | None = await db.get(University, uni_id)
    if not u:
        raise HTTPException(status_code=404, detail="University not found")

    sc: dict = u.scrape_config or {}
    recipe: dict = sc.get("recipe") or {}

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "scrape_url": u.scrape_url or "",
        "recipe": recipe,
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

    if english_patch:
        extraction_patch["english"] = english_patch
    if fee_patch:
        extraction_patch["fees"] = fee_patch

    if extraction_patch:
        admin_cfg: dict = dict(sc.get("admin_config") or {})
        existing_extraction = dict(admin_cfg.get("extraction") or {})
        for k, v in extraction_patch.items():
            if isinstance(v, dict) and isinstance(existing_extraction.get(k), dict):
                existing_extraction[k] = {**existing_extraction[k], **v}
            else:
                existing_extraction[k] = v
        admin_cfg["extraction"] = existing_extraction
        sc["admin_config"] = admin_cfg

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": json.dumps(sc), "id": uni_id},
    )
    await db.commit()

    return {"ok": True, "university_id": uni_id, "recipe": body}


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

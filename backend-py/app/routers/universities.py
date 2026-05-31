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

    return {
        "university_id": uni_id,
        "university_name": u.name,
        "scrape_url": u.scrape_url or "",
        "admin_config": admin_cfg,
        "health_score": health_score,
        "latest_job_id": job_stats.get("runtime_job_id"),
        "job_stats": {
            "total_found": found,
            "imported": imported,
            "skipped": int(job_stats.get("skipped") or 0),
            "errors": int(job_stats.get("errors") or 0),
            "avg_completeness_pct": round(avg_comp, 1),
            "min_expected_courses": min_expected,
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

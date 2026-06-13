"""
Auto API Discovery router
=========================
POST /api/scrape/discover-api          — start a discovery job
GET  /api/scrape/discover-api/{job_id} — poll status / results
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.dependencies import get_db
from app.permissions import require_permission

log = logging.getLogger(__name__)
router = APIRouter()

_REDIS_TTL_S = 3600  # keep results for 1 hour


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def _redis_set(key: str, value: dict, ttl: int = _REDIS_TTL_S) -> None:
    try:
        import redis.asyncio as aioredis  # type: ignore
        r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
        await r.set(key, json.dumps(value), ex=ttl)
        await r.aclose()
    except Exception as exc:
        log.warning("api_discovery redis_set failed: %s", exc)


async def _redis_get(key: str) -> dict | None:
    try:
        import redis.asyncio as aioredis  # type: ignore
        r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
        raw = await r.get(key)
        await r.aclose()
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.warning("api_discovery redis_get failed: %s", exc)
        return None


def _redis_key(job_id: str) -> str:
    return f"api_disc:{job_id}"


# ── Fetch sample URLs from DB ─────────────────────────────────────────────────

async def _get_sample_urls(uni_id: int, db: AsyncSession) -> list[str]:
    """Return up to 3 recently-scraped course URLs for the university."""
    try:
        row = await db.execute(
            text("SELECT scrape_url FROM universities WHERE id = :uid LIMIT 1"),
            {"uid": uni_id},
        )
        uni_row = row.fetchone()
        scrape_url: str = uni_row[0] if uni_row and uni_row[0] else ""

        # Try scraped_courses for real per-course page URLs first
        sc_rows = await db.execute(
            text("""
                SELECT DISTINCT source_url
                FROM scraped_courses
                WHERE university_id = :uid
                  AND source_url IS NOT NULL
                  AND source_url != ''
                ORDER BY created_at DESC
                LIMIT 6
            """),
            {"uid": uni_id},
        )
        urls = [r[0] for r in sc_rows.fetchall() if r[0]]
        if urls:
            return urls[:3]

        # Fall back to the university scrape_url itself
        if scrape_url:
            return [scrape_url]
    except Exception as exc:
        log.warning("api_discovery: could not fetch sample URLs for uni %s: %s", uni_id, exc)
    return []


async def _get_uni_hostname(uni_id: int, db: AsyncSession) -> str:
    try:
        row = await db.execute(
            text("SELECT scrape_url FROM universities WHERE id = :uid LIMIT 1"),
            {"uid": uni_id},
        )
        r = row.fetchone()
        if r and r[0]:
            return urlparse(r[0]).hostname or ""
    except Exception:
        pass
    return ""


async def _get_course_hints(uni_id: int, db: AsyncSession) -> list[str]:
    try:
        rows = await db.execute(
            text("""
                SELECT name FROM courses
                WHERE university_id = :uid
                  AND name IS NOT NULL AND name != ''
                ORDER BY id DESC LIMIT 5
            """),
            {"uid": uni_id},
        )
        return [r[0] for r in rows.fetchall()]
    except Exception:
        return []


# ── Background task ───────────────────────────────────────────────────────────

async def _run_discovery(
    job_id: str,
    uni_id: int,
    sample_urls: list[str],
    uni_hostname: str,
    course_hints: list[str],
) -> None:
    key = _redis_key(job_id)
    try:
        from app.services.scraper.api_discovery import discover_api_endpoints
        result = await discover_api_endpoints(
            sample_urls=sample_urls,
            uni_hostname=uni_hostname,
            course_hints=course_hints,
        )
        await _redis_set(key, {
            "status": "done",
            "uni_id": uni_id,
            "candidates": result["candidates"],
            "sample_urls": result["sample_urls"],
            "uni_hostname": result["uni_hostname"],
        })
        log.info("api_discovery job %s done — %d candidates", job_id, len(result["candidates"]))
    except Exception as exc:
        log.exception("api_discovery job %s failed", job_id)
        await _redis_set(key, {
            "status": "error",
            "uni_id": uni_id,
            "candidates": [],
            "sample_urls": sample_urls,
            "uni_hostname": uni_hostname,
            "error": str(exc),
        })


# ── Request / response schemas ────────────────────────────────────────────────

class DiscoverApiRequest(BaseModel):
    uni_id: int
    sample_urls: list[str] = []


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/discover-api")
async def start_discovery(
    body: DiscoverApiRequest,
    background_tasks: BackgroundTasks,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """
    Start an API discovery job.  Returns ``{"job_id": "..."}`` immediately;
    poll ``GET /api/scrape/discover-api/{job_id}`` for results.
    """
    job_id = str(uuid.uuid4())
    key = _redis_key(job_id)

    # Resolve sample URLs and university context
    sample_urls = body.sample_urls[:3] if body.sample_urls else await _get_sample_urls(body.uni_id, db)
    if not sample_urls:
        raise HTTPException(
            status_code=422,
            detail="No sample course URLs found for this university. Please provide sample_urls.",
        )

    uni_hostname = await _get_uni_hostname(body.uni_id, db)
    course_hints = await _get_course_hints(body.uni_id, db)

    # Store initial "running" state so polling works immediately
    await _redis_set(key, {
        "status": "running",
        "uni_id": body.uni_id,
        "candidates": [],
        "sample_urls": sample_urls,
        "uni_hostname": uni_hostname,
    })

    log.info(
        "api_discovery: starting job %s for uni_id=%s, %d sample URLs",
        job_id, body.uni_id, len(sample_urls),
    )

    background_tasks.add_task(
        _run_discovery, job_id, body.uni_id, sample_urls, uni_hostname, course_hints
    )

    return {"ok": True, "job_id": job_id, "sample_urls": sample_urls}


@router.get("/discover-api/{job_id}")
async def poll_discovery(
    job_id: str,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
) -> dict[str, Any]:
    """Poll the status and results of a discovery job."""
    data = await _redis_get(_redis_key(job_id))
    if data is None:
        raise HTTPException(status_code=404, detail="Discovery job not found or expired.")
    return data


# ── Shared request body ───────────────────────────────────────────────────────

class CandidateRequest(BaseModel):
    candidate_index: int = 0


# ── Slug resolution ───────────────────────────────────────────────────────────

async def _find_or_create_slug(uni_id: int | None, uni_hostname: str, db: AsyncSession) -> str:
    """
    Return the YAML slug for this university:
    1. Scan existing YAML files for a matching hostname comment.
    2. Fall back to apex-domain + uni_id derived slug.
    """
    import re
    from pathlib import Path

    _UNIS_DIR = Path(__file__).parent.parent.parent / "scraper_config" / "unis"

    # Clean hostname → apex label (e.g. "www.canterbury.ac.nz" → "canterbury")
    clean = re.sub(r"^(www|study|courses|handbook|programmes)\.", "", uni_hostname.lower())
    apex = clean.split(".")[0] if clean else "university"

    if _UNIS_DIR.exists():
        # Prefer exact apex prefix match (e.g. "canterbury_1759.yaml")
        matches = list(_UNIS_DIR.glob(f"{apex}*.yaml"))
        if matches:
            return matches[0].stem  # e.g. "canterbury_1759"

    # Derive a fresh slug: apex + uni_id
    suffix = f"_{uni_id}" if uni_id else ""
    raw = f"{apex}{suffix}"
    slug = re.sub(r"[^a-z0-9_-]", "_", raw)[:64]
    return slug


# ── Smoke test ────────────────────────────────────────────────────────────────

@router.post("/discover-api/{job_id}/smoke-test")
async def run_smoke_test(
    job_id: str,
    body: CandidateRequest,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
) -> dict[str, Any]:
    """
    Directly fetch the discovered API endpoint and report what course data is returned.
    Runs a safety check before fetching — blocks analytics/auth endpoints.
    """
    data = await _redis_get(_redis_key(job_id))
    if not data:
        raise HTTPException(status_code=404, detail="Discovery job not found or expired.")

    candidates = data.get("candidates", [])
    if body.candidate_index >= len(candidates):
        raise HTTPException(status_code=400, detail="Candidate index out of range.")

    candidate = candidates[body.candidate_index]

    from app.services.scraper.api_discovery import safety_check, smoke_test_endpoint
    safe, reason = safety_check(candidate)
    if not safe:
        return {"ok": False, "courses_found": 0, "sample_titles": [],
                "fields_detected": [], "error": f"Safety check failed: {reason}"}

    result = await smoke_test_endpoint(candidate["url"])
    return result


# ── Apply config ──────────────────────────────────────────────────────────────

@router.post("/discover-api/{job_id}/apply")
async def apply_api_config(
    job_id: str,
    body: CandidateRequest,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """
    Safety-check, merge, and save the discovered API config into the
    university's YAML scraper config file, then push to GitHub.
    """
    data = await _redis_get(_redis_key(job_id))
    if not data:
        raise HTTPException(status_code=404, detail="Discovery job not found or expired.")

    candidates = data.get("candidates", [])
    if body.candidate_index >= len(candidates):
        raise HTTPException(status_code=400, detail="Candidate index out of range.")

    candidate = candidates[body.candidate_index]
    uni_id: int | None = data.get("uni_id")
    uni_hostname: str = data.get("uni_hostname", "")

    # ── Safety gate ───────────────────────────────────────────────────────────
    from app.services.scraper.api_discovery import safety_check, inject_api_config
    safe, reason = safety_check(candidate)
    if not safe:
        return {"ok": False, "error": f"Safety check blocked save: {reason}"}

    # ── Resolve slug + paths ──────────────────────────────────────────────────
    from pathlib import Path
    slug = await _find_or_create_slug(uni_id, uni_hostname, db)
    _UNIS_DIR = Path(__file__).parent.parent.parent / "scraper_config" / "unis"
    slug_path = _UNIS_DIR / f"{slug}.yaml"

    current_yaml = slug_path.read_text(encoding="utf-8") if slug_path.exists() else ""

    # ── Inject API config ─────────────────────────────────────────────────────
    from datetime import datetime
    new_yaml = inject_api_config(
        current_yaml, candidate, uni_hostname,
        apply_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )

    # ── Write YAML file ───────────────────────────────────────────────────────
    _UNIS_DIR.mkdir(parents=True, exist_ok=True)
    slug_path.write_text(new_yaml, encoding="utf-8")
    log.info("api_discovery apply: wrote %s", slug_path)

    # ── History ───────────────────────────────────────────────────────────────
    try:
        from app.routers.scraper_configs import _append_history
        await _append_history(db, slug, new_yaml, "api_discovery_portal")
        await db.commit()
    except Exception:
        log.exception("Failed to record apply history for slug=%r", slug)

    # ── Git sync (non-fatal) ──────────────────────────────────────────────────
    git_result: dict = {}
    try:
        from app.routers.scraper_configs import _git_sync_config
        git_result = await _git_sync_config(slug)
    except Exception:
        log.exception("git sync failed after apply for slug=%r", slug)

    return {
        "ok": True,
        "slug": slug,
        "message": f"Saved API config to {slug}.yaml",
        "git_pushed": git_result.get("ok", False),
        "git_message": git_result.get("message") or git_result.get("error", ""),
    }


# ── Listing-page link extractor ───────────────────────────────────────────────

class FetchListingLinksRequest(BaseModel):
    url: str
    uni_id: int | None = None
    allow_patterns: list[str] | None = None


@router.post("/fetch-listing-links")
async def fetch_listing_links(
    body: FetchListingLinksRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Fetch a listing / search-results page and return the course-shaped links
    found on it.  Used by the Recipe-editor "Fetch from listing page" feature.

    Strategy
    --------
    1. Static HTTP (cffi / Scrape.do static) — zero browser cost.
    2. Parse all same-host ``<a href>`` links.
    3. Filter out obvious non-course URLs (global guards).
    4. If the university has ``allow_url_patterns`` in its YAML, apply them.
    5. Return results + ``needs_browser`` hint when the page appears JS-only.
    """
    import os
    import re as _re
    from urllib.parse import urlparse as _up

    from app.services.scraper.discovery import _is_known_non_course_url
    from app.services.scraper.guards import is_blocked_page

    raw_url = body.url.strip()
    if not raw_url.startswith("http"):
        return {"ok": False, "error": "URL must start with http:// or https://"}

    parsed_base = _up(raw_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    html: str | None = None
    method = "none"

    try:
        from app.services.scraper.http_fetcher import fetch_html_cffi
        html = await fetch_html_cffi(raw_url)
        if html:
            method = "static"
    except Exception:
        log.debug("fetch_listing_links: cffi failed for %s", raw_url)

    if not html and os.environ.get("SCRAPE_DO_TOKEN"):
        try:
            from app.services.scraper.http_fetcher import fetch_html_scrape_do
            html = await fetch_html_scrape_do(raw_url, render=False)
            if html:
                method = "static_proxy"
        except Exception:
            log.debug("fetch_listing_links: scrape.do static failed for %s", raw_url)

    if not html:
        return {
            "ok": True,
            "links": [],
            "method": "none",
            "total_raw": 0,
            "needs_browser": True,
            "error": "Could not fetch the page (network error or Cloudflare block)",
        }

    # ── 2. Extract all same-host links ────────────────────────────────────────
    href_re = _re.compile(r'href=["\']((?:https?://[^\s"\'<>]+|/[^\s"\'<>]*))["\']', _re.IGNORECASE)
    raw_hrefs: list[str] = href_re.findall(html)

    abs_links: list[str] = []
    netloc = parsed_base.netloc
    for href in raw_hrefs:
        href = href.split("#")[0].rstrip("/") or "/"
        if href.startswith("//"):
            href = parsed_base.scheme + ":" + href
        if href.startswith("/"):
            candidate = base_origin + href
        elif href.startswith("http"):
            candidate = href
        else:
            continue
        if _up(candidate).netloc != netloc:
            continue
        abs_links.append(candidate)

    # Deduplicate (preserve order)
    seen: set[str] = set()
    same_host: list[str] = []
    for lnk in abs_links:
        if lnk not in seen:
            seen.add(lnk)
            same_host.append(lnk)

    total_raw = len(same_host)

    # ── 3. Global guard filters ───────────────────────────────────────────────
    course_links: list[str] = []
    for link in same_host:
        if _is_known_non_course_url(link):
            continue
        blocked, _ = is_blocked_page(link)
        if blocked:
            continue
        course_links.append(link)

    # ── 4. Per-university allow_url_patterns (YAML) ───────────────────────────
    allow_pats: list[str] = list(body.allow_patterns or [])

    if not allow_pats and body.uni_id:
        try:
            from app.services.scraper.config.loader import load_uni_config
            row = (await db.execute(
                text("SELECT name, scrape_url FROM universities WHERE id = :id"),
                {"id": body.uni_id},
            )).mappings().first()
            if row:
                uni_cfg = load_uni_config(
                    slug=_up(row["scrape_url"]).netloc.lstrip("www.").split(".")[0] if row["scrape_url"] else "unknown",
                    name=row["name"] or "",
                    scrape_url=row["scrape_url"] or "",
                    university_id=body.uni_id,
                )
                allow_pats = list(getattr(uni_cfg.discovery, "allow_url_patterns", None) or [])
        except Exception:
            log.debug("fetch_listing_links: could not load YAML allow_url_patterns for uni_id=%s", body.uni_id)

    if allow_pats:
        compiled_pats = []
        for pat in allow_pats:
            try:
                compiled_pats.append(_re.compile(pat, _re.IGNORECASE))
            except Exception:
                pass
        if compiled_pats:
            course_links = [l for l in course_links if any(p.search(l) for p in compiled_pats)]

    # ── 5. Needs-browser hint ─────────────────────────────────────────────────
    # Heuristic: if we got an HTML page but almost no links overall, it's
    # almost certainly a JS-only SPA shell that needs Playwright rendering.
    needs_browser = (len(course_links) == 0 and total_raw < 15)

    log.info(
        "fetch_listing_links: url=%s → raw=%d same_host=%d filtered=%d needs_browser=%s",
        raw_url, len(raw_hrefs), total_raw, len(course_links), needs_browser,
    )
    return {
        "ok": True,
        "links": course_links,
        "method": method,
        "total_raw": total_raw,
        "needs_browser": needs_browser,
    }

"""CRUD + AI-generation for per-university scraper YAML configs.

Endpoints
---------
GET  /api/settings/scraper-configs                           → list all slugs + raw YAML (+ university_id)
GET  /api/settings/scraper-configs/{slug}                   → get one config's YAML
PUT  /api/settings/scraper-configs/{slug}                   → save / create a config (appends history)
DELETE /api/settings/scraper-configs/{slug}                 → delete a config
POST /api/settings/scraper-configs/generate                 → Gemini-generated YAML for a new university
POST /api/settings/scraper-configs/{slug}/trigger           → start a scrape job for this config's university
GET  /api/settings/scraper-configs/{slug}/history           → list recent history entries for a slug
POST /api/settings/scraper-configs/{slug}/restore/{hid}     → restore YAML from a history snapshot
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.permissions import require_permission

log = logging.getLogger(__name__)

router = APIRouter()

_UNIS_DIR = Path(__file__).parent.parent.parent / "scraper_config" / "unis"
_DEFAULTS_FILE = Path(__file__).parent.parent.parent / "scraper_config" / "defaults.yaml"
_TEMPLATE_FILE = Path(__file__).parent.parent.parent / "scraper_config" / "_template.yaml"

_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

_HISTORY_KEEP = 100  # rows per slug retained (older rows pruned on save)

# ── Gemini model config ───────────────────────────────────────────────────────
_GEMINI_PRIMARY   = "gemini-2.5-flash"
_GEMINI_FALLBACK  = "gemini-2.5-flash-lite"
# transient-error signals that warrant a retry / model fallback
_GEMINI_TRANSIENT = ("UNAVAILABLE", "503", "quota", "rate limit", "overloaded",
                     "Resource has been exhausted", "429")


async def _call_gemini_with_retry(client: Any, prompt: str) -> str:
    """Call Gemini with automatic retry + model fallback on 503/UNAVAILABLE.

    Strategy:
      1. gemini-2.5-flash  — attempt 1 (immediate)
      2. gemini-2.5-flash  — attempt 2 (after 2 s)
      3. gemini-2.5-flash-lite — attempt 1 (immediate)
      4. gemini-2.5-flash-lite — attempt 2 (after 2 s)

    Returns the stripped text response.
    Raises HTTPException(503) with a user-friendly message if all fail.
    """
    for model in (_GEMINI_PRIMARY, _GEMINI_FALLBACK):
        for delay in (0.0, 2.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                return (resp.text or "").strip()
            except Exception as exc:
                exc_s = str(exc)
                is_transient = any(sig.lower() in exc_s.lower() for sig in _GEMINI_TRANSIENT)
                if is_transient:
                    log.warning("Gemini %s transient error (will retry): %s", model, exc_s[:160])
                    continue
                raise  # non-transient — propagate immediately

    raise HTTPException(
        status_code=503,
        detail="Gemini is busy right now — please try again in a moment.",
    )


async def _load_example_yamls(db: AsyncSession, exclude_slug: str, max_examples: int = 3) -> list[dict]:
    """Return YAML excerpts from universities with the best recent scrape results.

    Only includes non-stub configs (>8 lines) that belong to universities with
    ≥15 courses staged in their most-recent completed scrape job.
    Limited to first 60 lines each so the token cost stays manageable.
    """
    rows = (await db.execute(
        text("""
            SELECT DISTINCT ON (srj.university_id)
                   u.name,
                   srj.imported,
                   LOWER(REGEXP_REPLACE(COALESCE(u.scrape_url, u.website, ''), '^https?://', '')) AS bare_url
            FROM   scrape_runtime_jobs srj
            JOIN   universities u ON u.id = srj.university_id
            WHERE  srj.status   = 'completed'
              AND  srj.job_type = 'scrape'
              AND  srj.imported >= 15
            ORDER  BY srj.university_id, srj.imported DESC
        """)
    )).all()

    # Sort by most courses staged — best examples first
    sorted_rows = sorted(rows, key=lambda r: r.imported, reverse=True)

    # Build hostname → slug map from disk
    slug_map: dict[str, str] = {}
    for f in _UNIS_DIR.glob("*.yaml"):
        raw = _read_yaml_raw(f)
        h = _extract_hostname_from_yaml(raw)
        if h:
            slug_map[re.sub(r"^www\.", "", h.lower())] = f.stem

    examples: list[dict] = []
    seen_slugs: set[str] = {exclude_slug}
    for row in sorted_rows:
        if not row.bare_url:
            continue
        url_host = re.sub(r"^www\.", "", row.bare_url.split("/")[0])
        slug = slug_map.get(url_host)
        if not slug or slug in seen_slugs:
            continue
        raw = _read_yaml_raw(_slug_path(slug))
        if not raw or raw.count("\n") < 8:
            continue  # skip stubs
        excerpt = "\n".join(raw.splitlines()[:60])
        examples.append({
            "slug": slug,
            "university_name": row.name or slug,
            "imported_count": row.imported,
            "yaml_excerpt": excerpt,
        })
        seen_slugs.add(slug)
        if len(examples) >= max_examples:
            break

    return examples


_INSPECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# URL path segments that almost certainly indicate course-detail pages
_COURSE_PATH_SIGNALS = re.compile(
    r"/(courses?|programs?|study|degrees?|postgraduate|undergraduate"
    r"|bachelor|master|phd|doctorate|diplom|certif|graduate)(/|$|-)",
    re.IGNORECASE,
)
# Segments that are navigation, not content
_NAV_PATH_SIGNALS = re.compile(
    r"/(about|contact|news|events|blog|careers|staff|research|login"
    r"|search|sitemap|privacy|terms|alumni|donate|library|portal|my-)",
    re.IGNORECASE,
)
_JS_SIGNALS = re.compile(
    r"(enable javascript|javascript is required|javascript is disabled"
    r"|loading\.\.\.|please wait|cloudflare ray id|just a moment)",
    re.IGNORECASE,
)


def _page_text(soup: "BeautifulSoup") -> str:  # type: ignore[name-defined]
    for tag in soup(["script", "style", "noscript", "meta", "link", "svg", "header", "footer", "nav"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
    return "\n".join(lines)


async def _inspect_website_for_ai(
    seed_url: str,
    extra_urls: list[str] | None = None,
) -> str:
    """Multi-step website inspection for AI diagnosis.

    Steps:
      1. Fetch the seed/listing URL — detect JS rendering, extract course links
      2. Follow one course detail page — check field visibility
      3. Check sitemap.xml

    Returns a rich structured text block for the Gemini prompt.
    Non-fatal: always returns a string even on total failure.
    """
    import httpx
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse
    from collections import Counter

    findings: list[str] = []
    errors: list[str] = []

    parsed_root = urlparse(seed_url)
    origin = f"{parsed_root.scheme}://{parsed_root.netloc}"

    async def _get(client: httpx.AsyncClient, url: str, timeout: float = 12.0) -> httpx.Response | None:
        try:
            r = await client.get(url, headers=_INSPECT_HEADERS, timeout=timeout, follow_redirects=True)
            return r
        except Exception as exc:
            errors.append(f"GET {url}: {exc}")
            return None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:

            # ── STEP 1: Fetch seed listing page ───────────────────────────────
            findings.append(f"STEP 1 — Fetching seed/listing page: {seed_url}")
            r = await _get(client, seed_url)
            if r is None or r.status_code >= 400:
                findings.append(f"  ERROR: Could not fetch seed URL (status={getattr(r,'status_code','timeout')})")
            else:
                soup = BeautifulSoup(r.text, "html.parser")
                body_text = _page_text(soup)
                body_len = len(body_text)

                # Detect JS rendering
                is_js_rendered = body_len < 400 or bool(_JS_SIGNALS.search(body_text[:600]))
                findings.append(f"  HTTP {r.status_code}  |  body text length: {body_len} chars")
                if is_js_rendered:
                    findings.append(
                        "  ⚠ JAVASCRIPT RENDERING DETECTED — the page body is nearly empty or shows "
                        "'enable JavaScript' text. Static HTTP fetching returns a shell; the scraper "
                        "needs always_browser_discover: true + use_stealth_browser: true to get real content."
                    )
                else:
                    findings.append("  ✓ Page rendered server-side (body has real content)")

                # Show a snippet of the page body
                snippet = "\n".join(body_text.splitlines()[:40])
                findings.append(f"\n  --- PAGE BODY SNIPPET (first 40 lines) ---\n{snippet}\n  --- END SNIPPET ---")

                # Extract all internal links + categorise by path pattern
                all_links = [
                    urljoin(seed_url, a["href"])
                    for a in soup.find_all("a", href=True)
                    if not a["href"].startswith(("#", "mailto:", "tel:", "javascript:"))
                    and urlparse(urljoin(seed_url, a["href"])).netloc == parsed_root.netloc
                ]

                # Count path-prefix patterns (first 2 segments)
                path_patterns: Counter = Counter()
                for link in all_links:
                    p = urlparse(link).path
                    segs = [s for s in p.split("/") if s]
                    prefix = "/" + "/".join(segs[:2]) if len(segs) >= 2 else "/" + "/".join(segs)
                    if prefix and prefix != "/":
                        path_patterns[prefix] += 1

                top_patterns = path_patterns.most_common(15)
                if top_patterns:
                    findings.append(f"\n  Internal links found: {len(all_links)}")
                    findings.append("  Top URL path patterns (prefix → count):")
                    for pat, cnt in top_patterns:
                        tag = ""
                        if _COURSE_PATH_SIGNALS.search(pat):
                            tag = "  ← LIKELY COURSE PAGES"
                        elif _NAV_PATH_SIGNALS.search(pat):
                            tag = "  (navigation)"
                        findings.append(f"    {pat}  ({cnt} links){tag}")
                else:
                    findings.append("  No internal links found on page — site may be fully JS-rendered.")

                # Pick the best candidate course detail URL to follow
                course_candidates = [
                    link for link in all_links
                    if _COURSE_PATH_SIGNALS.search(urlparse(link).path)
                    and not _NAV_PATH_SIGNALS.search(urlparse(link).path)
                    and len(urlparse(link).path.split("/")) >= 3  # must have depth > 2
                ]
                detail_url: str | None = course_candidates[0] if course_candidates else None

                # ── STEP 2: Fetch one course detail page ──────────────────────
                if detail_url:
                    findings.append(f"\nSTEP 2 — Fetching course detail page: {detail_url}")
                    r2 = await _get(client, detail_url)
                    if r2 and r2.status_code < 400:
                        soup2 = BeautifulSoup(r2.text, "html.parser")
                        detail_text = _page_text(soup2)
                        detail_len = len(detail_text)
                        findings.append(f"  HTTP {r2.status_code}  |  body text length: {detail_len} chars")

                        detail_js = detail_len < 400 or bool(_JS_SIGNALS.search(detail_text[:600]))
                        if detail_js:
                            findings.append("  ⚠ COURSE DETAIL PAGE IS ALSO JS-RENDERED — confirms browser rendering required.")
                        else:
                            findings.append("  ✓ Course detail page has real content")

                        # Check which fields are visible
                        fee_found = bool(re.search(r"\$[\d,]{4,}", detail_text))
                        ielts_found = bool(re.search(r"IELTS\s*[:\s]?\s*\d+\.?\d*", detail_text, re.IGNORECASE))
                        duration_found = bool(re.search(r"\d+\s*(year|month|semester|week)", detail_text, re.IGNORECASE))
                        intake_found = bool(re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}", detail_text, re.IGNORECASE))
                        degree_found = bool(re.search(r"(Bachelor|Master|PhD|Doctor|Graduate|Diploma|Certificate)", detail_text, re.IGNORECASE))

                        findings.append("\n  Fields visible on course detail page:")
                        findings.append(f"    degree/title: {'✓ YES' if degree_found else '✗ NOT FOUND'}")
                        findings.append(f"    international fee ($): {'✓ YES' if fee_found else '✗ NOT FOUND — fee extraction will fail'}")
                        findings.append(f"    IELTS score: {'✓ YES' if ielts_found else '✗ NOT FOUND'}")
                        findings.append(f"    duration: {'✓ YES' if duration_found else '✗ NOT FOUND'}")
                        findings.append(f"    intake months: {'✓ YES' if intake_found else '✗ NOT FOUND'}")

                        # Show detail page snippet
                        detail_snippet = "\n".join(detail_text.splitlines()[:50])
                        findings.append(f"\n  --- COURSE DETAIL PAGE SNIPPET (first 50 lines) ---\n{detail_snippet}\n  --- END SNIPPET ---")
                    else:
                        findings.append(f"  ERROR: Could not fetch detail page (status={getattr(r2,'status_code','timeout') if r2 else 'timeout'})")
                else:
                    findings.append("\nSTEP 2 — No course-like links found on listing page to follow.")
                    if not is_js_rendered:
                        findings.append("  This strongly suggests the listing page itself is not the right seed URL,")
                        findings.append("  OR the course links use a pattern not matching common keywords.")

            # ── STEP 3: Check sitemap ────────────────────────────────────────
            findings.append(f"\nSTEP 3 — Checking sitemap at {origin}/sitemap.xml")
            r3 = await _get(client, f"{origin}/sitemap.xml", timeout=8.0)
            if r3 and r3.status_code == 200:
                sitemap_text = r3.text[:8000]
                # Count URLs in sitemap matching course-like patterns
                sitemap_urls = re.findall(r"<loc>(.*?)</loc>", sitemap_text)
                course_sitemap = [u for u in sitemap_urls if _COURSE_PATH_SIGNALS.search(u)]
                findings.append(f"  ✓ sitemap.xml found — {len(sitemap_urls)} URLs total, {len(course_sitemap)} match course-like patterns")
                if course_sitemap:
                    findings.append(f"  Sample course URLs from sitemap:")
                    for u in course_sitemap[:5]:
                        findings.append(f"    {u}")
                    # Suggest the common prefix
                    if len(course_sitemap) > 3:
                        paths = [urlparse(u).path for u in course_sitemap]
                        prefix_counts: Counter = Counter()
                        for p in paths:
                            segs = [s for s in p.split("/") if s]
                            pfx = "/" + segs[0] if segs else "/"
                            prefix_counts[pfx] += 1
                        top_pfx = prefix_counts.most_common(1)[0][0]
                        findings.append(f"  → Most common path prefix in sitemap: {top_pfx}  (set as allow_url_patterns pattern)")
            else:
                status = getattr(r3, "status_code", "timeout") if r3 else "timeout"
                findings.append(f"  sitemap.xml returned {status} — no sitemap available")
                # Try sitemap_index.xml
                r3b = await _get(client, f"{origin}/sitemap_index.xml", timeout=6.0)
                if r3b and r3b.status_code == 200:
                    findings.append(f"  ✓ sitemap_index.xml found — use always_sitemap_supplement: true")

    except Exception as exc:
        log.exception("_inspect_website_for_ai(%s) outer error: %s", seed_url, exc)
        findings.append(f"[Inspection failed with error: {exc}]")

    if errors:
        findings.append("\nNetwork errors during inspection:")
        for e in errors:
            findings.append(f"  {e}")

    return "\n".join(findings)


def _extract_seed_urls_from_yaml(yaml_text: str) -> list[str]:
    """Extract seed_urls list from a YAML string; returns empty list on error."""
    try:
        data = yaml.safe_load(yaml_text) or {}
        disc = data.get("discovery", {}) or {}
        seeds = disc.get("seed_urls", []) or []
        return [s for s in seeds if isinstance(s, str) and s.startswith("http")]
    except Exception:
        return []


def _slug_path(slug: str) -> Path:
    return _UNIS_DIR / f"{slug}.yaml"


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Invalid slug — use lowercase letters, digits, hyphens, underscores only")


def _read_yaml_raw(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_yaml_safe(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


# ── Hostname helpers ──────────────────────────────────────────────────────────

_HOSTNAME_IN_PARENS_RE = re.compile(r"\(([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9]{2,})+)\)", re.IGNORECASE)
_HOSTNAME_LABEL_RE = re.compile(r"#\s*[Hh]ostname[:\s]+([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9]{2,})+)", re.IGNORECASE)


def _extract_hostname_from_yaml(raw: str) -> str | None:
    """Try to pull a hostname from the YAML comment block.

    Matches patterns like:
      # University Name (some.hostname.edu.au)
      # Hostname: some.hostname.edu.au
    Returns the bare hostname (e.g. ``federation.edu.au``) or None.
    """
    for line in raw.splitlines():
        if not line.startswith("#"):
            break
        m = _HOSTNAME_LABEL_RE.search(line) or _HOSTNAME_IN_PARENS_RE.search(line)
        if m:
            return m.group(1).lower()
    return None


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/scraper-configs")
async def list_scraper_configs(
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    # Build config list from YAML files
    # Skip internal files that start with _ (e.g. _template.yaml — it's a
    # developer reference, not a real university config, so hide it from the UI).
    configs = []
    hostnames: list[str] = []
    for f in sorted(_UNIS_DIR.glob("*.yaml")):
        slug = f.stem
        if slug.startswith("_"):
            continue
        raw = _read_yaml_raw(f)
        data = _parse_yaml_safe(raw)
        comment_lines = [ln.lstrip("# ").strip() for ln in raw.splitlines() if ln.startswith("#") and ln.strip() != "#"]
        title = comment_lines[0] if comment_lines else slug.replace("-", " ").replace("_", " ").title()
        hostname = _extract_hostname_from_yaml(raw)
        configs.append({"slug": slug, "title": title, "yaml": raw, "parsed": data,
                        "hostname": hostname, "university_id": None, "university_name": None})
        if hostname:
            hostnames.append(hostname)

    # Batch-resolve university_id for each config by matching hostname
    if hostnames:
        rows = (await db.execute(
            text("""
                SELECT id, name,
                       LOWER(REGEXP_REPLACE(
                           COALESCE(scrape_url, website, ''),
                           '^https?://', ''
                       )) AS bare_url
                FROM universities
                WHERE COALESCE(scrape_url, website, '') != ''
            """)
        )).all()
        # Pre-compute the host-only portion of each DB URL (strip path and www.)
        db_entries = []
        for uni_id, uni_name, bare_url in rows:
            if not bare_url:
                continue
            url_host = bare_url.split("/")[0]  # just the hostname, no path
            url_host = re.sub(r"^www\.", "", url_host)  # strip www.
            db_entries.append((uni_id, uni_name, url_host))

        for cfg in configs:
            if not cfg["hostname"]:
                continue
            # Strip www. from config hostname to get the apex-ish domain
            h_bare = re.sub(r"^www\.", "", cfg["hostname"].lower())
            for uni_id, uni_name, url_host in db_entries:
                # Match if:
                #   1. exact match after www-stripping (e.g. vit.edu.au == vit.edu.au)
                #   2. config domain is the apex of the URL host, allowing subdomains
                #      (e.g. study.csu.edu.au ends with .csu.edu.au)
                if url_host == h_bare or url_host.endswith("." + h_bare):
                    cfg["university_id"] = uni_id
                    cfg["university_name"] = uni_name
                    break

    # Drop internal fields not needed by the client
    for cfg in configs:
        cfg.pop("parsed", None)
        cfg.pop("hostname", None)

    return JSONResponse(content={"configs": configs})


# ── Trigger scrape ────────────────────────────────────────────────────────────

@router.post("/scraper-configs/{slug}/trigger")
async def trigger_scrape_for_config(
    slug: str,
    _user: Annotated[dict, Depends(require_permission("scraping.start"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Find the university for this YAML config and start a scrape job.

    Looks up the university by matching the hostname embedded in the YAML
    comment block against ``scrape_url``/``website`` in the DB.  Returns
    ``{"jobId": ..., "runtimeJobId": ..., "status": "queued", "universityId": N}``
    on success, or 404/503 on failure.
    """
    from app.models.university import University
    from app.routers.scrape import start_scrape
    from app.schemas.scrape import StartScrapeBody

    _validate_slug(slug)
    path = _slug_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No config for slug '{slug}'")

    raw = _read_yaml_raw(path)
    hostname = _extract_hostname_from_yaml(raw)
    uni = None

    if hostname:
        h_bare = re.sub(r"^www\.", "", hostname.lower())
        result = await db.execute(
            select(University).where(
                or_(
                    University.scrape_url.op("~*")(rf"://(?:www\.)?{re.escape(h_bare)}[/:]"),
                    University.website.op("~*")(rf"://(?:www\.)?{re.escape(h_bare)}[/:]"),
                )
            ).limit(1)
        )
        uni = result.scalar_one_or_none()

    if not uni:
        # Fallback: partial name match on the slug
        name_guess = slug.replace("-", " ").replace("_", " ")
        result = await db.execute(
            select(University).where(
                University.name.ilike(f"%{name_guess}%")
            ).limit(1)
        )
        uni = result.scalar_one_or_none()

    if not uni:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No university matched config slug '{slug}'"
                + (f" (hostname={hostname})" if hostname else " (no hostname in YAML comment)")
                + ". Add '# Hostname: your.domain.edu.au' to the YAML comment block."
            ),
        )

    body = StartScrapeBody(university_id=uni.id)
    resp = await start_scrape(body, db)
    return JSONResponse(
        status_code=202,
        content={
            "jobId": resp.runtime_job_id,
            "runtimeJobId": resp.runtime_job_id,
            "status": resp.status,
            "ok": resp.ok,
            "universityId": uni.id,
            "universityName": uni.name,
        },
    )


# ── Get one ───────────────────────────────────────────────────────────────────

@router.get("/scraper-configs/{slug}")
async def get_scraper_config(
    slug: str,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
) -> JSONResponse:
    _validate_slug(slug)
    path = _slug_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No config for slug '{slug}'")
    return JSONResponse(content={"slug": slug, "yaml": _read_yaml_raw(path)})


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/scraper-configs/{slug}/history")
async def get_scraper_config_history(
    slug: str,
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    before_id: Optional[int] = Query(default=None),
) -> JSONResponse:
    """Return paginated save history for one slug (newest first).

    Use *before_id* as a cursor: pass the smallest id returned by the
    previous page to get the next page of older entries.
    """
    _validate_slug(slug)

    cursor_clause = "AND id < :before_id" if before_id is not None else ""
    rows = (await db.execute(
        text(f"""
            SELECT id, slug, yaml_content, saved_by, saved_at
            FROM scraper_config_history
            WHERE slug = :slug
            {cursor_clause}
            ORDER BY saved_at DESC
            LIMIT :limit
        """),
        {"slug": slug, "before_id": before_id, "limit": limit},
    )).all()

    entries = [
        {
            "id": r.id,
            "slug": r.slug,
            "yaml_content": r.yaml_content,
            "saved_by": r.saved_by,
            "saved_at": r.saved_at.isoformat() if r.saved_at else None,
        }
        for r in rows
    ]
    has_more = len(rows) == limit
    return JSONResponse(content={"slug": slug, "history": entries, "has_more": has_more})


# ── Restore from history ──────────────────────────────────────────────────────

@router.post("/scraper-configs/{slug}/restore/{hid}")
async def restore_scraper_config(
    slug: str,
    hid: int,
    user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Restore the YAML file to a specific historical snapshot.

    This writes the historical YAML to disk (same as a normal save) and
    records a new history entry so the restore itself is auditable.
    """
    _validate_slug(slug)

    row = (await db.execute(
        text("SELECT yaml_content FROM scraper_config_history WHERE id = :hid AND slug = :slug"),
        {"hid": hid, "slug": slug},
    )).one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"History entry {hid} not found for slug '{slug}'")

    yaml_content: str = row.yaml_content

    # Validate the historical YAML is still parseable
    try:
        parsed = yaml.safe_load(yaml_content)
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Stored YAML is not a valid mapping")
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"Stored YAML parse error: {exc}") from exc

    _UNIS_DIR.mkdir(parents=True, exist_ok=True)
    _slug_path(slug).write_text(yaml_content, encoding="utf-8")

    saved_by = user.get("sub") or user.get("email") or "unknown"
    await db.execute(
        text("""
            INSERT INTO scraper_config_history (slug, yaml_content, saved_by)
            VALUES (:slug, :yaml_content, :saved_by)
        """),
        {"slug": slug, "yaml_content": yaml_content, "saved_by": f"restore:{saved_by}"},
    )
    await _prune_history(db, slug)
    await db.commit()

    log.info("Restored scraper config for slug=%r from history id=%d", slug, hid)

    git_result: dict = {}
    try:
        git_result = await _git_sync_config(slug)
    except Exception:
        log.exception("Unexpected error in _git_sync_config after restore for slug=%r", slug)
        git_result = {"ok": False, "error": "unexpected git sync error"}

    return JSONResponse(content={
        "ok": True,
        "slug": slug,
        "restored_from": hid,
        "git_pushed": git_result.get("ok", False),
        "git_message": git_result.get("message") or git_result.get("error", ""),
        "git_skipped": git_result.get("skipped", False),
    })


# ── Git sync helper ───────────────────────────────────────────────────────────

# Repo root: backend-py/app/routers/scraper_configs.py → up 4 levels
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


async def _git_sync_config(slug: str) -> dict:
    """Commit the YAML file and push to the GitHub remote.

    Returns ``{"ok": True, "message": ...}`` on success, or
    ``{"ok": False, "error": ..., "skipped": True}`` if PAT is missing, or
    ``{"ok": False, "error": ...}`` on git failure.

    Never raises — callers should treat git failures as non-fatal warnings.
    """
    pat = os.environ.get("STUDYINFO_GITHUB_PAT", "").strip()
    if not pat:
        log.debug("git sync skipped — STUDYINFO_GITHUB_PAT not set")
        return {"ok": False, "skipped": True, "error": "STUDYINFO_GITHUB_PAT not configured"}

    rel_path = f"backend-py/scraper_config/unis/{slug}.yaml"
    timeout = 30

    async def _run(*args: str, cwd: str = str(_REPO_ROOT)) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"timeout after {timeout}s"
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    # Check if the file is dirty (new or modified)
    rc, status_out, _ = await _run("git", "--no-optional-locks", "status", "--porcelain", rel_path)
    if rc != 0:
        return {"ok": False, "error": "git status failed — is this a git repo?"}
    if not status_out.strip():
        log.debug("git sync: %s is unchanged, nothing to commit", rel_path)
        return {"ok": True, "message": "no changes — already up-to-date"}

    # Stage the file
    rc, _, err = await _run("git", "add", rel_path)
    if rc != 0:
        return {"ok": False, "error": f"git add failed: {err.strip()[:200]}"}

    # Commit with a bot identity so it never fails on unconfigured user.name
    rc, commit_out, commit_err = await _run(
        "git",
        "-c", "user.name=University Portal Bot",
        "-c", "user.email=portal-bot@university.local",
        "commit",
        "-m", f"chore(scraper): update {slug}.yaml via portal [skip ci]",
    )
    if rc != 0:
        # git writes "nothing to commit" to stdout, not stderr
        combined = (commit_out + commit_err).strip()
        if "nothing to commit" in combined or "nothing added to commit" in combined:
            return {"ok": True, "message": "no changes — already up-to-date"}
        return {"ok": False, "error": f"git commit failed: {combined[:200]}"}

    # Discover the GitHub HTTPS remote URL.
    # GITHUB_PUSH_URL env var takes priority — set it to override which repo
    # the portal pushes to (e.g. when the `github` remote still points at an
    # old fork that the PAT can't write to).  Falls back to auto-discovery
    # from `git remote -v` when not set.
    push_url: str | None = os.environ.get("GITHUB_PUSH_URL", "").strip() or None
    if not push_url:
        rc, remotes_out, _ = await _run("git", "--no-optional-locks", "remote", "-v")
        for line in remotes_out.splitlines():
            if "github.com" in line and "(push)" in line:
                parts = line.split()
                if len(parts) >= 2:
                    # Prefer remotes whose URL contains the PAT account name so
                    # we don't accidentally pick an old fork the PAT can't push to.
                    candidate = parts[1]
                    if push_url is None:
                        push_url = candidate
                    elif "studyinfocentre" in candidate.lower():
                        push_url = candidate
                        break

    if not push_url:
        # No github.com remote found — undo the commit so we don't leave a dangling local commit
        await _run("git", "reset", "--soft", "HEAD~1")
        return {"ok": False, "error": "No github.com remote found — configure one and retry"}

    # Inject PAT: https://github.com/... → https://PAT@github.com/...
    auth_url = re.sub(r"^https://", f"https://{pat}@", push_url)

    rc, _, err = await _run("git", "push", auth_url, "HEAD:main")
    if rc != 0:
        # Undo the local commit to keep the repo consistent
        await _run("git", "reset", "--soft", "HEAD~1")
        err_clean = re.sub(pat, "***", err.strip())  # scrub PAT from log
        log.warning("git push failed for %s: %s", slug, err_clean[:300])
        return {"ok": False, "error": f"git push failed: {err_clean[:200]}"}

    log.info("git sync: pushed %s to GitHub (%s)", rel_path, push_url)
    return {"ok": True, "message": f"committed and pushed {rel_path} to GitHub"}


# ── History helpers ───────────────────────────────────────────────────────────

async def _append_history(db: AsyncSession, slug: str, yaml_content: str, saved_by: str) -> None:
    """Insert one history row, then prune old rows beyond _HISTORY_KEEP."""
    await db.execute(
        text("""
            INSERT INTO scraper_config_history (slug, yaml_content, saved_by)
            VALUES (:slug, :yaml_content, :saved_by)
        """),
        {"slug": slug, "yaml_content": yaml_content, "saved_by": saved_by},
    )
    await _prune_history(db, slug)


async def _prune_history(db: AsyncSession, slug: str) -> None:
    """Keep only the newest _HISTORY_KEEP rows for this slug."""
    await db.execute(
        text("""
            DELETE FROM scraper_config_history
            WHERE slug = :slug
              AND id NOT IN (
                  SELECT id FROM scraper_config_history
                  WHERE slug = :slug
                  ORDER BY saved_at DESC
                  LIMIT :keep
              )
        """),
        {"slug": slug, "keep": _HISTORY_KEEP},
    )


# ── Save / create ─────────────────────────────────────────────────────────────

class SaveConfigBody(BaseModel):
    yaml_content: str


@router.put("/scraper-configs/{slug}")
async def save_scraper_config(
    slug: str,
    body: SaveConfigBody,
    user: Annotated[dict, Depends(require_permission("settings.edit"))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    _validate_slug(slug)
    try:
        parsed = yaml.safe_load(body.yaml_content)
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Invalid YAML: root must be a mapping, not a scalar or list")
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}") from exc

    _UNIS_DIR.mkdir(parents=True, exist_ok=True)
    _slug_path(slug).write_text(body.yaml_content, encoding="utf-8")
    log.info("Saved scraper config for slug=%r", slug)

    # Record edit history
    saved_by = user.get("sub") or user.get("email") or "unknown"
    try:
        await _append_history(db, slug, body.yaml_content, saved_by)
        await db.commit()
    except Exception:
        log.exception("Failed to write scraper config history for slug=%r", slug)
        # Non-fatal — the file was already saved to disk

    # Best-effort git commit + push — never blocks the save response
    git_result: dict = {}
    try:
        git_result = await _git_sync_config(slug)
    except Exception:
        log.exception("Unexpected error in _git_sync_config for slug=%r", slug)
        git_result = {"ok": False, "error": "unexpected git sync error"}

    return JSONResponse(content={
        "ok": True,
        "slug": slug,
        "git_pushed": git_result.get("ok", False),
        "git_message": git_result.get("message") or git_result.get("error", ""),
        "git_skipped": git_result.get("skipped", False),
    })


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/scraper-configs/{slug}")
async def delete_scraper_config(
    slug: str,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
) -> JSONResponse:
    _validate_slug(slug)
    path = _slug_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No config for slug '{slug}'")
    path.unlink()
    log.info("Deleted scraper config for slug=%r", slug)
    return JSONResponse(content={"ok": True, "slug": slug})


# ── AI generation ─────────────────────────────────────────────────────────────

class GenerateConfigBody(BaseModel):
    university_name: str
    website_url: str
    country: str = "Australia"
    notes: str = ""


_DEFAULTS_YAML_SUMMARY = """
Key fields you can set (only include overrides from defaults — omit anything that should stay default):

discovery:
  fallback_subdomains: []          # e.g. ["handbook.{domain}"]
  always_sitemap_supplement: false # true for JS-heavy SPAs
  block_url_patterns: []           # regex list of URLs to skip
  allow_url_patterns: []           # whitelist (empty = allow all)
  use_wayback: false               # true only if site is WAF-blocked
  bfs_page_budget: null            # raise for large sites (e.g. 80)

extraction:
  fees:
    central_page: null             # URL of fee schedule page
    fees_pdf_url: null             # URL of fee PDF
    default_currency: "AUD"        # "NZD" for NZ universities
  english:
    central_page: null             # URL of English requirements page
    default_ielts: null            # e.g. 6.5
    default_pte: null              # e.g. 58
  filters:
    domestic_only:
      enabled: false               # true if site lists domestic-only courses
    online_only:
      enabled: false
"""


_SPA_MARKERS = [
    "__NEXT_DATA__", "_nuxt", "ng-version", "data-reactroot",
    "data-react-helmet", "__vue", "window.__INITIAL_STATE__",
    "window.Ember", "id=\"root\"", "id=\"app\"", "id=\"__next\"",
]

_FEE_PATHS = [
    "/international/fees", "/international/tuition",
    "/study/fees-and-scholarships", "/study/fees",
    "/fees-and-scholarships", "/fees", "/tuition-fees",
    "/future-students/fees", "/future-students/international/fees",
    "/international/study/fees", "/courses/fees",
    "/international/costs-and-funding",
]

_ENGLISH_PATHS = [
    "/international/english-language-requirements",
    "/international/entry-requirements/english-language",
    "/international/entry-requirements",
    "/entry-requirements/english-language-requirements",
    "/study/entry-requirements/english-language-requirements",
    "/english-language-requirements",
    "/international/apply/english-language-requirements",
    "/future-students/international/entry-requirements",
    "/international/how-to-apply/english-language-requirements",
]


async def _probe_university_site(base_url: str) -> dict:
    """Crawl the university site to gather real facts for the AI prompt.

    Returns a dict with keys:
      homepage_ok, homepage_status, waf_blocked, is_spa, spa_hits,
      sitemap_ok, nav_links, found_fee_url, found_english_url, probe_errors
    """
    from urllib.parse import urljoin, urlparse

    result: dict = {
        "homepage_ok": False,
        "homepage_status": None,   # actual HTTP status code
        "waf_blocked": False,      # True when site returns 403/407/429 (Cloudflare/Akamai)
        "is_spa": False,
        "spa_hits": [],
        "sitemap_ok": False,
        "nav_links": [],
        "found_fee_url": None,
        "found_english_url": None,
        "probe_errors": [],
    }

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Use a realistic browser UA to reduce WAF friction
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    }

    try:
        import httpx
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=12.0,
            verify=False,
        ) as client:

            # ── 1. Homepage ──────────────────────────────────────────────────
            try:
                resp = await client.get(base_url)
                result["homepage_status"] = resp.status_code
                if resp.status_code in (403, 407, 429):
                    result["waf_blocked"] = True
                elif resp.status_code < 400:
                    result["homepage_ok"] = True
                    html = resp.text

                    # SPA detection
                    html_lower = html.lower()
                    spa_hits = [m for m in _SPA_MARKERS if m.lower() in html_lower]
                    result["is_spa"] = len(spa_hits) >= 1
                    result["spa_hits"] = spa_hits[:3]

                    # Extract nav links matching fee/english/entry keywords
                    _kw = re.compile(
                        r"fee|tuition|english|language|entry.require|ielts|pte|toefl",
                        re.I,
                    )
                    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
                    nav_links = []
                    for h in hrefs:
                        if _kw.search(h):
                            full = h if h.startswith("http") else urljoin(origin, h)
                            if urlparse(full).netloc == parsed.netloc and full not in nav_links:
                                nav_links.append(full)
                    result["nav_links"] = nav_links[:15]
            except Exception as exc:
                result["probe_errors"].append(f"homepage: {exc}")

            # ── 2. Sitemap ───────────────────────────────────────────────────
            for sm_path in ["/sitemap.xml", "/sitemap_index.xml"]:
                try:
                    sm_resp = await client.get(origin + sm_path)
                    if sm_resp.status_code == 200 and "<url" in sm_resp.text.lower():
                        result["sitemap_ok"] = True
                        break
                except Exception:
                    pass

            # ── 3. Probe fee URLs (skip if WAF-blocked — all paths will also 403) ──
            if not result["waf_blocked"]:
                for path in _FEE_PATHS:
                    try:
                        r = await client.head(origin + path, timeout=6.0)
                        if r.status_code < 400:
                            result["found_fee_url"] = origin + path
                            break
                        if r.status_code == 405:
                            r2 = await client.get(origin + path, timeout=6.0)
                            if r2.status_code < 400:
                                result["found_fee_url"] = origin + path
                                break
                    except Exception:
                        pass

                # ── 4. Probe English requirement URLs ────────────────────────
                for path in _ENGLISH_PATHS:
                    try:
                        r = await client.head(origin + path, timeout=6.0)
                        if r.status_code < 400:
                            result["found_english_url"] = origin + path
                            break
                        if r.status_code == 405:
                            r2 = await client.get(origin + path, timeout=6.0)
                            if r2.status_code < 400:
                                result["found_english_url"] = origin + path
                                break
                    except Exception:
                        pass

    except Exception as exc:
        result["probe_errors"].append(f"client init: {exc}")

    return result


# ── AI YAML fix ───────────────────────────────────────────────────────────────

class AiFixBody(BaseModel):
    prompt: str
    yaml_content: str = ""  # current editor content; falls back to file on disk


@router.post("/scraper-configs/{slug}/ai-fix")
async def ai_fix_scraper_config(
    slug: str,
    body: AiFixBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
) -> JSONResponse:
    """Apply an operator-described change to a YAML config using Gemini.

    Accepts the current YAML from the editor plus a plain-English description
    of what to change.  Returns the updated YAML — no file is written; the
    operator must still click Save.
    """
    _validate_slug(slug)
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Gemini client error: {exc}") from exc

    # Use yaml_content from body if provided, otherwise load from the file on disk
    current_yaml = body.yaml_content.strip() if body.yaml_content.strip() else _read_yaml_raw(_slug_path(slug))
    if not current_yaml:
        raise HTTPException(
            status_code=404,
            detail=f"No config found for slug '{slug}' — save it first or paste YAML into the editor",
        )

    # Load YAML examples from universities with good scrape results
    examples = await _load_example_yamls(db, exclude_slug=slug, max_examples=3)
    examples_block = ""
    if examples:
        parts = []
        for ex in examples:
            parts.append(
                f"--- EXAMPLE: {ex['university_name']} ({ex['imported_count']} courses staged) ---\n"
                f"{ex['yaml_excerpt']}\n"
                f"--- END EXAMPLE ---"
            )
        examples_block = (
            "\n\nThe following are YAML configs from universities whose scrapes work well. "
            "Use them as style and structure references:\n\n" + "\n\n".join(parts)
        )

    settings_reference = _read_yaml_raw(_TEMPLATE_FILE)[:10000]

    prompt = f"""You are a university scraper configuration assistant helping a non-technical portal admin update YAML settings.

STRICT LANGUAGE RULES:
- NEVER mention code files, module names, function names, or internal implementation details.
- NEVER say "Developer should..." or "Engineering should...".
- If the request cannot be done with YAML settings, add ONLY this comment in the YAML:
  # This setting cannot be changed via YAML — please contact support with a description of what you need.
  Then leave the rest of the file unchanged. Do NOT add any technical explanation.

SETTINGS REFERENCE (every available key with comments and examples):
{settings_reference}
{examples_block}

Current YAML config for this university:
--- CURRENT CONFIG ---
{current_yaml}
--- END CURRENT CONFIG ---

Operator request:
{body.prompt.strip()}

Instructions:
- Apply ONLY the changes needed to fulfil the operator request.
- Preserve every existing key, comment, indentation, and structure that the request does not touch.
- Keep all existing rationale / bug-history comment lines unchanged.
- Only use YAML keys that exist in the SETTINGS REFERENCE above — never invent new keys.
- Output ONLY the complete updated YAML — no markdown fences, no explanation, no preamble."""

    try:
        yaml_text = await _call_gemini_with_retry(client, prompt)
        # Strip markdown fences if Gemini wrapped them anyway
        if yaml_text.startswith("```"):
            yaml_text = re.sub(r"^```(?:yaml)?\n?", "", yaml_text)
            yaml_text = re.sub(r"\n?```$", "", yaml_text.strip())
        yaml_text = yaml_text.strip()

        # Validate the output parses as valid YAML
        try:
            yaml.safe_load(yaml_text)
        except yaml.YAMLError as ye:
            raise HTTPException(status_code=422, detail=f"AI produced invalid YAML: {ye}")

        return JSONResponse(content={"yaml": yaml_text})

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Gemini ai_fix failed for slug=%r", slug)
        raise HTTPException(status_code=500, detail=f"AI fix failed: {exc}") from exc


# ── AI Diagnose & Fix ─────────────────────────────────────────────────────────

class AiDiagnoseBody(BaseModel):
    yaml_content: str = ""   # current editor YAML; falls back to file on disk
    prompt: str = ""         # optional extra instruction from operator


@router.post("/scraper-configs/{slug}/ai-diagnose")
async def ai_diagnose_scraper_config(
    slug: str,
    body: AiDiagnoseBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
) -> JSONResponse:
    """Auto-diagnose scrape problems and apply AI-suggested YAML fixes.

    Gathers evidence from the last scrape job (field fill rates, staged
    course samples, quality issues) and feeds it to Gemini so it can reason
    about root causes and apply targeted YAML fixes — no developer knowledge
    needed by the operator.

    Returns:
        university_found  — whether we resolved the slug to a DB university
        university_name   — matched university name
        last_job          — summary of the most-recent scrape job
        issues            — list of {severity, title, detail} objects
        changes           — list of plain-English change descriptions
        summary           — one-sentence non-technical summary
        yaml              — complete updated YAML (may be unchanged)
        has_changes       — whether the YAML was modified
    """
    _validate_slug(slug)
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Gemini client error: {exc}") from exc

    current_yaml = body.yaml_content.strip() if body.yaml_content.strip() else _read_yaml_raw(_slug_path(slug))
    if not current_yaml:
        raise HTTPException(
            status_code=404,
            detail=f"No config found for slug '{slug}' — save it first or paste YAML into the editor",
        )

    # ── Resolve slug → university_id via hostname matching ────────────────────
    hostname = _extract_hostname_from_yaml(current_yaml)
    university_id: Optional[int] = None
    university_name: str = "Unknown"

    admin_config_json: dict = {}  # DB scrape_config for this university (if found)
    if hostname:
        db_rows = (await db.execute(
            text("""
                SELECT id, name, scrape_config,
                       LOWER(REGEXP_REPLACE(COALESCE(scrape_url, website, ''), '^https?://', '')) AS bare_url
                FROM universities
                WHERE COALESCE(scrape_url, website, '') != ''
            """)
        )).all()
        h_bare = re.sub(r"^www\.", "", hostname.lower())
        for row in db_rows:
            uni_id, uni_name, uni_scrape_config, bare_url = row
            if not bare_url:
                continue
            url_host = re.sub(r"^www\.", "", bare_url.split("/")[0])
            if url_host == h_bare or url_host.endswith("." + h_bare):
                university_id = uni_id
                university_name = uni_name or "Unknown"
                admin_config_json = uni_scrape_config or {}
                break

    # ── Gather scrape evidence ─────────────────────────────────────────────────
    last_job_summary: dict[str, Any] = {}
    field_fill_rates: dict[str, dict] = {}
    staged_samples: list[dict] = []
    quality_issues: list[str] = []

    if university_id is not None:
        from sqlalchemy import select as _sel, desc as _desc, func as _func
        from app.models import ScrapeRuntimeJob, ScrapedCourse

        # Most-recent scrape job (any status)
        last_job = (await db.execute(
            _sel(ScrapeRuntimeJob)
            .where(ScrapeRuntimeJob.university_id == university_id)
            .order_by(_desc(ScrapeRuntimeJob.created_at))
            .limit(1)
        )).scalar_one_or_none()

        if last_job:
            pipeline_stats: dict = (last_job.discovered_config or {}).get("pipeline_stats") or {}
            last_job_summary = {
                "job_id": last_job.runtime_job_id,
                "status": last_job.status,
                "total_found": last_job.total_found or 0,
                "imported": last_job.imported or 0,
                "errors": last_job.errors or 0,
                "created_at": str(last_job.created_at)[:19] if last_job.created_at else None,
                "raw_discovered": pipeline_stats.get("raw_discovered", last_job.total_found or 0),
                "after_filter": pipeline_stats.get("after_filter", last_job.total_found or 0),
                "filter_drop_count": pipeline_stats.get("filter_drop_count", 0),
            }

            # ── Field fill rates ───────────────────────────────────────────────
            _FIELDS = [
                "course_name", "degree_level", "category", "study_mode",
                "course_location", "duration", "intake_months",
                "international_fee", "description", "academic_level",
                "academic_score", "english_test", "other_requirement",
            ]
            total_staged = (await db.execute(
                _sel(_func.count()).where(ScrapedCourse.scrape_job_id == last_job.runtime_job_id)
            )).scalar() or 0

            if total_staged > 0:
                try:
                    from app.models.evidence import ScrapedFieldEvidence
                    ev_rows = (await db.execute(
                        _sel(
                            ScrapedFieldEvidence.field_key,
                            _func.count(ScrapedFieldEvidence.id).label("filled"),
                        )
                        .join(ScrapedCourse, ScrapedFieldEvidence.scraped_course_id == ScrapedCourse.id)
                        .where(
                            ScrapedCourse.scrape_job_id == last_job.runtime_job_id,
                            ScrapedFieldEvidence.selected.is_(True),
                            ScrapedFieldEvidence.field_key.in_(_FIELDS),
                        )
                        .group_by(ScrapedFieldEvidence.field_key)
                    )).all()
                    filled_map = {r.field_key: r.filled for r in ev_rows}
                    for f in _FIELDS:
                        filled = filled_map.get(f, 0)
                        rate = round(filled / total_staged, 3)
                        field_fill_rates[f] = {"rate": rate, "filled": filled, "total": total_staged}
                        pct = int(rate * 100)
                        if rate < 0.50:
                            quality_issues.append(f"CRITICAL: {f} fill rate only {pct}% ({filled}/{total_staged} courses have this field)")
                        elif rate < 0.80:
                            quality_issues.append(f"WARNING: {f} fill rate {pct}% ({filled}/{total_staged} courses)")
                except Exception as _e:
                    log.warning("ai_diagnose: evidence query failed: %s", _e)

                # ── Staged samples (lowest completeness first) ─────────────────
                sample_rows = (await db.execute(
                    _sel(ScrapedCourse)
                    .where(ScrapedCourse.scrape_job_id == last_job.runtime_job_id)
                    .order_by(ScrapedCourse.completeness.asc())
                    .limit(6)
                )).scalars().all()
                for r in sample_rows:
                    missing = [
                        f for f, v in [
                            ("international_fee", r.international_fee),
                            ("study_mode", r.study_mode),
                            ("course_location", r.course_location),
                            ("intake_months", r.intake_months),
                            ("duration", r.duration),
                            ("degree_level", r.degree_level),
                            ("academic_level", r.academic_level),
                        ] if not v
                    ]
                    staged_samples.append({
                        "name": (r.course_name or "unnamed")[:80],
                        "completeness_pct": int((r.completeness or 0) * 100),
                        "missing_fields": missing,
                        "auto_publish_status": r.auto_publish_status or "unknown",
                    })

            # ── Zero-discovery / zero-staging check ───────────────────────────
            raw_disc = last_job_summary["raw_discovered"]
            after_filter = last_job_summary["after_filter"]
            if raw_disc == 0:
                quality_issues.insert(0, "CRITICAL: Zero courses discovered — site is likely JavaScript-rendered (BFS/static crawling returned nothing). Enable always_browser_discover and use_stealth_browser.")
            elif raw_disc > 10 and after_filter == 0:
                quality_issues.insert(0, f"CRITICAL: Discovery found {raw_disc} URLs but URL filter dropped ALL of them — allow_url_patterns or must_contain is too restrictive. Remove or relax those filters.")
            elif total_staged == 0 and raw_disc > 0:
                quality_issues.insert(0, (
                    f"CRITICAL: {raw_disc} URLs discovered, {after_filter} passed URL-filter, but 0 courses were staged — "
                    "the extraction step is failing on every page. Root causes to check: "
                    "(1) block_url_patterns is blocking actual course-detail pages, only listing/navigation pages should pass, "
                    "(2) allow_url_patterns is too strict and is passing only navigation pages not course pages, "
                    "(3) the site uses JavaScript rendering so pages arrive empty — enable always_browser_discover+use_stealth_browser, "
                    "(4) extraction selectors don't match the site's HTML structure."
                ))
            elif last_job_summary["imported"] < 10 and raw_disc > 20:
                quality_issues.append(
                    f"WARNING: Only {last_job_summary['imported']} courses staged out of {raw_disc} discovered — "
                    "very low staging rate suggests URL-filters are admitting navigation/listing pages instead of course-detail pages."
                )

    # ── Detect critical admin-config issues (DB scrape_config JSON) ───────────
    admin_config_issues: list[str] = []
    if admin_config_json:
        extr_cfg = admin_config_json.get("extraction", {}) or {}
        filters_cfg = extr_cfg.get("filters", {}) or {}
        online_only = filters_cfg.get("online_only", {}) or {}
        if online_only.get("enabled"):
            admin_config_issues.append(
                "CRITICAL: online_only filter is ENABLED in the DB admin config — "
                "only online/distance courses are kept; ALL on-campus courses are silently discarded. "
                "This is the most common cause of a suspiciously low staged count. "
                "Fix: go to the university's admin config and set online_only.enabled to false."
            )
        domestic_only = filters_cfg.get("domestic_only", {}) or {}
        if domestic_only.get("enabled"):
            admin_config_issues.append(
                "WARNING: domestic_only filter is ENABLED in the DB admin config — "
                "international course variants are being filtered out. "
                "Fix: set domestic_only.enabled to false unless this is intentional."
            )
        # Warn if _min_expected_courses far exceeds actual staged count
        min_expected = admin_config_json.get("_min_expected_courses", 0) or 0
        staged = last_job_summary.get("imported", 0)
        if min_expected > 0 and staged > 0 and staged < min_expected * 0.3:
            admin_config_issues.append(
                f"WARNING: Only {staged} courses staged but admin config expects ≥{min_expected} — "
                f"staged count is less than 30% of expected. A filter or discovery problem is likely."
            )

    # ── Live website inspection (always run — gives AI real evidence) ─────────
    # Inspect if: 0 staged, OR low staging rate (<30% of expected), OR any CRITICAL issue detected.
    # This ensures Gemini always has ground-truth data about what's actually on the site.
    live_page_block = ""
    should_inspect = (
        not last_job_summary  # never scraped
        or last_job_summary.get("imported", 0) == 0  # zero staged
        or last_job_summary.get("raw_discovered", 0) == 0  # zero discovered
        or len(quality_issues) > 0  # any quality issues
        or len(admin_config_issues) > 0  # any admin config issues
    )
    if should_inspect:
        # Resolve best seed URL: YAML seed_urls first, then DB scrape_url / website
        inspect_urls = _extract_seed_urls_from_yaml(current_yaml)
        if university_id and not inspect_urls:
            try:
                uni_row = (await db.execute(
                    text("SELECT COALESCE(scrape_url, website, '') AS url FROM universities WHERE id = :uid"),
                    {"uid": university_id}
                )).one_or_none()
                if uni_row and uni_row.url:
                    inspect_urls = [uni_row.url]
            except Exception:
                pass
        if inspect_urls:
            fetch_url = inspect_urls[0]
            log.info("ai_diagnose: running live website inspection on %s", fetch_url)
            inspection_report = await _inspect_website_for_ai(fetch_url)
            live_page_block = (
                f"\n=== LIVE WEBSITE INSPECTION REPORT (fetched right now from {fetch_url}) ===\n"
                "This is what the university's website ACTUALLY looks like today. "
                "Use this evidence to make specific, accurate diagnosis and YAML fixes:\n\n"
                + inspection_report
                + "\n=== END LIVE WEBSITE INSPECTION ==="
            )

    # ── Load successful YAML examples for AI context ───────────────────────────
    examples = await _load_example_yamls(db, exclude_slug=slug, max_examples=3)
    examples_section = ""
    if examples:
        parts = []
        for ex in examples:
            parts.append(
                f"--- EXAMPLE ({ex['university_name']}, {ex['imported_count']} courses staged successfully) ---\n"
                f"{ex['yaml_excerpt']}\n--- END EXAMPLE ---"
            )
        examples_section = (
            "\n=== REFERENCE CONFIGS FROM UNIVERSITIES THAT SCRAPE WELL ===\n"
            "(Use these as structural/stylistic references when suggesting fixes)\n\n"
            + "\n\n".join(parts)
            + "\n=== END REFERENCE CONFIGS ==="
        )

    # ── Build Gemini prompt ────────────────────────────────────────────────────
    settings_reference = _read_yaml_raw(_TEMPLATE_FILE)[:10000]

    admin_cfg_block = ""
    if admin_config_json:
        import json as _json
        admin_cfg_block = (
            "\n=== DB ADMIN CONFIG (universities.scrape_config JSON) ===\n"
            + _json.dumps(admin_config_json, indent=2)[:1200]
            + "\n=== END ADMIN CONFIG ==="
        )
    admin_issues_block = ""
    if admin_config_issues:
        admin_issues_block = (
            "\n=== ADMIN CONFIG ISSUES (detected before Gemini analysis) ===\n"
            + "\n".join(f"  - {i}" for i in admin_config_issues)
            + "\n=== END ADMIN CONFIG ISSUES ==="
        )

    job_block = "No scrape jobs found for this university yet — config has never been run." if not last_job_summary else (
        f"  Status: {last_job_summary['status']}\n"
        f"  Raw URLs discovered: {last_job_summary['raw_discovered']}\n"
        f"  URLs after URL-filter: {last_job_summary['after_filter']}\n"
        f"  Courses staged (extracted): {last_job_summary['imported']}\n"
        f"  Errors: {last_job_summary['errors']}\n"
        f"  Run date: {last_job_summary.get('created_at', 'unknown')}"
    )
    fill_block = "\n".join(
        f"  {f}: {int(d['rate'] * 100)}%  ({d['filled']}/{d['total']} courses)"
        for f, d in field_fill_rates.items()
    ) or "  No field fill data available (no staged courses in last job)."
    quality_block = "\n".join(f"  - {q}" for q in quality_issues) or "  No critical issues detected from data."
    samples_block = "\n".join(
        f"  [{i+1}] {s['name']}  completeness={s['completeness_pct']}%  missing=[{', '.join(s['missing_fields']) or 'none'}]  status={s['auto_publish_status']}"
        for i, s in enumerate(staged_samples)
    ) or "  No staged courses to sample."

    extra_instr = f"\n\nOperator's additional note: {body.prompt.strip()}" if body.prompt.strip() else ""

    gemini_prompt = f"""You are a university scraper configuration assistant. You help non-technical university administrators improve their scraping results by adjusting YAML settings. Your audience is a portal admin, NOT a developer.

STRICT LANGUAGE RULES — violations will confuse and frustrate the user:
- NEVER mention code files, module names, function names, or internal implementation details
  (no "browser_discovery.py", no "requests.get", no "page.locator", no "discover_candidate_course_pages", no Python/JS code)
- NEVER say "Developer should...", "Engineering should...", or "Check the code..."
- If a problem cannot be fixed with YAML settings, say exactly:
  "This issue requires a support ticket — YAML changes alone cannot fix it."
  Then briefly describe WHAT is wrong in plain English (e.g. "The university's website blocks automated requests") and stop.
- Write as if explaining to someone who has never seen a terminal. Plain English only.

HOW TO READ THE EVIDENCE AND APPLY YAML FIXES:

Using the live website inspection report:
- If the inspection says "JAVASCRIPT RENDERING DETECTED" or the body is very short (< 400 chars):
  → The site is JavaScript-rendered. Set always_browser_discover: true and use_stealth_browser: true.
  → Do NOT suggest URL pattern changes — the problem is rendering, not filtering.
- If the inspection lists URL path patterns that contain /courses/, /study/, or /programs/:
  → Set allow_url_patterns to those paths. Example: allow_url_patterns: ["/courses/"]
  → Remove any block_url_patterns entries that would block those same paths.
- If the sitemap was found and contains course URLs:
  → Add always_sitemap_supplement: true and set allow_url_patterns to the path prefix from the sitemap.
- If the seed URL returned an error or was empty:
  → Update seed_urls to the correct course listing page URL you found in the inspection.
- If "international fee: NOT FOUND" on the course page:
  → The fee is probably on a separate fees page or behind a JavaScript tab. Suggest fees.central_page or fees.fees_pdf_url.
- If "IELTS: NOT FOUND" on the course page:
  → The English requirements are probably on a central page. Suggest english.central_page.

=== YAML SETTINGS REFERENCE (every available key with comments and examples) ===
{settings_reference}
=== END REFERENCE ==={examples_section}{live_page_block}

=== UNIVERSITY BEING DIAGNOSED ===
Name: {university_name}
Slug: {slug}
{admin_cfg_block}{admin_issues_block}

=== LAST SCRAPE JOB STATISTICS ===
{job_block}

=== FIELD FILL RATES (% of staged courses where each field was successfully extracted) ===
{fill_block}

=== PROBLEMS DETECTED FROM DATA (machine-verified — include every CRITICAL issue in your DIAGNOSIS) ===
{quality_block}

=== WORST-PERFORMING STAGED COURSES (samples with lowest completeness) ===
{samples_block}

=== CURRENT YAML CONFIG ===
{current_yaml}
=== END CURRENT CONFIG ==={extra_instr}

Respond in this EXACT format (no markdown, no code fences, no extra sections):

DIAGNOSIS:
- [CRITICAL] Short plain-English title | What is wrong and why — quote actual numbers from the stats. No code or file names.
- [WARNING] Short plain-English title | Explanation.
- [INFO] Short plain-English title | Explanation.
(one bullet per distinct issue; if an issue cannot be fixed by YAML say "Needs support ticket" in the title)

CHANGES:
- Describe each YAML setting you added or changed and why it helps. Plain English. No code.
(one bullet per change; write "No changes needed" if the config is already correct)

SUMMARY:
One plain-English sentence saying what was wrong and what the fix does (or that a support ticket is needed).

YAML:
(complete updated YAML with all fixes applied — preserve every existing comment and key)

Rules:
- Include every CRITICAL issue from "PROBLEMS DETECTED FROM DATA" in the DIAGNOSIS.
- Quote actual numbers and URL patterns from the evidence — never make up values.
- Only use YAML keys that exist in the SETTINGS REFERENCE above — never invent new keys.
- Preserve all existing YAML comments and structure.
- Output ONLY the four sections above. No extra commentary after YAML."""

    try:
        raw_text = await _call_gemini_with_retry(client, gemini_prompt)

        # ── Parse structured sections ──────────────────────────────────────────
        def _section(text: str, header: str) -> str:
            m = re.search(
                rf"^{re.escape(header)}\s*\n(.*?)(?=\n[A-Z]+:|\Z)",
                text, re.MULTILINE | re.DOTALL,
            )
            return m.group(1).strip() if m else ""

        diagnosis_raw = _section(raw_text, "DIAGNOSIS")
        changes_raw = _section(raw_text, "CHANGES")
        summary_raw = _section(raw_text, "SUMMARY")
        yaml_raw = _section(raw_text, "YAML")

        # Strip fences Gemini may add despite instructions
        if yaml_raw.startswith("```"):
            yaml_raw = re.sub(r"^```(?:yaml)?\n?", "", yaml_raw)
            yaml_raw = re.sub(r"\n?```$", "", yaml_raw.strip())
        yaml_raw = yaml_raw.strip()

        # Validate; fall back to original on bad YAML
        if yaml_raw:
            try:
                yaml.safe_load(yaml_raw)
            except yaml.YAMLError as ye:
                log.warning("ai_diagnose: Gemini produced invalid YAML for %r: %s", slug, ye)
                yaml_raw = current_yaml
                summary_raw = f"AI produced invalid YAML ({ye}); original config preserved unchanged."

        # ── Parse diagnosis bullets into structured items ───────────────────
        issues: list[dict] = []
        for raw_line in diagnosis_raw.splitlines():
            line = raw_line.strip().lstrip("- ").strip()
            if not line:
                continue
            line_upper = line.upper()
            if "[CRITICAL]" in line_upper[:20]:
                severity = "critical"
            elif "[WARNING]" in line_upper[:20]:
                severity = "warning"
            elif "[INFO]" in line_upper[:20]:
                severity = "info"
            else:
                severity = "info"
            cleaned = re.sub(r"\[(CRITICAL|WARNING|INFO)\]\s*", "", line, flags=re.IGNORECASE).strip()
            parts = cleaned.split("|", 1)
            issues.append({
                "severity": severity,
                "title": parts[0].strip(),
                "detail": parts[1].strip() if len(parts) > 1 else "",
            })

        # ── Merge deterministic issues Gemini may have missed ─────────────
        # Machine-verified problems are ground truth — inject any that Gemini
        # didn't mention so operators always see the real data-backed issues.
        existing_titles_lower = {i["title"].lower() for i in issues}

        def _inject_if_missing(raw_issue: str, severity: str) -> None:
            parts = raw_issue.split("|", 1) if "|" in raw_issue else [raw_issue]
            title = re.sub(r"^CRITICAL:\s*|^WARNING:\s*|^INFO:\s*", "", parts[0], flags=re.IGNORECASE).strip()
            detail = parts[1].strip() if len(parts) > 1 else parts[0].strip()
            # Check if Gemini already covered this (fuzzy — first 6 words)
            key = " ".join(title.lower().split()[:6])
            already_covered = any(key in t for t in existing_titles_lower)
            if not already_covered:
                issues.insert(0 if severity == "critical" else len(issues), {
                    "severity": severity,
                    "title": title[:120],
                    "detail": detail,
                })

        for qi in quality_issues:
            if qi.upper().startswith("CRITICAL"):
                _inject_if_missing(qi, "critical")
            elif qi.upper().startswith("WARNING"):
                _inject_if_missing(qi, "warning")

        for ai_issue in admin_config_issues:
            if ai_issue.upper().startswith("CRITICAL"):
                _inject_if_missing(ai_issue, "critical")
            elif ai_issue.upper().startswith("WARNING"):
                _inject_if_missing(ai_issue, "warning")

        # ── Parse changes list ────────────────────────────────────────────
        changes_list = [
            ln.strip().lstrip("- ").strip()
            for ln in changes_raw.splitlines()
            if ln.strip().lstrip("- ").strip()
        ]

        has_changes = bool(yaml_raw) and yaml_raw != current_yaml.strip()

        # If deterministic issues exist but Gemini said no changes — flag it
        crit_count = sum(1 for i in issues if i["severity"] == "critical")
        if crit_count > 0 and not has_changes and not changes_list:
            changes_list = ["⚠ AI could not determine automatic YAML fixes for the detected issues — review the CRITICAL items above and adjust config manually or add an operator note describing the problem."]
            if not summary_raw:
                summary_raw = f"{crit_count} critical issue(s) detected from scrape data — manual review needed."

        return JSONResponse(content={
            "university_found": university_id is not None,
            "university_name": university_name,
            "university_id": university_id,
            "last_job": last_job_summary or None,
            "issues": issues,
            "changes": changes_list,
            "summary": summary_raw,
            "yaml": yaml_raw or current_yaml,
            "has_changes": has_changes,
        })

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Gemini ai_diagnose failed for slug=%r", slug)
        raise HTTPException(status_code=500, detail=f"AI diagnosis failed: {exc}") from exc


@router.post("/scraper-configs/generate")
async def generate_scraper_config(
    body: GenerateConfigBody,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
) -> JSONResponse:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    try:
        from google import genai as google_genai
        client = google_genai.Client(api_key=api_key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Gemini client error: {exc}") from exc

    # Build a meaningful slug by skipping generic words and joining the rest
    _GENERIC_WORDS = {"university", "of", "the", "and", "college", "institute",
                      "technology", "for", "at", "in", "a", "an"}
    _name_words = [
        re.sub(r"[^a-z0-9]+", "", w)
        for w in body.university_name.lower().split()
        if re.sub(r"[^a-z0-9]+", "", w) not in _GENERIC_WORDS
    ]
    slug_candidate = "".join(_name_words)[:30] or re.sub(r"[^a-z0-9]+", "", body.university_name.lower())[:20] or "university"

    # Derive currency from country
    _country_lower = body.country.lower()
    if "new zealand" in _country_lower or "nz" in _country_lower:
        _currency = "NZD"
    elif "united kingdom" in _country_lower or "uk" in _country_lower or "britain" in _country_lower:
        _currency = "GBP"
    elif "canada" in _country_lower:
        _currency = "CAD"
    elif "united states" in _country_lower or "usa" in _country_lower:
        _currency = "USD"
    else:
        _currency = "AUD"

    # ── Pre-crawl the real site so Gemini works from facts, not hallucinations ──
    log.info("Probing university site %r before Gemini generation", body.website_url)
    probe = await _probe_university_site(body.website_url)
    log.info("Probe result for %r: homepage_ok=%s is_spa=%s sitemap_ok=%s fee_url=%s english_url=%s nav_links=%d errors=%s",
             body.website_url, probe["homepage_ok"], probe["is_spa"], probe["sitemap_ok"],
             probe["found_fee_url"], probe["found_english_url"], len(probe["nav_links"]),
             probe["probe_errors"])

    # ── Translate probe results into concrete YAML decisions ─────────────────
    # These drive what we TELL Gemini to set — never bleed raw probe output
    # into the YAML comment block.
    _discovery_directives: list[str] = []
    _extraction_directives: list[str] = []
    _rationale_lines: list[str] = []

    if probe["waf_blocked"]:
        _discovery_directives.append(
            f'  use_stealth_browser: true  '
            f'# Site returned HTTP {probe["homepage_status"]} (WAF/Cloudflare) — '
            f'plain HTTP probes are blocked; stealth Playwright bypasses it'
        )
        _discovery_directives.append(
            '  always_sitemap_supplement: true  '
            '# WAF-protected sites are usually SPAs; sitemap is more reliable than BFS'
        )
        _rationale_lines.append(
            f'  - Site returns HTTP {probe["homepage_status"]} to plain HTTP probes '
            f'(Cloudflare/WAF). Stealth browser mode enabled.'
        )
    elif probe["is_spa"]:
        _discovery_directives.append(
            f'  always_sitemap_supplement: true  '
            f'# SPA detected ({", ".join(probe["spa_hits"][:2])}); '
            f'BFS misses JS-rendered course pages'
        )
        _rationale_lines.append(
            f'  - JS-rendered SPA (markers: {", ".join(probe["spa_hits"][:2])}). '
            f'Sitemap supplement ensures full course discovery.'
        )

    if probe["sitemap_ok"] and not probe["is_spa"] and not probe["waf_blocked"]:
        _rationale_lines.append('  - sitemap.xml present and usable.')

    _fees_lines: list[str] = [f'    default_currency: "{_currency}"']
    if probe["found_fee_url"]:
        _fees_lines.append(f'    central_page: "{probe["found_fee_url"]}"  # confirmed HTTP 200')
        _rationale_lines.append(f'  - Fee page confirmed at: {probe["found_fee_url"]}')

    _english_lines: list[str] = []
    if probe["found_english_url"]:
        _english_lines.append(f'    central_page: "{probe["found_english_url"]}"  # confirmed HTTP 200')
        _rationale_lines.append(f'  - English requirements page confirmed at: {probe["found_english_url"]}')

    # Nav links found but no confirmed standard path — give Gemini the list to pick from
    _nav_hint = ""
    if probe["nav_links"] and not probe["found_fee_url"] and not probe["found_english_url"]:
        _nav_hint = (
            "\n\nThese navigation links were extracted from the homepage "
            "(fee/english/entry keywords). You may recognise the correct "
            "fee or English-requirements page URL from this list — if so, "
            "include it as extraction.fees.central_page or "
            "extraction.english.central_page. If unsure, omit the field entirely:\n"
            + "\n".join(f"  - {lnk}" for lnk in probe["nav_links"])
        )

    _rationale_block = (
        "#\n# Bug history / rationale:\n"
        + "\n".join(f"#   {l}" for l in _rationale_lines)
        if _rationale_lines else ""
    )

    _discovery_block = (
        "discovery:\n" + "\n".join(_discovery_directives)
        if _discovery_directives else ""
    )

    _fees_block = "  fees:\n" + "\n".join(_fees_lines)

    _extraction_inner = _fees_block
    # english.central_page: ONLY if the live probe confirmed a real URL —
    # never from Gemini guessing (unverified URLs produce noise or zero records)
    if _english_lines:
        _extraction_inner += "\n  english:\n" + "\n".join(_english_lines)

    _hostname = body.website_url.split("//")[-1].rstrip("/").split("/")[0]

    _yaml_template = f"""# {body.university_name}
# Hostname: {_hostname}
{_rationale_block}
{_discovery_block}
extraction:
{_extraction_inner}
  filters:
    domestic_only:
      enabled: false  # SAFE DEFAULT — only set true if you have confirmed the site serves ONLY domestic courses
    online_only:
      enabled: false  # set true only for distance-education-only institutions"""

    prompt = f"""You are a scraper configuration expert for a university course data system.

The following is a complete, safe starter YAML config for {body.university_name} ({body.website_url}).
It has already been populated from a live probe of the site. Output it with ONLY the improvements listed below.

{_yaml_template}

Operator notes: {body.notes or "none"}.

THIS IS A FIRST-RUN STUB. Output the template above UNCHANGED.
The only permitted modification is incorporating specific information from the operator notes above, if any.

STRICTLY FORBIDDEN — every item below causes real harm:
- DO NOT add bfs_page_budget — it is set after a real scrape shows BFS hitting the page cap. Guessing 750 on a small university wastes 30-60 minutes of stealth-browser time per run.
- DO NOT add block_url_patterns — these are written from actual [DISCOVER] log output after a first scrape, never speculatively. Wrong patterns silently drop real course pages.
- DO NOT add default_ielts or default_pte — these stamp fabricated scores onto every course with no real data. A student sees 6.5, applies to Nursing (real requirement: 7.0), gets rejected.
- DO NOT change domestic_only.enabled to true — false is the safe default.
- DO NOT invent or guess any URL (central_page, fees_pdf_url, etc.) not already confirmed in the template.
- DO NOT add or change any rationale comment lines.
- DO NOT add any field not already in the template unless the operator notes explicitly request it.

Output ONLY the completed YAML — no markdown fences, no commentary:"""

    try:
        yaml_text = await _call_gemini_with_retry(client, prompt)
        # Strip markdown fences if Gemini wrapped them anyway
        if yaml_text.startswith("```"):
            yaml_text = re.sub(r"^```(?:yaml)?\n?", "", yaml_text)
            yaml_text = re.sub(r"\n?```$", "", yaml_text.strip())
        yaml_text = yaml_text.strip()

        # Validate it parses
        try:
            yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            yaml_text = f"# {body.university_name}\n# Auto-generated (parse failed — edit manually)\n\ndiscovery: {{}}\nextraction:\n  fees:\n    default_currency: \"{'NZD' if 'nz' in body.website_url.lower() else 'AUD'}\"\n"

        return JSONResponse(content={
            "slug": slug_candidate,
            "yaml": yaml_text,
        })
    except Exception as exc:
        log.exception("Gemini generate failed for %r", body.university_name)
        raise HTTPException(status_code=500, detail=f"AI generation failed: {exc}") from exc

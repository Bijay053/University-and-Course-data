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

_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

_HISTORY_KEEP = 100  # rows per slug retained (older rows pruned on save)


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
    configs = []
    hostnames: list[str] = []
    for f in sorted(_UNIS_DIR.glob("*.yaml")):
        slug = f.stem
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

    _fees_block = "  fees:\n" + "\n".join(f"  {l}" for l in _fees_lines)

    _extraction_inner = _fees_block
    # english.central_page: ONLY if the live probe confirmed a real URL —
    # never from Gemini guessing (unverified URLs produce noise or zero records)
    if _english_lines:
        _extraction_inner += "\n  english:\n" + "\n".join(f"  {l}" for l in _english_lines)

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

ALLOWED CHANGES (operator notes say: {body.notes or "none"}):
- You may add discovery.bfs_page_budget if {body.university_name} is a large university with many courses.
- You may add discovery.block_url_patterns if you know specific URL patterns to avoid.
- Keep the comment block exactly as written — do NOT add or change any rationale lines.
- Do NOT change any confirmed WAF/SPA settings or confirmed URLs.

STRICTLY FORBIDDEN — these cause silent data corruption:
- DO NOT add default_ielts or default_pte — these stamp fabricated values onto every course that lacks real data. Blank is always better than a wrong number.
- DO NOT change domestic_only.enabled to true — the safe default is false. Only true if you have confirmed the site physically serves ONLY domestic courses with no international option at all.
- DO NOT invent or guess any central_page, fees_pdf_url, or other URL not already in the template above.
- DO NOT add any field you are not 100% certain applies. When in doubt, omit it.

Output ONLY the completed YAML — no markdown fences, no commentary:"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        yaml_text: str = response.text or ""
        yaml_text = yaml_text.strip()
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

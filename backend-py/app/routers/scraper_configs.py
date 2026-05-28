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

    slug_candidate = re.sub(r"[^a-z0-9]+", "", body.university_name.lower().split()[0])[:20] or "university"

    prompt = f"""You are a scraper configuration expert for a university course data system.

Generate a per-university YAML scraper config for:
- University name: {body.university_name}
- Website: {body.website_url}
- Country: {body.country}
- Additional notes: {body.notes or "None"}

{_DEFAULTS_YAML_SUMMARY}

Rules:
1. Start with a comment block: # {body.university_name}\\n# Hostname: <derived from URL>
2. Only include fields that need to be DIFFERENT from defaults — keep the config minimal.
3. For NZ/UK universities, set default_currency to "NZD" or "GBP".
4. If the site is known to be React/Vue/Angular SPA (check domain hints), set always_sitemap_supplement: true.
5. If the university only lists international courses, domestic_only.enabled should be false (default).
6. Add a brief comment explaining each non-default setting.
7. Output ONLY valid YAML — no markdown fences, no extra text before or after.

Example minimal config:
# Example University
# Hostname: example.edu.au

extraction:
  english:
    central_page: "https://example.edu.au/international/english-requirements"
  filters:
    domestic_only:
      enabled: true  # Site lists domestic and international courses mixed

Now generate the config for {body.university_name}:"""

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

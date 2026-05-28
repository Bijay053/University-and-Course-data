"""CRUD + AI-generation for per-university scraper YAML configs.

Endpoints
---------
GET  /api/settings/scraper-configs                → list all slugs + raw YAML (+ university_id)
GET  /api/settings/scraper-configs/{slug}         → get one config's YAML
PUT  /api/settings/scraper-configs/{slug}         → save / create a config
DELETE /api/settings/scraper-configs/{slug}       → delete a config
POST /api/settings/scraper-configs/generate       → Gemini-generated YAML for a new university
POST /api/settings/scraper-configs/{slug}/trigger → start a scrape job for this config's university
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
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
                           '^https?://(www\\.)?', ''
                       )) AS bare_url
                FROM universities
                WHERE COALESCE(scrape_url, website, '') != ''
            """)
        )).all()
        # Match: config hostname appears at the start of the bare URL (after stripping www.)
        for cfg in configs:
            if not cfg["hostname"]:
                continue
            h = cfg["hostname"].lower()
            # strip www. from the config hostname too for comparison
            h_bare = re.sub(r"^www\.", "", h)
            for uni_id, uni_name, bare_url in rows:
                if bare_url and (bare_url.startswith(h_bare + "/") or bare_url.startswith(h_bare + ":")):
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

    # Discover the GitHub HTTPS remote URL
    rc, remotes_out, _ = await _run("git", "--no-optional-locks", "remote", "-v")
    push_url: str | None = None
    for line in remotes_out.splitlines():
        if "github.com" in line and "(push)" in line:
            parts = line.split()
            if len(parts) >= 2:
                push_url = parts[1]
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


# ── Save / create ─────────────────────────────────────────────────────────────

class SaveConfigBody(BaseModel):
    yaml_content: str


@router.put("/scraper-configs/{slug}")
async def save_scraper_config(
    slug: str,
    body: SaveConfigBody,
    _user: Annotated[dict, Depends(require_permission("settings.edit"))],
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

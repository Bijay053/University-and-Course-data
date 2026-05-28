"""CRUD + AI-generation for per-university scraper YAML configs.

Endpoints
---------
GET  /api/settings/scraper-configs           → list all slugs + raw YAML
GET  /api/settings/scraper-configs/{slug}    → get one config's YAML
PUT  /api/settings/scraper-configs/{slug}    → save / create a config
DELETE /api/settings/scraper-configs/{slug}  → delete a config
POST /api/settings/scraper-configs/generate  → Gemini-generated YAML for a new university
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.dependencies import get_current_user
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


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/scraper-configs")
async def list_scraper_configs(
    _user: Annotated[dict, Depends(require_permission("settings.view"))],
) -> JSONResponse:
    configs = []
    for f in sorted(_UNIS_DIR.glob("*.yaml")):
        slug = f.stem
        raw = _read_yaml_raw(f)
        data = _parse_yaml_safe(raw)
        comment_lines = [ln.lstrip("# ").strip() for ln in raw.splitlines() if ln.startswith("#") and ln.strip() != "#"]
        title = comment_lines[0] if comment_lines else slug.replace("-", " ").replace("_", " ").title()
        configs.append({"slug": slug, "title": title, "yaml": raw, "parsed": data})
    return JSONResponse(content={"configs": configs})


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
    return JSONResponse(content={"ok": True, "slug": slug})


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

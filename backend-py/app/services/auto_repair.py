"""Auto Repair Mode — pipeline service.

Steps for each university:
  1. gather_and_diagnose(uni_id, db)  — gather key metrics, call Gemini, return diagnosis
  2. If risk_label == 'developer_required' → store immediately with that status
  3. _build_patched_config(uni, safe_fix, fix_yaml_snippet) → patched UniConfig
  4. repair_validator.validate_proposed_fix(...) → before/after extraction metrics
  5. Compute confidence + store in auto_repair_suggestions

Safety rules enforced here:
  - Never auto-apply; store only — operator clicks Apply in UI.
  - If safe_fix is None AND fix_yaml_snippet is None → treat as developer_required
    (no safe YAML-level fix was identified).
  - Rate limit: skip if a non-dismissed suggestion already exists for this
    university in the last 24 hours.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ── Gemini prompt (same schema as ai_root_cause_analysis route) ────────────────

_SYSTEM_PROMPT = """You are an expert university scraper diagnostic system. Analyse the real operational data below and identify the root cause of any issues.

STRICT RULES:
1. Base your analysis ONLY on the provided data — do not invent or guess.
2. If data shows healthy operation (completeness ≥85%, no critical alerts), set root_cause_category to "healthy".
3. Every evidence item MUST quote an exact value from the data.
4. safe_fix rules — only suggest ONE of these two actions, or null:
   a. "clear_admin_override": an admin_config key is causing the problem → key = dot-notation path
   b. "set_admin_override": a small config change in admin_config can fix it → key = dot-notation path, value = the new value
   Never suggest safe_fix for issues that require code changes.
5. risk_label = "developer_required" when fix needs: changing Python extractors/regex, adding a new provider, fixing a runtime exception, or changing discovery browser logic.
6. fix_yaml_snippet: only YAML that belongs in the per-uni YAML config file (discovery or extraction section). Max 10 lines. null if not applicable.

OPERATIONAL DATA:
{context_doc}

Return ONLY a valid JSON object:
{{
  "issue_summary": "<1-2 sentence plain-English summary>",
  "root_cause_category": "<discovery|filtering|extraction|config_conflict|api|pdf|browser|staging_gate|healthy>",
  "confidence": "<high|medium|low>",
  "evidence": [{{"type": "<job_stat|rejection|config|alert|extraction|discovery>", "label": "<short label>", "value": "<exact value>", "source": "<source>"}}],
  "fix_recommendation": "<plain-English recommended fix>",
  "fix_yaml_snippet": null,
  "safe_fix": null,
  "risk_label": "<low|medium|developer_required>",
  "developer_required": false,
  "developer_note": null
}}

Include 3-8 evidence items. safe_fix and fix_yaml_snippet may be null."""


# ── Context gathering ──────────────────────────────────────────────────────────

async def _gather_context(uni_id: int, db: AsyncSession) -> tuple[dict[str, Any], str]:
    """Gather key operational signals and return (uni_row_dict, context_doc_str)."""
    from collections import Counter

    # University basics
    uni_res = await db.execute(text("""
        SELECT id, name, scrape_url, scrape_config
        FROM universities WHERE id = :uid
    """), {"uid": uni_id})
    uni_row = dict(uni_res.mappings().first() or {})
    if not uni_row:
        raise ValueError(f"University {uni_id} not found")

    sc_dict: dict = dict(uni_row.get("scrape_config") or {})
    admin_cfg: dict = sc_dict.get("admin_config") or {}
    scrape_url: str = uni_row.get("scrape_url") or ""

    # Last 2 scrape jobs
    jobs_res = await db.execute(text("""
        SELECT runtime_job_id, status, total_found, imported, errors, skipped,
               total_gemini_cost_usd, cost_ceiling_hit, error_message,
               EXTRACT(EPOCH FROM (completed_at - started_at))::int AS duration_s,
               created_at
        FROM scrape_runtime_jobs
        WHERE university_id = :uid
        ORDER BY created_at DESC LIMIT 2
    """), {"uid": uni_id})
    jobs = [dict(r._mapping) for r in jobs_res]
    last_job_id: str | None = jobs[0]["runtime_job_id"] if jobs else None

    # Scraped courses summary
    _jf = "AND scrape_job_id = :jid" if last_job_id else ""
    _jp: dict = {"uid": uni_id}
    if last_job_id:
        _jp["jid"] = last_job_id
    cs_res = await db.execute(text(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
               COUNT(*) FILTER (WHERE status = 'approved') AS approved,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               ROUND(AVG(completeness)) AS avg_completeness,
               COUNT(*) FILTER (WHERE completeness < 70) AS low_count
        FROM scraped_courses
        WHERE university_id = :uid {_jf}
    """), _jp)
    cs = dict(cs_res.mappings().first() or {})

    # Top rejections
    rejection_agg: dict[str, int] = {}
    if last_job_id:
        rej_res = await db.execute(text("""
            SELECT payload->>'kind' AS kind, payload->>'reason' AS reason,
                   payload->>'pattern' AS pattern
            FROM scrape_runtime_logs
            WHERE runtime_job_id = :jid
              AND payload->>'kind' IN (
                  'block_url_filter','extract_block_url_filter',
                  'must_contain_filter','domestic_only_filter','rejected_course'
              )
            LIMIT 200
        """), {"jid": last_job_id})
        rejection_agg = dict(Counter(
            f"{r.kind}:{r.reason or r.pattern or '?'}"
            for r in rej_res
        ).most_common(8))

    # Effective config key fields
    eff_cfg: dict = {}
    try:
        from urllib.parse import urlparse as _up
        from app.services.scraper.config.loader import get_config_for_host as _gcfh
        _host = (_up(scrape_url).hostname or "") if scrape_url else ""
        if _host:
            _cfg = _gcfh(
                hostname=_host, name=uni_row.get("name", ""),
                scrape_url=scrape_url, university_id=uni_id,
                db_scrape_config=sc_dict,
            )
            _disc = _cfg.discovery
            _dom = (
                _cfg.extraction.filters.domestic_only
                if _cfg.extraction and _cfg.extraction.filters and _cfg.extraction.filters.domestic_only
                else None
            )
            eff_cfg = {
                "allow_url_patterns":   list(_disc.allow_url_patterns or []),
                "block_url_patterns":   list(_disc.block_url_patterns or []),
                "must_contain":         list(_disc.must_contain or []),
                "domestic_only_enabled": getattr(_dom, "enabled", False),
                "bfs_page_budget":       getattr(_disc, "bfs_page_budget", None),
            }
    except Exception as exc:
        eff_cfg = {"load_error": str(exc)}

    # Build context doc
    lines = [
        f"=== UNIVERSITY ===\nName: {uni_row.get('name','?')}\nScrape URL: {scrape_url}\nUni ID: {uni_id}",
    ]
    if jobs:
        j = jobs[0]
        lines.append(
            f"\n=== LAST SCRAPE JOB ===\n"
            f"Status: {j.get('status','?')}  Found: {j.get('total_found',0)}  "
            f"Imported: {j.get('imported',0)}  Errors: {j.get('errors',0)}  "
            f"Skipped: {j.get('skipped',0)}\n"
            f"Cost ceiling hit: {j.get('cost_ceiling_hit',False)}  "
            f"Error: {j.get('error_message') or 'None'}"
        )
    else:
        lines.append("\n=== LAST SCRAPE JOB ===\nNo scrape jobs found.")

    lines.append(
        f"\n=== SCRAPED COURSES ===\n"
        f"Total: {cs.get('total',0)}  Pending: {cs.get('pending',0)}  "
        f"Approved: {cs.get('approved',0)}  Rejected: {cs.get('rejected',0)}\n"
        f"Avg completeness: {cs.get('avg_completeness',0)}%  Low(<70%): {cs.get('low_count',0)}"
    )
    if rejection_agg:
        lines.append("\n=== TOP REJECTION PATTERNS ===")
        for k, v in rejection_agg.items():
            lines.append(f"  - {k}: {v} times")
    if admin_cfg:
        lines.append(f"\n=== ADMIN OVERRIDES ===\n{json.dumps(admin_cfg, indent=2)}")
    else:
        lines.append("\n=== ADMIN OVERRIDES ===\nNone active.")
    lines.append(f"\n=== EFFECTIVE CONFIG (key fields) ===\n{json.dumps(eff_cfg, indent=2)}")

    return uni_row, "\n".join(lines)


async def gather_and_diagnose(uni_id: int, db: AsyncSession) -> dict[str, Any]:
    """Gather operational metrics and call Gemini for diagnosis.

    Returns a structured dict matching the ai_root_cause_analysis schema.
    Raises RuntimeError on Gemini failure.
    """
    _api_key = os.environ.get("GEMINI_API_KEY", "")
    if not _api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    uni_row, context_doc = await _gather_context(uni_id, db)

    try:
        from google import genai as _gai
        from google.genai import types as _gt
        _gc = _gai.Client(api_key=_api_key)
        resp = _gc.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=_SYSTEM_PROMPT.format(context_doc=context_doc),
            config=_gt.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        raw = (resp.text or "").strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = raw[:-3].rstrip()
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned invalid JSON: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Gemini call failed: {exc}") from exc


# ── Config patching ────────────────────────────────────────────────────────────

def _apply_safe_fix_in_memory(db_scrape_config: dict, safe_fix: dict) -> dict:
    """Return a deep copy of db_scrape_config with safe_fix applied in-memory."""
    import copy
    cfg = copy.deepcopy(db_scrape_config)
    admin_cfg: dict = cfg.setdefault("admin_config", {})
    fix_type = safe_fix.get("type", "")
    key: str = safe_fix.get("key", "")
    parts = [p for p in key.split(".") if p]

    if not parts:
        return cfg

    if fix_type == "clear_admin_override":
        d = admin_cfg
        for part in parts[:-1]:
            if not isinstance(d, dict):
                break
            d = d.get(part, {})
        if isinstance(d, dict):
            d.pop(parts[-1], None)

    elif fix_type == "set_admin_override":
        value = safe_fix.get("value")
        d = admin_cfg
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = value

    return cfg


def build_patched_config(
    slug: str, name: str, scrape_url: str, university_id: int,
    db_scrape_config: dict,
    safe_fix: dict | None,
    fix_yaml_snippet: str | None,
):
    """Build a UniConfig with the proposed fix applied (for validation use)."""
    from app.services.scraper.config.loader import (
        load_uni_config, _deep_merge,
    )
    from app.services.scraper.config.schema import UniConfig
    import yaml

    # Apply safe_fix to DB config copy
    patched_db = (
        _apply_safe_fix_in_memory(db_scrape_config, safe_fix)
        if safe_fix else dict(db_scrape_config)
    )

    # Load config with patched DB
    config = load_uni_config(
        slug=slug, name=name, scrape_url=scrape_url,
        university_id=university_id,
        db_scrape_config=patched_db,
    )

    # Merge YAML snippet on top
    if fix_yaml_snippet:
        try:
            patch = yaml.safe_load(fix_yaml_snippet) or {}
            merged = _deep_merge(config.model_dump(), patch)
            config = UniConfig.model_validate(merged)
        except Exception as exc:
            log.warning("build_patched_config: could not apply yaml snippet: %s", exc)

    return config


# ── Applying the fix to the university (operator action) ──────────────────────

async def apply_fix_to_university(suggestion_id: int, db: AsyncSession) -> dict:
    """Apply the suggested fix to the university's config and mark the suggestion applied."""
    from datetime import datetime, timezone

    # 1. Load suggestion
    s_res = await db.execute(text("""
        SELECT id, university_id, safe_fix, fix_yaml_snippet, status
        FROM auto_repair_suggestions WHERE id = :sid
    """), {"sid": suggestion_id})
    s = dict(s_res.mappings().first() or {})
    if not s:
        raise ValueError(f"Suggestion {suggestion_id} not found")
    if s["status"] not in ("ready",):
        raise ValueError(f"Suggestion {suggestion_id} is not in 'ready' state (status={s['status']})")

    # 2. Load university
    uni_res = await db.execute(text("""
        SELECT id, scrape_url, scrape_config FROM universities WHERE id = :uid
    """), {"uid": s["university_id"]})
    uni_row = dict(uni_res.mappings().first() or {})
    if not uni_row:
        raise ValueError(f"University {s['university_id']} not found")

    db_scrape_config = dict(uni_row.get("scrape_config") or {})

    # 3. Apply safe_fix to admin_config in DB
    safe_fix = s.get("safe_fix")
    if safe_fix:
        db_scrape_config = _apply_safe_fix_in_memory(db_scrape_config, safe_fix)
        await db.execute(text("""
            UPDATE universities SET scrape_config = :cfg WHERE id = :uid
        """), {"cfg": json.dumps(db_scrape_config), "uid": s["university_id"]})

    # 4. Apply YAML snippet to per-uni YAML file
    fix_yaml = s.get("fix_yaml_snippet")
    if fix_yaml and uni_row.get("scrape_url"):
        try:
            _apply_yaml_snippet_to_file(uni_row["scrape_url"], fix_yaml)
        except Exception as exc:
            log.warning("apply_fix: could not write YAML file: %s", exc)

    # 5. Mark suggestion as applied
    await db.execute(text("""
        UPDATE auto_repair_suggestions
        SET status = 'applied', applied_at = NOW()
        WHERE id = :sid
    """), {"sid": suggestion_id})

    await db.commit()
    log.info("auto_repair: applied suggestion %d for uni %d", suggestion_id, s["university_id"])
    return {"id": suggestion_id, "status": "applied", "university_id": s["university_id"]}


def _apply_yaml_snippet_to_file(scrape_url: str, snippet_yaml: str) -> None:
    """Merge a YAML snippet into the per-university YAML config file."""
    import yaml
    from urllib.parse import urlparse
    from app.services.scraper.config.loader import (
        _hostname_to_slug, _UNIS_DIR, _load_yaml_file, _deep_merge,
    )

    hostname = urlparse(scrape_url).hostname or ""
    if not hostname:
        return

    slug = _hostname_to_slug(hostname)
    yaml_path = _UNIS_DIR / f"{slug}.yaml"

    existing = _load_yaml_file(yaml_path)
    patch = yaml.safe_load(snippet_yaml) or {}
    merged = _deep_merge(existing, patch)

    with open(yaml_path, "w", encoding="utf-8") as fh:
        yaml.dump(merged, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)

    log.info("auto_repair: wrote YAML snippet to %s", yaml_path)


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def run_auto_repair_pipeline(
    university_id: int,
    regression_alert_id: int | None,
    db: AsyncSession,
) -> int:
    """Run the full auto-repair pipeline for one university.

    Returns the id of the created auto_repair_suggestions row.
    Idempotent: skips if a non-dismissed suggestion already exists in last 24 h.
    """
    from app.services.repair_validator import validate_proposed_fix
    from urllib.parse import urlparse
    from app.services.scraper.config.loader import _hostname_to_slug, load_uni_config

    # ── Rate-limit guard ────────────────────────────────────────────────────
    existing_res = await db.execute(text("""
        SELECT id FROM auto_repair_suggestions
        WHERE university_id = :uid
          AND status NOT IN ('dismissed', 'failed')
          AND created_at > NOW() - INTERVAL '24 hours'
        LIMIT 1
    """), {"uid": university_id})
    if existing_res.scalar():
        log.info("auto_repair: skipping uni %d — recent suggestion already exists", university_id)
        return 0

    # ── Create 'pending' placeholder ────────────────────────────────────────
    ins_res = await db.execute(text("""
        INSERT INTO auto_repair_suggestions
            (university_id, regression_alert_id, status)
        VALUES (:uid, :raid, 'pending')
        RETURNING id
    """), {"uid": university_id, "raid": regression_alert_id})
    await db.commit()
    suggestion_id: int = ins_res.scalar()

    try:
        # ── Step 1: AI diagnosis ─────────────────────────────────────────────
        diagnosis = await gather_and_diagnose(university_id, db)

        risk_label = diagnosis.get("risk_label") or "low"
        safe_fix = diagnosis.get("safe_fix")
        fix_yaml = diagnosis.get("fix_yaml_snippet")
        root_cause_category = diagnosis.get("root_cause_category", "unknown")

        # ── Step 2: Developer-required path ─────────────────────────────────
        is_dev_required = (
            risk_label == "developer_required"
            or diagnosis.get("developer_required", False)
            or (safe_fix is None and not fix_yaml and root_cause_category != "healthy")
        )

        if is_dev_required:
            await db.execute(text("""
                UPDATE auto_repair_suggestions SET
                    status = 'developer_required',
                    issue_summary = :summary,
                    root_cause_category = :category,
                    fix_recommendation = :rec,
                    risk_label = 'developer_required',
                    developer_note = :dnote,
                    evidence = :evidence::jsonb,
                    confidence = :conf
                WHERE id = :sid
            """), {
                "sid":      suggestion_id,
                "summary":  diagnosis.get("issue_summary"),
                "category": root_cause_category,
                "rec":      diagnosis.get("fix_recommendation"),
                "dnote":    diagnosis.get("developer_note"),
                "evidence": json.dumps(diagnosis.get("evidence") or []),
                "conf":     diagnosis.get("confidence"),
            })
            await db.commit()
            log.info("auto_repair: uni %d → developer_required (suggestion %d)", university_id, suggestion_id)
            return suggestion_id

        # ── Step 3: Build patched config ─────────────────────────────────────
        uni_res = await db.execute(text("""
            SELECT name, scrape_url, scrape_config FROM universities WHERE id = :uid
        """), {"uid": university_id})
        uni_row = dict(uni_res.mappings().first() or {})

        scrape_url: str = uni_row.get("scrape_url") or ""
        db_scrape_config: dict = dict(uni_row.get("scrape_config") or {})
        hostname = urlparse(scrape_url).hostname or ""
        slug = _hostname_to_slug(hostname) if hostname else "unknown"

        current_cfg = load_uni_config(
            slug=slug, name=uni_row.get("name", ""), scrape_url=scrape_url,
            university_id=university_id, db_scrape_config=db_scrape_config,
        )
        patched_cfg = build_patched_config(
            slug=slug, name=uni_row.get("name", ""), scrape_url=scrape_url,
            university_id=university_id, db_scrape_config=db_scrape_config,
            safe_fix=safe_fix, fix_yaml_snippet=fix_yaml,
        )

        # ── Step 4: Validate ─────────────────────────────────────────────────
        validation_result = await validate_proposed_fix(
            university_id, current_cfg, patched_cfg, db
        )
        confidence = validation_result.get("confidence", diagnosis.get("confidence", "medium"))

        # ── Step 5: Store ready suggestion ───────────────────────────────────
        await db.execute(text("""
            UPDATE auto_repair_suggestions SET
                status = 'ready',
                issue_summary = :summary,
                root_cause_category = :category,
                fix_recommendation = :rec,
                fix_yaml_snippet = :fyaml,
                safe_fix = :sfx::jsonb,
                risk_label = :rlabel,
                evidence = :evidence::jsonb,
                validation_result = :vresult::jsonb,
                confidence = :conf
            WHERE id = :sid
        """), {
            "sid":      suggestion_id,
            "summary":  diagnosis.get("issue_summary"),
            "category": root_cause_category,
            "rec":      diagnosis.get("fix_recommendation"),
            "fyaml":    fix_yaml,
            "sfx":      json.dumps(safe_fix) if safe_fix else "null",
            "rlabel":   risk_label,
            "evidence": json.dumps(diagnosis.get("evidence") or []),
            "vresult":  json.dumps(validation_result),
            "conf":     confidence,
        })
        await db.commit()
        log.info("auto_repair: uni %d → ready (suggestion %d, confidence=%s)", university_id, suggestion_id, confidence)
        return suggestion_id

    except Exception as exc:
        log.error("auto_repair: pipeline failed for uni %d suggestion %d: %s", university_id, suggestion_id, exc)
        await db.execute(text("""
            UPDATE auto_repair_suggestions
            SET status = 'failed', issue_summary = :msg
            WHERE id = :sid
        """), {"sid": suggestion_id, "msg": f"Pipeline error: {exc}"})
        await db.commit()
        return suggestion_id

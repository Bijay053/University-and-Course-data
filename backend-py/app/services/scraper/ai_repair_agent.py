"""AI-powered scrape repair agent.

Uses Gemini to iteratively diagnose failing scrape jobs and apply
config patches until discovery improves or MAX_ATTEMPTS is reached.

Loop (per attempt):
  1. Gather context  — job stats, dropped URLs, current YAML, staging quality
  2. Call Gemini     — returns structured JSON diagnosis + patches
  3. Apply patches   — writes to admin_config (DB) + YAML file on disk
  4. Simulate filter — tests the new allow/block patterns against dropped URLs
  5. Record attempt  — stored in Redis (key ``ai_repair:{job_id}``)
  6. Terminate if improvement >= 50 % of dropped URLs rescued, or MAX_ATTEMPTS hit

Session state stored in Redis as JSON under key ``ai_repair:{job_id}`` with
24-hour TTL so the frontend can poll for live progress.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
_REDIS_KEY_PREFIX = "ai_repair:"
_REDIS_TTL_SEC = 86_400  # 24 h


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_key(job_id: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{job_id}"


def _redis_client():
    from app.config import settings
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def read_session(job_id: str) -> dict:
    """Return the current session state for a job (empty dict if missing)."""
    try:
        raw = _redis_client().get(_redis_key(job_id))
        return json.loads(raw) if raw else {}
    except Exception as exc:
        log.warning("ai_repair: Redis read error: %s", exc)
        return {}


def _write_session(job_id: str, session: dict) -> None:
    try:
        _redis_client().set(_redis_key(job_id), json.dumps(session), ex=_REDIS_TTL_SEC)
    except Exception as exc:
        log.warning("ai_repair: Redis write error: %s", exc)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ── Context gathering ─────────────────────────────────────────────────────────

async def _gather_context(job_id: str, db) -> dict:
    """Collect job stats, config, and staging quality for the AI prompt."""
    from sqlalchemy import text

    row = (await db.execute(
        text("""
            SELECT srj.university_id,
                   srj.total_found,
                   srj.imported,
                   srj.total_errors,
                   srj.discovered_config,
                   u.name            AS uni_name,
                   u.scrape_url      AS scrape_url,
                   u.scrape_config   AS scrape_config_raw
            FROM   scrape_runtime_jobs srj
            JOIN   universities u ON u.id = srj.university_id
            WHERE  srj.runtime_job_id = :jid
        """),
        {"jid": job_id},
    )).mappings().first()

    if not row:
        return {}

    uni_id: int = row["university_id"]
    disc_cfg: dict = row["discovered_config"] or {}
    pipeline: dict = disc_cfg.get("pipeline_stats", {})

    raw_discovered: int = pipeline.get("raw_discovered", row["total_found"] or 0)
    after_filter: int   = pipeline.get("after_filter", row["imported"] or 0)
    dropped_sample: list[str] = pipeline.get("dropped_sample", [])[:12]
    passed_sample:  list[str] = pipeline.get("passed_sample", [])[:5]

    sc: dict = row["scrape_config_raw"] or {}
    admin_config: dict = sc.get("admin_config") or {}

    # YAML file on disk
    unis_dir = Path(__file__).parent.parent.parent.parent / "scraper_config" / "unis"
    yaml_files = list(unis_dir.glob(f"*_{uni_id}.yaml"))
    yaml_content = ""
    if yaml_files:
        try:
            yaml_content = yaml_files[0].read_text(encoding="utf-8")
        except Exception:
            pass

    # Extraction quality from staged courses for this job
    q = (await db.execute(
        text("""
            SELECT COUNT(*)                      AS total,
                   COUNT(international_fee)      AS has_fee,
                   COUNT(ielts_overall)          AS has_ielts,
                   COUNT(intake_months)          AS has_intakes,
                   COUNT(course_location)        AS has_location,
                   COUNT(degree_level)           AS has_degree_level,
                   COUNT(study_mode)             AS has_mode
            FROM   scraped_courses
            WHERE  university_id = :uid
              AND  scrape_job_id = :jid
              AND  status IN ('pending', 'review', 'approved')
        """),
        {"uid": uni_id, "jid": job_id},
    )).mappings().first()

    quality: dict = {}
    total = (q["total"] or 0) if q else 0
    if total > 0:
        quality = {
            "total_staged":    total,
            "fee_pct":         round(100 * (q["has_fee"]         or 0) / total),
            "ielts_pct":       round(100 * (q["has_ielts"]       or 0) / total),
            "intakes_pct":     round(100 * (q["has_intakes"]     or 0) / total),
            "location_pct":    round(100 * (q["has_location"]    or 0) / total),
            "degree_level_pct":round(100 * (q["has_degree_level"]or 0) / total),
            "mode_pct":        round(100 * (q["has_mode"]        or 0) / total),
        }

    drop_rate = round(100 * (1 - after_filter / raw_discovered)) if raw_discovered > 0 else 0

    return {
        "job_id":          job_id,
        "university_id":   uni_id,
        "uni_name":        row["uni_name"] or "Unknown",
        "scrape_url":      row["scrape_url"] or "",
        "raw_discovered":  raw_discovered,
        "after_filter":    after_filter,
        "imported":        row["imported"] or 0,
        "total_errors":    row["total_errors"] or 0,
        "drop_rate":       drop_rate,
        "dropped_sample":  dropped_sample,
        "passed_sample":   passed_sample,
        "admin_config":    admin_config,
        "yaml_content":    yaml_content[:2500],
        "quality":         quality,
        "unis_dir":        unis_dir,
        "yaml_file":       yaml_files[0] if yaml_files else None,
    }


# ── AI prompt ─────────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict, previous_attempts: list[dict]) -> str:
    prev_block = ""
    if previous_attempts:
        lines = []
        for a in previous_attempts:
            lines.append(
                f"  Attempt {a['attempt_number']}: {a['diagnosis']}"
                f" | Before: {a['before_pass_count']} After: {a['after_pass_count']}"
                f" | Patch: {json.dumps(a.get('patches_applied', []))}"
            )
        prev_block = "PREVIOUS REPAIR ATTEMPTS (do NOT repeat these):\n" + "\n".join(lines)

    q = ctx.get("quality") or {}
    quality_str = (
        f"  Staged: {q.get('total_staged', 0)}"
        f" | Fee: {q.get('fee_pct', 0)}%"
        f" | IELTS: {q.get('ielts_pct', 0)}%"
        f" | Intakes: {q.get('intakes_pct', 0)}%"
        f" | Location: {q.get('location_pct', 0)}%"
        f" | DegreeLevel: {q.get('degree_level_pct', 0)}%"
    ) if q else "  No courses staged yet"

    return f"""You are an expert web scraping engineer specialising in university course scrapers.
Analyse this failed/poor scrape job and return the single most impactful fix.

UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

DISCOVERY STATS:
  Raw URLs found by crawler: {ctx['raw_discovered']}
  URLs that passed filters:  {ctx['after_filter']}
  Courses staged:            {ctx['imported']}
  URL drop rate:             {ctx['drop_rate']}%

SAMPLE DROPPED URLs (blocked by the current filter — these SHOULD be course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

SAMPLE PASSED URLs (currently allowed through):
{json.dumps(ctx['passed_sample'], indent=2)}

CURRENT ADMIN_CONFIG OVERRIDE (stored in DB, highest priority):
{json.dumps(ctx['admin_config'], indent=2)}

CURRENT YAML CONFIG (file on disk):
{ctx['yaml_content'] or "(empty)"}

EXTRACTION QUALITY:
{quality_str}

{prev_block}

DIAGNOSIS PRIORITY (address in this order):
1. drop_rate > 50 % with dropped samples available → fix allow_url_patterns or block_url_patterns
2. raw_discovered == 0 → increase bfs_page_budget (up to 200) or enable use_browser: true
3. staged > 0 but fee_pct < 40 % → suggest a note in explanation (cannot auto-fix extraction)
4. staged > 0 but ielts_pct < 30 % → suggest a note in explanation

RULES FOR allow_url_patterns:
- Must be a list of regex strings anchored to the URL PATH (not full URL)
- Derive them from the actual dropped URL paths above, not guesses
- Keep patterns broad enough to catch all course URL variants
- Example: dropped URL "/courses/undergraduate/computing-bsc" → pattern "/courses/[^/]+/[^/]+"
- Do NOT use ^ anchors; paths are matched with re.search(), not re.match()

RULES FOR block_url_patterns:
- List of regex strings for paths to EXCLUDE
- Only suggest if a specific non-course path is leaking through

RULES FOR bfs_page_budget:
- Integer 20-200; only suggest if raw_discovered is very low (< 5)

RULES FOR use_browser:
- Boolean; only suggest if site is known SPA / heavy JavaScript

Return ONLY a single valid JSON object. No markdown, no explanation outside JSON.

{{
  "diagnosis": "one sentence describing the root problem",
  "root_cause": "allow_url_patterns|block_url_patterns|bfs_page_budget|use_browser|extraction|unknown",
  "confidence": 85,
  "explanation": "2-3 sentences explaining why this fix will work",
  "patches": [
    {{
      "section": "discovery",
      "field": "allow_url_patterns",
      "action": "replace",
      "value": ["/courses/[^/]+/[^/]+"]
    }}
  ]
}}"""


# ── Response parsing ──────────────────────────────────────────────────────────

def _parse_ai_response(text: str) -> dict | None:
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", text.strip(), "")
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("patches"):
            return data
    except json.JSONDecodeError:
        pass

    # Try to extract JSON object from mixed content
    m = re.search(r'\{[^{}]*"patches"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and data.get("patches"):
                return data
        except json.JSONDecodeError:
            pass

    return None


def _build_config_patch(patches: list[dict]) -> dict:
    """Convert AI patch list → admin_config patch dict."""
    config_patch: dict = {}
    for p in patches:
        section = p.get("section", "discovery")
        field   = p.get("field")
        value   = p.get("value")
        if not field or value is None:
            continue
        config_patch.setdefault(section, {})[field] = value
    return config_patch


# ── Simulation ────────────────────────────────────────────────────────────────

def _simulate_filter(dropped_urls: list[str], allow_pats: list[str], block_pats: list[str]) -> dict:
    """Test how many dropped course URLs would now pass the new patterns."""
    import re as _re

    # Exclude media / asset URLs (same filter as simulate-fix endpoint)
    _MEDIA_EXT = _re.compile(
        r"\.(jpe?g|png|gif|webp|svg|ico|bmp|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?)$",
        _re.IGNORECASE,
    )
    _ASSET_PATH = _re.compile(
        r"/(images?|assets?|globalassets|static|media|uploads?|fonts?|icons?|scripts?)/",
        _re.IGNORECASE,
    )
    course_urls = [
        u for u in dropped_urls
        if not _MEDIA_EXT.search(u) and not _ASSET_PATH.search(u)
    ]
    if not course_urls:
        return {"before": 0, "after": 0, "total": 0, "rescued": []}

    def _compile(pats: list[str]) -> list:
        out = []
        for p in pats:
            try:
                out.append(_re.compile(p, _re.IGNORECASE))
            except _re.error:
                pass
        return out

    allow_c = _compile(allow_pats)
    block_c = _compile(block_pats)

    passing = []
    for u in course_urls:
        ok = True
        if allow_c and not any(c.search(u) for c in allow_c):
            ok = False
        if ok and block_c and any(c.search(u) for c in block_c):
            ok = False
        if ok:
            passing.append(u)

    return {"before": 0, "after": len(passing), "total": len(course_urls), "rescued": passing[:6]}


# ── Patch application ─────────────────────────────────────────────────────────

async def _apply_to_db(uni_id: int, config_patch: dict, db) -> None:
    from sqlalchemy import text
    import json as _json

    row = (await db.execute(
        text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()

    sc: dict = dict((row.get("scrape_config") or {}) if row else {})
    existing = sc.get("admin_config") or {}
    sc["_prev_admin_config"] = existing
    sc["admin_config"] = _deep_merge(existing, config_patch)

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": _json.dumps(sc), "id": uni_id},
    )
    await db.commit()


def _apply_to_yaml(yaml_file, unis_dir: Path, uni_id: int, scrape_url: str, config_patch: dict) -> None:
    import yaml as _yaml
    import re as _re2

    if not yaml_file:
        candidates = list(unis_dir.glob(f"*_{uni_id}.yaml"))
        if not candidates:
            bare = _re2.sub(r"^www\.", "", _re2.sub(r"^https?://", "", scrape_url).split("/")[0].lower())
            if bare:
                for f in unis_dir.glob("*.yaml"):
                    if bare in f.read_text(encoding="utf-8")[:600]:
                        candidates = [f]
                        break
        if not candidates:
            log.warning("ai_repair: no YAML file found for uni_id=%s", uni_id)
            return
        yaml_file = candidates[0]

    try:
        existing_text = yaml_file.read_text(encoding="utf-8")
        comment_lines = []
        for ln in existing_text.splitlines():
            if ln.strip().startswith("#"):
                comment_lines.append(ln)
            else:
                break
        header = ("\n".join(comment_lines) + "\n") if comment_lines else ""
        merged = _deep_merge(_yaml.safe_load(existing_text) or {}, config_patch)
        new_text = header + _yaml.dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False)
        yaml_file.write_text(new_text, encoding="utf-8")
        log.info("ai_repair: wrote config to %s", yaml_file.name)
    except Exception as exc:
        log.warning("ai_repair: YAML write failed: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_ai_repair_loop(job_id: str, db) -> dict:
    """Run the AI repair loop. Writes progress to Redis after every attempt."""
    from app.services.ai import gemini_client

    session: dict = {
        "session_id":     str(uuid.uuid4())[:8],
        "job_id":         job_id,
        "status":         "running",
        "current_attempt": 0,
        "attempts":       [],
        "final_verdict":  None,
        "uni_name":       None,
        "started_at":     datetime.now(timezone.utc).isoformat(),
        "completed_at":   None,
        "error":          None,
    }
    _write_session(job_id, session)

    try:
        ctx = await _gather_context(job_id, db)
        if not ctx:
            session.update(status="failed", error="Job not found")
            return session

        session["uni_name"] = ctx["uni_name"]
        _write_session(job_id, session)

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            session["current_attempt"] = attempt_num
            _write_session(job_id, session)
            log.info("ai_repair: job=%s attempt=%d", job_id, attempt_num)

            prompt = _build_prompt(ctx, session["attempts"])

            resp = await gemini_client.generate(
                prompt,
                max_output_tokens=2048,
                call_type="ai_repair",
            )

            if resp.skipped or not resp.text:
                session.update(
                    status="completed",
                    final_verdict="AI service unavailable — manual config review required.",
                )
                break

            ai_data = _parse_ai_response(resp.text)
            if not ai_data:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"AI returned an unparseable response after {attempt_num} attempt(s). "
                        "Raw preview: " + resp.text[:300]
                    ),
                )
                break

            patches       = ai_data.get("patches", [])
            config_patch  = _build_config_patch(patches)
            disc_patch    = config_patch.get("discovery", {})

            # Simulate URL filter change (if relevant)
            sim: dict = {"before": 0, "after": 0, "total": 0, "rescued": []}
            if "allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch:
                from sqlalchemy import text as _text
                dc_row = (await db.execute(
                    _text("SELECT discovered_config FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
                    {"j": job_id},
                )).first()
                dc: dict = (dc_row[0] or {}) if dc_row else {}
                dropped = dc.get("pipeline_stats", {}).get("dropped_sample") or ctx["dropped_sample"]
                sim = _simulate_filter(
                    dropped,
                    disc_patch.get("allow_url_patterns", []),
                    disc_patch.get("block_url_patterns", []),
                )

            attempt_record: dict = {
                "attempt_number":   attempt_num,
                "diagnosis":        ai_data.get("diagnosis", "Unknown issue"),
                "root_cause":       ai_data.get("root_cause", "unknown"),
                "confidence":       ai_data.get("confidence", 0),
                "explanation":      ai_data.get("explanation", ""),
                "patches_applied":  [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches
                ],
                "before_pass_count": sim["before"],
                "after_pass_count":  sim["after"],
                "total_test_urls":   sim["total"],
                "rescued_sample":    sim["rescued"],
                "ai_cost_usd":       round(resp.cost_usd, 6),
                "patch_applied_ok":  False,
            }
            session["attempts"].append(attempt_record)

            # Apply patch
            if config_patch:
                try:
                    await _apply_to_db(ctx["university_id"], config_patch, db)
                    _apply_to_yaml(
                        ctx.get("yaml_file"),
                        ctx["unis_dir"],
                        ctx["university_id"],
                        ctx["scrape_url"],
                        config_patch,
                    )
                    attempt_record["patch_applied_ok"] = True
                except Exception as exc:
                    log.warning("ai_repair: patch apply error: %s", exc)
                    attempt_record["patch_error"] = str(exc)

            _write_session(job_id, session)

            # Termination check
            is_url_fix     = "allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch
            good_enough    = is_url_fix and sim["total"] > 0 and sim["after"] >= sim["total"] * 0.5
            no_url_problem = not is_url_fix and ctx["drop_rate"] < 20

            if good_enough:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Fix applied — {sim['after']}/{sim['total']} previously-dropped URLs now "
                        f"pass the new filter. Re-run discovery to confirm the improvement."
                    ),
                )
                break
            if no_url_problem:
                session.update(
                    status="completed",
                    final_verdict="Config patch applied. Re-run discovery to verify the changes.",
                )
                break
            if attempt_num == MAX_ATTEMPTS:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Reached {MAX_ATTEMPTS} attempts. Best result: "
                        f"{sim['after']}/{sim['total']} URLs rescued. Manual review may be needed."
                    ),
                )

    except Exception as exc:
        log.exception("ai_repair: unexpected error job=%s: %s", job_id, exc)
        session.update(status="failed", error=str(exc))

    finally:
        session["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_session(job_id, session)

    return session

"""AI-powered scrape repair agent — OpenAI edition.

Uses the Replit AI Integrations OpenAI proxy (gpt-5.4) to iteratively
diagnose failing scrape jobs and apply config patches until discovery
improves or MAX_ATTEMPTS is reached.

Loop (per attempt):
  1. Gather context  — job stats, dropped URLs, current YAML + admin_config,
                       staging quality (fee %, IELTS %, etc.)
  2. Call OpenAI     — structured JSON diagnosis + patches via json_object mode
  3. Validate patch  — strict whitelist of allowed sections/fields/types/regexes
  4. Apply patches   — writes to admin_config (DB) + YAML file on disk
  5. Simulate filter — tests new allow/block patterns against dropped URLs
  6. Record attempt  — stored in Redis (key ``ai_repair:{job_id}``, TTL 24 h)
  7. Terminate if improvement >= 50 % of dropped URLs rescued, or MAX_ATTEMPTS hit

Session state is stored in Redis as JSON under key ``ai_repair:{job_id}``
so the frontend can poll for live progress.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    """Return the current session state (empty dict if missing)."""
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


# ── Strict patch validation ───────────────────────────────────────────────────

_ALLOWED_DISCOVERY_FIELDS: dict[str, type | tuple] = {
    "allow_url_patterns": list,
    "block_url_patterns": list,
    "must_contain":       list,
    "bfs_page_budget":    int,
    "use_browser":        bool,
    "sitemap_url":        str,
}
_ALLOWED_SECTIONS = {"discovery"}   # extraction patches are read-only hints only


class PatchValidationError(ValueError):
    """Raised when an AI patch fails strict validation."""


def _validate_patch(patch: dict) -> dict:
    """Validate and sanitise one AI patch dict.

    Returns the sanitised patch on success.
    Raises PatchValidationError with a human-readable reason on failure.
    """
    section = patch.get("section")
    field   = patch.get("field")
    value   = patch.get("value")

    if section not in _ALLOWED_SECTIONS:
        raise PatchValidationError(
            f"Section '{section}' is not in the allowed set {_ALLOWED_SECTIONS}. "
            "Only 'discovery' patches may be applied automatically."
        )

    if field not in _ALLOWED_DISCOVERY_FIELDS:
        raise PatchValidationError(
            f"Field '{field}' is not an allowed discovery field. "
            f"Allowed: {sorted(_ALLOWED_DISCOVERY_FIELDS)}"
        )

    expected_type = _ALLOWED_DISCOVERY_FIELDS[field]
    if not isinstance(value, expected_type):
        raise PatchValidationError(
            f"Field '{field}' must be {expected_type.__name__}, got {type(value).__name__}."
        )

    # Per-field semantic checks
    if field in ("allow_url_patterns", "block_url_patterns", "must_contain"):
        if not value:
            raise PatchValidationError(f"'{field}' must not be an empty list.")
        for i, pat in enumerate(value):
            if not isinstance(pat, str):
                raise PatchValidationError(f"'{field}[{i}]' must be a string.")
            if len(pat) > 500:
                raise PatchValidationError(f"'{field}[{i}]' pattern is suspiciously long (>500 chars).")
            try:
                re.compile(pat)
            except re.error as exc:
                raise PatchValidationError(
                    f"'{field}[{i}]' is not a valid regex: {exc}  (pattern: {pat!r})"
                ) from exc

    if field == "bfs_page_budget":
        if not (5 <= value <= 300):
            raise PatchValidationError(
                f"'bfs_page_budget' must be between 5 and 300, got {value}."
            )

    if field == "sitemap_url":
        if not value.startswith(("http://", "https://")):
            raise PatchValidationError(
                f"'sitemap_url' must start with http:// or https://, got {value!r}."
            )

    return patch


def _validate_and_build_config_patch(patches: list[dict]) -> tuple[dict, list[str]]:
    """Validate all patches and build a config_patch dict.

    Returns (config_patch, errors) — errors is empty on full success.
    Patches that fail validation are skipped (not applied) and their
    error message is recorded.
    """
    config_patch: dict = {}
    errors: list[str] = []

    for p in patches:
        try:
            validated = _validate_patch(p)
            section = validated["section"]
            field   = validated["field"]
            value   = validated["value"]
            config_patch.setdefault(section, {})[field] = value
        except PatchValidationError as exc:
            errors.append(str(exc))
            log.warning("ai_repair: patch rejected by validator: %s", exc)

    return config_patch, errors


# ── Context gathering ─────────────────────────────────────────────────────────

async def _gather_context(job_id: str, db) -> dict:
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
    after_filter:   int = pipeline.get("after_filter",   row["imported"]   or 0)
    dropped_sample: list[str] = pipeline.get("dropped_sample", [])[:15]
    passed_sample:  list[str] = pipeline.get("passed_sample",  [])[:5]

    sc: dict           = row["scrape_config_raw"] or {}
    admin_config: dict = sc.get("admin_config") or {}

    unis_dir = Path(__file__).parent.parent.parent.parent / "scraper_config" / "unis"
    yaml_files = list(unis_dir.glob(f"*_{uni_id}.yaml"))
    yaml_content = ""
    if yaml_files:
        try:
            yaml_content = yaml_files[0].read_text(encoding="utf-8")
        except Exception:
            pass

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
            "total_staged":     total,
            "fee_pct":          round(100 * (q["has_fee"]          or 0) / total),
            "ielts_pct":        round(100 * (q["has_ielts"]        or 0) / total),
            "intakes_pct":      round(100 * (q["has_intakes"]      or 0) / total),
            "location_pct":     round(100 * (q["has_location"]     or 0) / total),
            "degree_level_pct": round(100 * (q["has_degree_level"] or 0) / total),
            "mode_pct":         round(100 * (q["has_mode"]         or 0) / total),
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
        "yaml_content":    yaml_content[:3000],
        "quality":         quality,
        "unis_dir":        unis_dir,
        "yaml_file":       yaml_files[0] if yaml_files else None,
    }


# ── OpenAI prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert web scraping engineer specialising in university course scrapers.
Your job is to analyse a failing scrape job and return the single most impactful fix.

You MUST return a valid JSON object — no markdown, no text outside the JSON.

The JSON must follow this exact schema:
{
  "diagnosis": "string — one sentence describing the root problem",
  "root_cause": "string — one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | extraction | unknown",
  "confidence": number — integer 0-100,
  "explanation": "string — 2-3 sentences explaining why this fix will work",
  "patches": [
    {
      "section": "string — must be 'discovery'",
      "field": "string — one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | must_contain | sitemap_url",
      "action": "string — 'replace'",
      "value": <list of strings for pattern fields | integer for bfs_page_budget | boolean for use_browser | string for sitemap_url>
    }
  ]
}

Rules you MUST follow:
- patches[].section must always be "discovery" — extraction patches are not supported
- allow_url_patterns and block_url_patterns values must be valid Python regex strings
- Patterns are matched with re.search() against the URL path, NOT the full URL
- Do NOT use ^ anchors — paths are matched mid-string
- Each pattern must not exceed 200 characters
- bfs_page_budget must be an integer between 5 and 300
- use_browser must be true or false (boolean)
- If you cannot identify a concrete fix, return an empty patches list with a clear diagnosis
- Do NOT suggest the same fix as any previous repair attempt listed in the context\
"""


def _build_user_message(ctx: dict, previous_attempts: list[dict]) -> str:
    prev_block = ""
    if previous_attempts:
        lines = [f"  #{a['attempt_number']}: {a['diagnosis']} | patches={json.dumps([p.get('field') for p in a.get('patches_applied', [])])}" for a in previous_attempts]
        prev_block = "\nPREVIOUS REPAIR ATTEMPTS (do NOT repeat these):\n" + "\n".join(lines)

    q = ctx.get("quality") or {}
    quality_str = (
        f"staged={q.get('total_staged', 0)}"
        f" fee={q.get('fee_pct', 0)}%"
        f" ielts={q.get('ielts_pct', 0)}%"
        f" intakes={q.get('intakes_pct', 0)}%"
        f" location={q.get('location_pct', 0)}%"
        f" degree_level={q.get('degree_level_pct', 0)}%"
    ) if q else "no courses staged yet"

    return f"""UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

DISCOVERY STATS:
  raw_urls_found={ctx['raw_discovered']}  after_filter={ctx['after_filter']}  staged={ctx['imported']}  drop_rate={ctx['drop_rate']}%

DROPPED URLs (these are being incorrectly blocked — they look like real course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

PASSED URLs (these currently make it through the filter):
{json.dumps(ctx['passed_sample'], indent=2)}

ADMIN_CONFIG (DB override, highest priority):
{json.dumps(ctx['admin_config'], indent=2)}

YAML CONFIG (file on disk):
{ctx['yaml_content'] or "(empty — no YAML file found)"}

EXTRACTION QUALITY:
{quality_str}
{prev_block}

DIAGNOSIS PRIORITY (work through in order):
1. drop_rate > 50% AND dropped sample is non-empty → fix allow_url_patterns or block_url_patterns
2. raw_discovered == 0 OR raw_discovered < 5 → increase bfs_page_budget (up to 200) or enable use_browser
3. staged > 0 but fee_pct < 40% → note in explanation only (no patch — extraction cannot be auto-fixed)
4. Otherwise → return empty patches with a clear diagnosis explaining why no auto-fix is possible

HOW TO DERIVE allow_url_patterns:
- Look at the dropped URL paths above
- Find the common path prefix or pattern
- Write a regex that matches those paths with re.search()
- Example: dropped paths like "/courses/undergraduate/computing-bsc-hons" → pattern "/courses/[^/]+/[^/]+"
- Make the pattern broad enough to catch all similar URLs but not so broad it catches non-course pages

Return ONLY the JSON object described above. No markdown, no explanation outside the JSON."""


# ── URL filter simulation ─────────────────────────────────────────────────────

_MEDIA_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?)$",
    re.IGNORECASE,
)
_ASSET_PATH = re.compile(
    r"/(images?|assets?|globalassets|static|media|uploads?|fonts?|icons?|scripts?)/",
    re.IGNORECASE,
)


def _is_course_url(u: str) -> bool:
    return not _MEDIA_EXT.search(u) and not _ASSET_PATH.search(u)


def _simulate_filter(dropped_urls: list[str], allow_pats: list[str], block_pats: list[str]) -> dict:
    course_urls = [u for u in dropped_urls if _is_course_url(u)]
    if not course_urls:
        return {"before": 0, "after": 0, "total": 0, "rescued": []}

    def _compile(pats: list[str]) -> list:
        out = []
        for p in pats:
            try:
                out.append(re.compile(p, re.IGNORECASE))
            except re.error:
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


def _apply_to_yaml(yaml_file: Any, unis_dir: Path, uni_id: int, scrape_url: str, config_patch: dict) -> None:
    import yaml as _yaml

    if not yaml_file:
        candidates = list(unis_dir.glob(f"*_{uni_id}.yaml"))
        if not candidates:
            bare = re.sub(r"^www\.", "", re.sub(r"^https?://", "", scrape_url).split("/")[0].lower())
            if bare:
                for f in unis_dir.glob("*.yaml"):
                    try:
                        if bare in f.read_text(encoding="utf-8")[:600]:
                            candidates = [f]
                            break
                    except Exception:
                        continue
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
        log.info("ai_repair: wrote config patch to %s", yaml_file.name)
    except Exception as exc:
        log.warning("ai_repair: YAML write failed: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_ai_repair_loop(job_id: str, db) -> dict:
    """Run the OpenAI-powered repair loop. Writes progress to Redis after every attempt."""
    from app.services.ai.openai_client import chat_json

    session: dict = {
        "session_id":      str(uuid.uuid4())[:8],
        "job_id":          job_id,
        "status":          "running",
        "current_attempt": 0,
        "attempts":        [],
        "final_verdict":   None,
        "uni_name":        None,
        "started_at":      datetime.now(timezone.utc).isoformat(),
        "completed_at":    None,
        "error":           None,
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
            log.info("ai_repair: job=%s attempt=%d/%d", job_id, attempt_num, MAX_ATTEMPTS)

            user_msg = _build_user_message(ctx, session["attempts"])

            ai_data = await chat_json(
                system=_SYSTEM_PROMPT,
                user=user_msg,
                max_tokens=2048,
            )

            if ai_data is None:
                session.update(
                    status="completed",
                    final_verdict=(
                        "OpenAI service unavailable or returned an invalid response. "
                        "Check that AI_INTEGRATIONS_OPENAI_BASE_URL and "
                        "AI_INTEGRATIONS_OPENAI_API_KEY are set correctly."
                    ),
                )
                break

            # Extract fields with safe defaults
            patches_raw: list[dict] = ai_data.get("patches") or []

            # Strict validation — reject bad patches, record errors
            config_patch, validation_errors = _validate_and_build_config_patch(patches_raw)
            disc_patch = config_patch.get("discovery", {})

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
                "attempt_number":     attempt_num,
                "diagnosis":          ai_data.get("diagnosis", "Unknown issue"),
                "root_cause":         ai_data.get("root_cause", "unknown"),
                "confidence":         ai_data.get("confidence", 0),
                "explanation":        ai_data.get("explanation", ""),
                "patches_applied":    [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches_raw
                    if p.get("section") in _ALLOWED_SECTIONS
                    and p.get("field") in _ALLOWED_DISCOVERY_FIELDS
                ],
                "validation_errors":  validation_errors,
                "before_pass_count":  sim["before"],
                "after_pass_count":   sim["after"],
                "total_test_urls":    sim["total"],
                "rescued_sample":     sim["rescued"],
                "patch_applied_ok":   False,
            }
            session["attempts"].append(attempt_record)

            # Apply patch (only if there are valid patches)
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
            elif validation_errors:
                attempt_record["patch_applied_ok"] = False

            _write_session(job_id, session)

            # Termination checks
            is_url_fix  = "allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch
            good_enough = is_url_fix and sim["total"] > 0 and sim["after"] >= sim["total"] * 0.5
            no_url_prob = not is_url_fix and ctx["drop_rate"] < 20
            no_patches  = not patches_raw

            if good_enough:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Fix applied — {sim['after']}/{sim['total']} previously-dropped URLs "
                        f"now pass the new filter. Re-run discovery to confirm the improvement."
                    ),
                )
                break

            if no_url_prob:
                session.update(
                    status="completed",
                    final_verdict="Config patch applied. Re-run discovery to verify the changes.",
                )
                break

            if no_patches:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"OpenAI could not identify an automatic fix after {attempt_num} attempt(s). "
                        f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}. Manual config review recommended."
                    ),
                )
                break

            if attempt_num == MAX_ATTEMPTS:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Reached maximum {MAX_ATTEMPTS} attempts. "
                        f"Best result: {sim['after']}/{sim['total']} URLs rescued. "
                        "Manual review may be needed for remaining issues."
                    ),
                )

    except Exception as exc:
        log.exception("ai_repair: unexpected error job=%s: %s", job_id, exc)
        session.update(status="failed", error=str(exc))

    finally:
        session["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_session(job_id, session)

    return session

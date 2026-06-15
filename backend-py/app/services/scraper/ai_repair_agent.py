"""AI-powered scrape repair agent — OpenAI edition (full quality loop).

Uses the Replit AI Integrations OpenAI proxy (gpt-5.4) to iteratively
diagnose failing scrape jobs and apply config patches — both discovery and
extraction — until ALL quality criteria are met or MAX_ATTEMPTS is reached.

Loop per attempt:
  1. Snapshot quality BEFORE  (discovery stats + extraction fill rates)
  2. Call OpenAI              (json_object mode → diagnosis + patches)
  3. Strict patch validation  (whitelist, type, regex, range)
  4. Apply validated patches  (DB admin_config + YAML on disk)
  5. URL filter simulation    (discovery patches only — no live re-scrape needed)
  6. Predict extraction gains (DB queries: IELTS fills, junk-location clears, …)
  7. Evaluate success criteria (6-dimensional: discovery, fee, IELTS, location, mode, degree)
  8. Record attempt           (Redis, TTL 24 h)
  9. Terminate if overall_ok OR no more patches OR MAX_ATTEMPTS

Session key: ``ai_repair:{job_id}``
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
_REDIS_TTL_SEC    = 86_400  # 24 h

# Success thresholds
_DISC_DROP_RATE_OK   = 30   # % — acceptable URL drop-rate after filter
_DISC_RESCUE_OK      = 0.50 # fraction of dropped URLs rescued
_FEE_PCT_OK          = 50   # %
_IELTS_PCT_OK        = 50   # %
_LOCATION_PCT_OK     = 70   # %
_MODE_PCT_OK         = 60   # %
_DEGREE_PCT_OK       = 70   # %
_CRITERIA_PASS_MIN   = 4    # out of 6 criteria must pass for overall_ok


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_key(job_id: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{job_id}"


def _redis_client():
    from app.config import settings
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


def read_session(job_id: str) -> dict:
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


def _set_dotpath(dotpath: str, value: Any) -> dict:
    """Convert 'fees.central_page' + value → nested dict fragment."""
    keys = dotpath.split(".")
    result: dict = {}
    node = result
    for k in keys[:-1]:
        node[k] = {}
        node = node[k]
    node[keys[-1]] = value
    return result


# ── Strict patch validation ───────────────────────────────────────────────────

_ALLOWED_DISCOVERY_FIELDS: dict[str, type] = {
    "allow_url_patterns": list,
    "block_url_patterns": list,
    "must_contain":       list,
    "bfs_page_budget":    int,
    "use_browser":        bool,
    "sitemap_url":        str,
}

_ALLOWED_EXTRACTION_FIELDS: dict[str, str] = {
    "fees.central_page":                     "url_or_null",
    "fees.fees_pdf_url":                     "url_or_null",
    "fees.default_currency":                 "iso_currency",
    "fees.credit_points_per_unit":           "int_1_200_or_null",
    "english.central_page":                  "url_or_null",
    "english.requirements_pdf_url":          "url_or_null",
    "english.trust_vision_ocr":              "bool",
    "english.default_ielts":                 "ielts_score_or_null",
    "english.default_pte":                   "pte_score_or_null",
    "english.default_toefl":                 "toefl_score_or_null",
    "filters.domestic_only.enabled":         "bool",
    "filters.online_only.enabled":           "bool",
    "text_cleaning.location.strip_patterns": "regex_list",
    "text_cleaning.location.reject_values":  "str_list",
    "text_cleaning.location.allowed_values": "str_list",
    "text_cleaning.duration.split_on_slash": "bool",
    "staging.reject_if_missing":             "known_field_list",
}

_KNOWN_STAGING_FIELDS = {
    "course_name", "degree_level", "category", "study_mode", "course_location",
    "duration", "intake_months", "international_fee", "description",
    "academic_level", "academic_score", "english_test", "other_requirement",
}

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class PatchValidationError(ValueError):
    pass


def _validate_discovery_patch(field: str, value: Any) -> None:
    if field not in _ALLOWED_DISCOVERY_FIELDS:
        raise PatchValidationError(
            f"Discovery field '{field}' not allowed. Allowed: {sorted(_ALLOWED_DISCOVERY_FIELDS)}"
        )
    exp = _ALLOWED_DISCOVERY_FIELDS[field]
    if not isinstance(value, exp):
        raise PatchValidationError(f"discovery.{field} must be {exp.__name__}, got {type(value).__name__}.")
    if field in ("allow_url_patterns", "block_url_patterns", "must_contain"):
        if not value:
            raise PatchValidationError(f"'{field}' must not be empty.")
        for i, pat in enumerate(value):
            if not isinstance(pat, str):
                raise PatchValidationError(f"'{field}[{i}]' must be a string.")
            if len(pat) > 500:
                raise PatchValidationError(f"'{field}[{i}]' pattern too long (>500 chars).")
            try:
                re.compile(pat)
            except re.error as exc:
                raise PatchValidationError(f"'{field}[{i}]' invalid regex: {exc} ({pat!r})") from exc
    if field == "bfs_page_budget" and not (5 <= value <= 300):
        raise PatchValidationError(f"'bfs_page_budget' must be 5–300, got {value}.")
    if field == "sitemap_url" and not value.startswith(("http://", "https://")):
        raise PatchValidationError(f"'sitemap_url' must start with http(s)://, got {value!r}.")


def _validate_extraction_patch(field: str, value: Any) -> None:
    tag = _ALLOWED_EXTRACTION_FIELDS.get(field)
    if tag is None:
        raise PatchValidationError(
            f"Extraction field '{field}' not allowed. Allowed: {sorted(_ALLOWED_EXTRACTION_FIELDS)}"
        )
    if tag == "bool":
        if not isinstance(value, bool):
            raise PatchValidationError(f"extraction.{field} must be bool.")
    elif tag == "url_or_null":
        if value is not None:
            if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                raise PatchValidationError(f"extraction.{field} must be http(s):// URL or null.")
            if len(value) > 2048:
                raise PatchValidationError(f"extraction.{field} URL too long.")
    elif tag == "iso_currency":
        if not isinstance(value, str) or not _ISO_CURRENCY_RE.match(value):
            raise PatchValidationError(f"extraction.{field} must be 3-letter ISO code, got {value!r}.")
    elif tag == "int_1_200_or_null":
        if value is not None:
            if not isinstance(value, int) or not (1 <= value <= 200):
                raise PatchValidationError(f"extraction.{field} must be int 1–200 or null.")
    elif tag == "ielts_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)) or not (4.0 <= float(value) <= 9.0):
                raise PatchValidationError(f"extraction.{field} must be 4.0–9.0 or null.")
    elif tag == "pte_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)) or not (30 <= int(value) <= 90):
                raise PatchValidationError(f"extraction.{field} must be 30–90 or null.")
    elif tag == "toefl_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)) or not (30 <= int(value) <= 120):
                raise PatchValidationError(f"extraction.{field} must be 30–120 or null.")
    elif tag == "regex_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be list of regex strings.")
        for i, pat in enumerate(value):
            if not isinstance(pat, str):
                raise PatchValidationError(f"extraction.{field}[{i}] must be string.")
            if len(pat) > 500:
                raise PatchValidationError(f"extraction.{field}[{i}] too long.")
            try:
                re.compile(pat)
            except re.error as exc:
                raise PatchValidationError(f"extraction.{field}[{i}] invalid regex: {exc} ({pat!r})") from exc
    elif tag == "str_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be list of strings.")
        for i, v in enumerate(value):
            if not isinstance(v, str) or len(v) > 500:
                raise PatchValidationError(f"extraction.{field}[{i}] must be string ≤500 chars.")
    elif tag == "known_field_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be a list.")
        for v in value:
            if v not in _KNOWN_STAGING_FIELDS:
                raise PatchValidationError(
                    f"'{v}' is not a known staging field. Allowed: {sorted(_KNOWN_STAGING_FIELDS)}"
                )


def _validate_and_build_config_patch(patches: list[dict]) -> tuple[dict, list[str]]:
    config_patch: dict = {}
    errors: list[str] = []
    for p in patches:
        section = p.get("section", "")
        field   = p.get("field",   "")
        value   = p.get("value")
        try:
            if section == "discovery":
                _validate_discovery_patch(field, value)
                config_patch.setdefault("discovery", {})[field] = value
            elif section == "extraction":
                _validate_extraction_patch(field, value)
                nested = _set_dotpath(field, value)
                config_patch["extraction"] = _deep_merge(config_patch.get("extraction", {}), nested)
            else:
                raise PatchValidationError(
                    f"Section '{section}' not allowed. Must be 'discovery' or 'extraction'."
                )
        except PatchValidationError as exc:
            errors.append(f"{section}.{field}: {exc}")
            log.warning("ai_repair: patch rejected: %s", exc)
    return config_patch, errors


# ── Quality snapshot ──────────────────────────────────────────────────────────

async def _quality_snapshot(job_id: str, uni_id: int, db) -> dict:
    """Query staged-course fill rates for this job. Returns quality dict."""
    from sqlalchemy import text

    q = (await db.execute(
        text("""
            SELECT COUNT(*)                                      AS total,
                   COUNT(international_fee)                      AS has_fee,
                   COUNT(ielts_overall)                          AS has_ielts,
                   COUNT(intake_months)                          AS has_intakes,
                   COUNT(course_location)                        AS has_location,
                   COUNT(degree_level)                           AS has_degree_level,
                   COUNT(study_mode)                             AS has_mode,
                   COUNT(duration)                               AS has_duration,
                   COUNT(academic_level)                         AS has_academic_level,
                   array_agg(DISTINCT course_location)
                     FILTER (WHERE course_location IS NOT NULL)  AS sample_locations,
                   array_agg(DISTINCT degree_level)
                     FILTER (WHERE degree_level IS NOT NULL)     AS sample_degrees,
                   array_agg(DISTINCT study_mode)
                     FILTER (WHERE study_mode IS NOT NULL)       AS sample_modes
            FROM   scraped_courses
            WHERE  university_id = :uid
              AND  scrape_job_id = :jid
              AND  status IN ('pending','review','approved')
        """),
        {"uid": uni_id, "jid": job_id},
    )).mappings().first()

    total = (q["total"] or 0) if q else 0
    if total == 0:
        return {
            "total_staged": 0, "fee_pct": 0, "ielts_pct": 0, "intakes_pct": 0,
            "location_pct": 0, "degree_level_pct": 0, "mode_pct": 0,
            "duration_pct": 0, "academic_level_pct": 0,
            "sample_locations": [], "sample_degrees": [], "sample_modes": [],
        }

    pct = lambda n: round(100 * (n or 0) / total)
    return {
        "total_staged":       total,
        "fee_pct":            pct(q["has_fee"]),
        "ielts_pct":          pct(q["has_ielts"]),
        "intakes_pct":        pct(q["has_intakes"]),
        "location_pct":       pct(q["has_location"]),
        "degree_level_pct":   pct(q["has_degree_level"]),
        "mode_pct":           pct(q["has_mode"]),
        "duration_pct":       pct(q["has_duration"]),
        "academic_level_pct": pct(q["has_academic_level"]),
        "sample_locations":   list((q["sample_locations"]  or [])[:8]),
        "sample_degrees":     list((q["sample_degrees"]    or [])[:8]),
        "sample_modes":       list((q["sample_modes"]      or [])[:8]),
    }


# ── Post-patch quality prediction ─────────────────────────────────────────────

async def _predict_quality(
    ctx: dict,
    config_patch: dict,
    sim: dict,
    db,
) -> tuple[dict, dict]:
    """Compute predicted quality metrics and predicted fills after applying patches.

    Returns (quality_predicted, predicted_fills).
    Does NOT run a live scrape — uses DB row counts to estimate improvements.
    """
    from sqlalchemy import text

    q     = ctx["quality"]
    total = q.get("total_staged", 0)
    uid   = ctx["university_id"]
    jid   = ctx["job_id"]

    pred  = dict(q)   # start from current snapshot, update where we can predict
    fills: dict = {}  # descriptive predictions for UI

    disc_patch = config_patch.get("discovery", {})
    extr_patch = config_patch.get("extraction", {})

    # ── Discovery: URL simulation already ran ────────────────────────────
    if sim.get("total", 0) > 0 and sim.get("after", 0) > 0:
        rescued = sim["after"]
        fills["discovery_rescued"] = rescued
        old_after = ctx["after_filter"]
        new_after = old_after + rescued
        raw       = ctx["raw_discovered"]
        pred["drop_rate"]    = max(0, round(100 * (1 - new_after / raw))) if raw > 0 else 0
        pred["total_staged"] = total + rescued

    if total == 0:
        return pred, fills

    # ── IELTS default fills NULL rows ─────────────────────────────────────
    default_ielts = (extr_patch.get("english") or {}).get("default_ielts")
    if default_ielts is not None:
        null_count = (await db.execute(text(
            "SELECT COUNT(*) FROM scraped_courses "
            "WHERE university_id=:u AND scrape_job_id=:j "
            "  AND status IN ('pending','review','approved') AND ielts_overall IS NULL"
        ), {"u": uid, "j": jid})).scalar() or 0
        fills["ielts_fills"] = int(null_count)
        new_has  = round(total * q["ielts_pct"] / 100) + null_count
        pred["ielts_pct"] = min(100, round(new_has / total * 100))

    # ── Location: reject_values clears junk ───────────────────────────────
    reject_vals = ((extr_patch.get("text_cleaning") or {}).get("location") or {}).get("reject_values") or []
    if reject_vals:
        conditions = " OR ".join(
            f"LOWER(course_location) LIKE LOWER(:rv{i})"
            for i in range(len(reject_vals))
        )
        params: dict = {"u": uid, "j": jid}
        params.update({f"rv{i}": f"%{v}%" for i, v in enumerate(reject_vals)})
        junk_count = (await db.execute(text(
            f"SELECT COUNT(*) FROM scraped_courses "
            f"WHERE university_id=:u AND scrape_job_id=:j "
            f"  AND status IN ('pending','review','approved') "
            f"  AND course_location IS NOT NULL AND ({conditions})"
        ), params)).scalar() or 0
        fills["location_junk_removed"] = int(junk_count)

    # ── Location: strip_patterns narrows remaining noise ──────────────────
    strip_pats = ((extr_patch.get("text_cleaning") or {}).get("location") or {}).get("strip_patterns") or []
    if strip_pats:
        combined = "|".join(strip_pats)
        try:
            compiled = re.compile(combined, re.IGNORECASE)
            sample   = q.get("sample_locations") or []
            strip_count = sum(1 for loc in sample if loc and compiled.search(loc))
            fills["location_strip_count"] = strip_count
        except re.error:
            pass

    # ── Qualitative flags ─────────────────────────────────────────────────
    if (extr_patch.get("english") or {}).get("central_page"):
        fills["english_central_page_set"] = True
    if (extr_patch.get("fees") or {}).get("central_page"):
        fills["fees_central_page_set"]    = True
    if (extr_patch.get("filters") or {}).get("online_only", {}).get("enabled") is False:
        fills["online_only_disabled"]     = True
    if (extr_patch.get("filters") or {}).get("domestic_only", {}).get("enabled") is False:
        fills["domestic_only_disabled"]   = True

    return pred, fills


# ── Success criteria ──────────────────────────────────────────────────────────

def _evaluate_success(ctx: dict, quality_pred: dict, sim: dict, fills: dict) -> dict:
    """Evaluate 6 quality criteria. Returns criteria dict + overall_ok flag."""
    q_before = ctx["quality"]

    disc_ok = (
        quality_pred.get("drop_rate", 100) < _DISC_DROP_RATE_OK
        or (sim.get("total", 0) > 0 and sim.get("after", 0) >= sim["total"] * _DISC_RESCUE_OK)
        or ctx["drop_rate"] < _DISC_DROP_RATE_OK  # already OK before patch
    )
    fee_ok = (
        q_before.get("fee_pct", 0) >= _FEE_PCT_OK
        or fills.get("fees_central_page_set", False)
    )
    ielts_ok = (
        quality_pred.get("ielts_pct", 0) >= _IELTS_PCT_OK
    )
    location_ok = (
        q_before.get("location_pct", 0) >= _LOCATION_PCT_OK
        or fills.get("location_junk_removed", 0) > 0
    )
    mode_ok = (
        q_before.get("mode_pct", 0) >= _MODE_PCT_OK
        or fills.get("online_only_disabled", False)
    )
    degree_ok = (
        q_before.get("degree_level_pct", 0) >= _DEGREE_PCT_OK
    )

    all_flags   = [disc_ok, fee_ok, ielts_ok, location_ok, mode_ok, degree_ok]
    pass_count  = sum(1 for f in all_flags if f)
    overall_ok  = pass_count >= _CRITERIA_PASS_MIN

    return {
        "discovery_ok":   disc_ok,
        "fee_ok":         fee_ok,
        "ielts_ok":       ielts_ok,
        "location_ok":    location_ok,
        "mode_ok":        mode_ok,
        "degree_level_ok": degree_ok,
        "criteria_pass":  pass_count,
        "overall_ok":     overall_ok,
    }


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
                   u.name          AS uni_name,
                   u.scrape_url    AS scrape_url,
                   u.scrape_config AS scrape_config_raw
            FROM   scrape_runtime_jobs srj
            JOIN   universities u ON u.id = srj.university_id
            WHERE  srj.runtime_job_id = :jid
        """),
        {"jid": job_id},
    )).mappings().first()

    if not row:
        return {}

    uni_id: int   = row["university_id"]
    disc_cfg: dict = row["discovered_config"] or {}
    pipeline: dict = disc_cfg.get("pipeline_stats", {})

    raw_discovered: int = pipeline.get("raw_discovered", row["total_found"] or 0)
    after_filter:   int = pipeline.get("after_filter",   row["imported"]   or 0)
    dropped_sample      = pipeline.get("dropped_sample", [])[:15]
    passed_sample       = pipeline.get("passed_sample",  [])[:5]

    sc           = row["scrape_config_raw"] or {}
    admin_config = sc.get("admin_config") or {}

    unis_dir   = Path(__file__).parent.parent.parent.parent / "scraper_config" / "unis"
    yaml_files = list(unis_dir.glob(f"*_{uni_id}.yaml"))
    yaml_content = ""
    if yaml_files:
        try:
            yaml_content = yaml_files[0].read_text(encoding="utf-8")
        except Exception:
            pass

    quality = await _quality_snapshot(job_id, uni_id, db)
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
        "yaml_content":    yaml_content[:4000],
        "quality":         quality,
        "unis_dir":        unis_dir,
        "yaml_file":       yaml_files[0] if yaml_files else None,
    }


# ── OpenAI prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert web scraping engineer specialising in university course scrapers.
Analyse a failing or low-quality scrape job and return the most impactful fix.

Return ONLY a valid JSON object — no markdown, no text outside the JSON.

Schema:
{
  "diagnosis": "one sentence describing the root problem",
  "root_cause": "one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | fees | english | location | study_mode | filters | duration | unknown",
  "confidence": <integer 0-100>,
  "explanation": "2-3 sentences explaining why this fix will work",
  "patches": [
    { "section": "discovery" | "extraction", "field": "<dotpath>", "action": "replace", "value": <value> }
  ]
}

DISCOVERY PATCHES (section="discovery"):
  allow_url_patterns   list[regex]  — rescue filtered-out course URLs
  block_url_patterns   list[regex]  — block non-course URLs leaking through
  must_contain         list[str]    — only keep URLs containing these strings
  bfs_page_budget      int 5-300    — raise when too few pages crawled
  use_browser          bool         — enable for JS-rendered SPAs
  sitemap_url          str (URL)    — override sitemap URL

EXTRACTION PATCHES (section="extraction"):
  fees.central_page                  str (URL) or null   — fee schedule page
  fees.fees_pdf_url                  str (URL) or null   — fee schedule PDF
  fees.default_currency              str (ISO-3)         — e.g. AUD, GBP, USD
  fees.credit_points_per_unit        int 1-200 or null
  english.central_page               str (URL) or null   — IELTS/English req page
  english.requirements_pdf_url       str (URL) or null
  english.trust_vision_ocr           bool                — false = disable OCR
  english.default_ielts              float 4.0-9.0 or null
  english.default_pte                int 30-90 or null
  english.default_toefl              int 30-120 or null
  filters.domestic_only.enabled      bool
  filters.online_only.enabled        bool
  text_cleaning.location.strip_patterns  list[regex]   — strip noise from locations
  text_cleaning.location.reject_values   list[str]     — clear location if it contains these (e.g. ["Fees", "Campus Map"])
  text_cleaning.location.allowed_values  list[str]     — allowlist of valid campuses
  text_cleaning.duration.split_on_slash  bool
  staging.reject_if_missing              list[known_fields]

RULES:
- All regex patterns must be valid Python re.search() patterns (no ^ anchors)
- field must use exact dot-notation paths from the tables above
- Do NOT repeat a fix from any previous attempt listed in context
- Return up to 3 patches per attempt; prioritise highest-impact fix first
- Return empty patches if no safe automatic fix is possible

DIAGNOSIS PRIORITY:
1. drop_rate > 50% + dropped_sample non-empty → fix allow/block_url_patterns
2. raw_discovered = 0 → raise bfs_page_budget or enable use_browser
3. fee_pct < 40% + staged > 0 → set fees.central_page or fees.default_currency
4. ielts_pct < 40% + staged > 0 → set english.central_page or english.default_ielts
5. location_pct < 40% or junk in sample_locations → text_cleaning.location.reject_values
6. mode_pct < 40% → check filters.online_only.enabled (set false if appropriate)
7. Otherwise → return empty patches with clear explanation\
"""


def _build_user_message(ctx: dict, previous_attempts: list[dict]) -> str:
    prev_block = ""
    if previous_attempts:
        lines = [
            f"  #{a['attempt_number']}: root_cause={a['root_cause']} "
            f"patched={[p.get('field') for p in a.get('patches_applied', [])]} "
            f"success_criteria={a.get('success_criteria', {}).get('criteria_pass', '?')}/6"
            for a in previous_attempts
        ]
        prev_block = "\nPREVIOUS ATTEMPTS (do NOT repeat these fixes):\n" + "\n".join(lines)

    q = ctx["quality"]
    total = q.get("total_staged", 0)
    quality_str = (
        f"total_staged={total}  fee={q.get('fee_pct',0)}%  ielts={q.get('ielts_pct',0)}%  "
        f"intakes={q.get('intakes_pct',0)}%  location={q.get('location_pct',0)}%  "
        f"degree_level={q.get('degree_level_pct',0)}%  study_mode={q.get('mode_pct',0)}%  "
        f"duration={q.get('duration_pct',0)}%"
    ) if total else "no courses staged yet"

    admin_disc = ctx["admin_config"].get("discovery", {})
    admin_extr = ctx["admin_config"].get("extraction", {})

    return f"""UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

DISCOVERY STATS:
  raw_discovered={ctx['raw_discovered']}  after_filter={ctx['after_filter']}  staged={ctx['imported']}  drop_rate={ctx['drop_rate']}%  errors={ctx['total_errors']}

DROPPED URLs (filtered out — may be real course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

PASSED URLs (currently making it through the filter):
{json.dumps(ctx['passed_sample'], indent=2)}

EXTRACTION QUALITY (staged courses):
{quality_str}
  sample_locations:     {json.dumps(q.get('sample_locations', []))}
  sample_degree_levels: {json.dumps(q.get('sample_degrees', []))}
  sample_study_modes:   {json.dumps(q.get('sample_modes', []))}

ADMIN_CONFIG discovery overrides (DB, highest priority):
{json.dumps(admin_disc, indent=2) if admin_disc else "(none)"}

ADMIN_CONFIG extraction overrides (DB):
{json.dumps(admin_extr, indent=2) if admin_extr else "(none)"}

YAML CONFIG ON DISK:
{ctx['yaml_content'] or "(no YAML file found)"}
{prev_block}

Analyse and return the highest-impact fix as JSON.\
"""


# ── URL filter simulation ─────────────────────────────────────────────────────

_MEDIA_EXT  = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|bmp|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?)$", re.I)
_ASSET_PATH = re.compile(r"/(images?|assets?|globalassets|static|media|uploads?|fonts?|icons?|scripts?)/", re.I)


def _is_course_url(u: str) -> bool:
    return not _MEDIA_EXT.search(u) and not _ASSET_PATH.search(u)


def _simulate_filter(dropped: list[str], allow_pats: list[str], block_pats: list[str]) -> dict:
    course_urls = [u for u in dropped if _is_course_url(u)]
    if not course_urls:
        return {"before": 0, "after": 0, "total": 0, "rescued": []}

    def _c(pats: list[str]) -> list:
        out = []
        for p in pats:
            try:
                out.append(re.compile(p, re.IGNORECASE))
            except re.error:
                pass
        return out

    allow_c = _c(allow_pats)
    block_c = _c(block_pats)
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

    sc       = dict((row.get("scrape_config") or {}) if row else {})
    existing = sc.get("admin_config") or {}
    sc["_prev_admin_config"] = existing
    sc["admin_config"]       = _deep_merge(existing, config_patch)

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
                            candidates = [f]; break
                    except Exception:
                        continue
        if not candidates:
            log.warning("ai_repair: no YAML for uni_id=%s", uni_id); return
        yaml_file = candidates[0]

    try:
        existing_text = yaml_file.read_text(encoding="utf-8")
        comment_lines = [ln for ln in existing_text.splitlines() if ln.strip().startswith("#")]
        header  = ("\n".join(comment_lines) + "\n") if comment_lines else ""
        merged  = _deep_merge(_yaml.safe_load(existing_text) or {}, config_patch)
        new_txt = header + _yaml.dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False)
        yaml_file.write_text(new_txt, encoding="utf-8")
        log.info("ai_repair: wrote config patch to %s", yaml_file.name)
    except Exception as exc:
        log.warning("ai_repair: YAML write failed: %s", exc)


# ── Quality delta computation ─────────────────────────────────────────────────

def _compute_delta(before: dict, predicted: dict) -> dict:
    keys = ["fee_pct", "ielts_pct", "intakes_pct", "location_pct",
            "degree_level_pct", "mode_pct", "duration_pct", "drop_rate"]
    delta: dict = {}
    for k in keys:
        b = before.get(k, 0)
        p = predicted.get(k, 0)
        if b != p:
            delta[k] = p - b
    return delta


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_ai_repair_loop(job_id: str, db) -> dict:
    """Run the OpenAI-powered full quality repair loop.

    After each patch: simulates discovery improvement AND predicts extraction
    gains from the DB, evaluates 6-dimensional success criteria, and continues
    until overall_ok or MAX_ATTEMPTS.
    """
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
            session.update(status="failed", error="Job not found in database.")
            return session

        session["uni_name"] = ctx["uni_name"]
        _write_session(job_id, session)

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            session["current_attempt"] = attempt_num
            _write_session(job_id, session)
            log.info("ai_repair: job=%s attempt=%d/%d", job_id, attempt_num, MAX_ATTEMPTS)

            # ① Snapshot quality BEFORE this attempt
            quality_before = await _quality_snapshot(job_id, ctx["university_id"], db)
            quality_before["drop_rate"] = ctx["drop_rate"]

            # ② Call OpenAI
            user_msg = _build_user_message(ctx, session["attempts"])
            ai_data  = await chat_json(system=_SYSTEM_PROMPT, user=user_msg, max_tokens=2048)

            if ai_data is None:
                session.update(
                    status="completed",
                    final_verdict=(
                        "OpenAI service unavailable. Check AI_INTEGRATIONS_OPENAI_BASE_URL "
                        "and AI_INTEGRATIONS_OPENAI_API_KEY."
                    ),
                )
                break

            # ③ Validate patches
            patches_raw: list[dict] = ai_data.get("patches") or []
            config_patch, validation_errors = _validate_and_build_config_patch(patches_raw)
            disc_patch = config_patch.get("discovery", {})

            # ④ URL filter simulation
            sim: dict = {"before": 0, "after": 0, "total": 0, "rescued": []}
            if "allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch:
                from sqlalchemy import text as _text
                dc_row = (await db.execute(
                    _text("SELECT discovered_config FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
                    {"j": job_id},
                )).first()
                dc: dict = (dc_row[0] or {}) if dc_row else {}
                dropped  = dc.get("pipeline_stats", {}).get("dropped_sample") or ctx["dropped_sample"]
                sim = _simulate_filter(
                    dropped,
                    disc_patch.get("allow_url_patterns", []),
                    disc_patch.get("block_url_patterns", []),
                )

            # ⑤ Apply patches
            patch_applied_ok  = False
            patch_error: str  = ""
            if config_patch:
                try:
                    await _apply_to_db(ctx["university_id"], config_patch, db)
                    _apply_to_yaml(ctx.get("yaml_file"), ctx["unis_dir"],
                                   ctx["university_id"], ctx["scrape_url"], config_patch)
                    patch_applied_ok = True
                except Exception as exc:
                    log.warning("ai_repair: apply error: %s", exc)
                    patch_error = str(exc)

            # ⑥ Predict quality after patches + fills
            quality_predicted, predicted_fills = await _predict_quality(
                ctx, config_patch, sim, db
            )

            # ⑦ Evaluate success criteria
            success_criteria = _evaluate_success(ctx, quality_predicted, sim, predicted_fills)
            quality_delta    = _compute_delta(quality_before, quality_predicted)

            attempt_record: dict = {
                "attempt_number":    attempt_num,
                "diagnosis":         ai_data.get("diagnosis", "Unknown"),
                "root_cause":        ai_data.get("root_cause", "unknown"),
                "confidence":        ai_data.get("confidence", 0),
                "explanation":       ai_data.get("explanation", ""),
                "patches_applied":   [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches_raw
                    if p.get("section") in {"discovery", "extraction"} and p.get("field")
                ],
                "validation_errors": validation_errors,
                # URL simulation (discovery)
                "before_pass_count": sim["before"],
                "after_pass_count":  sim["after"],
                "total_test_urls":   sim["total"],
                "rescued_sample":    sim["rescued"],
                # Patch save status
                "patch_applied_ok":  patch_applied_ok,
                "patch_error":       patch_error if patch_error else None,
                # Quality tracking
                "quality_before":    quality_before,
                "quality_predicted": quality_predicted,
                "quality_delta":     quality_delta,
                "predicted_fills":   predicted_fills,
                "success_criteria":  success_criteria,
            }
            session["attempts"].append(attempt_record)
            _write_session(job_id, session)

            crit_pass = success_criteria["criteria_pass"]
            overall   = success_criteria["overall_ok"]

            # ⑧ Termination
            if not patches_raw:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"OpenAI could not identify a further automatic fix "
                        f"({crit_pass}/6 quality criteria already met). "
                        f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}. "
                        "Re-run the scrape to validate applied patches."
                    ),
                )
                break

            if overall:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"All quality targets met ({crit_pass}/6 criteria pass). "
                        "Re-run the scrape to apply config changes to live data."
                    ),
                )
                break

            if attempt_num == MAX_ATTEMPTS:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Reached maximum {MAX_ATTEMPTS} attempts. "
                        f"{crit_pass}/6 quality criteria passing. "
                        f"Outstanding: {_failing_criteria_str(success_criteria)}. "
                        "Re-run the scrape with patches applied."
                    ),
                )

            # Update ctx quality for next attempt's context message
            ctx["quality"] = quality_predicted

    except Exception as exc:
        log.exception("ai_repair: unexpected error job=%s: %s", job_id, exc)
        session.update(status="failed", error=str(exc))
    finally:
        session["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_session(job_id, session)

    return session


def _failing_criteria_str(sc: dict) -> str:
    labels = {
        "discovery_ok":  "discovery",
        "fee_ok":        "fees",
        "ielts_ok":      "IELTS",
        "location_ok":   "location",
        "mode_ok":       "study mode",
        "degree_level_ok": "degree level",
    }
    return ", ".join(label for key, label in labels.items() if not sc.get(key, True))

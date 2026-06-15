"""AI-powered scrape repair agent - OpenAI edition (full quality loop).

Uses the Replit AI Integrations OpenAI proxy to iteratively diagnose failing
scrape jobs and apply config patches until BOTH discovery quality AND extraction
quality improve, or MAX_ATTEMPTS is reached.

Loop per attempt:
  1. Snapshot quality BEFORE  (discovery stats + extraction fill rates)
  2. Call OpenAI              (json_object mode → diagnosis + patches)
  3. Strict patch validation  (whitelist, type, regex, range)
  4. Apply validated patches  (DB admin_config + YAML on disk)
  5. URL filter simulation    (discovery patches only — fast, no live re-scrape)
  6. Real extraction scan     (re-fetch up to 5 staged course URLs with patched config;
                               SQL fast-path for default_ielts / reject_values)
  7. Real quality snapshot    (DB fill rates AFTER scan — not predicted, actual)
  8. Evaluate success         (6-dimensional: discovery, fee, IELTS, location, mode, degree)
  9. Record attempt           (Redis, TTL 24 h)
 10. Terminate if overall_ok OR no more patches OR MAX_ATTEMPTS

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

MAX_ATTEMPTS = 6
_REDIS_KEY_PREFIX = "ai_repair:"
_REDIS_TTL_SEC    = 86_400  # 24 h

# Success thresholds
_DISC_DROP_RATE_OK   = 30   # % - acceptable URL drop-rate after filter
_DISC_RESCUE_OK      = 0.50 # fraction of dropped URLs rescued
_FEE_PCT_OK          = 50   # %
_IELTS_PCT_OK        = 50   # %
_LOCATION_PCT_OK     = 70   # %
_MODE_PCT_OK         = 60   # %
_DEGREE_PCT_OK       = 70   # %
_CRITERIA_PASS_MIN   = 4    # out of 6 criteria must pass for overall_ok

# Extraction quality thresholds
_EXTRACTION_AVG_THRESHOLD = 60   # avg key-field fill rate to be considered "ok"
_EXTRACTION_MIN_FIELD    = 35    # any single key field below this is "poor"


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

_ALLOWED_DISCOVERY_FIELDS: dict[str, type | tuple] = {
    "allow_url_patterns":   list,
    "block_url_patterns":   list,
    "must_contain":         list,
    "bfs_page_budget":      int,
    "use_browser":          bool,
    "sitemap_url":          str,
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

_ALLOWED_RECIPE_FIELDS: dict[str, type | tuple] = {
    "fee_source_urls":            list,
    "fee_term":                   str,
    "location_reject_values":     list,
    "location_allowed_values":    list,
    "study_mode_online_keywords": list,
    "course_name_remove_after":   list,
}

_ALLOWED_SECTIONS = {"discovery", "recipe"}


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
            "Only 'discovery' and 'recipe' patches may be applied automatically."
        )

    if section == "discovery":
        if field not in _ALLOWED_DISCOVERY_FIELDS:
            raise PatchValidationError(
                f"Field '{field}' is not an allowed discovery field. "
                f"Allowed: {sorted(_ALLOWED_DISCOVERY_FIELDS)}"
            )
        expected_type = _ALLOWED_DISCOVERY_FIELDS[field]
        if not isinstance(value, expected_type):
            raise PatchValidationError(
                f"Discovery field '{field}' must be {expected_type.__name__}, "
                f"got {type(value).__name__}."
            )
        if field in ("allow_url_patterns", "block_url_patterns", "must_contain"):
            if not value:
                raise PatchValidationError(f"'{field}' must not be an empty list.")
            for i, pat in enumerate(value):
                if not isinstance(pat, str):
                    raise PatchValidationError(f"'{field}[{i}]' must be a string.")
                if len(pat) > 500:
                    raise PatchValidationError(
                        f"'{field}[{i}]' pattern is suspiciously long (>500 chars)."
                    )
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

    elif section == "recipe":
        if field not in _ALLOWED_RECIPE_FIELDS:
            raise PatchValidationError(
                f"Field '{field}' is not an allowed recipe field. "
                f"Allowed: {sorted(_ALLOWED_RECIPE_FIELDS)}"
            )
        expected_type = _ALLOWED_RECIPE_FIELDS[field]
        if not isinstance(value, expected_type):
            raise PatchValidationError(
                f"Recipe field '{field}' must be {expected_type.__name__}, "
                f"got {type(value).__name__}."
            )
        if isinstance(value, list):
            if not value:
                raise PatchValidationError(f"Recipe field '{field}' must not be an empty list.")
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    raise PatchValidationError(
                        f"Recipe field '{field}[{i}]' must be a string, got {type(item).__name__}."
                    )
        if field == "fee_term" and value not in ("Annual", "Per Unit", "Full Course", ""):
            raise PatchValidationError(
                f"'fee_term' must be one of Annual | Per Unit | Full Course, got {value!r}."
            )
    return patch


def _validate_and_build_config_patch(patches: list[dict]) -> tuple[dict, dict, list[str]]:
    """Validate all patches and build separate discovery and extraction patch dicts.

    Returns (discovery_patch, extraction_patch, errors).
    - discovery_patch: flat dict of validated discovery fields
    - extraction_patch: NESTED dict ready to deep-merge into admin_config.extraction.
      Dotpath fields (e.g. "fees.central_page") are expanded to nested dicts so the
      config loader receives properly-structured data.
    - errors: human-readable rejection reasons for skipped patches
    """
    discovery_patch: dict = {}
    extraction_patch: dict = {}
    errors: list[str] = []
    for p in patches:
        section = p.get("section", "")
        field   = p.get("field",   "")
        value   = p.get("value")
        try:
            if section == "discovery":
                _validate_discovery_patch(field, value)
                discovery_patch[field] = value
            elif section == "recipe":
                if field in _ALLOWED_EXTRACTION_FIELDS:
                    # Dotpath extraction field (e.g. "fees.central_page"):
                    # validate with the extraction validator then expand to nested dict
                    _validate_extraction_patch(field, value)
                    frag = _set_dotpath(field, value)
                    extraction_patch = _deep_merge(extraction_patch, frag)
                elif field in _ALLOWED_RECIPE_FIELDS:
                    # Legacy flat recipe field — validate then store at top level
                    validated = _validate_patch(p)
                    extraction_patch[validated["field"]] = validated["value"]
                else:
                    raise PatchValidationError(
                        f"Field '{field}' is not in allowed discovery or extraction fields. "
                        f"Discovery allowed: {sorted(_ALLOWED_DISCOVERY_FIELDS)}. "
                        f"Extraction allowed: {sorted(_ALLOWED_EXTRACTION_FIELDS)}."
                    )
            else:
                raise PatchValidationError(
                    f"Section '{section}' is not in the allowed set {_ALLOWED_SECTIONS}. "
                    "Only 'discovery' and 'recipe' patches may be applied automatically."
                )
        except PatchValidationError as exc:
            errors.append(f"{section}.{field}: {exc}")
            log.warning("ai_repair: patch rejected: %s", exc)

    return discovery_patch, extraction_patch, errors


# ── Extraction quality helpers ────────────────────────────────────────────────

def _extraction_quality_ok(quality: dict) -> bool:
    """True when average key-field fill rate is acceptable."""
    if not quality or quality.get("total_staged", 0) == 0:
        return True  # No staged courses yet - don't block on extraction
    key_pcts = [
        quality.get("fee_pct", 0),
        quality.get("ielts_pct", 0),
        quality.get("intakes_pct", 0),
        quality.get("location_pct", 0),
        quality.get("degree_level_pct", 0),
        quality.get("mode_pct", 0),
        quality.get("duration_pct", 0),
    ]
    avg = sum(key_pcts) / len(key_pcts)
    min_field = min(key_pcts)
    return avg >= _EXTRACTION_AVG_THRESHOLD and min_field >= _EXTRACTION_MIN_FIELD


def _quality_summary(quality: dict) -> str:
    if not quality:
        return "no courses staged yet"
    return (
        f"staged={quality.get('total_staged', 0)}"
        f" fee={quality.get('fee_pct', 0)}%"
        f" ielts={quality.get('ielts_pct', 0)}%"
        f" intakes={quality.get('intakes_pct', 0)}%"
        f" location={quality.get('location_pct', 0)}%"
        f" degree_level={quality.get('degree_level_pct', 0)}%"
        f" mode={quality.get('mode_pct', 0)}%"
        f" duration={quality.get('duration_pct', 0)}%"
    )


# ── Quality snapshot ───────────────────────────────────────────────────────────

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
    Does NOT run a live scrape - uses DB row counts to estimate improvements.
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


# ── Real post-patch extraction scan ───────────────────────────────────────────

async def _run_extraction_scan(
    ctx: dict,
    config_patch: dict,
    db,
    max_courses: int = 5,
) -> dict:
    """Re-run extraction on a sample of staged courses using the patched config.

    Two phases:
    1. SQL fast path — directly apply config-derivable updates without any page
       fetches: ``english.default_ielts`` fills NULL IELTS rows; location
       ``reject_values`` clears junk locations.
    2. Fetch + extract path — re-fetch up to ``max_courses`` course URLs and run
       the full extraction pipeline (no AI/Gemini) with the new config to capture
       fee, IELTS, or mode improvements from central pages / new patterns.

    Updates ``scraped_courses`` rows in-place and commits.
    Returns a fill-summary dict for the UI chips (actual counts, not estimates).
    """
    from sqlalchemy import text

    uni_id     = ctx["university_id"]
    job_id     = ctx["job_id"]
    scrape_url = ctx["scrape_url"]
    extr_patch = config_patch.get("extraction") or {}

    fills: dict[str, Any] = {
        "fee_fills":          0,
        "ielts_fills":        0,
        "location_clears":    0,
        "mode_fills":         0,
        "courses_rescanned":  0,
    }
    _base_where  = (
        "university_id = :uid AND scrape_job_id = :jid "
        "AND status IN ('pending','review','approved')"
    )
    _base_params: dict[str, Any] = {"uid": uni_id, "jid": job_id}

    # ── Phase 1: SQL fast path ────────────────────────────────────────────────

    # 1a. default_ielts → fill NULL ielts_overall rows immediately
    default_ielts = (extr_patch.get("english") or {}).get("default_ielts")
    if default_ielts is not None:
        res = await db.execute(
            text(
                f"UPDATE scraped_courses SET ielts_overall = :iv "
                f"WHERE {_base_where} AND ielts_overall IS NULL"
            ),
            {**_base_params, "iv": float(default_ielts)},
        )
        fills["ielts_fills"] = res.rowcount or 0

    # 1b. reject_values → clear junk course_location values
    reject_vals = (
        (extr_patch.get("text_cleaning") or {}).get("location") or {}
    ).get("reject_values") or []
    for val in reject_vals:
        res = await db.execute(
            text(
                f"UPDATE scraped_courses SET course_location = NULL "
                f"WHERE {_base_where} AND course_location IS NOT NULL "
                f"AND LOWER(course_location) LIKE LOWER(:rv)"
            ),
            {**_base_params, "rv": f"%{val}%"},
        )
        fills["location_clears"] += res.rowcount or 0

    await db.commit()

    # ── Phase 2: Fetch + extract sample courses ───────────────────────────────
    # Run on ALL attempts — not just extraction-patch attempts.  Even a discovery
    # patch may rescue new URLs that should be tested, and even without patches the
    # live scan tells OpenAI whether the current config can fill critical fields.

    # Count staged courses before deciding whether fetch phase is worth running
    staged_count = (await db.execute(
        text(
            "SELECT COUNT(*) FROM scraped_courses "
            "WHERE university_id=:uid AND scrape_job_id=:jid "
            "  AND status IN ('pending','review','approved')"
        ),
        _base_params,
    )).scalar() or 0

    if staged_count == 0:
        log.info("extraction_scan: no staged courses yet; skipping fetch phase")
        return fills

    # Load the freshly-patched config so extract_course() picks up the changes
    try:
        from urllib.parse import urlparse as _urlparse
        from app.services.scraper.config.loader import load_uni_config
        from app.services.scraper.config.context import set_uni_config
        from app.services.scraper.pipelines.single_course import extract_course

        _host = _urlparse(scrape_url).netloc
        _slug = _host.removeprefix("www.").split(".")[0] if _host else "unknown"

        _sc_row = (await db.execute(
            text("SELECT scrape_config FROM universities WHERE id = :uid"),
            {"uid": uni_id},
        )).first()
        _db_sc = _sc_row[0] if (_sc_row and _sc_row[0]) else {}

        uni_cfg = load_uni_config(
            slug=_slug,
            name=ctx["uni_name"],
            scrape_url=scrape_url,
            university_id=uni_id,
            db_scrape_config=_db_sc,
        )
        set_uni_config(uni_cfg)
    except Exception as exc:
        log.warning("extraction_scan: config load failed — skipping fetch phase: %s", exc)
        return fills

    # Pick courses with any missing key field (fee, IELTS, mode, location, degree_level).
    # Prefer courses missing the most fields so each fetch has the highest chance of
    # improving quality metrics.  course_url must be a detail page (not a hub):
    # filter to URLs with at least 3 path segments to exclude /study/ category pages.
    sample_rows = (await db.execute(text("""
        SELECT id, course_url, international_fee, ielts_overall, study_mode,
               course_location, degree_level
        FROM   scraped_courses
        WHERE  university_id = :uid
          AND  scrape_job_id = :jid
          AND  status IN ('pending','review','approved')
          AND  course_url IS NOT NULL
          AND  (
               international_fee IS NULL
            OR ielts_overall     IS NULL
            OR study_mode        IS NULL
            OR course_location   IS NULL
            OR degree_level      IS NULL
          )
        ORDER BY (
            CASE WHEN international_fee IS NULL THEN 1 ELSE 0 END +
            CASE WHEN ielts_overall     IS NULL THEN 1 ELSE 0 END +
            CASE WHEN study_mode        IS NULL THEN 1 ELSE 0 END +
            CASE WHEN course_location   IS NULL THEN 1 ELSE 0 END +
            CASE WHEN degree_level      IS NULL THEN 1 ELSE 0 END
        ) DESC,
        id
        LIMIT  :n
    """), {**_base_params, "n": max_courses})).mappings().all()

    # Prefer real detail-page URLs: score by path-segment depth (more segments = more specific)
    def _detail_score(url: str) -> int:
        try:
            from urllib.parse import urlparse as _up
            path = _up(url).path.rstrip("/")
            segments = [s for s in path.split("/") if s]
            # Penalise generic hub-page segments
            _hub_segs = {"study", "courses", "undergraduate", "postgraduate", "programs",
                         "programmes", "find-a-course", "search", "browse"}
            hub_penalty = sum(1 for s in segments if s.lower() in _hub_segs)
            return len(segments) - hub_penalty
        except Exception:
            return 0

    sorted_rows = sorted(sample_rows, key=lambda r: _detail_score(r["course_url"] or ""), reverse=True)
    fills["courses_rescanned"] = len(sorted_rows)

    for row in sorted_rows:
        url = row["course_url"]
        if not url:
            continue
        try:
            result = await extract_course(url=url, use_ai_fallback=False)
            update: dict[str, Any] = {}
            if row["international_fee"] is None and result.get("international_fee") is not None:
                update["international_fee"] = result["international_fee"]
                fills["fee_fills"] += 1
            if row["ielts_overall"] is None and result.get("ielts_overall") is not None:
                update["ielts_overall"] = result["ielts_overall"]
                fills["ielts_fills"] += 1
            if row["study_mode"] is None and result.get("study_mode") is not None:
                update["study_mode"] = result["study_mode"]
                fills["mode_fills"] += 1
            if update:
                clauses = ", ".join(f"{k}=:{k}" for k in update)
                await db.execute(
                    text(f"UPDATE scraped_courses SET {clauses} WHERE id=:rid"),
                    {"rid": row["id"], **update},
                )
        except Exception as exc:
            log.warning("extraction_scan: url=%s error=%s", url, exc)

    await db.commit()
    return fills


# ── Success criteria ──────────────────────────────────────────────────────────

def _evaluate_success(
    quality_after: dict,
    sim: dict,
    predicted_fills: dict,
    ctx: dict,
) -> dict:
    """Evaluate 6 quality criteria against the REAL post-scan quality_after snapshot.

    ``quality_after`` comes from ``_quality_snapshot()`` called after
    ``_run_extraction_scan()`` has committed DB updates — these are real numbers,
    not estimates.  ``predicted_fills`` is kept as a helper for qualitative flags
    that only take effect on the next full scrape (e.g. fees_central_page_set).
    """
    disc_ok = (
        quality_after.get("drop_rate", 100) < _DISC_DROP_RATE_OK
        or (sim.get("total", 0) > 0 and sim.get("after", 0) >= sim["total"] * _DISC_RESCUE_OK)
        or ctx["drop_rate"] < _DISC_DROP_RATE_OK
    )
    fee_ok = (
        quality_after.get("fee_pct", 0) >= _FEE_PCT_OK
        or predicted_fills.get("fees_central_page_set", False)
    )
    ielts_ok  = quality_after.get("ielts_pct",        0) >= _IELTS_PCT_OK
    loc_ok    = quality_after.get("location_pct",     0) >= _LOCATION_PCT_OK
    mode_ok   = (
        quality_after.get("mode_pct", 0) >= _MODE_PCT_OK
        or predicted_fills.get("online_only_disabled", False)
    )
    degree_ok = quality_after.get("degree_level_pct", 0) >= _DEGREE_PCT_OK

    all_flags  = [disc_ok, fee_ok, ielts_ok, loc_ok, mode_ok, degree_ok]
    pass_count = sum(1 for f in all_flags if f)
    overall_ok = pass_count >= _CRITERIA_PASS_MIN

    return {
        "discovery_ok":    disc_ok,
        "fee_ok":          fee_ok,
        "ielts_ok":        ielts_ok,
        "location_ok":     loc_ok,
        "mode_ok":         mode_ok,
        "degree_level_ok": degree_ok,
        "criteria_pass":   pass_count,
        "overall_ok":      overall_ok,
    }


# ── Context gathering ─────────────────────────────────────────────────────────

async def _gather_context(job_id: str, db) -> dict:
    from sqlalchemy import text

    row = (await db.execute(
        text("""
            SELECT srj.university_id,
                   srj.total_found,
                   srj.imported,
                   srj.errors          AS total_errors,
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

    q = (await db.execute(
        text("""
            SELECT COUNT(*)                                          AS total,
                   COUNT(international_fee)                          AS has_fee,
                   COUNT(ielts_overall)                              AS has_ielts,
                   COUNT(intake_months)                              AS has_intakes,
                   COUNT(course_location)                            AS has_location,
                   COUNT(degree_level)                               AS has_degree_level,
                   COUNT(study_mode)                                 AS has_mode,
                   COUNT(duration)                                   AS has_duration,
                   COUNT(academic_level)                             AS has_academic_level,
                   array_agg(DISTINCT course_location)
                     FILTER (WHERE course_location IS NOT NULL)      AS sample_locations,
                   array_agg(DISTINCT degree_level)
                     FILTER (WHERE degree_level IS NOT NULL)         AS sample_degree_levels,
                   array_agg(DISTINCT study_mode)
                     FILTER (WHERE study_mode IS NOT NULL)           AS sample_modes
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
            "total_staged":       total,
            "fee_pct":            round(100 * (q["has_fee"]           or 0) / total),
            "ielts_pct":          round(100 * (q["has_ielts"]         or 0) / total),
            "intakes_pct":        round(100 * (q["has_intakes"]       or 0) / total),
            "location_pct":       round(100 * (q["has_location"]      or 0) / total),
            "degree_level_pct":   round(100 * (q["has_degree_level"]  or 0) / total),
            "mode_pct":           round(100 * (q["has_mode"]          or 0) / total),
            "duration_pct":       round(100 * (q["has_duration"]      or 0) / total),
            "academic_level_pct": round(100 * (q["has_academic_level"] or 0) / total),
            "sample_locations":   list((q["sample_locations"]    or [])[:8]),
            "sample_degrees":     list((q["sample_degree_levels"] or [])[:8]),
            "sample_modes":       list((q["sample_modes"]         or [])[:8]),
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
        "yaml_content":    yaml_content[:4000],
        "quality":         quality,
        "unis_dir":        unis_dir,
        "yaml_file":       yaml_files[0] if yaml_files else None,
    }


async def _requery_quality(uni_id: int, job_id: str, db) -> dict:
    """Re-read per-field fill rates from the staging table for before/after comparison."""
    from sqlalchemy import text

    q = (await db.execute(
        text("""
            SELECT COUNT(*)                              AS total,
                   COUNT(international_fee)             AS has_fee,
                   COUNT(ielts_overall)                 AS has_ielts,
                   COUNT(intake_months)                 AS has_intakes,
                   COUNT(course_location)               AS has_location,
                   COUNT(degree_level)                  AS has_degree_level,
                   COUNT(study_mode)                    AS has_mode,
                   COUNT(duration)                      AS has_duration
            FROM   scraped_courses
            WHERE  university_id = :uid
              AND  scrape_job_id = :jid
              AND  status IN ('pending', 'review', 'approved')
        """),
        {"uid": uni_id, "jid": job_id},
    )).mappings().first()

    total = (q["total"] or 0) if q else 0
    if total == 0:
        return {}

    return {
        "total_staged":     total,
        "fee_pct":          round(100 * (q["has_fee"]          or 0) / total),
        "ielts_pct":        round(100 * (q["has_ielts"]        or 0) / total),
        "intakes_pct":      round(100 * (q["has_intakes"]      or 0) / total),
        "location_pct":     round(100 * (q["has_location"]     or 0) / total),
        "degree_level_pct": round(100 * (q["has_degree_level"] or 0) / total),
        "mode_pct":         round(100 * (q["has_mode"]         or 0) / total),
        "duration_pct":     round(100 * (q["has_duration"]     or 0) / total),
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
    { "section": "discovery" | "recipe", "field": "<dotpath>", "action": "replace", "value": <value> }
  ]
}

DISCOVERY PATCHES (section="discovery"):
  allow_url_patterns   list[regex]  — rescue filtered-out course URLs
  block_url_patterns   list[regex]  — block non-course URLs leaking through
  must_contain         list[str]    — only keep URLs containing these strings
  bfs_page_budget      int 5-300    — raise when too few pages crawled
  use_browser          bool         — enable for JS-rendered SPAs
  sitemap_url          str (URL)    — override sitemap URL

RECIPE PATCHES (section="recipe"):
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
  text_cleaning.location.strip_patterns  list[regex]
  text_cleaning.location.reject_values   list[str]
  text_cleaning.location.allowed_values  list[str]
  text_cleaning.duration.split_on_slash  bool
  staging.reject_if_missing              list[known_fields]

RULES:
- All regex patterns must be valid Python re.search() patterns (no ^ anchors)
- field must use exact dot-notation paths from the tables above
- Do NOT repeat a fix from any previous attempt listed in context
- Return up to 3 patches per attempt; prioritise highest-impact fix first
- Return empty patches if no safe automatic fix is possible\
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
    """Merge config_patch into universities.scrape_config.admin_config and commit."""
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


async def _apply_discovery_to_db(uni_id: int, disc_patch: dict, db) -> None:
    """Write discovery-section patch into admin_config.discovery."""
    await _apply_to_db(uni_id, {"discovery": disc_patch}, db)


async def _apply_recipe_to_db(uni_id: int, recipe_patch: dict, db) -> None:
    """Write recipe-section patch into admin_config.extraction."""
    await _apply_to_db(uni_id, {"extraction": recipe_patch}, db)


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

def _compute_delta(before: dict, after: dict) -> dict:
    keys = ["fee_pct", "ielts_pct", "intakes_pct", "location_pct",
            "degree_level_pct", "mode_pct", "duration_pct", "drop_rate"]
    delta: dict = {}
    for k in keys:
        b = before.get(k, 0)
        p = after.get(k, 0)
        if b != p:
            delta[k] = p - b
    return delta


# ── User message builder ──────────────────────────────────────────────────────

def _build_user_message(ctx: dict, previous_attempts: list[dict], phase: str = "discovery") -> str:
    prev_block = ""
    if previous_attempts:
        lines = []
        for a in previous_attempts:
            patches_detail = "; ".join(
                f"{p.get('section')}.{p.get('field')}={json.dumps(p.get('new_value'))[:80]}"
                for p in a.get("patches_applied", [])
            ) or "(no patches applied)"
            q_after = a.get("quality_after") or {}
            lines.append(
                f"  Attempt #{a['attempt_number']} [{a.get('phase','?')} phase]: "
                f"root_cause={a['root_cause']} | "
                f"patches={patches_detail} | "
                f"result={a.get('success_criteria',{}).get('criteria_pass','?')}/6 criteria | "
                f"fee={q_after.get('fee_pct',0)}% ielts={q_after.get('ielts_pct',0)}% "
                f"loc={q_after.get('location_pct',0)}% mode={q_after.get('mode_pct',0)}% "
                f"degree={q_after.get('degree_level_pct',0)}%"
            )
        prev_block = (
            "\nPREVIOUS ATTEMPTS — CRITICAL: do NOT repeat any patch already listed below.\n"
            "Each patch has already been applied. Repeating the same field with the same\n"
            "or similar values will not improve quality. Choose a different fix strategy.\n"
            + "\n".join(lines)
        )

    q = ctx.get("quality") or {}
    quality_str = _quality_summary(q)

    # Build failing-field hints for extraction phase
    _failing_hints: list[str] = []
    fee_pct  = q.get("fee_pct",          0)
    ielts_pct= q.get("ielts_pct",        0)
    loc_pct  = q.get("location_pct",     0)
    mode_pct = q.get("mode_pct",         0)
    deg_pct  = q.get("degree_level_pct", 0)
    int_pct  = q.get("intakes_pct",      0)
    if fee_pct   < _FEE_PCT_OK:    _failing_hints.append(f"fees ({fee_pct}% < {_FEE_PCT_OK}% target)")
    if ielts_pct < _IELTS_PCT_OK:  _failing_hints.append(f"IELTS ({ielts_pct}% < {_IELTS_PCT_OK}% target)")
    if loc_pct   < _LOCATION_PCT_OK: _failing_hints.append(f"location ({loc_pct}% < {_LOCATION_PCT_OK}% target)")
    if mode_pct  < _MODE_PCT_OK:   _failing_hints.append(f"study_mode ({mode_pct}% < {_MODE_PCT_OK}% target)")
    if deg_pct   < _DEGREE_PCT_OK: _failing_hints.append(f"degree_level ({deg_pct}% < {_DEGREE_PCT_OK}% target)")
    if int_pct   < 40:             _failing_hints.append(f"intakes ({int_pct}% fill rate)")
    failing_str = ", ".join(_failing_hints) if _failing_hints else "all fields meeting targets"

    if phase == "extraction":
        focus_block = (
            "CURRENT FOCUS: EXTRACTION QUALITY — discovery is working (drop_rate acceptable).\n"
            "★★★ DO NOT suggest ANY section='discovery' patches. ★★★\n"
            "The problem is that staged courses are missing key fields.\n"
            f"FAILING FIELDS: {failing_str}\n"
            "Suggest ONLY section='recipe' patches targeting the failing fields above.\n"
            "Valid recipe fields: fees.central_page, fees.fees_pdf_url, fees.default_currency,\n"
            "  english.central_page, english.default_ielts, english.default_pte,\n"
            "  filters.domestic_only.enabled, filters.online_only.enabled,\n"
            "  text_cleaning.location.reject_values, text_cleaning.location.strip_patterns,\n"
            "  text_cleaning.duration.split_on_slash, staging.reject_if_missing"
        )
        diag_priority = (
            "DIAGNOSIS PRIORITY (extraction phase — pick the lowest-fill field first):\n"
            f"  Failing: {failing_str}\n"
            "1. fee_pct low     → section='recipe', field='fees.central_page', value='<URL to fee schedule page>'\n"
            "2. ielts_pct low   → section='recipe', field='english.central_page', value='<URL>' OR\n"
            "                     section='recipe', field='english.default_ielts', value=6.0\n"
            "3. location noise  → section='recipe', field='text_cleaning.location.reject_values', value=[<nav labels>]\n"
            "4. mode_pct low    → section='recipe', field='filters.online_only.enabled', value=false\n"
            "5. degree_level low→ note in explanation only (degree level is scraped from page H-tags)\n"
            "6. intakes low     → note in explanation only (intakes are scraped from individual course pages)\n"
            "7. Nothing fixable → return empty patches with a clear explanation"
        )
    else:
        focus_block = (
            "CURRENT FOCUS: Fix URL DISCOVERY — the scraper is not finding enough course pages.\n"
            "Check the drop_rate and dropped URL sample. Fix allow_url_patterns or block_url_patterns.\n"
            "If raw_discovered is very low (< 10), raise bfs_page_budget or enable use_browser.\n"
            "Once discovery is fixed (drop_rate < 20%), the next attempt will switch to extraction."
        )
        diag_priority = (
            "DIAGNOSIS PRIORITY (discovery phase):\n"
            "1. drop_rate > 50% AND dropped_sample non-empty → fix allow_url_patterns or block_url_patterns\n"
            "2. raw_discovered == 0 OR raw_discovered < 5   → increase bfs_page_budget or enable use_browser\n"
            "3. staged > 0 but fee_pct < 40%               → also add a fees.central_page recipe patch\n"
            "4. Otherwise → return empty patches with a clear diagnosis\n"
            "\nHOW TO DERIVE allow_url_patterns:\n"
            "- Look at the dropped URL paths in DROPPED URLs below\n"
            "- Find the common path prefix or pattern across those URLs\n"
            "- Write a regex that matches those paths with re.search()\n"
            "- Example: paths like '/courses/undergraduate/computing-bsc-hons' → '/courses/[^/]+/[^/]+'\n"
            "- Make it broad enough to catch all similar URLs, narrow enough to exclude navigation"
        )

    admin_disc = ctx["admin_config"].get("discovery", {})
    admin_extr = ctx["admin_config"].get("extraction", {})

    return f"""UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

{focus_block}

DISCOVERY STATS:
  raw_discovered={ctx['raw_discovered']}  after_filter={ctx['after_filter']}  staged={ctx['imported']}  drop_rate={ctx['drop_rate']}%  errors={ctx['total_errors']}

DROPPED URLs (incorrectly blocked — look like real course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

PASSED URLs (currently making it through the filter):
{json.dumps(ctx['passed_sample'], indent=2)}

EXTRACTION QUALITY (fill rates for staged courses):
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

{diag_priority}

Return ONLY the JSON object described above. No markdown, no explanation outside the JSON."""


# ── Main loop ─────────────────────────────────────────────────────────────────

def _patch_fingerprint(p: dict) -> str:
    """Return a stable string key for a validated patch dict (section, field, value)."""
    return f"{p.get('section')}:{p.get('field')}:{json.dumps(p.get('value'), sort_keys=True)[:120]}"


async def run_ai_repair_loop(job_id: str, db) -> dict:
    """Run the OpenAI-powered full quality repair loop.

    After each patch: simulates discovery improvement AND runs a real extraction
    scan, evaluates 6-dimensional success criteria, and continues until overall_ok
    or MAX_ATTEMPTS.

    Stage-aware strategy:
    - Phase 1 (discovery): runs while drop_rate > 20%.  Once the simulation or
      initial context shows discovery is OK, _discovery_phase_done is set to True
      and the loop locks into extraction phase for all remaining attempts.
    - Phase 2 (extraction): OpenAI is explicitly told NOT to suggest discovery
      patches; only recipe/extraction patches are accepted.
    - Duplicate detection: a fingerprint of each applied patch is tracked; patches
      with the same (section, field, value) are silently skipped to prevent the
      loop from repeating identical fixes.
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
        "quality_before":  None,
    }
    _write_session(job_id, session)

    try:
        ctx = await _gather_context(job_id, db)
        if not ctx:
            session.update(status="failed", error="Job not found in database.")
            return session

        session["uni_name"]       = ctx["uni_name"]
        quality_baseline          = ctx.get("quality") or {}
        session["quality_before"] = quality_baseline
        _write_session(job_id, session)

        # ── Session-level state ────────────────────────────────────────────────
        # Tracks (section:field:value) fingerprints so duplicates are skipped
        _applied_fingerprints: set[str] = set()
        # True once discovery quality is confirmed acceptable
        _discovery_phase_done: bool = ctx["drop_rate"] <= 20

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            session["current_attempt"] = attempt_num
            _write_session(job_id, session)
            log.info("ai_repair: job=%s attempt=%d/%d phase_done=%s drop_rate=%s%%",
                     job_id, attempt_num, MAX_ATTEMPTS, _discovery_phase_done, ctx["drop_rate"])

            # ① Snapshot quality BEFORE this attempt
            quality_before = await _quality_snapshot(job_id, ctx["university_id"], db)
            quality_before["drop_rate"] = ctx["drop_rate"]

            # ② Determine current repair phase (locked once discovery is done)
            discovery_needs_fix = ctx["drop_rate"] > 20 and not _discovery_phase_done
            phase = "discovery" if discovery_needs_fix else "extraction"

            # ③ Call OpenAI
            user_msg = _build_user_message(ctx, session["attempts"], phase=phase)
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

            # ④ Validate patches → (disc_patch, extr_patch, errors)
            #    extr_patch is already a NESTED dict (dotpath fields expanded)
            patches_raw: list[dict] = ai_data.get("patches") or []

            # ── Duplicate-patch filter ────────────────────────────────────────
            # Skip patches whose (section, field, value) fingerprint was already
            # applied in a previous attempt to prevent the loop from repeating
            # identical or near-identical fixes.
            dup_skipped: list[str] = []
            deduped_patches: list[dict] = []
            for p in patches_raw:
                fp = _patch_fingerprint(p)
                if fp in _applied_fingerprints:
                    dup_skipped.append(f"{p.get('section')}.{p.get('field')}")
                    log.info("ai_repair: skipping duplicate patch %s.%s", p.get("section"), p.get("field"))
                else:
                    deduped_patches.append(p)
            patches_raw = deduped_patches

            # ── Extraction-phase guard ────────────────────────────────────────
            # When discovery is confirmed done, strip any pure-discovery patches
            # OpenAI may still suggest; only extraction fixes are meaningful now.
            disc_only_blocked: list[str] = []
            if phase == "extraction":
                allowed, blocked = [], []
                for p in patches_raw:
                    if p.get("section") == "discovery":
                        blocked.append(f"discovery.{p.get('field')}")
                    else:
                        allowed.append(p)
                patches_raw = allowed
                disc_only_blocked = blocked
                if blocked:
                    log.info("ai_repair: blocked discovery patches in extraction phase: %s", blocked)

            disc_patch, extr_patch, validation_errors = _validate_and_build_config_patch(patches_raw)
            if dup_skipped:
                validation_errors.append(f"Duplicate patches skipped: {', '.join(dup_skipped)}")
            if disc_only_blocked:
                validation_errors.append(
                    f"Discovery patches blocked (extraction phase active): {', '.join(disc_only_blocked)}"
                )

            # ⑤ URL filter simulation (discovery patches only — no live re-scrape needed)
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
                # Update ctx drop_rate immediately so next iteration uses the improved value
                if sim.get("after", 0) > 0:
                    _raw      = ctx["raw_discovered"]
                    _new_after = ctx["after_filter"] + sim["after"]
                    ctx["drop_rate"] = max(0, round(100 * (1 - _new_after / _raw))) if _raw > 0 else 0
                    if ctx["drop_rate"] <= 20:
                        _discovery_phase_done = True
                        log.info(
                            "ai_repair: discovery phase complete — drop_rate now %s%% after simulation",
                            ctx["drop_rate"],
                        )

            # ⑥ Apply discovery patch
            patch_applied_ok  = False
            patch_error: str  = ""
            extr_patch_applied_keys: list = []
            if disc_patch:
                try:
                    await _apply_discovery_to_db(ctx["university_id"], disc_patch, db)
                    _apply_to_yaml(ctx.get("yaml_file"), ctx["unis_dir"],
                                   ctx["university_id"], ctx["scrape_url"],
                                   {"discovery": disc_patch})
                    patch_applied_ok = True
                    # Register fingerprints so this exact patch is not repeated
                    for p in patches_raw:
                        if p.get("section") == "discovery":
                            _applied_fingerprints.add(_patch_fingerprint(p))
                    log.info("ai_repair: discovery patch applied: %s", list(disc_patch.keys()))
                except Exception as exc:
                    log.warning("ai_repair: discovery patch apply error: %s", exc)
                    patch_error = str(exc)

            # ⑦ Apply extraction patch (dotpath fields already expanded to nested dict)
            if extr_patch:
                try:
                    await _apply_recipe_to_db(ctx["university_id"], extr_patch, db)
                    extr_patch_applied_keys = _flatten_dotpaths(extr_patch)
                    patch_applied_ok = True
                    # Register fingerprints
                    for p in patches_raw:
                        if p.get("section") == "recipe":
                            _applied_fingerprints.add(_patch_fingerprint(p))
                    log.info("ai_repair: extraction patch applied: %s", extr_patch_applied_keys)
                except Exception as exc:
                    log.warning("ai_repair: extraction patch apply error: %s", exc)
                    if not patch_error:
                        patch_error = str(exc)

            # ⑧ Real extraction scan:
            #    Phase 1 — SQL fast-path (default_ielts fills, reject_values clears)
            #    Phase 2 — re-fetch up to 8 staged course detail URLs with patched config
            config_patch_combined: dict = {}
            if disc_patch:
                config_patch_combined["discovery"] = disc_patch
            if extr_patch:
                config_patch_combined["extraction"] = extr_patch
            scan_fills = await _run_extraction_scan(ctx, config_patch_combined, db, max_courses=8)

            # ⑨ Real quality snapshot AFTER the extraction scan
            quality_after = await _quality_snapshot(job_id, ctx["university_id"], db)
            if sim.get("total", 0) > 0:
                _raw   = ctx["raw_discovered"]
                _after = ctx["after_filter"] + sim.get("after", 0)
                quality_after["drop_rate"] = (
                    max(0, round(100 * (1 - _after / _raw))) if _raw > 0
                    else quality_before.get("drop_rate", 0)
                )
            else:
                quality_after["drop_rate"] = quality_before.get("drop_rate", 0)

            # predicted_fills: qualitative flags for UI chips (e.g. fees_central_page_set)
            _, predicted_fills = await _predict_quality(ctx, config_patch_combined, sim, db)
            predicted_fills.update({k: v for k, v in scan_fills.items() if v and k not in predicted_fills})
            for _real_key, _ui_key in [
                ("ielts_fills",     "ielts_fills"),
                ("fee_fills",       "fee_fills"),
                ("location_clears", "location_junk_removed"),
            ]:
                if scan_fills.get(_real_key):
                    predicted_fills[_ui_key] = scan_fills[_real_key]

            # ⑩ Evaluate success using REAL quality_after (not predicted)
            success_criteria = _evaluate_success(quality_after, sim, predicted_fills, ctx)
            quality_delta    = _compute_delta(quality_before, quality_after)
            crit_pass        = success_criteria["criteria_pass"]
            overall          = success_criteria["overall_ok"]

            attempt_record: dict = {
                "attempt_number":       attempt_num,
                "phase":                phase,
                "diagnosis":            ai_data.get("diagnosis", "Unknown"),
                "root_cause":           ai_data.get("root_cause", "unknown"),
                "confidence":           ai_data.get("confidence", 0),
                "explanation":          ai_data.get("explanation", ""),
                "patches_applied":      [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches_raw
                    if p.get("section") in _ALLOWED_SECTIONS
                    and (
                        p.get("field") in _ALLOWED_DISCOVERY_FIELDS
                        or p.get("field") in _ALLOWED_RECIPE_FIELDS
                        or p.get("field") in _ALLOWED_EXTRACTION_FIELDS
                    )
                ],
                "validation_errors":    validation_errors,
                "before_pass_count":    sim["before"],
                "after_pass_count":     sim["after"],
                "total_test_urls":      sim["total"],
                "rescued_sample":       sim["rescued"],
                "patch_applied_ok":     patch_applied_ok,
                "patch_error":          patch_error if patch_error else None,
                "recipe_patch_applied": extr_patch_applied_keys,
                "quality_before":       quality_before,
                "quality_after":        quality_after,
                "quality_delta":        quality_delta,
                "predicted_fills":      predicted_fills,
                "success_criteria":     success_criteria,
                "courses_rescanned":    scan_fills.get("courses_rescanned", 0),
            }
            session["attempts"].append(attempt_record)
            _write_session(job_id, session)

            urls_rescued_enough = sim["total"] > 0 and sim["after"] >= sim["total"] * 0.5
            has_url_patch       = bool("allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch)
            discovery_now_ok    = (
                _discovery_phase_done
                or urls_rescued_enough
                or (not has_url_patch and ctx["drop_rate"] < 20)
            )
            extraction_ok = _extraction_quality_ok(quality_after)

            # Lock into extraction phase if discovery has been confirmed OK
            if discovery_now_ok and not _discovery_phase_done:
                _discovery_phase_done = True
                ctx["drop_rate"] = 0
                log.info("ai_repair: discovery confirmed OK — locking into extraction phase")

            # ⑪ Termination checks
            if not patches_raw and not dup_skipped and not disc_only_blocked:
                if discovery_now_ok and extraction_ok:
                    verdict = "No issues detected — discovery and extraction quality are both acceptable."
                elif discovery_now_ok:
                    verdict = (
                        f"OpenAI could not identify an extraction fix after {attempt_num} attempt(s). "
                        f"Discovery is working. Extraction quality ({_quality_summary(quality_after)}) "
                        f"may require manual recipe configuration. "
                        f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}"
                    )
                else:
                    verdict = (
                        f"OpenAI could not identify an automatic fix after {attempt_num} attempt(s). "
                        f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}. Manual config review recommended."
                    )
                session.update(status="completed", final_verdict=verdict)
                break

            if extr_patch and not disc_patch:
                extraction_note = (
                    "Extraction quality is acceptable."
                    if extraction_ok
                    else (
                        f"Extraction quality was poor ({_quality_summary(quality_after)}). "
                        "The extraction patch targets this — re-run the scrape to measure improvement."
                    )
                )
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Extraction patch applied ({', '.join(extr_patch_applied_keys or ['(nested)'])}). "
                        f"{extraction_note} "
                        "Re-run a full scrape to confirm extraction improvements."
                    ),
                )
                break

            if overall:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"All quality targets met — {crit_pass}/6 criteria pass on real "
                        f"post-scan data ({scan_fills.get('courses_rescanned', 0)} courses re-extracted). "
                        "Re-run the full scrape to propagate improvements across all courses."
                    ),
                )
                break

            if urls_rescued_enough and not extraction_ok:
                log.info(
                    "ai_repair: discovery fixed but extraction quality poor (%s) — continuing to extraction phase",
                    _quality_summary(quality_after),
                )
                ctx["quality"]          = quality_after
                ctx["drop_rate"]        = 0
                _discovery_phase_done   = True
                if attempt_num == MAX_ATTEMPTS:
                    session.update(
                        status="completed",
                        final_verdict=(
                            f"DISCOVERY FILTER FIXED ({sim['after']}/{sim['total']} URLs rescued). "
                            f"EXTRACTION RULES PROBLEM: key fields still not filling. "
                            f"{_quality_summary(quality_after)}. "
                            "Re-run a full scrape and use the Recipe Editor to fix fee/IELTS/location extraction."
                        ),
                    )
                continue

            if attempt_num == MAX_ATTEMPTS:
                # Build a specific verdict that names the root problem
                if not _discovery_phase_done and ctx["raw_discovered"] < 10:
                    problem_type = (
                        f"DISCOVERY DEPTH PROBLEM: only {ctx['raw_discovered']} pages were crawled. "
                        "Raise bfs_page_budget (try 80–150) or enable use_browser for JS-rendered sites."
                    )
                elif not _discovery_phase_done:
                    problem_type = (
                        f"DISCOVERY FILTER PROBLEM: drop_rate={ctx['drop_rate']}% after {attempt_num} attempts. "
                        "The allow_url_patterns regex is not matching the course URL structure. "
                        "Manually inspect a few course URLs and update the YAML allow_url_patterns."
                    )
                else:
                    problem_type = (
                        f"EXTRACTION RULES PROBLEM: discovery OK but key fields are not filling. "
                        f"Failing: {_failing_criteria_str(success_criteria)}. "
                        "Check the Recipe Editor for fee/IELTS/location extraction config."
                    )
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Reached maximum {MAX_ATTEMPTS} attempts. "
                        f"{crit_pass}/6 quality criteria passing. "
                        f"{problem_type}"
                    ),
                )

            ctx["quality"] = quality_after

    except Exception as exc:
        log.exception("ai_repair: unexpected error job=%s: %s", job_id, exc)
        session.update(status="failed", error=str(exc))
    finally:
        session["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_session(job_id, session)

    return session


def _flatten_dotpaths(nested: dict, prefix: str = "") -> list[str]:
    """Flatten a nested dict back to dotpath keys for display purposes."""
    result: list[str] = []
    for k, v in nested.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.extend(_flatten_dotpaths(v, full))
        else:
            result.append(full)
    return result


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

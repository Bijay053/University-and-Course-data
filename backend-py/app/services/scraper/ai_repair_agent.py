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

    return discovery_patch, recipe_patch, errors


def _validate_and_build_config_patch(patches: list[dict]) -> tuple[dict, dict, list[str]]:
    """Validate all patches and build separate discovery and recipe patch dicts.

    Returns (discovery_patch, recipe_patch, errors).
    Patches that fail validation are skipped (not applied) and their
    error message is recorded.
    """
    discovery_patch: dict = {}
    recipe_patch: dict = {}
    errors: list[str] = []
    for p in patches:
        section = p.get("section", "")
        field   = p.get("field",   "")
        value   = p.get("value")
        try:
            validated = _validate_patch(p)
            section = validated["section"]
            field   = validated["field"]
            value   = validated["value"]
            if section == "discovery":
                discovery_patch[field] = value
            else:
                recipe_patch[field] = value
        except PatchValidationError as exc:
            errors.append(f"{section}.{field}: {exc}")
            log.warning("ai_repair: patch rejected: %s", exc)

    return discovery_patch, recipe_patch, errors


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
    # Skip if patch doesn't touch anything that benefits from a live fetch
    needs_fetch = bool(extr_patch.get("fees") or extr_patch.get("english") or extr_patch.get("filters"))
    if not needs_fetch:
        log.info("extraction_scan: no live-fetch-sensitive patches; skipping fetch phase")
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

    # Pick courses with worst quality first (NULL fee OR NULL IELTS)
    sample_rows = (await db.execute(text("""
        SELECT id, course_url, international_fee, ielts_overall, study_mode
        FROM   scraped_courses
        WHERE  university_id = :uid
          AND  scrape_job_id = :jid
          AND  status IN ('pending','review','approved')
          AND  course_url IS NOT NULL
          AND  (international_fee IS NULL OR ielts_overall IS NULL)
        ORDER BY id
        LIMIT  :n
    """), {**_base_params, "n": max_courses})).mappings().all()

    fills["courses_rescanned"] = len(sample_rows)

    for row in sample_rows:
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

Return ONLY a valid JSON object - no markdown, no text outside the JSON.

Schema:
{
  "diagnosis": "string - one sentence describing the root problem",
  "root_cause": "string - one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | fee_extraction | location_extraction | ielts_extraction | intake_extraction | mode_extraction | unknown",
  "confidence": number - integer 0-100,
  "explanation": "string - 2-3 sentences explaining why this fix will work",
  "patches": [
    {
      "section": "string - 'discovery' for URL/crawl fixes | 'recipe' for extraction data-cleaning fixes",
      "field": "string - see allowed fields below",
      "action": "string - 'replace'",
      "value": <appropriate type for the field>
    }
  ]
}
    quality_str = _quality_summary(ctx.get("quality"))

    if phase == "extraction":
        focus_block = (
            "CURRENT FOCUS: Discovery is working - concentrate on EXTRACTION QUALITY.\n"
            "The drop_rate is acceptable. The problem is that staged courses are missing key fields.\n"
            "Suggest recipe patches (section='recipe') to fix fee extraction, location noise,\n"
            "IELTS/English data, intake months, study mode, or course name contamination.\n"
            "You may also include a discovery patch if relevant, but extraction is the priority."
        )
        diag_priority = (
            "DIAGNOSIS PRIORITY (extraction phase):\n"
            "1. fee_pct < 40%  → suggest fee_source_urls pointing to the university fee schedule page\n"
            "2. location_pct < 40% OR location values look like nav-text → suggest location_reject_values\n"
            "3. mode_pct < 40% → suggest study_mode_online_keywords if online courses exist\n"
            "4. degree_level_pct < 40% → suggest course_name_remove_after to strip suffixes\n"
            "5. ielts_pct < 40% → note in explanation (IELTS is usually scraped from course pages, not recipe-fixable)\n"
            "6. intakes_pct < 40% → note in explanation (intake months are usually scraped from course pages)\n"
            "7. duration_pct < 40% → note in explanation (duration is usually scraped from course pages)\n"
            "8. If nothing is recipe-fixable, return empty patches with a clear explanation"
        )
    else:
        focus_block = (
            "CURRENT FOCUS: Fix URL DISCOVERY - the scraper is not finding enough course pages.\n"
            "Check the drop_rate and dropped URL sample. Fix allow_url_patterns or block_url_patterns first."
        )
        diag_priority = (
            "DIAGNOSIS PRIORITY (discovery phase):\n"
            "1. drop_rate > 50% AND dropped sample non-empty → fix allow_url_patterns or block_url_patterns\n"
            "2. raw_discovered == 0 OR raw_discovered < 5 → increase bfs_page_budget or enable use_browser\n"
            "3. staged > 0 but fee_pct < 40% → also suggest a fee_source_urls recipe patch\n"
            "4. Otherwise → return empty patches with a clear diagnosis\n"
            "\nHOW TO DERIVE allow_url_patterns:\n"
            "- Look at the dropped URL paths above\n"
            "- Find the common path prefix or pattern\n"
            "- Write a regex that matches those paths with re.search()\n"
            "- Example: dropped paths like \"/courses/undergraduate/computing-bsc-hons\" → pattern \"/courses/[^/]+/[^/]+\"\n"
            "- Make the pattern broad enough to catch all similar URLs but not so broad it catches non-course pages"
        )

    admin_disc = ctx["admin_config"].get("discovery", {})
    admin_extr = ctx["admin_config"].get("extraction", {})

    return f"""UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

{focus_block}

DISCOVERY STATS:
  raw_discovered={ctx['raw_discovered']}  after_filter={ctx['after_filter']}  staged={ctx['imported']}  drop_rate={ctx['drop_rate']}%  errors={ctx['total_errors']}

DROPPED URLs (incorrectly blocked - look like real course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

PASSED URLs (currently making it through the filter):
{json.dumps(ctx['passed_sample'], indent=2)}

EXTRACTION QUALITY (fill rates for staged courses):
{quality_str}
  sample_locations:     {json.dumps(q.get('sample_locations', []))}
  sample_degree_levels: {json.dumps(q.get('sample_degrees', []))}
  sample_study_modes:   {json.dumps(q.get('sample_modes', []))}

    """Run the OpenAI-powered repair loop. Writes progress to Redis after every attempt.

ADMIN_CONFIG (database overrides):
{json.dumps(ctx['admin_config'], indent=2)}

YAML CONFIG (file on disk):
{ctx['yaml_content'] or "(empty — no YAML file found)"}
{prev_block}

{diag_priority}

Return ONLY the JSON object described above. No markdown, no explanation outside the JSON."""
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

        session["uni_name"] = ctx["uni_name"]
        quality_baseline = ctx.get("quality") or {}
        session["quality_before"] = quality_baseline
        _write_session(job_id, session)

        # Carry forward the "before" quality for each attempt
        quality_before_attempt = quality_baseline

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            session["current_attempt"] = attempt_num
            _write_session(job_id, session)
            log.info("ai_repair: job=%s attempt=%d/%d", job_id, attempt_num, MAX_ATTEMPTS)

            # Determine current repair phase
            discovery_needs_fix = (
                ctx["drop_rate"] > 20
            disc_patch, recipe_patch, validation_errors = _validate_and_build_config_patch(patches_raw)

            # Simulate URL filter change (only for allow/block pattern changes)
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
            # Determine current repair phase
            discovery_needs_fix = (
                ctx["drop_rate"] > 20
            phase = "discovery" if discovery_needs_fix else "extraction"

            # ⑥ Real extraction scan:
            #    Phase 1 — SQL fast-path (default_ielts fills, reject_values clears)
            #    Phase 2 — re-fetch up to 5 staged course URLs with patched config
            scan_fills = await _run_extraction_scan(ctx, config_patch, db)

            # ⑦ Real quality snapshot AFTER the extraction scan
            quality_after = await _quality_snapshot(job_id, ctx["university_id"], db)
            # Drop-rate: use URL simulation for discovery patches; carry forward otherwise
            # (we cannot re-run full discovery inline — the simulation is the best proxy)
            if sim.get("total", 0) > 0:
                _raw   = ctx["raw_discovered"]
                _after = ctx["after_filter"] + sim.get("after", 0)
                quality_after["drop_rate"] = (
                    max(0, round(100 * (1 - _after / _raw))) if _raw > 0
                    else quality_before.get("drop_rate", 0)
                )
            else:
                quality_after["drop_rate"] = quality_before.get("drop_rate", 0)

            # predicted_fills: qualitative flags for UI chips that take effect on the
            # next full scrape (e.g. fees_central_page_set, online_only_disabled).
            # Merge real scan counts on top so the UI shows actual fill numbers.
            _, predicted_fills = await _predict_quality(ctx, config_patch, sim, db)
            predicted_fills.update({
                k: v for k, v in scan_fills.items()
                if v and k not in predicted_fills
            })
            # Real fill counts always win over estimates
            for _real_key, _ui_key in [
                ("ielts_fills",     "ielts_fills"),
                ("fee_fills",       "fee_fills"),
                ("location_clears", "location_junk_removed"),
            ]:
                if scan_fills.get(_real_key):
                    predicted_fills[_ui_key] = scan_fills[_real_key]

            # ⑧ Evaluate success using REAL quality_after (not predicted)
            success_criteria = _evaluate_success(quality_after, sim, predicted_fills, ctx)
            quality_delta    = _compute_delta(quality_before, quality_after)

            attempt_record: dict = {
                "attempt_number":     attempt_num,
                "phase":              phase,
                "diagnosis":          ai_data.get("diagnosis", "Unknown issue"),
                "root_cause":         ai_data.get("root_cause", "unknown"),
                "confidence":         ai_data.get("confidence", 0),
                "explanation":        ai_data.get("explanation", ""),
                "patches_applied":    [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches_raw
                    if p.get("section") in _ALLOWED_SECTIONS
                    and (
                        p.get("field") in _ALLOWED_DISCOVERY_FIELDS
                        or p.get("field") in _ALLOWED_RECIPE_FIELDS
                    )
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
                # Quality tracking — quality_after is REAL (from DB after scan)
                "quality_before":    quality_before,
                "quality_after":     quality_after,
                "quality_delta":     quality_delta,
                "predicted_fills":   predicted_fills,
                "success_criteria":  success_criteria,
                "courses_rescanned": scan_fills.get("courses_rescanned", 0),
            }
            session["attempts"].append(attempt_record)

            # Apply discovery patch
            if disc_patch:
                try:
                    await _apply_discovery_to_db(ctx["university_id"], disc_patch, db)
                    _apply_to_yaml(
                        ctx.get("yaml_file"),
                        ctx["unis_dir"],
                        ctx["university_id"],
                        ctx["scrape_url"],
                        disc_patch,
                    )
                    attempt_record["patch_applied_ok"] = True
                    log.info("ai_repair: discovery patch applied: %s", list(disc_patch.keys()))
                except Exception as exc:
                    log.warning("ai_repair: discovery patch apply error: %s", exc)
                    attempt_record["patch_error"] = str(exc)

            # Apply recipe patch
            if recipe_patch:
                try:
                    await _apply_recipe_to_db(ctx["university_id"], recipe_patch, db)
                    attempt_record["patch_applied_ok"] = True
                    attempt_record["recipe_patch_applied"] = list(recipe_patch.keys())
                    log.info("ai_repair: recipe patch applied: %s", list(recipe_patch.keys()))
                except Exception as exc:
                    log.warning("ai_repair: recipe patch apply error: %s", exc)
                    attempt_record["recipe_patch_error"] = str(exc)

            # Re-query extraction quality for before/after comparison
            quality_after = await _requery_quality(ctx["university_id"], job_id, db)
            attempt_record["quality_after"] = quality_after
            _write_session(job_id, session)

            # ── Termination checks ────────────────────────────────────────────

            has_url_patch = bool("allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch)
            urls_rescued_enough = (
                has_url_patch
                and sim["total"] > 0
                and sim["after"] >= sim["total"] * 0.5
            )
            discovery_now_ok = urls_rescued_enough or (
                not has_url_patch and ctx["drop_rate"] < 20
            )
            extraction_ok = _extraction_quality_ok(quality_after)
            no_patches = not patches_raw

            if no_patches:
                if discovery_now_ok and extraction_ok:
                    session.update(
                        status="completed",
                        final_verdict="No issues detected - discovery and extraction quality are both acceptable.",
                    )
                elif discovery_now_ok and not extraction_ok:
                    session.update(
                        status="completed",
                        final_verdict=(
                            f"OpenAI could not identify an extraction fix after {attempt_num} attempt(s). "
                            f"Discovery is working. Extraction quality ({_quality_summary(quality_after)}) "
                            "may require manual recipe configuration. "
                            f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}"
                        ),
                    )
                else:
                    session.update(
                        status="completed",
                        final_verdict=(
                            f"OpenAI could not identify an automatic fix after {attempt_num} attempt(s). "
                            f"Diagnosis: {ai_data.get('diagnosis', 'N/A')}. Manual config review recommended."
                        ),
                    )
                break

            if recipe_patch and not disc_patch:
                # Pure recipe patch - recipe changes need a re-scrape to verify quality improvement
                recipe_fields = ", ".join(recipe_patch.keys())
                extraction_note = (
                    "Extraction quality is acceptable."
                    if extraction_ok
                    else (
                        f"Extraction quality was poor ({_quality_summary(quality_after)}). "
                        "The recipe patch targets this - re-run the scrape to measure improvement."
                    )
                )
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Recipe patch applied ({recipe_fields}). "
                        f"{extraction_note} "
                        "Re-run a full scrape to confirm extraction improvements."
                    ),
                )
                break

            if urls_rescued_enough:
                if extraction_ok:
                    session.update(
                        status="completed",
                        final_verdict=(
                            f"Discovery fix applied - {sim['after']}/{sim['total']} previously-dropped URLs "
                            f"now pass the new filter. Extraction quality is also acceptable ({_quality_summary(quality_after)}). "
                            "Re-run a full scrape to confirm."
                        ),
                    )
                    break
                else:
                    # Discovery fixed but extraction is still poor → continue to extraction phase
                    log.info(
                        "ai_repair: discovery fixed but extraction quality poor (%s) - continuing to extraction phase",
                        _quality_summary(quality_after),
                    )
                    # Update context quality for next attempt so the AI sees current state
                    ctx["quality"] = quality_after
                    ctx["drop_rate"] = 0  # Signal that discovery is no longer the problem
                    quality_before_attempt = quality_after
                    if attempt_num == MAX_ATTEMPTS:
                        session.update(
                            status="completed",
                            final_verdict=(
                                f"Discovery fix applied ({sim['after']}/{sim['total']} URLs rescued). "
                                f"Extraction quality remains poor: {_quality_summary(quality_after)}. "
                                "Re-run a full scrape and check the Recipe Editor for extraction improvements."
                            ),
                        )
                    continue

            if not has_url_patch and ctx["drop_rate"] < 20:
                # No URL problem, non-recipe patch applied
                if extraction_ok:
                    session.update(
                        status="completed",
                        final_verdict=(
                            "Config patch applied. Discovery and extraction quality are both acceptable. "
                            "Re-run a scrape to verify the changes."
                        ),
                    )
                else:
                    session.update(
                        status="completed",
                        final_verdict=(
                            "Config patch applied. "
                            f"Extraction quality: {_quality_summary(quality_after)}. "
                            "Re-run a scrape to verify - or use the Recipe Editor to tune extraction rules."
                        ),
                    )
                break
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

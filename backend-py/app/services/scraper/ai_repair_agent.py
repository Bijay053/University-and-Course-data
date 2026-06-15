"""AI-powered scrape repair agent — OpenAI edition (discovery + extraction).

Uses the Replit AI Integrations OpenAI proxy (gpt-5.4) to iteratively
diagnose failing scrape jobs and apply config patches — both discovery
(URL filters, BFS budget, browser flag) and extraction (fees, IELTS,
location cleanup, filters, duration, staging) — until quality improves or
MAX_ATTEMPTS is reached.

Loop (per attempt):
  1. Gather context  — job stats, dropped URLs, current config, staging quality
  2. Call OpenAI     — json_object mode → structured diagnosis + patch list
  3. Validate patch  — strict whitelist per section/field, type check, regex check
  4. Apply patches   — writes to admin_config (DB) + YAML file on disk
  5. Simulate filter — URL filter simulation for discovery patches
  6. Record attempt  — stored in Redis (key ``ai_repair:{job_id}``, TTL 24 h)
  7. Terminate if improvement threshold met or MAX_ATTEMPTS reached

Session state is stored in Redis as JSON under key ``ai_repair:{job_id}``.
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


def _set_dotpath(d: dict, dotpath: str, value: Any) -> dict:
    """Write *value* into nested dict *d* at *dotpath* (e.g. 'fees.central_page').

    Returns a new nested dict fragment suitable for deep-merging into config.
    Example: _set_dotpath({}, 'text_cleaning.location.reject_values', ['Fees'])
             → {'text_cleaning': {'location': {'reject_values': ['Fees']}}}
    """
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
    "allow_url_patterns": list,
    "block_url_patterns": list,
    "must_contain":       list,
    "bfs_page_budget":    int,
    "use_browser":        bool,
    "sitemap_url":        str,
}

# Extraction field paths: dotpath → validator_tag
_ALLOWED_EXTRACTION_FIELDS: dict[str, str] = {
    # ── fees ──────────────────────────────────────────────────────────
    "fees.central_page":             "url_or_null",
    "fees.fees_pdf_url":             "url_or_null",
    "fees.default_currency":         "iso_currency",
    "fees.credit_points_per_unit":   "int_1_200_or_null",

    # ── english / IELTS ───────────────────────────────────────────────
    "english.central_page":          "url_or_null",
    "english.requirements_pdf_url":  "url_or_null",
    "english.trust_vision_ocr":      "bool",
    "english.default_ielts":         "ielts_score_or_null",
    "english.default_pte":           "pte_score_or_null",
    "english.default_toefl":         "toefl_score_or_null",

    # ── filters (study mode / domestic / online) ───────────────────────
    "filters.domestic_only.enabled": "bool",
    "filters.online_only.enabled":   "bool",

    # ── location cleanup ──────────────────────────────────────────────
    "text_cleaning.location.strip_patterns": "regex_list",
    "text_cleaning.location.reject_values":  "str_list",
    "text_cleaning.location.allowed_values": "str_list",

    # ── duration ──────────────────────────────────────────────────────
    "text_cleaning.duration.split_on_slash": "bool",

    # ── staging gate ──────────────────────────────────────────────────
    "staging.reject_if_missing": "known_field_list",
}

_KNOWN_STAGING_FIELDS = {
    "course_name", "degree_level", "category", "study_mode", "course_location",
    "duration", "intake_months", "international_fee", "description",
    "academic_level", "academic_score", "english_test", "other_requirement",
}

_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_IELTS_RANGE  = (4.0, 9.0)
_PTE_RANGE    = (30, 90)
_TOEFL_RANGE  = (30, 120)


class PatchValidationError(ValueError):
    """Raised when an AI patch fails strict validation."""


def _validate_discovery_patch(field: str, value: Any) -> None:
    if field not in _ALLOWED_DISCOVERY_FIELDS:
        raise PatchValidationError(
            f"Discovery field '{field}' is not allowed. "
            f"Allowed: {sorted(_ALLOWED_DISCOVERY_FIELDS)}"
        )
    expected = _ALLOWED_DISCOVERY_FIELDS[field]
    if not isinstance(value, expected):
        raise PatchValidationError(
            f"discovery.{field} must be {expected.__name__}, got {type(value).__name__}."
        )
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
    if field == "bfs_page_budget" and not (5 <= value <= 300):
        raise PatchValidationError(f"'bfs_page_budget' must be 5–300, got {value}.")
    if field == "sitemap_url" and not value.startswith(("http://", "https://")):
        raise PatchValidationError(f"'sitemap_url' must start with http(s)://, got {value!r}.")


def _validate_extraction_patch(field: str, value: Any) -> None:
    tag = _ALLOWED_EXTRACTION_FIELDS.get(field)
    if tag is None:
        raise PatchValidationError(
            f"Extraction field '{field}' is not in the allowed set. "
            f"Allowed paths: {sorted(_ALLOWED_EXTRACTION_FIELDS)}"
        )

    if tag == "bool":
        if not isinstance(value, bool):
            raise PatchValidationError(f"extraction.{field} must be bool, got {type(value).__name__}.")

    elif tag == "url_or_null":
        if value is not None:
            if not isinstance(value, str):
                raise PatchValidationError(f"extraction.{field} must be a URL string or null.")
            if not value.startswith(("http://", "https://")):
                raise PatchValidationError(f"extraction.{field} must start with http(s)://.")
            if len(value) > 2048:
                raise PatchValidationError(f"extraction.{field} URL is suspiciously long.")

    elif tag == "iso_currency":
        if not isinstance(value, str) or not _ISO_CURRENCY_RE.match(value):
            raise PatchValidationError(
                f"extraction.{field} must be a 3-letter ISO 4217 code (e.g. AUD), got {value!r}."
            )

    elif tag == "int_1_200_or_null":
        if value is not None:
            if not isinstance(value, int):
                raise PatchValidationError(f"extraction.{field} must be int or null, got {type(value).__name__}.")
            if not (1 <= value <= 200):
                raise PatchValidationError(f"extraction.{field} must be 1–200, got {value}.")

    elif tag == "ielts_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)):
                raise PatchValidationError(f"extraction.{field} must be a number or null.")
            lo, hi = _IELTS_RANGE
            if not (lo <= float(value) <= hi):
                raise PatchValidationError(f"extraction.{field} IELTS score must be {lo}–{hi}, got {value}.")

    elif tag == "pte_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)):
                raise PatchValidationError(f"extraction.{field} must be a number or null.")
            lo, hi = _PTE_RANGE
            if not (lo <= int(value) <= hi):
                raise PatchValidationError(f"extraction.{field} PTE score must be {lo}–{hi}, got {value}.")

    elif tag == "toefl_score_or_null":
        if value is not None:
            if not isinstance(value, (int, float)):
                raise PatchValidationError(f"extraction.{field} must be a number or null.")
            lo, hi = _TOEFL_RANGE
            if not (lo <= int(value) <= hi):
                raise PatchValidationError(f"extraction.{field} TOEFL score must be {lo}–{hi}, got {value}.")

    elif tag == "regex_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be a list of regex strings.")
        for i, pat in enumerate(value):
            if not isinstance(pat, str):
                raise PatchValidationError(f"extraction.{field}[{i}] must be a string.")
            if len(pat) > 500:
                raise PatchValidationError(f"extraction.{field}[{i}] is too long (>500 chars).")
            try:
                re.compile(pat)
            except re.error as exc:
                raise PatchValidationError(
                    f"extraction.{field}[{i}] is not a valid regex: {exc}  (pattern: {pat!r})"
                ) from exc

    elif tag == "str_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be a list of strings.")
        for i, v in enumerate(value):
            if not isinstance(v, str):
                raise PatchValidationError(f"extraction.{field}[{i}] must be a string, got {type(v).__name__}.")
            if len(v) > 500:
                raise PatchValidationError(f"extraction.{field}[{i}] is too long (>500 chars).")

    elif tag == "known_field_list":
        if not isinstance(value, list):
            raise PatchValidationError(f"extraction.{field} must be a list of field names.")
        for v in value:
            if v not in _KNOWN_STAGING_FIELDS:
                raise PatchValidationError(
                    f"'{v}' is not a known staging field. "
                    f"Allowed: {sorted(_KNOWN_STAGING_FIELDS)}"
                )


def _validate_and_build_config_patch(patches: list[dict]) -> tuple[dict, list[str]]:
    """Validate all patches and build a nested config_patch dict.

    Returns (config_patch, validation_errors).
    Invalid patches are skipped and their error recorded.
    """
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
                nested = _set_dotpath({}, field, value)
                config_patch["extraction"] = _deep_merge(
                    config_patch.get("extraction", {}), nested
                )

            else:
                raise PatchValidationError(
                    f"Section '{section}' is not allowed. Must be 'discovery' or 'extraction'."
                )

        except PatchValidationError as exc:
            errors.append(f"{section}.{field}: {exc}")
            log.warning("ai_repair: patch rejected: %s", exc)

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


# ── OpenAI prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert web scraping engineer specialising in university course scrapers.
Your job is to analyse a failing or low-quality scrape job and return the most impactful fix.

You MUST return a valid JSON object with NO markdown, NO text outside the JSON.

The JSON must follow this exact schema:
{
  "diagnosis": "string — one sentence describing the root problem",
  "root_cause": "string — one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | fees | english | location | study_mode | filters | duration | unknown",
  "confidence": number — integer 0-100,
  "explanation": "string — 2-3 sentences explaining why this fix will work",
  "patches": [
    {
      "section": "string — 'discovery' or 'extraction'",
      "field": "string — dot-notation field path (see below)",
      "action": "replace",
      "value": <the new value — see types below>
    }
  ]
}

ALLOWED DISCOVERY PATCHES (section = "discovery"):
  field                   value type            notes
  allow_url_patterns      list[str]            Valid Python regexes matched with re.search()
  block_url_patterns      list[str]            Valid Python regexes matched with re.search()
  must_contain            list[str]            Strings that must appear in the URL
  bfs_page_budget         integer 5-300        Max pages to crawl for discovery
  use_browser             boolean              true to enable JS rendering
  sitemap_url             string (https://..)  Override sitemap URL

ALLOWED EXTRACTION PATCHES (section = "extraction"):
  field                                    value type                     notes
  fees.central_page                        string (URL) or null           Fee schedule page URL
  fees.fees_pdf_url                        string (URL) or null           Fee schedule PDF URL
  fees.default_currency                    string (3-letter ISO)          e.g. "GBP", "AUD", "USD"
  fees.credit_points_per_unit              integer 1-200 or null          Multiply per-unit fee by this
  english.central_page                     string (URL) or null           English requirements page URL
  english.requirements_pdf_url             string (URL) or null           English requirements PDF URL
  english.trust_vision_ocr                 boolean                        false = disable OCR hallucination
  english.default_ielts                    number 4.0-9.0 or null         Institutional default IELTS
  english.default_pte                      integer 30-90 or null          Institutional default PTE
  english.default_toefl                    integer 30-120 or null         Institutional default TOEFL
  filters.domestic_only.enabled            boolean                        false = allow domestic-only courses
  filters.online_only.enabled              boolean                        false = allow online-only courses
  text_cleaning.location.strip_patterns    list[str] (valid regexes)      Strip these from raw location strings
  text_cleaning.location.reject_values     list[str]                      Clear location if it contains these
  text_cleaning.location.allowed_values    list[str]                      Only allow locations containing these
  text_cleaning.duration.split_on_slash    boolean                        true = split "3 years / 6 trimesters"
  staging.reject_if_missing                list[str] (known fields)       Reject staged course if field blank

RULES YOU MUST FOLLOW:
- Discovery patches: all regex patterns must be valid Python regex strings (re.search())
- Extraction patches: use exact dot-notation field paths from the table above
- Do NOT suggest the same fix as any previous attempt listed in the context
- Do NOT suggest unknown/undocumented fields
- If you cannot identify a fix, return an empty patches list with a clear diagnosis
- You may return up to 3 patches in one attempt (one per root problem)
- Prefer the most impactful fix first

DIAGNOSIS PRIORITY:
1. drop_rate > 50% AND dropped_sample is non-empty → fix discovery (allow/block patterns)
2. raw_discovered == 0 → increase bfs_page_budget or enable use_browser
3. fee_pct < 40% AND staged > 0 → set fees.central_page or fees.default_currency
4. ielts_pct < 40% AND staged > 0 → set english.central_page or english.default_ielts
5. location_pct < 40% OR sample_locations contains junk values → fix text_cleaning.location.reject_values
6. mode_pct < 40% OR sample_modes is empty → check filters.online_only.enabled
7. Otherwise → return empty patches with a clear explanation

Return ONLY the JSON object. No markdown, no explanation outside it.\
"""


def _build_user_message(ctx: dict, previous_attempts: list[dict]) -> str:
    prev_block = ""
    if previous_attempts:
        lines = [
            f"  #{a['attempt_number']}: {a['diagnosis']} | root_cause={a['root_cause']} "
            f"| fields_patched={[p.get('field') for p in a.get('patches_applied', [])]}"
            for a in previous_attempts
        ]
        prev_block = "\nPREVIOUS REPAIR ATTEMPTS (do NOT repeat these):\n" + "\n".join(lines)

    q = ctx.get("quality") or {}
    quality_str = (
        f"total_staged={q.get('total_staged', 0)}"
        f"  fee={q.get('fee_pct', 0)}%"
        f"  ielts={q.get('ielts_pct', 0)}%"
        f"  intakes={q.get('intakes_pct', 0)}%"
        f"  location={q.get('location_pct', 0)}%"
        f"  degree_level={q.get('degree_level_pct', 0)}%"
        f"  study_mode={q.get('mode_pct', 0)}%"
        f"  duration={q.get('duration_pct', 0)}%"
        f"  academic_level={q.get('academic_level_pct', 0)}%"
    ) if q else "no courses staged yet"

    loc_samples = json.dumps(q.get("sample_locations", []))
    deg_samples = json.dumps(q.get("sample_degrees", []))
    mode_samples = json.dumps(q.get("sample_modes", []))

    admin_disc  = ctx["admin_config"].get("discovery", {})
    admin_extr  = ctx["admin_config"].get("extraction", {})

    return f"""UNIVERSITY: {ctx['uni_name']}
SCRAPE URL: {ctx['scrape_url']}

DISCOVERY STATS:
  raw_urls_found={ctx['raw_discovered']}  after_filter={ctx['after_filter']}  staged={ctx['imported']}  drop_rate={ctx['drop_rate']}%  errors={ctx['total_errors']}

DROPPED URLs (filtered out — may be real course pages):
{json.dumps(ctx['dropped_sample'], indent=2)}

PASSED URLs (currently making it through):
{json.dumps(ctx['passed_sample'], indent=2)}

EXTRACTION QUALITY:
{quality_str}
  sample_locations: {loc_samples}
  sample_degree_levels: {deg_samples}
  sample_study_modes: {mode_samples}

CURRENT ADMIN_CONFIG — discovery overrides (DB, highest priority):
{json.dumps(admin_disc, indent=2) if admin_disc else "(none)"}

CURRENT ADMIN_CONFIG — extraction overrides (DB, highest priority):
{json.dumps(admin_extr, indent=2) if admin_extr else "(none)"}

YAML CONFIG ON DISK (complete file):
{ctx['yaml_content'] or "(no YAML file found for this university)"}
{prev_block}

Analyse the data above and return the single most impactful fix as JSON.
Choose from discovery OR extraction patches as needed.\
"""


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
            log.warning("ai_repair: no YAML file found for uni_id=%s — skipping YAML write", uni_id)
            return
        yaml_file = candidates[0]

    try:
        existing_text = yaml_file.read_text(encoding="utf-8")
        comment_lines = [ln for ln in existing_text.splitlines() if ln.strip().startswith("#")]
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
            session.update(status="failed", error="Job not found in database.")
            return session

        session["uni_name"] = ctx["uni_name"]
        _write_session(job_id, session)

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            session["current_attempt"] = attempt_num
            _write_session(job_id, session)
            log.info("ai_repair: job=%s attempt=%d/%d", job_id, attempt_num, MAX_ATTEMPTS)

            user_msg = _build_user_message(ctx, session["attempts"])
            ai_data = await chat_json(system=_SYSTEM_PROMPT, user=user_msg, max_tokens=2048)

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

            patches_raw: list[dict] = ai_data.get("patches") or []
            config_patch, validation_errors = _validate_and_build_config_patch(patches_raw)
            disc_patch = config_patch.get("discovery", {})

            # URL filter simulation for discovery patches
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
                "attempt_number":    attempt_num,
                "diagnosis":         ai_data.get("diagnosis", "Unknown issue"),
                "root_cause":        ai_data.get("root_cause", "unknown"),
                "confidence":        ai_data.get("confidence", 0),
                "explanation":       ai_data.get("explanation", ""),
                "patches_applied":   [
                    {
                        "section":   p.get("section"),
                        "field":     p.get("field"),
                        "new_value": p.get("value"),
                    }
                    for p in patches_raw
                    if p.get("section") in {"discovery", "extraction"}
                    and p.get("field")
                ],
                "validation_errors": validation_errors,
                "before_pass_count": sim["before"],
                "after_pass_count":  sim["after"],
                "total_test_urls":   sim["total"],
                "rescued_sample":    sim["rescued"],
                "patch_applied_ok":  False,
            }
            session["attempts"].append(attempt_record)

            # Apply validated patches
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

            # Termination checks
            is_url_fix  = "allow_url_patterns" in disc_patch or "block_url_patterns" in disc_patch
            good_enough = is_url_fix and sim["total"] > 0 and sim["after"] >= sim["total"] * 0.5
            is_extr_fix = bool(config_patch.get("extraction"))
            no_url_drop = ctx["drop_rate"] < 20
            no_patches  = not patches_raw

            if good_enough:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Discovery fix applied — {sim['after']}/{sim['total']} previously-dropped URLs "
                        f"now pass the new filter. Re-run discovery to confirm the improvement."
                    ),
                )
                break

            if is_extr_fix and no_url_drop:
                session.update(
                    status="completed",
                    final_verdict=(
                        "Extraction config patch applied. Re-run scrape to verify quality improvements "
                        "(fees, IELTS, location, study mode, etc.)."
                    ),
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
                rescued = f"{sim['after']}/{sim['total']} URLs rescued" if sim["total"] else "no URL filter applied"
                session.update(
                    status="completed",
                    final_verdict=(
                        f"Reached maximum {MAX_ATTEMPTS} attempts. "
                        f"Best result: {rescued}. "
                        "Re-run the scrape to validate applied patches. "
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

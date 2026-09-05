"""OpenAI-powered scrape URL-filter repair agent.

Uses the Replit AI Integrations OpenAI proxy to iteratively diagnose failing
scrape jobs and apply URL-filter patches only after deterministic validation.
Extraction-rule suggestions are replayed against stored HTML snapshots and are
persisted only when they improve missing fields without changing known values.

Loop per attempt:
  1. Snapshot quality BEFORE  (discovery stats + extraction fill rates)
  2. Call OpenAI              (json_object mode → diagnosis + patches)
  3. Strict patch validation  (whitelist, type, regex, range)
  4. Apply validated patches  (DB admin_config + YAML on disk)
  5. URL filter simulation    (discovery patches only — fast, no live re-scrape)
  6. Snapshot extraction scan (replay stored HTML; never mutate course rows)
  7. Validation report        (field-specific fill gain + populated-value preservation)
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

MAX_ATTEMPTS = 5
_REDIS_KEY_PREFIX = "ai_repair:"
_REDIS_LEASE_PREFIX = "ai_repair_lease:"
_REDIS_TTL_SEC    = 86_400  # 24 h
_REDIS_LEASE_TTL_SEC = 3_600

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


def _audit_urls(session: dict) -> list[str]:
    urls: set[str] = set()
    for attempt in session.get("attempts") or []:
        urls.update(u for u in attempt.get("rescued_sample") or [] if u)
        validation = attempt.get("extraction_validation") or {}
        for report in validation.get("reports") or []:
            urls.update(
                str(sample["url"])
                for sample in report.get("samples") or []
                if sample.get("url")
            )
    return sorted(urls)


async def persist_repair_audit(session: dict, db) -> None:
    """Upsert compact repair evidence and link it to existing page snapshots."""
    from sqlalchemy import text

    session_id = session.get("session_id")
    job_id = session.get("job_id")
    university_id = session.get("university_id")
    if not session_id or not job_id or university_id is None:
        return

    urls = _audit_urls(session)
    snapshot_refs: list[dict[str, Any]] = []
    if urls:
        rows = (await db.execute(
            text(
                "SELECT DISTINCT ON (course_url) id, course_url, snapshot_type "
                "FROM page_snapshots "
                "WHERE scrape_job_id = :job_id AND course_url = ANY(CAST(:urls AS TEXT[])) "
                "ORDER BY course_url, fetched_at DESC"
            ),
            {"job_id": job_id, "urls": urls},
        )).mappings().all()
        snapshot_refs = [
            {"snapshot_id": int(row["id"]), "url": row["course_url"], "type": row["snapshot_type"]}
            for row in rows
        ]

    evidence = {
        key: session.get(key)
        for key in (
            "session_id", "job_id", "university_id", "uni_name", "status",
            "current_attempt", "attempts", "final_verdict", "queued_at",
            "started_at", "completed_at", "error", "quality_before",
            "rollback_status",
        )
    }
    evidence["snapshot_refs"] = snapshot_refs
    await db.execute(
        text(
            "INSERT INTO ai_repair_audits "
            "(session_id, scrape_job_id, university_id, status, evidence) "
            "VALUES (:session_id, :job_id, :university_id, :status, CAST(:evidence AS JSONB)) "
            "ON CONFLICT (session_id) DO UPDATE SET "
            "status = EXCLUDED.status, evidence = EXCLUDED.evidence, updated_at = NOW()"
        ),
        {
            "session_id": session_id,
            "job_id": job_id,
            "university_id": int(university_id),
            "status": session.get("status") or "unknown",
            "evidence": json.dumps(evidence),
        },
    )
    await db.commit()


async def load_repair_audit(job_id: str, db, *, session_id: str | None = None) -> dict:
    """Load the latest durable repair run for a scrape job."""
    from sqlalchemy import text

    session_filter = "AND session_id = :session_id" if session_id else ""
    row = (await db.execute(
        text(
            "SELECT evidence FROM ai_repair_audits "
            f"WHERE scrape_job_id = :job_id {session_filter} "
            "ORDER BY updated_at DESC LIMIT 1"
        ),
        {"job_id": job_id, "session_id": session_id},
    )).scalar_one_or_none()
    return dict(row or {})


async def fail_repair_audit(
    job_id: str,
    university_id: int,
    session_id: str,
    error: str,
    db,
) -> dict:
    """Persist a terminal worker/queue failure without depending on Redis."""
    session = await load_repair_audit(job_id, db, session_id=session_id)
    session.update({
        "session_id": session_id,
        "job_id": job_id,
        "university_id": university_id,
        "status": "failed",
        "error": error,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "attempts": session.get("attempts") or [],
        "rollback_status": session.get("rollback_status") or "unchanged",
    })
    await persist_repair_audit(session, db)
    return session


def acquire_repair_lease(university_id: int, lease_token: str) -> bool:
    """Acquire a one-hour, single-flight lease for one university."""
    try:
        return bool(
            _redis_client().set(
                f"{_REDIS_LEASE_PREFIX}{university_id}",
                lease_token,
                nx=True,
                ex=_REDIS_LEASE_TTL_SEC,
            )
        )
    except Exception as exc:
        log.warning("ai_repair: Redis lease acquire error: %s", exc)
        return False


def release_repair_lease(university_id: int, lease_token: str) -> None:
    """Release the lease only when it is still owned by this repair job."""
    try:
        _redis_client().eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            f"{_REDIS_LEASE_PREFIX}{university_id}",
            lease_token,
        )
    except Exception as exc:
        log.warning("ai_repair: Redis lease release error: %s", exc)


def renew_repair_lease(university_id: int, lease_token: str) -> bool:
    """Extend the lease only when the caller still owns the fencing token."""
    try:
        return bool(_redis_client().eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('expire', KEYS[1], ARGV[2])
            end
            return 0
            """,
            1,
            f"{_REDIS_LEASE_PREFIX}{university_id}",
            lease_token,
            _REDIS_LEASE_TTL_SEC,
        ))
    except Exception as exc:
        log.warning("ai_repair: Redis lease renew error: %s", exc)
        return False


def claim_repair_session(
    job_id: str,
    university_id: int,
    lease_token: str,
) -> bool:
    """Atomically claim queued work only while the matching lease is owned."""
    try:
        return bool(_redis_client().eval(
            """
            if redis.call('get', KEYS[2]) ~= ARGV[1] then return 0 end
            local raw = redis.call('get', KEYS[1])
            if not raw then return 0 end
            local session = cjson.decode(raw)
            if session['session_id'] ~= ARGV[1] or session['status'] ~= 'queued' then
              return 0
            end
            session['status'] = 'starting'
            session['started_at'] = ARGV[2]
            redis.call('set', KEYS[1], cjson.encode(session), 'EX', ARGV[3])
            redis.call('expire', KEYS[2], ARGV[4])
            return 1
            """,
            2,
            _redis_key(job_id),
            f"{_REDIS_LEASE_PREFIX}{university_id}",
            lease_token,
            datetime.now(timezone.utc).isoformat(),
            _REDIS_TTL_SEC,
            _REDIS_LEASE_TTL_SEC,
        ))
    except Exception as exc:
        log.warning("ai_repair: Redis session claim error: %s", exc)
        return False


def expire_queued_repair(
    job_id: str,
    university_id: int,
    lease_token: str,
    *,
    error: str,
) -> bool:
    """Atomically fail only the same still-queued repair and release its lease."""
    try:
        return bool(_redis_client().eval(
            """
            local raw = redis.call('get', KEYS[1])
            if not raw then return 0 end
            local session = cjson.decode(raw)
            if session['session_id'] ~= ARGV[1] or session['status'] ~= 'queued' then
              return 0
            end
            session['status'] = 'failed'
            session['error'] = ARGV[2]
            session['completed_at'] = ARGV[3]
            redis.call('set', KEYS[1], cjson.encode(session), 'EX', ARGV[4])
            if redis.call('get', KEYS[2]) == ARGV[1] then
              redis.call('del', KEYS[2])
            end
            return 1
            """,
            2,
            _redis_key(job_id),
            f"{_REDIS_LEASE_PREFIX}{university_id}",
            lease_token,
            error,
            datetime.now(timezone.utc).isoformat(),
            _REDIS_TTL_SEC,
        ))
    except Exception as exc:
        log.warning("ai_repair: queued-session expiry error: %s", exc)
        return False


_REPAIR_TERMINAL_STATUSES = {
    "completed",
    "completed_with_warnings",
    "done",
    "failed",
    "failed_degraded",
    "failed_provider",
    "error",
    "stopped",
    "skipped",
}


def validate_url_repair_target(status: str, discovered_config: dict) -> tuple[bool, str]:
    """Require a terminal job with concrete evidence of a destructive URL gate."""
    if status not in _REPAIR_TERMINAL_STATUSES:
        return False, f"Scrape is still {status!r}; wait for it to finish before repair."

    pipeline = (discovered_config or {}).get("pipeline_stats") or {}
    raw = int(pipeline.get("raw_discovered") or 0)
    after = int(pipeline.get("after_filter") or 0)
    pre_block = int(pipeline.get("pre_block_discovered") or raw)
    block_dropped = int(pipeline.get("block_dropped_count") or 0)
    has_filter_failure = raw > 5 and (after == 0 or after < raw * 0.5)
    has_block_failure = pre_block > 5 and block_dropped > pre_block * 0.8
    dropped_sample = pipeline.get("dropped_sample") or []
    if not (has_filter_failure or has_block_failure) or not dropped_sample:
        return (
            False,
            "This job has no completed URL-filter failure with dropped URL evidence.",
        )
    return True, ""


def validate_ai_repair_target(
    status: str,
    discovered_config: dict,
    *,
    has_extraction_gap: bool,
) -> tuple[bool, str]:
    """Allow terminal URL-filter failures or jobs with missing repairable fields."""
    if status not in _REPAIR_TERMINAL_STATUSES:
        return False, f"Scrape is still {status!r}; wait for it to finish before repair."
    url_repairable, _ = validate_url_repair_target(status, discovered_config)
    if url_repairable or has_extraction_gap:
        return True, ""
    return False, (
        "This job has neither a URL-filter failure nor missing fee, IELTS, "
        "duration, or location values that OpenAI can repair safely."
    )


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
    "course_detail_url_patterns": list,
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
    "text_cleaning.location.strip_patterns": "regex_list",
    "text_cleaning.location.reject_values":  "str_list",
    "text_cleaning.location.allowed_values": "str_list",
    "text_cleaning.duration.split_on_slash": "bool",
    "staging.reject_if_missing":             "known_field_list",
    "extraction_rules.international_fee":    "extraction_rule",
    "extraction_rules.ielts_overall":        "extraction_rule",
    "extraction_rules.duration":             "extraction_rule",
    "extraction_rules.course_location":      "extraction_rule",
    "extraction_rules.course_name":          "extraction_rule",
}

_SAFE_REPAIR_FIELDS = {
    "international_fee",
    "ielts_overall",
    "duration",
    "course_location",
    "course_name",
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
    if field in (
        "allow_url_patterns",
        "block_url_patterns",
        "must_contain",
        "course_detail_url_patterns",
    ):
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
    elif tag == "extraction_rule":
        if not isinstance(value, dict):
            raise PatchValidationError(f"extraction.{field} must be a rule object.")
        if not any(value.get(key) for key in ("css", "xpath", "regex")):
            raise PatchValidationError(
                f"extraction.{field} needs at least one CSS, XPath, or regex selector."
            )
        confidence = value.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0.85 <= float(confidence) <= 1:
            raise PatchValidationError(
                f"extraction.{field} confidence must be between 0.85 and 1.0."
            )
        for key in ("css", "xpath", "regex", "attribute", "transform", "quoted_text"):
            item = value.get(key)
            if item is not None and (not isinstance(item, str) or len(item) > 1000):
                raise PatchValidationError(f"extraction.{field}.{key} must be a string ≤1000 chars.")
        pattern = value.get("regex")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise PatchValidationError(
                    f"extraction.{field}.regex is invalid: {exc}"
                ) from exc


def _normalise_repair_value(field: str, value: Any) -> Any:
    """Return a comparison-safe, field-specific value or None when implausible."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    try:
        if field == "international_fee":
            number = float(re.sub(r"[^\d.]", "", text.replace(",", "")))
            return round(number, 2) if 100 <= number <= 1_000_000 else None
        if field == "ielts_overall":
            number = float(text)
            return round(number, 1) if 4 <= number <= 9 else None
        if field == "duration":
            number = float(re.search(r"\d+(?:\.\d+)?", text).group(0))
            return round(number, 2) if 0 < number <= 20 else None
        if field == "course_location":
            if re.search(r"\{\{[^{}]*\}\}", text):
                return None
            cleaned = re.sub(r"\s+", " ", text).strip(" ,;|")
            if not cleaned or len(cleaned) > 200 or cleaned.casefold() in {
                "location", "campus", "study", "apply", "contact us",
            }:
                return None
            return cleaned.casefold()
        if field == "course_name":
            from app.services.scraper.course_name_cleaner import (
                normalise_generated_course_name_with_config,
            )
            cleaned = normalise_generated_course_name_with_config(text)
            return cleaned.casefold() if cleaned else None
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def validate_extraction_rule_on_samples(
    field: str,
    rule: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replay one rule without side effects and prove fill gain + preservation."""
    if field not in _SAFE_REPAIR_FIELDS:
        raise PatchValidationError(f"Field {field!r} is not safe for automatic extraction repair.")
    _validate_extraction_patch(f"extraction_rules.{field}", rule)
    from app.services.scraper.ai_extractor_run import apply_extraction_rules

    tested: list[dict[str, Any]] = []
    filled_missing = corrected_invalid = regressions = valid_outputs = populated_tested = 0
    for sample in samples:
        raw_before = sample.get("before")
        baseline = _normalise_repair_value(field, raw_before)
        invalid_before = raw_before not in (None, "") and baseline is None
        result = apply_extraction_rules(sample.get("html") or "", {field: rule})
        raw_after = result.get(field, (None, "ai_rule:miss"))[0]
        after = _normalise_repair_value(field, raw_after)
        preserved = baseline is None or after == baseline
        if baseline is not None:
            populated_tested += 1
        if baseline is not None and after != baseline:
            regressions += 1
        if baseline is None and after is not None:
            if invalid_before:
                corrected_invalid += 1
            else:
                filled_missing += 1
        if after is not None:
            valid_outputs += 1
        tested.append({
            "url": sample.get("url"),
            "before": sample.get("before"),
            "after": raw_after,
            "method": result.get(field, (None, "ai_rule:miss"))[1],
            "preserved": preserved,
            "corrected_invalid": invalid_before and after is not None,
        })

    reasons: list[str] = []
    if len(samples) < 2:
        reasons.append("At least two stored HTML samples are required.")
    if filled_missing + corrected_invalid < 1:
        reasons.append("The rule did not fill a missing value or correct an invalid value.")
    if populated_tested < 1 and corrected_invalid < 2:
        reasons.append(
            "No populated baseline value was available to prove preservation, "
            "and fewer than two invalid values were corrected."
        )
    if valid_outputs < 2:
        reasons.append("The rule produced too few plausible values.")
    if regressions:
        reasons.append(f"The rule changed {regressions} already-populated value(s).")
    return {
        "field": field,
        "accepted": not reasons,
        "confidence": float(rule["confidence"]),
        "samples_tested": len(samples),
        "missing_filled": filled_missing,
        "invalid_corrected": corrected_invalid,
        "valid_outputs": valid_outputs,
        "populated_tested": populated_tested,
        "regressions": regressions,
        "rejection_reasons": reasons,
        "samples": tested,
    }


async def _validate_extraction_patch_on_snapshots(
    job_id: str,
    extr_patch: dict[str, Any],
    db,
    *,
    max_samples: int = 12,
) -> dict[str, Any]:
    """Load representative stored HTML and validate proposed Stage-0 rules."""
    from sqlalchemy import select
    from app.models.page_snapshot import PageSnapshot
    from app.services.snapshot_store import download_snapshot

    rules = extr_patch.get("extraction_rules") or {}
    reports: list[dict[str, Any]] = []
    accepted_rules: dict[str, Any] = {}
    snapshots = list((await db.execute(
        select(PageSnapshot)
        .where(PageSnapshot.scrape_job_id == job_id)
        .where(PageSnapshot.snapshot_type.in_(["html", "repair"]))
        .where(PageSnapshot.storage_path.isnot(None))
        .order_by(PageSnapshot.fetched_at.desc())
        .limit(max_samples)
    )).scalars().all())

    for field, rule in rules.items():
        samples: list[dict[str, Any]] = []
        # Prefer a balanced set: missing rows prove gain, populated rows prove preservation.
        ordered = sorted(
            snapshots,
            key=lambda snap: (snap.original_extraction or {}).get(field) is not None,
        )
        for snap in ordered:
            raw = await download_snapshot(snap.storage_path)
            if not raw:
                continue
            samples.append({
                "url": snap.course_url,
                "html": raw.decode("utf-8", errors="replace"),
                "before": (snap.original_extraction or {}).get(field),
            })
        report = validate_extraction_rule_on_samples(field, rule, samples)
        reports.append(report)
        if report["accepted"]:
            accepted_rules[field] = rule
    return {
        "accepted": bool(rules) and len(accepted_rules) == len(rules),
        "rules": accepted_rules,
        "reports": reports,
        "rollback_status": "not_needed",
    }


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
        if field in (
            "allow_url_patterns",
            "block_url_patterns",
            "must_contain",
            "course_detail_url_patterns",
        ):
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


def _validated_ai_patches(ai_data: Any) -> list[dict]:
    """Validate the OpenAI response envelope before reading or applying it."""
    if not isinstance(ai_data, dict):
        raise PatchValidationError("OpenAI response must be a JSON object.")
    patches = ai_data.get("patches")
    if not isinstance(patches, list):
        raise PatchValidationError("OpenAI response must contain a patches list.")
    if len(patches) > 3:
        raise PatchValidationError("OpenAI response contains more than 3 patches.")
    if any(not isinstance(patch, dict) for patch in patches):
        raise PatchValidationError("Every OpenAI patch must be a JSON object.")
    confidence = ai_data.get("confidence", 0)
    if not isinstance(confidence, int) or not 0 <= confidence <= 100:
        raise PatchValidationError("OpenAI confidence must be an integer from 0 to 100.")
    return patches


# ── Extraction quality helpers ────────────────────────────────────────────────

def _extraction_quality_ok(quality: dict) -> bool:
    """True when average key-field fill rate is acceptable."""
    if not quality or quality.get("total_staged", 0) == 0:
        return True  # No staged courses yet - don't block on extraction
    if quality.get("bad_course_names", 0) or quality.get("bad_locations", 0):
        return False
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
        f" invalid_names={quality.get('bad_course_names', 0)}"
        f" invalid_locations={quality.get('bad_locations', 0)}"
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
                   COUNT(*) FILTER (
                     WHERE course_location LIKE '%%{{%%'
                   )                                             AS bad_locations,
                   COUNT(degree_level)                           AS has_degree_level,
                   COUNT(study_mode)                             AS has_mode,
                   COUNT(duration)                               AS has_duration,
                   COUNT(course_name)                            AS has_course_name,
                   COUNT(*) FILTER (
                     WHERE course_name LIKE '%%{{%%'
                         OR course_name ~* '\\|\\s*(university|unisc)'
                   )                                             AS bad_course_names,
                   COUNT(academic_level)                         AS has_academic_level,
                   array_agg(DISTINCT course_location)
                     FILTER (WHERE course_location IS NOT NULL)  AS sample_locations,
                   array_agg(DISTINCT course_location)
                     FILTER (WHERE course_location LIKE '%%{{%%')
                                                                 AS sample_bad_locations,
                   array_agg(DISTINCT degree_level)
                     FILTER (WHERE degree_level IS NOT NULL)     AS sample_degrees,
                   array_agg(DISTINCT study_mode)
                     FILTER (WHERE study_mode IS NOT NULL)       AS sample_modes,
                   array_agg(DISTINCT course_name)
                     FILTER (
                       WHERE course_name LIKE '%%{{%%'
                           OR course_name ~* '\\|\\s*(university|unisc)'
                     )                                           AS sample_bad_course_names
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
            "course_name_pct": 0, "bad_course_names": 0,
            "bad_locations": 0,
            "sample_locations": [], "sample_degrees": [], "sample_modes": [],
            "sample_bad_course_names": [], "sample_bad_locations": [],
        }

    pct = lambda n: round(100 * (n or 0) / total)
    return {
        "total_staged":       total,
        "fee_pct":            pct(q["has_fee"]),
        "ielts_pct":          pct(q["has_ielts"]),
        "intakes_pct":        pct(q["has_intakes"]),
        "location_pct":       pct((q["has_location"] or 0) - (q["bad_locations"] or 0)),
        "degree_level_pct":   pct(q["has_degree_level"]),
        "mode_pct":           pct(q["has_mode"]),
        "duration_pct":       pct(q["has_duration"]),
        "course_name_pct":    pct((q["has_course_name"] or 0) - (q["bad_course_names"] or 0)),
        "bad_course_names":   int(q["bad_course_names"] or 0),
        "bad_locations":      int(q["bad_locations"] or 0),
        "academic_level_pct": pct(q["has_academic_level"]),
        "sample_locations":   list((q["sample_locations"]  or [])[:8]),
        "sample_bad_locations": list((q["sample_bad_locations"] or [])[:8]),
        "sample_degrees":     list((q["sample_degrees"]    or [])[:8]),
        "sample_modes":       list((q["sample_modes"]      or [])[:8]),
        "sample_bad_course_names": list((q["sample_bad_course_names"] or [])[:8]),
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
    # improving quality metrics.  course_website must be a detail page (not a hub):
    # filter to URLs with at least 3 path segments to exclude /study/ category pages.
    sample_rows = (await db.execute(text("""
        SELECT id, course_website, international_fee, ielts_overall, study_mode,
               course_location, degree_level
        FROM   scraped_courses
        WHERE  university_id = :uid
          AND  scrape_job_id = :jid
          AND  status IN ('pending','review','approved')
          AND  course_website IS NOT NULL
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

    sorted_rows = sorted(sample_rows, key=lambda r: _detail_score(r["course_website"] or ""), reverse=True)
    fills["courses_rescanned"] = len(sorted_rows)

    for row in sorted_rows:
        url = row["course_website"]
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

    unis_dir = Path(__file__).parent.parent.parent.parent / "scraper_config" / "unis"
    yaml_files: list[Path] = []
    effective_discovery: dict = {}
    try:
        from urllib.parse import urlparse
        from app.services.scraper.config.loader import (
            _hostname_to_slug,
            _select_uni_yaml,
            get_config_for_host,
        )

        scrape_url = row["scrape_url"] or ""
        hostname = urlparse(scrape_url).hostname or ""
        slug = _hostname_to_slug(hostname)
        yaml_path, _ = _select_uni_yaml(
            slug=slug,
            university_id=uni_id,
            scrape_url=scrape_url,
        )
        if yaml_path.exists():
            yaml_files = [yaml_path]

        effective_cfg = get_config_for_host(
            hostname=hostname,
            name=row["uni_name"] or "Unknown",
            scrape_url=scrape_url,
            university_id=uni_id,
            db_scrape_config=sc,
            create_missing_stub=False,
        )
        effective_discovery = {
            field: list(getattr(effective_cfg.discovery, field, None) or [])
            for field in (
                "allow_url_patterns",
                "block_url_patterns",
                "must_contain",
                "course_detail_url_patterns",
            )
        }
    except Exception as exc:
        log.warning("ai_repair: effective discovery config load failed: %s", exc)

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
        "effective_discovery": effective_discovery,
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
  "root_cause": "one of: allow_url_patterns | block_url_patterns | bfs_page_budget | use_browser | fees | english | location | course_name | study_mode | filters | duration | unknown",
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
  course_detail_url_patterns list[regex] — final post-discovery course-page gate
  bfs_page_budget      int 5-300    — raise when too few pages crawled
  use_browser          bool         — enable for JS-rendered SPAs
  sitemap_url          str (URL)    — override sitemap URL

RECIPE PATCHES (section="recipe"):
  extraction_rules.international_fee  object — CSS/XPath/regex Stage-0 rule
  extraction_rules.ielts_overall      object — CSS/XPath/regex Stage-0 rule
  extraction_rules.duration           object — CSS/XPath/regex Stage-0 rule
  extraction_rules.course_location    object — CSS/XPath/regex Stage-0 rule
  extraction_rules.course_name        object — CSS/XPath/regex Stage-0 rule

RULES:
- Regex patterns must be valid Python re.search() patterns
- An empty list is allowed when the correct repair is to clear a broken URL filter
- field must use exact dot-notation paths from the tables above
- Do NOT repeat a fix from any previous attempt listed in context
- Return up to 3 patches per attempt; prioritise highest-impact fix first
- Return empty patches if no safe automatic fix is possible
- Extraction rule objects require confidence >= 0.85 and at least one of css/xpath/regex.
- Prefer extraction_rules patches for missing or invalid fee, IELTS, duration,
  location, or course-name values.
"""


# ── URL filter simulation ─────────────────────────────────────────────────────

_MEDIA_EXT  = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|bmp|pdf|css|js|woff2?|ttf|eot|mp[34]|zip|docx?)$", re.I)
_ASSET_PATH = re.compile(r"/(images?|assets?|globalassets|static|media|uploads?|fonts?|icons?|scripts?)/", re.I)


def _is_course_url(u: str) -> bool:
    return not _MEDIA_EXT.search(u) and not _ASSET_PATH.search(u)


def _simulate_filter(
    dropped: list[str],
    allow_pats: list[str],
    block_pats: list[str],
    must_contain: list[str] | None = None,
    course_detail_pats: list[str] | None = None,
) -> dict:
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
    detail_c = _c(course_detail_pats or [])
    must_lower = [value.lower() for value in (must_contain or []) if value]
    passing = []
    for u in course_urls:
        ok = True
        if allow_c and not any(c.search(u) for c in allow_c):
            ok = False
        if ok and must_lower and not any(value in u.lower() for value in must_lower):
            ok = False
        if ok and block_c and any(c.search(u) for c in block_c):
            ok = False
        if ok and detail_c and not any(c.search(u) for c in detail_c):
            ok = False
        if ok:
            passing.append(u)

    return {"before": 0, "after": len(passing), "total": len(course_urls), "rescued": passing[:6]}


# ── Patch application ─────────────────────────────────────────────────────────

def _merge_repair_patch(base: dict, patch: dict) -> dict:
    """Merge an approved repair patch, preserving explicit empty-list clears."""
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_repair_patch(result[key], value)
        else:
            result[key] = value
    return result


async def _apply_to_db(uni_id: int, config_patch: dict, db) -> dict:
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
    original_sc = dict(sc)
    sc["admin_config"] = _merge_repair_patch(existing, config_patch)

    await db.execute(
        text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
        {"cfg": _json.dumps(sc), "id": uni_id},
    )
    await db.commit()
    return original_sc


async def _restore_db_config(
    uni_id: int,
    original_sc: dict,
    db,
    *,
    expected_current: dict | None = None,
) -> None:
    """Restore pre-repair config, optionally fenced against concurrent edits."""
    from sqlalchemy import text
    import json as _json

    await db.rollback()
    if expected_current is None:
        result = await db.execute(
            text("UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) WHERE id = :id"),
            {"cfg": _json.dumps(original_sc), "id": uni_id},
        )
    else:
        result = await db.execute(
            text(
                "UPDATE universities SET scrape_config = CAST(:cfg AS jsonb) "
                "WHERE id = :id AND scrape_config = CAST(:expected AS jsonb)"
            ),
            {
                "cfg": _json.dumps(original_sc),
                "expected": _json.dumps(expected_current),
                "id": uni_id,
            },
        )
        if result.rowcount != 1:
            await db.rollback()
            raise RuntimeError(
                "Scraper config changed after repair; automatic rollback refused "
                "to overwrite the newer config."
            )
    await db.commit()


async def _apply_discovery_to_db(uni_id: int, disc_patch: dict, db) -> dict:
    """Write discovery-section patch into admin_config.discovery."""
    return await _apply_to_db(uni_id, {"discovery": disc_patch}, db)


async def _read_scrape_config(uni_id: int, db) -> dict:
    """Capture the exact config document used as the validation fence."""
    from sqlalchemy import text

    row = (await db.execute(
        text("SELECT scrape_config FROM universities WHERE id = :id"),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise RuntimeError(f"University {uni_id} no longer exists.")
    return dict(row.get("scrape_config") or {})


async def _apply_recipe_to_db(
    uni_id: int,
    recipe_patch: dict,
    db,
    *,
    expected_config: dict | None = None,
) -> dict:
    """Atomically merge validated Stage-0 rules into the runtime auto_config."""
    from sqlalchemy import text
    import json as _json

    original_sc = (
        dict(expected_config)
        if expected_config is not None
        else await _read_scrape_config(uni_id, db)
    )
    updated_sc = dict(original_sc)
    auto_config = dict(updated_sc.get("auto_config") or {})
    current_rules = dict(auto_config.get("extraction_rules") or {})
    proposed_rules = dict(recipe_patch.get("extraction_rules") or {})
    auto_config["extraction_rules"] = _merge_repair_patch(current_rules, proposed_rules)
    auto_config["_extraction_rules_source"] = "openai_validated_snapshot_replay"
    auto_config["_extraction_rules_repaired_at"] = datetime.now(timezone.utc).isoformat()
    updated_sc["auto_config"] = auto_config
    result = await db.execute(
        text(
            "UPDATE universities SET scrape_config = CAST(:after AS jsonb) "
            "WHERE id = :id AND scrape_config = CAST(:before AS jsonb)"
        ),
        {
            "after": _json.dumps(updated_sc),
            "before": _json.dumps(original_sc),
            "id": uni_id,
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise RuntimeError("Scraper config changed during validation; no repair was saved.")
    await db.commit()
    return {"before": original_sc, "applied": updated_sc}


def _apply_to_yaml(
    yaml_file: Any,
    unis_dir: Path,
    uni_id: int,
    scrape_url: str,
    config_patch: dict,
) -> tuple[Path, str | None]:
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
            from urllib.parse import urlparse
            from app.services.scraper.config.loader import _hostname_to_slug

            hostname = urlparse(scrape_url).hostname or "university"
            yaml_file = unis_dir / f"{_hostname_to_slug(hostname)}_{uni_id}.yaml"
        else:
            yaml_file = candidates[0]

    original_text = yaml_file.read_text(encoding="utf-8") if yaml_file.exists() else None
    existing_text = original_text or ""
    comment_lines = [ln for ln in existing_text.splitlines() if ln.strip().startswith("#")]
    header = ("\n".join(comment_lines) + "\n") if comment_lines else ""
    merged = _merge_repair_patch(_yaml.safe_load(existing_text) or {}, config_patch)
    new_txt = header + _yaml.dump(
        merged,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    tmp_path = yaml_file.with_suffix(yaml_file.suffix + ".ai-repair.tmp")
    tmp_path.write_text(new_txt, encoding="utf-8")
    tmp_path.replace(yaml_file)
    log.info("ai_repair: wrote config patch to %s", yaml_file.name)
    return yaml_file, original_text


def _restore_yaml(yaml_file: Path, original_text: str | None) -> None:
    """Restore or remove a YAML file after a failed durable apply."""
    if original_text is None:
        yaml_file.unlink(missing_ok=True)
    else:
        yaml_file.write_text(original_text, encoding="utf-8")


async def _assert_effective_discovery_patch(uni_id: int, disc_patch: dict, db) -> None:
    """Reload the merged config and prove every approved URL field is effective."""
    from urllib.parse import urlparse
    from sqlalchemy import text
    from app.services.scraper.config.loader import get_config_for_host

    row = (await db.execute(
        text(
            "SELECT name, scrape_url, scrape_config "
            "FROM universities WHERE id = :id"
        ),
        {"id": uni_id},
    )).mappings().first()
    if not row:
        raise RuntimeError(f"University {uni_id} disappeared while applying repair.")

    scrape_url = row["scrape_url"] or ""
    cfg = get_config_for_host(
        hostname=urlparse(scrape_url).hostname or "",
        name=row["name"] or "Unknown",
        scrape_url=scrape_url,
        university_id=uni_id,
        db_scrape_config=row["scrape_config"] or {},
        create_missing_stub=False,
    )
    for field, expected in disc_patch.items():
        actual = getattr(cfg.discovery, field)
        if actual != expected:
            raise RuntimeError(
                f"Effective config verification failed for discovery.{field}: "
                f"expected {expected!r}, got {actual!r}"
            )


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
    bad_names = q.get("bad_course_names", 0)
    bad_locations = q.get("bad_locations", 0)
    if fee_pct   < _FEE_PCT_OK:    _failing_hints.append(f"fees ({fee_pct}% < {_FEE_PCT_OK}% target)")
    if ielts_pct < _IELTS_PCT_OK:  _failing_hints.append(f"IELTS ({ielts_pct}% < {_IELTS_PCT_OK}% target)")
    if loc_pct   < _LOCATION_PCT_OK: _failing_hints.append(f"location ({loc_pct}% < {_LOCATION_PCT_OK}% target)")
    if mode_pct  < _MODE_PCT_OK:   _failing_hints.append(f"study_mode ({mode_pct}% < {_MODE_PCT_OK}% target)")
    if deg_pct   < _DEGREE_PCT_OK: _failing_hints.append(f"degree_level ({deg_pct}% < {_DEGREE_PCT_OK}% target)")
    if int_pct   < 40:             _failing_hints.append(f"intakes ({int_pct}% fill rate)")
    if bad_names:
        _failing_hints.append(
            f"course_name ({bad_names} contaminated values; examples: "
            f"{q.get('sample_bad_course_names', [])[:3]})"
        )
    if bad_locations:
        _failing_hints.append(
            f"location ({bad_locations} template-contaminated values; examples: "
            f"{q.get('sample_bad_locations', [])[:3]})"
        )
    failing_str = ", ".join(_failing_hints) if _failing_hints else "all fields meeting targets"

    if phase == "extraction":
        focus_block = (
            "CURRENT FOCUS: EXTRACTION QUALITY — discovery is working (drop_rate acceptable).\n"
            "★★★ DO NOT suggest ANY section='discovery' patches. ★★★\n"
            "The problem is that staged courses are missing key fields.\n"
            f"FAILING FIELDS: {failing_str}\n"
            "Suggest ONLY section='recipe' patches targeting the failing fields above.\n"
            "Valid recipe fields: extraction_rules.international_fee,\n"
            "  extraction_rules.ielts_overall, extraction_rules.duration,\n"
            "  extraction_rules.course_location, extraction_rules.course_name. "
            "Each value must be a rule object\n"
            "  with confidence >= 0.85 and at least one of css, xpath, or regex."
        )
        diag_priority = (
            "DIAGNOSIS PRIORITY (extraction phase — pick the lowest-fill field first):\n"
            f"  Failing: {failing_str}\n"
            "1. fee_pct low     → extraction_rules.international_fee\n"
            "2. ielts_pct low   → extraction_rules.ielts_overall\n"
            "3. duration low    → extraction_rules.duration\n"
            "4. location low   → extraction_rules.course_location\n"
            "5. contaminated course names → extraction_rules.course_name\n"
            "6. Other fields   → note in explanation only; do not propose an unsupported patch\n"
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


async def run_ai_repair_loop(job_id: str, db, *, lease_token: str | None = None) -> dict:
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

    queued_session = read_session(job_id)
    session: dict = {
        "session_id":      lease_token or queued_session.get("session_id") or str(uuid.uuid4())[:8],
        "job_id":          job_id,
        "status":          "running",
        "current_attempt": 0,
        "attempts":        [],
        "final_verdict":   None,
        "university_id":   queued_session.get("university_id"),
        "uni_name":        None,
        "queued_at":       queued_session.get("queued_at"),
        "started_at":      datetime.now(timezone.utc).isoformat(),
        "completed_at":    None,
        "error":           None,
        "quality_before":  None,
    }
    _write_session(job_id, session)
    pending_extraction_rollback: dict[str, dict[str, Any]] | None = None

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

        current_attempt_evidence: dict[str, Any] | None = None
        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            current_attempt_evidence = None
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
            current_attempt_evidence = {
                "attempt_number": attempt_num,
                "phase": phase,
                "diagnosis": "Unknown",
                "root_cause": "unknown",
                "confidence": 0,
                "explanation": "",
                "patches_proposed": [],
                "patches_applied": [],
                "validation_errors": [],
                "patch_applied_ok": False,
                "quality_before": quality_before,
                "rollback_status": "unchanged",
                "outcome": "failed",
            }

            # ③ Call OpenAI
            user_msg = _build_user_message(ctx, session["attempts"], phase=phase)
            ai_data  = await chat_json(system=_SYSTEM_PROMPT, user=user_msg, max_tokens=2048)

            if ai_data is None:
                current_attempt_evidence["validation_errors"] = ["OpenAI service unavailable."]
                session["attempts"].append(current_attempt_evidence)
                session.update(
                    status="completed",
                    final_verdict=(
                        "OpenAI service unavailable. Check AI_INTEGRATIONS_OPENAI_BASE_URL "
                        "and AI_INTEGRATIONS_OPENAI_API_KEY."
                    ),
                )
                break
            patches_proposed = [
                {
                    "section": patch.get("section"),
                    "field": patch.get("field"),
                    "new_value": patch.get("value"),
                }
                for patch in (ai_data.get("patches") or [])
                if isinstance(patch, dict)
            ]
            current_attempt_evidence.update({
                "diagnosis": ai_data.get("diagnosis", "Unknown"),
                "root_cause": ai_data.get("root_cause", "unknown"),
                "confidence": ai_data.get("confidence", 0),
                "explanation": ai_data.get("explanation", ""),
                "patches_proposed": patches_proposed,
            })
            try:
                patches_raw = _validated_ai_patches(ai_data)
            except PatchValidationError as exc:
                rejection = f"Invalid repair response: {exc}"
                current_attempt_evidence.update(
                    validation_errors=[rejection],
                    patch_error=str(exc),
                    outcome="rejected",
                )
                session["attempts"].append(current_attempt_evidence)
                session.update(
                    status="failed",
                    error=f"OpenAI returned an invalid repair response: {exc} No config was changed.",
                )
                break

            # ④ Validate patches → (disc_patch, extr_patch, errors)
            #    extr_patch is already a NESTED dict (dotpath fields expanded)
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
            extraction_validation: dict[str, Any] | None = None
            extraction_config_before: dict[str, Any] | None = None
            if extr_patch:
                unsupported = set(extr_patch) - {"extraction_rules"}
                if unsupported:
                    validation_errors.append(
                        "Extraction patch rejected: automatic repair supports only tested "
                        f"Stage-0 extraction_rules, not {', '.join(sorted(unsupported))}."
                    )
                    extr_patch = {}
                elif int(ai_data.get("confidence") or 0) < 85:
                    validation_errors.append(
                        "Extraction patch rejected: OpenAI confidence was below 85%."
                    )
                    extr_patch = {}
                else:
                    # Capture before replay starts. The eventual write must CAS
                    # against this exact document so operator edits made during
                    # snapshot validation can never be overwritten.
                    extraction_config_before = await _read_scrape_config(
                        ctx["university_id"], db
                    )
                    extraction_validation = await _validate_extraction_patch_on_snapshots(
                        job_id, extr_patch, db
                    )
                    if not extraction_validation["accepted"]:
                        for report in extraction_validation["reports"]:
                            validation_errors.extend(
                                f"{report['field']}: {reason}"
                                for reason in report["rejection_reasons"]
                            )
                        extr_patch = {}

            # ⑤ URL filter simulation (discovery patches only — no live re-scrape needed)
            sim: dict = {"before": 0, "after": 0, "total": 0, "rescued": []}
            _URL_FILTER_FIELDS = {
                "allow_url_patterns",
                "block_url_patterns",
                "must_contain",
                "course_detail_url_patterns",
            }
            if _URL_FILTER_FIELDS.intersection(disc_patch):
                from sqlalchemy import text as _text
                dc_row = (await db.execute(
                    _text("SELECT discovered_config FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
                    {"j": job_id},
                )).first()
                dc: dict = (dc_row[0] or {}) if dc_row else {}
                dropped  = dc.get("pipeline_stats", {}).get("dropped_sample") or ctx["dropped_sample"]
                current_disc = dict(ctx.get("effective_discovery") or {})
                baseline_sim = _simulate_filter(
                    dropped,
                    current_disc.get("allow_url_patterns", []),
                    current_disc.get("block_url_patterns", []),
                    current_disc.get("must_contain", []),
                    current_disc.get("course_detail_url_patterns", []),
                )
                proposed_disc = {**current_disc, **disc_patch}
                sim = _simulate_filter(
                    dropped,
                    proposed_disc.get("allow_url_patterns", []),
                    proposed_disc.get("block_url_patterns", []),
                    proposed_disc.get("must_contain", []),
                    proposed_disc.get("course_detail_url_patterns", []),
                )
                sim["before"] = baseline_sim["after"]

                minimum_rescue = max(1, (sim["total"] + 1) // 2)
                if (
                    sim["total"] == 0
                    or sim["after"] <= sim["before"]
                    or (ctx["after_filter"] == 0 and sim["after"] < minimum_rescue)
                ):
                    validation_errors.append(
                        "OpenAI URL patch rejected: full effective-filter simulation "
                        f"rescued {sim['after']}/{sim['total']} URLs "
                        f"(before {sim['before']}); at least {minimum_rescue} required."
                    )
                    log.warning(
                        "ai_repair: rejecting non-improving URL patch before apply: %s",
                        validation_errors[-1],
                    )
                    disc_patch = {}

                # Update ctx drop_rate immediately so next iteration uses the improved value
                if disc_patch and sim.get("after", 0) > sim.get("before", 0):
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
            extraction_apply_failed = False
            extr_patch_applied_keys: list = []
            if disc_patch:
                yaml_state: tuple[Path, str | None] | None = None
                db_before: dict | None = None
                try:
                    if lease_token and not renew_repair_lease(
                        ctx["university_id"],
                        lease_token,
                    ):
                        raise RuntimeError(
                            "Repair ownership was lost before config apply; no change was saved."
                        )
                    yaml_state = _apply_to_yaml(
                        ctx.get("yaml_file"),
                        ctx["unis_dir"],
                        ctx["university_id"],
                        ctx["scrape_url"],
                        {"discovery": disc_patch},
                    )
                    db_before = await _apply_discovery_to_db(
                        ctx["university_id"],
                        disc_patch,
                        db,
                    )
                    await _assert_effective_discovery_patch(
                        ctx["university_id"],
                        disc_patch,
                        db,
                    )
                    patch_applied_ok = True
                    # Register fingerprints so this exact patch is not repeated
                    for p in patches_raw:
                        if p.get("section") == "discovery":
                            _applied_fingerprints.add(_patch_fingerprint(p))
                    log.info("ai_repair: discovery patch applied: %s", list(disc_patch.keys()))
                except Exception as exc:
                    log.warning("ai_repair: discovery patch apply error: %s", exc)
                    patch_error = str(exc)
                    if db_before is not None:
                        try:
                            await _restore_db_config(
                                ctx["university_id"],
                                db_before,
                                db,
                            )
                        except Exception as rollback_exc:
                            patch_error += f"; DB rollback failed: {rollback_exc}"
                    else:
                        await db.rollback()
                    if yaml_state is not None:
                        try:
                            _restore_yaml(*yaml_state)
                        except Exception as rollback_exc:
                            patch_error += f"; YAML rollback failed: {rollback_exc}"
                    validation_errors.append(
                        f"Approved URL patch was not saved: {patch_error}"
                    )
                    disc_patch = {}

            # ⑦ Persist only rules that passed non-mutating snapshot validation.
            if extr_patch:
                try:
                    if lease_token and not renew_repair_lease(
                        ctx["university_id"], lease_token
                    ):
                        raise RuntimeError(
                            "Repair ownership was lost before extraction config apply."
                        )
                    pending_extraction_rollback = await _apply_recipe_to_db(
                        ctx["university_id"],
                        extr_patch,
                        db,
                        expected_config=extraction_config_before,
                    )
                    extr_patch_applied_keys = _flatten_dotpaths(extr_patch)
                    patch_applied_ok = True
                    # Register fingerprints
                    for p in patches_raw:
                        if p.get("section") == "recipe":
                            _applied_fingerprints.add(_patch_fingerprint(p))
                    log.info("ai_repair: extraction patch applied: %s", extr_patch_applied_keys)
                except Exception as exc:
                    log.warning("ai_repair: extraction patch apply error: %s", exc)
                    await db.rollback()
                    extraction_apply_failed = True
                    extr_patch = {}
                    if not patch_error:
                        patch_error = str(exc)
                    validation_errors.append(
                        f"Validated extraction patch was not saved: {exc}. "
                        "No scraper config was changed."
                    )

            # Extraction-rule patches were already replayed against stored HTML.
            # Never mutate staged/production course rows during repair validation.
            config_patch_combined: dict = {}
            if disc_patch:
                config_patch_combined["discovery"] = disc_patch
            if extr_patch:
                config_patch_combined["extraction"] = extr_patch
            scan_fills = {
                "courses_rescanned": sum(
                    report.get("samples_tested", 0)
                    for report in (extraction_validation or {}).get("reports", [])
                ),
                "ielts_fills": 0,
                "fee_fills": 0,
                "location_clears": 0,
            }

            # ⑨ Real quality snapshot AFTER the extraction scan
            quality_after = dict(quality_before)
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
                "patches_proposed":     patches_proposed,
                "patches_applied":      [
                    {"section": p.get("section"), "field": p.get("field"), "new_value": p.get("value")}
                    for p in patches_raw
                    if patch_applied_ok
                    and p.get("section") == "discovery"
                    and p.get("field") in disc_patch
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
                "extraction_validation": extraction_validation,
                "rollback_status": (
                    "not_needed" if patch_applied_ok
                    else "unchanged"
                ),
                "outcome": (
                    "accepted" if patch_applied_ok
                    else "rejected" if validation_errors
                    else "no_change"
                ),
            }
            session["attempts"].append(attempt_record)
            current_attempt_evidence = attempt_record
            _write_session(job_id, session)
            try:
                await persist_repair_audit(session, db)
            except Exception as audit_exc:
                rollback = getattr(db, "rollback", None)
                if rollback is not None:
                    await rollback()
                log.exception(
                    "ai_repair: durable attempt audit failed for job=%s: %s",
                    job_id,
                    audit_exc,
                )

            urls_rescued_enough = sim["total"] > 0 and sim["after"] >= sim["total"] * 0.5
            has_url_patch       = bool(_URL_FILTER_FIELDS.intersection(disc_patch))
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
            if extraction_apply_failed:
                session.update(
                    status="failed",
                    error=(
                        f"Validated extraction patch was not saved: {patch_error}. "
                        "No scraper config was changed."
                    ),
                    final_verdict="No extraction repair was applied; scraper config is unchanged.",
                    rollback_status="unchanged",
                )
                break

            if patch_applied_ok and has_url_patch and urls_rescued_enough:
                session.update(
                    status="completed",
                    final_verdict=(
                        f"OpenAI repaired and saved the URL filters. Deterministic "
                        f"validation rescued {sim['after']}/{sim['total']} known course URLs. "
                        "Re-run the scrape to verify live discovery."
                    ),
                )
                break

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
                pending_extraction_rollback = None
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
        rollback_status = "unchanged"
        rollback_error = ""
        if pending_extraction_rollback is not None:
            try:
                await _restore_db_config(
                    int(session["university_id"]),
                    pending_extraction_rollback["before"],
                    db,
                    expected_current=pending_extraction_rollback["applied"],
                )
                rollback_status = "restored"
            except Exception as rollback_exc:
                rollback_status = "failed"
                rollback_error = f" Config rollback failed: {rollback_exc}"
        session.update(
            status="failed",
            error=f"{exc}{rollback_error}",
            rollback_status=rollback_status,
        )
        recorded_attempts = {
            attempt.get("attempt_number") for attempt in session["attempts"]
        }
        if current_attempt_evidence and session["current_attempt"] not in recorded_attempts:
            current_attempt_evidence.update({
                "patches_applied": (
                    current_attempt_evidence["patches_proposed"]
                    if locals().get("patch_applied_ok")
                    else []
                ),
                "validation_errors": (
                    current_attempt_evidence.get("validation_errors") or []
                ) + [str(exc)],
                "patch_applied_ok": bool(locals().get("patch_applied_ok")),
                "patch_error": str(exc),
                "extraction_validation": locals().get("extraction_validation"),
                "rollback_status": rollback_status,
                "outcome": "rolled_back" if rollback_status == "restored" else "failed",
            })
            session["attempts"].append(current_attempt_evidence)
        if session["attempts"]:
            session["attempts"][-1]["rollback_status"] = rollback_status
    finally:
        session["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_session(job_id, session)
        try:
            await persist_repair_audit(session, db)
        except Exception as audit_exc:
            rollback = getattr(db, "rollback", None)
            if rollback is not None:
                await rollback()
            log.exception("ai_repair: durable audit write failed for job=%s: %s", job_id, audit_exc)

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

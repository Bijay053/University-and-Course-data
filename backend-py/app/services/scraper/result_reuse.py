"""Reuse deterministic course-owned evidence only against the current source bytes."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from html import unescape
from pathlib import Path
from sqlalchemy import text
from app.services.worker_fencing import OwnershipLost

# Capture source identity once per loaded worker, not mutable file timestamps.
try:
    _app_root = Path(__file__).resolve().parents[2]
    _BUILD_SOURCE_HASH = hashlib.sha256(b"".join(
        p.relative_to(_app_root).as_posix().encode() + b"\0" + p.read_bytes()
        for p in sorted(_app_root.rglob("*.py")))).hexdigest()
except OSError:
    _BUILD_SOURCE_HASH = None
    logging.getLogger(__name__).warning("Extraction reuse disabled: parser source identity unavailable")

log = logging.getLogger(__name__)
TTL_SECONDS = 300
MAX_BYTES = 262144
METHODS = ("regex", "css", "xpath", "static", "structured", "json_ld", "jsonld",
           "surrey_option", "uws_structured", "course_facts")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def cache_key(url, html, scope):
    if not html or not scope.get("config") or not scope.get("parser"):
        return None
    return fingerprint({"url": url, "source": hashlib.sha256(html.encode()).hexdigest(),
                        "scope": scope, "schema": 1})


def authoritative(result, url, html=None):
    if not html:
        return False
    if not isinstance(result, dict) or result.get("error") or result.get("_retry_after") or result.get("skip"):
        return False
    if result.get("gemini_calls", 0) or result.get("gemini_primary_cost_usd", 0):
        return False
    evidence = [e for e in result.get("evidence", []) if
                e.get("decision_status") == "selected" or e.get("selected") is True]
    if not evidence:
        return False
    source_text = " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())
    for e in evidence:
        method = str(e.get("extraction_method") or "")
        if e.get("source_url") != url or not method.startswith(METHODS) or re_ai(method):
            return False
        quote = " ".join(str(e.get("raw_value") or "").split())
        if not quote or quote not in source_text:
            return False
        if e.get("field_key") == "international_fee" and not re.search(r"\b(?:international|overseas|non[- ]UK)\b", quote, re.I):
            return False
    covered = {e.get("field_key") for e in evidence}
    # Atomic value/unit bundles have one tuition/duration evidence owner.
    bundles = {"fee_term": "international_fee", "fee_year": "international_fee",
               "currency": "international_fee", "duration_term": "duration",
               "degree_level": "course_name", "delivery_mode": "study_mode"}
    for key, value in (result.get("payload") or {}).items():
        if key == "course_website" and value == url:
            continue
        if value not in (None, "", [], {}) and bundles.get(key, key) not in covered:
            return False
        if key in covered and value not in (None, "", [], {}):
            if not any(e.get("field_key") == key and e.get("normalized_value") == value for e in evidence):
                return False
    return True


def re_ai(method):
    return any(word in method.lower() for word in ("gemini", "openai", "llm", "vision", "sibling", "central"))


async def ensure_schema(db):
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS scrape_extraction_result_cache (
            cache_key TEXT PRIMARY KEY, payload JSONB NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        )
    """))
    await db.execute(text("CREATE INDEX IF NOT EXISTS ix_scrape_result_cache_expiry ON scrape_extraction_result_cache(expires_at)"))


async def reuse(url, html, scope, extract, *, session_factory=None):
    key = cache_key(url, html, scope)
    if key is None:
        return await extract()
    if session_factory is None:
        from app.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    try:
        async with session_factory() as db:
            saved = await db.scalar(text(
                "SELECT payload FROM scrape_extraction_result_cache WHERE cache_key=:key AND expires_at>now()"
            ), {"key": key})
        if saved and saved.get("scope") == scope and saved.get("source") == fingerprint(html):
            result = saved.get("result")
            if authoritative(result, url, html) and saved.get("checksum") == fingerprint(result):
                hit = deepcopy(result)
                hit["_result_cache_hit"] = True
                return hit
    except OwnershipLost:
        raise
    except Exception:
        log.warning("Course result cache unavailable; performing fresh extraction for %s", url, exc_info=True)
    result = await extract()
    if authoritative(result, url, html):
        envelope = {"scope": scope, "source": fingerprint(html), "result": result,
                    "checksum": fingerprint(result)}
        encoded = json.dumps(envelope, default=str)
        if len(encoded.encode()) <= MAX_BYTES:
            try:
                async with session_factory() as db:
                    await db.execute(text(
                        "INSERT INTO scrape_extraction_result_cache(cache_key,payload,expires_at) "
                        "VALUES (:key,CAST(:payload AS JSONB),now()+interval '300 seconds') "
                        "ON CONFLICT(cache_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at"
                    ), {"key": key, "payload": encoded})
                    await db.execute(text(
                        "DELETE FROM scrape_extraction_result_cache WHERE cache_key IN "
                        "(SELECT cache_key FROM scrape_extraction_result_cache WHERE expires_at<now() LIMIT 100)"
                    ))
                    await db.commit()
            except OwnershipLost:
                raise
            except Exception:
                log.warning("Course result cache write failed for %s; fresh result retained", url, exc_info=True)
    return result


def current_scope(*, country, title, central, pdf, rules, ai_provider):
    from app.services.scraper.config import get_uni_config
    from app.config import settings
    from app.release_info import get_release_revision
    revision = get_release_revision()
    # Uncommitted development edits cannot share a parser identity safely.
    if settings.debug or not _BUILD_SOURCE_HASH or not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        return {"config": None, "parser": None}
    cfg = get_uni_config()
    return {"config": fingerprint(cfg.model_dump(mode="json")) if cfg else None,
            "parser": {"revision": revision, "source": _BUILD_SOURCE_HASH}, "audience": "international",
            "year": datetime.now(timezone.utc).year, "identity": title, "country": country,
            "central": fingerprint(central), "pdf": fingerprint(pdf),
            "rules": fingerprint(rules), "aiProvider": ai_provider}

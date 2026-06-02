"""Autonomous XHR/Fetch API discovery bridge.

Triggered mid-scrape when all preceding discovery tiers (YAML API, auto_config
API, BFS, Scrapy) return fewer than ``_TRIGGER_THRESHOLD`` course links AND
``discovery.auto_api_discovery: true`` is set in the per-uni YAML.

Pipeline
--------
1. Open the university's course-listing page in a headless browser
   (reuses ``xhr_interceptor.capture_xhr_signals``).
2. Intercept all JSON XHR/Fetch calls made during page load.
3. Classify each call via ``api_classifier.classify_captures``
   (SearchStax, Algolia, Elasticsearch, Solr, REST JSON).
4. If a candidate scores >= ``_MIN_CONFIDENCE``, build a
   ``GenericSearchApiConfig`` and immediately fetch course links.
5. Persist the discovered endpoint URL + auth hint to ``auto_config``
   so future scrapes hit the API tier without re-running XHR capture.

Privacy & security guarantees
------------------------------
* Session cookies are NEVER captured. ``xhr_interceptor.py`` filters
  ``cookie`` out of every request-header dict before storing a capture.
* Only STATIC, READ-ONLY API keys are persisted. ``_is_safe_auth_token()``
  accepts only ``Token <key>`` and ``ApiKey <key>`` patterns (SearchStax,
  Algolia, Funnelback). ``Bearer <jwt>`` and ``Basic <b64>`` tokens are
  REJECTED — they are user credentials or short-lived session tokens and
  are NEVER stored anywhere.
* No personal data is written to auto_config. Only the API endpoint URL,
  provider type, confidence score, and (when safe) the public search key.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Run auto-discovery when fewer than this many links found by prior tiers.
_TRIGGER_THRESHOLD: int = 10
# Minimum classifier confidence to accept an API candidate.
_MIN_CONFIDENCE: float = 0.45
# XHR capture page timeout (ms)
_CAPTURE_TIMEOUT_MS: int = 25_000

# ── Safe auth-token filter ────────────────────────────────────────────────────
# Only "Token <key>" and "ApiKey <key>" patterns represent stable, read-only,
# public search keys (SearchStax, Algolia, Funnelback). These are safe to store.
# "Bearer <jwt>" = session/OAuth token  → REJECT (short-lived user credential)
# "Basic <b64>"  = username:password    → REJECT (user credential)
# Anything else                          → REJECT (unknown, err on side of caution)
_SAFE_AUTH_RE = re.compile(
    r"^(?:Token|ApiKey|api-key)\s+[A-Za-z0-9._\-]{8,}$",
    re.I,
)


def _is_safe_auth_token(value: str) -> bool:
    """Return True only for public read-only API keys safe to store in auto_config.

    Accepts: ``Token <key>`` (SearchStax), ``ApiKey <key>`` (Algolia/Funnelback).
    Rejects: ``Bearer <jwt>``, ``Basic <b64>``, empty strings, unknown schemes.
    """
    return bool(value and _SAFE_AUTH_RE.match(value.strip()))


# ── Config builder ────────────────────────────────────────────────────────────

def _build_generic_api_config(
    classified: "ClassifiedAPI",  # noqa: F821
    base_url: str,
) -> dict[str, Any]:
    """Convert a ClassifiedAPI into a GenericSearchApiConfig-compatible dict."""
    endpoint = classified.endpoint_url
    results_path = classified.results_path or ""
    api_type = classified.api_type

    from urllib.parse import parse_qs, urlparse, urlunparse

    parsed = urlparse(endpoint)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    flat_params: dict[str, str] = {k: v[0] for k, v in qs.items()}

    # Strip pagination keys we own; keep all others as static params.
    _PAGINATION_KEYS = {"rows", "start", "page", "offset", "limit", "size", "from"}
    static_params = {k: v for k, v in flat_params.items() if k not in _PAGINATION_KEYS}

    # Rebuild clean endpoint URL (no query string — params go in params dict).
    clean_url = urlunparse(parsed._replace(query=""))

    page_size: Optional[int]
    page_size_param: str
    offset_param: str

    if api_type in ("searchstax", "solr"):
        page_size = 250
        page_size_param = "rows"
        offset_param = "start"
        if "q" not in static_params:
            static_params["q"] = "*"
    elif api_type == "algolia":
        page_size = 200
        page_size_param = "hitsPerPage"
        offset_param = "page"
        if "query" not in static_params:
            static_params["query"] = ""
    elif api_type == "elasticsearch":
        page_size = 200
        page_size_param = "size"
        offset_param = "from"
    else:
        # rest_json / graphql — single-page fetch
        page_size = None
        page_size_param = "limit"
        offset_param = "offset"

    url_fields = ["url", "course_url", "page_url", "link", "path", "href",
                  "courseUrl", "pageUrl", "Url"]
    title_fields = ["title", "name", "course_name", "Title", "Name",
                    "courseName", "label"]

    if not base_url:
        p = urlparse(endpoint)
        base_url = f"{p.scheme}://{p.netloc}"

    return dict(
        enabled=True,
        method="GET",
        url=clean_url,
        headers={},           # auth injected separately if safe
        params=static_params,
        root_path=results_path or None,
        url_fields=url_fields,
        title_fields=title_fields,
        allow_url_patterns=[],
        block_url_patterns=[],
        normalize_relative_urls=True,
        base_url=base_url,
        page_size=page_size,
        page_size_param=page_size_param,
        offset_param=offset_param,
        max_pages=20,
        max_courses=None,
    )


# ── Persistence ───────────────────────────────────────────────────────────────

async def _persist_discovery(
    university_id: int,
    classified: "ClassifiedAPI",  # noqa: F821
    auth_token: str,
    db: Any,
) -> None:
    """Write discovered API config into ``auto_config`` for future scrapes.

    Only safe, read-only public API keys are written.  Session cookies and
    Bearer/Basic tokens are never stored here.
    """
    try:
        from sqlalchemy import text
        import json as _json

        row = await db.execute(
            text("SELECT scrape_config FROM universities WHERE id = :uid"),
            {"uid": university_id},
        )
        rec = row.fetchone()
        if rec is None:
            return
        scrape_cfg = rec[0] or {}
        if isinstance(scrape_cfg, str):
            scrape_cfg = _json.loads(scrape_cfg)
        auto_cfg = dict(scrape_cfg.get("auto_config") or {})

        # Don't overwrite if endpoint is already the same (avoid clobbering
        # manual corrections made by an operator after a previous auto-discovery).
        if auto_cfg.get("_auto_api_url") == classified.endpoint_url:
            log.debug(
                "[AUTO_API] auto_config already has this endpoint — skipping persist"
            )
            return

        auto_cfg["_api_provider"] = classified.api_type
        auto_cfg["_api_endpoint_hint"] = classified.endpoint_url
        auto_cfg["_auto_api_url"] = classified.endpoint_url
        auto_cfg["_auto_api_results_path"] = classified.results_path or ""
        auto_cfg["_auto_api_confidence"] = round(classified.confidence, 3)

        # Only persist if it is a safe, read-only public search key.
        # Bearer JWTs, Basic credentials, and cookies are NEVER stored.
        if auth_token and _is_safe_auth_token(auth_token):
            auto_cfg["_auto_api_auth"] = auth_token
            log.debug("[AUTO_API] public search key stored in auto_config")
        elif auth_token:
            log.info(
                "[AUTO_API] auth header present but is not a safe public key "
                "(type=%s) — NOT stored in auto_config",
                auth_token.split()[0] if auth_token else "empty",
            )

        new_cfg = dict(scrape_cfg)
        new_cfg["auto_config"] = auto_cfg

        await db.execute(
            text(
                "UPDATE universities SET scrape_config = :cfg::jsonb WHERE id = :uid"
            ),
            {"cfg": _json.dumps(new_cfg), "uid": university_id},
        )
        await db.commit()
        log.info("[AUTO_API] Saved endpoint for future runs (%s)", classified.endpoint_url[:80])
    except Exception as exc:
        log.warning("[AUTO_API] failed to persist discovered API: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_auto_api_discovery(
    *,
    listing_url: str,
    university_id: int,
    base_url: str = "",
    db: Any,
    emit: Optional[Callable] = None,
) -> list[dict]:
    """Autonomous XHR/Fetch API discovery — called from the orchestrator.

    Runs XHR capture → classify → build config → fetch links.
    Persists discovered config to auto_config for future scrapes.

    Parameters
    ----------
    listing_url : str
        The university's main course-listing page URL.
    university_id : int
        DB primary key — used to persist the discovered config.
    base_url : str
        Origin URL (e.g. 'https://www.jcu.edu.au').  Derived from listing_url
        if empty.
    db : AsyncSession
        SQLAlchemy async session from the orchestrator's DB context.
    emit : Callable | None
        Progress callback — ``await emit("status", msg, phase="discover")``.

    Returns
    -------
    list[dict]
        Course link dicts (same format as other discovery tiers).
        Empty list if no API found or fetch failed.

    Privacy guarantee
    -----------------
    Session cookies are NEVER captured (stripped by xhr_interceptor before
    any capture is stored).  Bearer/Basic tokens are NEVER persisted.
    Only stable, read-only public search keys (Token/ApiKey scheme) are saved.
    """
    if not base_url:
        parsed = urlparse(listing_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    async def _emit(msg: str) -> None:
        log.info("[AUTO_API] %s", msg)
        if emit:
            try:
                await emit("status", f"[AUTO_API] {msg}", phase="discover")
            except Exception:
                pass

    await _emit(f"Scanning XHR traffic on {listing_url} (timeout {_CAPTURE_TIMEOUT_MS // 1000}s)…")

    # ── Step 1: XHR capture ───────────────────────────────────────────────────
    # xhr_interceptor strips session cookies from every captured request before
    # returning.  The _auth_header field is stored separately (not in
    # request_headers) so it is never accidentally logged.
    try:
        from app.services.scraper.xhr_interceptor import capture_xhr_signals
        captures = await capture_xhr_signals(listing_url, timeout_ms=_CAPTURE_TIMEOUT_MS)
    except Exception as exc:
        log.warning("[AUTO_API] XHR capture failed: %s", exc)
        return []

    if not captures:
        log.info("[AUTO_API] No JSON XHR calls intercepted — skipping auto-discovery")
        return []

    log.info("[AUTO_API] Intercepted %d JSON call(s) — classifying…", len(captures))

    # ── Step 2: classify ──────────────────────────────────────────────────────
    try:
        from app.services.scraper.api_classifier import classify_captures
        classified = classify_captures(captures)
    except Exception as exc:
        log.warning("[AUTO_API] Classification failed: %s", exc)
        return []

    if classified is None or classified.confidence < _MIN_CONFIDENCE:
        conf = classified.confidence if classified else 0.0
        log.info(
            "[AUTO_API] No API candidate reached confidence threshold "
            "(best=%.2f, need=%.2f) — falling through to BFS/browser",
            conf, _MIN_CONFIDENCE,
        )
        await _emit(
            f"No search API detected (best confidence {conf:.0%} < {_MIN_CONFIDENCE:.0%}) "
            f"— standard discovery tiers will be used"
        )
        return []

    # User-facing: tell the client what the AI found automatically
    provider_label = classified.api_type.title()  # e.g. "Searchstax", "Algolia"
    log.info(
        "[AUTO_API] Captured %s endpoint: %s (confidence %.0f%%)",
        provider_label, classified.endpoint_url[:80], classified.confidence * 100,
    )
    await _emit(f"Captured {provider_label} endpoint: {classified.endpoint_url[:80]}")

    # Recover auth token — only from the specific capture that matched.
    # Never pull cookies; only the Authorization header.
    auth_token = ""
    for cap in captures:
        if cap.url == classified.endpoint_url:
            raw_auth = cap.auth_token()
            if _is_safe_auth_token(raw_auth):
                auth_token = raw_auth
            elif raw_auth:
                log.info(
                    "[AUTO_API] Auth header present but not a safe public key "
                    "(scheme=%s) — will scrape without auth header",
                    raw_auth.split()[0] if raw_auth else "?",
                )
            break

    # ── Step 3: build config ──────────────────────────────────────────────────
    try:
        cfg_dict = _build_generic_api_config(classified, base_url)
        if auth_token:
            cfg_dict["headers"] = {"authorization": auth_token}

        from app.services.scraper.config.schema import GenericSearchApiConfig
        api_cfg = GenericSearchApiConfig(**cfg_dict)
    except Exception as exc:
        log.error("[AUTO_API] Config build failed: %s", exc, exc_info=True)
        return []

    log.info(
        "[AUTO_API] Built config — provider=%s results_path=%r page_size=%s",
        classified.api_type,
        classified.results_path,
        cfg_dict.get("page_size"),
    )
    await _emit(
        f"Built config — {classified.api_type}, "
        f"results path: {classified.results_path or '(root array)'}, "
        f"page size: {cfg_dict.get('page_size') or 'single-fetch'}"
    )

    # ── Step 4: fetch course links ────────────────────────────────────────────
    await _emit(f"Fetching courses from {provider_label} API…")
    try:
        from app.services.scraper.generic_search_api import fetch_yaml_api_links
        links = await fetch_yaml_api_links(api_cfg, emit=emit)
    except Exception as exc:
        log.error("[AUTO_API] Link fetch failed: %s", exc, exc_info=True)
        await _emit(f"Fetch failed ({exc}) — falling through to BFS")
        links = []

    if links:
        log.info("[AUTO_API] Added %d course URLs via auto-discovered %s API", len(links), provider_label)
        await _emit(f"Added {len(links)} course URLs via auto-discovered {provider_label} API")
    else:
        log.info("[AUTO_API] API returned 0 course links — falling through to BFS/browser")
        await _emit("API returned 0 course links — falling through to BFS/browser")

    # ── Step 5: persist for future scrapes ────────────────────────────────────
    # Fire-and-forget — does not block the scrape.
    # Only safe, public read-only keys are stored. No cookies. No Bearer tokens.
    if classified.confidence >= _MIN_CONFIDENCE:
        asyncio.create_task(
            _persist_discovery(
                university_id=university_id,
                classified=classified,
                auth_token=auth_token,  # already filtered by _is_safe_auth_token above
                db=db,
            )
        )

    return links

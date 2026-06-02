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
4. If a candidate scores ≥ ``_MIN_CONFIDENCE``, build a
   ``GenericSearchApiConfig`` and immediately fetch course links.
5. Persist the discovered endpoint URL + auth hint to ``auto_config``
   (under ``_auto_api_url``, ``_api_provider``, ``_api_endpoint_hint``,
   ``_auto_api_auth``) so future scrapes hit the API tier without
   re-running XHR capture.

Auth handling
-------------
The Authorization header value is read-only public search key (SearchStax
/ Algolia publish-key / Funnelback API key) — NOT a user credential.
It is stored in ``auto_config._auto_api_auth`` alongside the endpoint URL.
Treat it as semi-public; rotate via the provider console if compromised.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Run auto-discovery when fewer than this many links found by prior tiers.
_TRIGGER_THRESHOLD: int = 10
# Minimum classifier confidence to accept an API candidate.
_MIN_CONFIDENCE: float = 0.45
# XHR capture page timeout (ms)
_CAPTURE_TIMEOUT_MS: int = 25_000


def _build_generic_api_config(
    classified: "ClassifiedAPI",  # noqa: F821
    base_url: str,
) -> dict[str, Any]:
    """Convert a ClassifiedAPI into a GenericSearchApiConfig-compatible dict.

    Returns a plain dict (not a Pydantic model) so it can be passed directly
    to ``GenericSearchApiConfig(**result)`` by the caller.
    """
    from app.services.scraper.api_classifier import ClassifiedAPI  # noqa: F401

    endpoint = classified.endpoint_url
    results_path = classified.results_path or ""
    api_type = classified.api_type

    # Strip pagination params from URL so we control them via page_size/offset.
    # Common params that interfere with our own pagination: rows, start, page, q.
    # We keep them in the params dict so GenericSearchApiConfig sends them explicitly.
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(endpoint)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    # Flatten single-value lists from parse_qs
    flat_params: dict[str, str] = {k: v[0] for k, v in qs.items()}

    # Strip out pagination keys we want to own; keep all others as static params.
    _PAGINATION_KEYS = {"rows", "start", "page", "offset", "limit", "size", "from"}
    static_params = {k: v for k, v in flat_params.items() if k not in _PAGINATION_KEYS}

    # Rebuild clean endpoint URL (no query string — params go in params dict).
    clean_url = urlunparse(parsed._replace(query=""))

    # Provider-specific defaults.
    page_size: Optional[int]
    page_size_param: str
    offset_param: str

    if api_type in ("searchstax", "solr"):
        page_size = 250
        page_size_param = "rows"
        offset_param = "start"
        # Ensure wildcard query is present
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
        # rest_json / graphql — single-page fetch (many don't support pagination)
        page_size = None
        page_size_param = "limit"
        offset_param = "offset"

    # URL-field candidates ordered by specificity.
    url_fields = ["url", "course_url", "page_url", "link", "path", "href",
                  "courseUrl", "pageUrl", "Url"]
    title_fields = ["title", "name", "course_name", "Title", "Name",
                    "courseName", "label"]

    # Derive base_url from the endpoint if not passed
    if not base_url:
        p = urlparse(endpoint)
        base_url = f"{p.scheme}://{p.netloc}"

    return dict(
        enabled=True,
        method="GET",
        url=clean_url,
        headers={},           # auth injected separately after build
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


async def _persist_discovery(
    university_id: int,
    classified: "ClassifiedAPI",  # noqa: F821
    auth_token: str,
    db: Any,
) -> None:
    """Write discovered API config into ``auto_config`` for future scrapes."""
    try:
        from sqlalchemy import text

        row = await db.execute(
            text("SELECT scrape_config FROM universities WHERE id = :uid"),
            {"uid": university_id},
        )
        rec = row.fetchone()
        if rec is None:
            return
        import json as _json
        scrape_cfg = rec[0] or {}
        if isinstance(scrape_cfg, str):
            scrape_cfg = _json.loads(scrape_cfg)
        auto_cfg = dict(scrape_cfg.get("auto_config") or {})

        # Only update if endpoint differs (avoid clobbering manual corrections).
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
        # Semi-public read-only key (SearchStax publish key / Algolia search key).
        # Stored so future scrapes can re-use without re-running XHR capture.
        if auth_token:
            auto_cfg["_auto_api_auth"] = auth_token

        new_cfg = dict(scrape_cfg)
        new_cfg["auto_config"] = auto_cfg

        await db.execute(
            text(
                "UPDATE universities SET scrape_config = :cfg::jsonb WHERE id = :uid"
            ),
            {"cfg": _json.dumps(new_cfg), "uid": university_id},
        )
        await db.commit()
        log.info(
            "[AUTO_API] persisted discovered API to auto_config: provider=%r endpoint=%s",
            classified.api_type,
            classified.endpoint_url[:80],
        )
    except Exception as exc:
        log.warning("[AUTO_API] failed to persist discovered API: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass


async def run_auto_api_discovery(
    *,
    listing_url: str,
    university_id: int,
    base_url: str = "",
    db: Any,
    emit: Optional[Callable] = None,
) -> list[dict]:
    """Entry point called from the orchestrator.

    Runs XHR capture → classify → build config → fetch links.
    Persists discovered config to auto_config for future scrapes.

    Parameters
    ----------
    listing_url:
        The university's main course-listing page URL.
    university_id:
        DB primary key — used to persist the discovered config.
    base_url:
        Origin URL (e.g. 'https://www.jcu.edu.au').  Derived from listing_url
        if empty.
    db:
        AsyncSession from the orchestrator's DB context.
    emit:
        Progress callback — ``await emit("status", msg, phase="discover")``.

    Returns
    -------
    list[dict]
        Course link dicts (same format as other discovery tiers).
        Empty list if no API found or fetch failed.
    """
    if not base_url:
        parsed = urlparse(listing_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

    async def _emit(msg: str) -> None:
        log.info("[AUTO_API] %s", msg)
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:
                pass

    await _emit(
        f"[AUTO DISCOVER] scanning XHR traffic on {listing_url} "
        f"(timeout {_CAPTURE_TIMEOUT_MS // 1000}s)…"
    )

    # Step 1 — XHR capture
    try:
        from app.services.scraper.xhr_interceptor import capture_xhr_signals

        captures = await capture_xhr_signals(listing_url, timeout_ms=_CAPTURE_TIMEOUT_MS)
    except Exception as exc:
        log.warning("[AUTO_API] XHR capture failed: %s", exc)
        await _emit(f"[AUTO DISCOVER] XHR capture failed: {exc} — skipping")
        return []

    if not captures:
        await _emit("[AUTO DISCOVER] no JSON XHR calls intercepted — skipping")
        return []

    await _emit(f"[AUTO DISCOVER] intercepted {len(captures)} JSON call(s) — classifying…")

    # Step 2 — classify
    try:
        from app.services.scraper.api_classifier import classify_captures

        classified = classify_captures(captures)
    except Exception as exc:
        log.warning("[AUTO_API] classification failed: %s", exc)
        await _emit(f"[AUTO DISCOVER] classification error: {exc} — skipping")
        return []

    if classified is None:
        await _emit(
            "[AUTO DISCOVER] no JSON API scored above confidence threshold "
            f"({_MIN_CONFIDENCE:.0%}) — falling through to BFS/browser"
        )
        return []

    if classified.confidence < _MIN_CONFIDENCE:
        await _emit(
            f"[AUTO DISCOVER] best candidate {classified.endpoint_url[:60]} "
            f"confidence={classified.confidence:.2f} < {_MIN_CONFIDENCE:.2f} — skipping"
        )
        return []

    await _emit(
        f"[AUTO DISCOVER] found {classified.api_type!r} API at "
        f"{classified.endpoint_url[:80]} (confidence={classified.confidence:.2f})"
    )

    # Recover the auth token from the best capture that matched the endpoint.
    auth_token = ""
    for cap in captures:
        if cap.url == classified.endpoint_url:
            auth_token = cap.auth_token()
            break

    # Step 3 — build GenericSearchApiConfig
    try:
        cfg_dict = _build_generic_api_config(classified, base_url)
        if auth_token:
            cfg_dict["headers"] = {"authorization": auth_token}

        from app.services.scraper.config.schema import GenericSearchApiConfig

        api_cfg = GenericSearchApiConfig(**cfg_dict)
    except Exception as exc:
        log.error("[AUTO_API] config build failed: %s", exc, exc_info=True)
        await _emit(f"[AUTO DISCOVER] config build error: {exc} — skipping")
        return []

    # Step 4 — fetch links with discovered config
    await _emit(
        f"[AUTO DISCOVER] fetching courses from {classified.api_type!r} API…"
    )
    try:
        from app.services.scraper.generic_search_api import fetch_yaml_api_links

        links = await fetch_yaml_api_links(api_cfg, emit=emit)
    except Exception as exc:
        log.error("[AUTO_API] link fetch failed: %s", exc, exc_info=True)
        await _emit(f"[AUTO DISCOVER] fetch failed: {exc} — falling through to BFS")
        links = []

    await _emit(
        f"[AUTO DISCOVER] {len(links)} course link(s) from auto-discovered "
        f"{classified.api_type!r} API"
    )

    # Step 5 — persist to auto_config (fire-and-forget, don't block the scrape)
    if classified.confidence >= _MIN_CONFIDENCE:
        asyncio.create_task(
            _persist_discovery(
                university_id=university_id,
                classified=classified,
                auth_token=auth_token,
                db=db,
            )
        )

    return links

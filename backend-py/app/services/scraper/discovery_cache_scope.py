"""Stable scope identity for discovery URL cache entries.

Discovery results are only reusable when the scrape starts from the same URL
and uses the same effective discovery configuration.  The cache table is keyed
by university_id for backwards compatibility, so the scope fingerprint lives
inside the JSON link payload as a metadata record.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_discovery_start_url(url: str) -> str:
    """Return a stable identity for the operator-supplied discovery start URL."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = (parts.scheme or "https").lower()
    hostname = (parts.hostname or "").lower()
    if not hostname:
        return raw.rstrip("/")
    try:
        port = parts.port
    except ValueError:
        # An operator typo such as ``https://host:bad`` must not make the
        # optional cache layer abort discovery.
        return raw.rstrip("/")
    netloc = hostname
    if port and not (
        (scheme == "https" and port == 443)
        or (scheme == "http" and port == 80)
    ):
        netloc = f"{hostname}:{port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def discovery_cache_scope_key(
    *,
    scrape_url: str,
    discovery_config: Any,
    recipe: dict[str, Any] | None = None,
) -> str:
    """Hash every input that can materially change discovery coverage."""
    payload = {
        "version": 1,
        "scrape_url": normalize_discovery_start_url(scrape_url),
        "discovery": _jsonable(discovery_config),
        # Recipe seeds and strategy are merged after the cache gate in the
        # orchestrator, so include the raw recipe explicitly here.
        "recipe": _jsonable(recipe or {}),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def discovery_cache_metadata(
    *,
    scrape_url: str,
    scope_key: str,
) -> dict[str, Any]:
    return {
        "cache_meta": True,
        "scope_version": 1,
        "scope_key": scope_key,
        "scrape_url": normalize_discovery_start_url(scrape_url),
    }


def discovery_cache_coverage_sufficient(
    *,
    course_count: int,
    expected_min_courses: int | None,
) -> bool:
    """Return whether a discovery result is complete enough to cache/reuse."""
    required = max(5, int(expected_min_courses or 0))
    return int(course_count or 0) >= required
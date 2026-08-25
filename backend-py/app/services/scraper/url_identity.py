"""Shared course-URL identity helpers.

The scraper encounters the same course through live links, sitemaps, Wayback
CDX, resume checkpoints, and recovery sentinels.  Those sources commonly vary
the scheme, ``www`` prefix, default port, trailing slash, query ordering, and
tracking/audience parameters.  This module provides one conservative identity
key for deduplication without rewriting the URL that is actually fetched.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_NOISE_QUERY_KEYS = frozenset(
    {
        "_ga",
        "_gid",
        "_gl",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "studenttype",
        "student_type",
    }
)


def _semantic_query_pairs(query: str) -> list[tuple[str, str]]:
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in _NOISE_QUERY_KEYS or key_lower.startswith("utm_"):
            continue
        pairs.append((key, value))
    pairs.sort()
    return pairs


def normalize_course_fetch_url(url: str) -> str:
    """Normalize transport-only URL variants without losing semantic queries."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parse_target = raw if "://" in raw else f"//{raw}"
    try:
        parts = urlsplit(parse_target)
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return raw
        port = parts.port
        netloc = host
        if port and port not in (80, 443):
            netloc = f"{host}:{port}"
        query = urlencode(_semantic_query_pairs(parts.query), doseq=True)
        return urlunsplit(("https", netloc, parts.path, query, ""))
    except (TypeError, ValueError):
        return raw


def canonical_course_url_key(url: str | None) -> str:
    """Return a stable identity key while preserving semantic query params.

    Unknown query parameters are retained because some course sites use them
    to select a genuinely different international view.  Only known tracking
    and audience-toggle noise is removed.  Query pairs are sorted so links that
    differ only in parameter ordering compare equal.
    """
    if not url or not url.strip():
        return ""

    raw = url.strip()
    parse_target = raw if "://" in raw else f"//{raw}"
    try:
        parts = urlsplit(parse_target)
        host = (parts.hostname or "").lower().rstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return raw.lower().rstrip("/")

        port = parts.port
        host_port = host
        if port and port not in (80, 443):
            host_port = f"{host}:{port}"

        path = parts.path or ""
        if path != "/":
            path = path.rstrip("/")

        query_pairs = _semantic_query_pairs(parts.query)
        query = urlencode(query_pairs, doseq=True)

        result = f"{host_port}{path}"
        if query:
            result = f"{result}?{query}"
        return result
    except (TypeError, ValueError):
        return raw.lower().rstrip("/")
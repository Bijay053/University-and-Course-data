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


def strip_course_url_query_parameters(
    url: str,
    parameter_names: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> str:
    """Remove selected query parameters from the URL actually fetched.

    This is intentionally opt-in rather than part of the global identity
    normalizer: unknown query parameters can select a real international view.
    Per-university discovery config may use this for stale catalogue selectors
    such as UTAS ``?year=2025``, where the yearless canonical URL is the live,
    authoritative entry-year page.
    """
    raw = (url or "").strip()
    drop = {str(name).strip().lower() for name in parameter_names if str(name).strip()}
    if not raw or not drop:
        return raw
    try:
        parts = urlsplit(raw)
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in drop
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept, doseq=True), parts.fragment)
        )
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


def strip_and_deduplicate_course_query_parameters(
    items: list[dict],
    parameter_names: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> tuple[list[dict], int, int]:
    """Rewrite configured query parameters and collapse resulting duplicates."""
    rewritten_items: list[dict] = []
    seen: set[str] = set()
    rewritten = 0
    duplicates = 0
    for item in items:
        old_url = item.get("url") or ""
        new_url = strip_course_url_query_parameters(old_url, parameter_names)
        output_item = item
        if new_url != old_url:
            rewritten += 1
            output_item = dict(item)
            output_item["url"] = new_url
        key = canonical_course_url_key(new_url)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        rewritten_items.append(output_item)
    return rewritten_items, rewritten, duplicates


def deduplicate_latest_course_year_queries(
    items: list[dict],
) -> tuple[list[dict], int]:
    """Collapse course URLs that differ only by a ``year=20xx`` query.

    A yearless URL wins because catalogue sites normally use it as the moving
    canonical page.  When no yearless URL was discovered, the highest explicit
    four-digit year wins.  Every other semantic query parameter remains part of
    the identity, so audience, campus, mode, and programme selectors are never
    merged accidentally.
    """
    grouped: dict[str, list[tuple[int | None, int, dict]]] = {}
    passthrough: list[tuple[int, dict]] = []

    for index, item in enumerate(items):
        raw_url = item.get("url") or ""
        try:
            parts = urlsplit(raw_url)
            year: int | None = None
            other_pairs: list[tuple[str, str]] = []
            saw_course_year = False
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                if key.lower() == "year" and value.isdigit() and len(value) == 4:
                    parsed_year = int(value)
                    if 2000 <= parsed_year <= 2099:
                        year = max(year or parsed_year, parsed_year)
                        saw_course_year = True
                        continue
                other_pairs.append((key, value))

            # Yearless candidates must join the same group as their dated
            # variants, while unrelated query-bearing URLs remain distinct.
            group_url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(other_pairs, doseq=True),
                    "",
                )
            )
            group_parts = urlsplit(group_url)
            host = (group_parts.hostname or "").lower().rstrip(".")
            if host.startswith("www."):
                host = host[4:]
            port = group_parts.port
            host_port = (
                f"{host}:{port}"
                if port and port not in (80, 443)
                else host
            )
            path = group_parts.path or ""
            if path != "/":
                path = path.rstrip("/")
            # Unlike canonical_course_url_key(), this grouping key retains
            # every non-year pair, including studenttype/student_type. Those
            # parameters can select a materially different audience and fee.
            exact_pairs = sorted(
                (key, value)
                for key, value in parse_qsl(
                    group_parts.query,
                    keep_blank_values=True,
                )
            )
            key = repr((host_port, path, exact_pairs))
            if not key:
                passthrough.append((index, item))
                continue
            grouped.setdefault(key, []).append(
                (year if saw_course_year else None, index, item)
            )
        except (TypeError, ValueError):
            passthrough.append((index, item))

    kept: list[tuple[int, dict]] = list(passthrough)
    dropped = 0
    for variants in grouped.values():
        if len(variants) == 1:
            kept.append((variants[0][1], variants[0][2]))
            continue
        yearless = [variant for variant in variants if variant[0] is None]
        if yearless:
            winner = min(yearless, key=lambda variant: variant[1])
        else:
            winner = max(variants, key=lambda variant: (variant[0] or -1, -variant[1]))
        kept.append((winner[1], winner[2]))
        dropped += len(variants) - 1

    kept.sort(key=lambda pair: pair[0])
    return [item for _, item in kept], dropped
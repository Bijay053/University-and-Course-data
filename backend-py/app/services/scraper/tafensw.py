"""TAFE NSW international course discovery provider.

The TAFE NSW international course search page is a Nuxt SPA that lazy-loads
~153 courses via an internal Elasticsearch endpoint.  Scrape.do render of the
initial DOM only captures category navigation tabs, never individual course
links — making BFS useless for this university.

This provider calls the internal API directly:
  GET /api/international/course/search?from=0&size=500&query_string=

It then expands coursePackage arrays (mirroring the SPA's JS composable) and
builds URLs for each course, returning bare link dicts so the normal per-course
extraction pipeline runs on each detail page.

URL routing (from the Nuxt bundle's composable, function h vs p):
  * Regular courses (non-numeric course code, e.g. ICT50220, SIT50322):
      /international/courses/<slug>--<CODE>
  * Package courses (numeric packageId, e.g. 1349, 1473):
      /international/package/<slug>--<PACKAGEID>

Usage in YAML:
  discovery:
    tafensw_api:
      base_url: "https://www.tafensw.edu.au"
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_PART_OF_PKG_RE = re.compile(r"\s*-*\s*\(?\s*part\s+of\s+a?\s*package?\s*\)?\s*", re.IGNORECASE)
_SLUG_STRIP_RE  = re.compile(r"[^a-z0-9]+")
_NUMERIC_RE     = re.compile(r"^\d+$")

_API_PATH  = "/api/international/course/search"
_PAGE_SIZE = 500
_MAX_PAGES = 10          # safety ceiling; ~153 courses fit in one page of 500
_TIMEOUT   = 20.0


def _slugify(text: str) -> str:
    """Mimic the TAFE NSW Nuxt $slugify plugin used by IntCourseSearchCard."""
    text = text.lower()
    text = _SLUG_STRIP_RE.sub("-", text)
    return text.strip("-")


def _sanitise(title: str) -> str:
    return _PART_OF_PKG_RE.sub("", title).strip()


def _is_valid_base(hit: dict) -> bool:
    """P(u) filter: id must not be purely numeric; title must not contain
    '(part of a package)'.  Mirrors the JS composable's P() function."""
    hit_id = hit.get("_id", "")
    title  = hit.get("_source", {}).get("title", "")
    return bool(hit_id and not _NUMERIC_RE.match(hit_id) and not _PART_OF_PKG_RE.search(title))


def _expand_hit(hit: dict) -> list[dict[str, str]]:
    """Expand a single API hit into one or more entry dicts.

    Each returned dict has: ``{"title": ..., "id": ..., "path": ...}``

    ``path`` is either ``"courses"`` (non-numeric course code → detail page at
    ``/international/courses/<slug>--<code>``) or ``"package"`` (numeric
    packageId → study-pathway bundle at ``/international/package/<slug>--<id>``).

    The Nuxt bundle (Dg2d6mTw.js) defines two URL builder functions:
      h(title, courseId) → /international/courses/<slug>--<ID>   (regular)
      p(title, pkgId)    → /international/package/<slug>--<ID>   (package)

    All coursePackage.packageId values in the API are purely numeric (e.g.
    1349, 1473, 1355), so they all route to /international/package/.
    """
    src      = hit.get("_source", {})
    packages = src.get("coursePackage") or []
    results: list[dict[str, str]] = []

    if packages:
        seen: set[str] = set()
        for pkg in packages:
            pkg_id = pkg.get("packageId") or ""
            if not pkg_id or pkg_id in seen:
                continue
            seen.add(pkg_id)
            csl   = pkg.get("courseSimpleList") or []
            last  = csl[-1] if csl else {}
            title = _sanitise(last.get("title") or src.get("title", ""))
            # Numeric packageIds → /international/package/ (Nuxt function p)
            # Non-numeric packageIds → /international/courses/ (Nuxt function h)
            path  = "package" if _NUMERIC_RE.match(pkg_id) else "courses"
            results.append({"title": title, "id": pkg_id, "path": path})

    if _is_valid_base(hit):
        # Non-numeric base course code → /international/courses/ (function h)
        results.append({
            "title": _sanitise(src.get("title", "")),
            "id": hit["_id"],
            "path": "courses",
        })

    return results


async def fetch_tafensw_links(
    cfg: Any,
    *,
    emit=None,
) -> list[dict]:
    """Return bare link dicts for all TAFE NSW international courses.

    Each returned dict: {"name": ..., "url": ...}
    No searchstax_result key → normal per-course extraction runs on each URL.

    URL routing mirrors the Nuxt SPA:
      - Non-numeric course codes → /international/courses/<slug>--<code>
      - Numeric package IDs      → /international/package/<slug>--<id>
    """

    base_url: str = getattr(cfg, "base_url", "https://www.tafensw.edu.au").rstrip("/")

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit(msg)
            except Exception:  # noqa: BLE001
                pass

    await _emit("[TAFENSW] Fetching international course list from internal API...")

    links: list[dict] = []
    seen_ids: set[str] = set()
    total: int | None = None
    fetched  = 0

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    ) as client:
        for _page in range(_MAX_PAGES):
            params = {"from": fetched, "size": _PAGE_SIZE, "query_string": ""}
            try:
                resp = await client.get(_API_PATH, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("[TAFENSW] API fetch failed (from=%d): %s", fetched, exc)
                await _emit(f"[TAFENSW] API fetch failed (from={fetched}): {exc}")
                break

            hits_block = data.get("hits", {})
            if total is None:
                total = hits_block.get("total", {}).get("value")
            hits = hits_block.get("hits", [])
            if not hits:
                break

            for hit in hits:
                for entry in _expand_hit(hit):
                    eid = entry["id"]
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    slug = _slugify(entry["title"])
                    path = entry["path"]   # "courses" or "package"
                    url  = f"{base_url}/international/{path}/{slug}--{eid}"
                    links.append({"name": entry["title"], "url": url})

            fetched += len(hits)
            if total is not None and fetched >= total:
                break

    courses_count = sum(1 for l in links if "/international/courses/" in l["url"])
    packages_count = sum(1 for l in links if "/international/package/" in l["url"])
    await _emit(
        f"[TAFENSW] {len(links)} international course URLs built from API "
        f"({courses_count} courses, {packages_count} packages; API total={total})."
    )
    log.info(
        "[TAFENSW] %d links built from API (%d courses, %d packages, total=%s)",
        len(links), courses_count, packages_count, total,
    )
    return links

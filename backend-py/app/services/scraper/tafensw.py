"""TAFE NSW international course discovery provider.

The TAFE NSW international course search page is a Nuxt SPA that lazy-loads
153 courses via an internal Elasticsearch endpoint.  Scrape.do render of the
initial DOM only captures category navigation tabs, never individual course
links — making BFS useless for this university.

This provider calls the internal API directly:
  GET /api/international/course/search?from=0&size=500&query_string=

It then expands coursePackage arrays (mirroring the SPA's JS logic) and
builds /international/courses/<slug>--<id> URLs for each course, returning
bare link dicts so the normal per-course extraction pipeline runs on each.

Usage in YAML:
  discovery:
    tafensw_api:
      base_url: "https://www.tafensw.edu.au"
      allow_url_patterns:   # optional extra allow filter (default accepts all)
        - "/international/courses/"
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_PART_OF_PKG_RE = re.compile(r"\s*-*\s*\(?\s*part\s+of\s+a?\s*package?\s*\)?\s*", re.IGNORECASE)
_SLUG_STRIP_RE  = re.compile(r"[^a-z0-9]+")

_API_PATH  = "/api/international/course/search"
_PAGE_SIZE = 500
_MAX_PAGES = 10          # safety ceiling; 153 courses fit in one page of 500
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
    return bool(hit_id and not re.match(r"^\d+$", hit_id) and not _PART_OF_PKG_RE.search(title))


def _expand_hit(hit: dict) -> list[dict[str, str]]:
    """Expand a single API hit into one or more (name, url_id, title_for_slug)
    tuples, matching the SPA's package-expansion loop.

    Package IDs in the TAFE NSW API are always purely numeric (e.g. 1355, 1473).
    These are study-pathway bundle placeholders — the TAFE NSW website has no
    ``/international/courses/<slug>--<numeric-id>`` detail pages for them (all
    return 404).  Only proper TAFE course codes (e.g. ICT50220, SIT50322) have
    real detail pages.  Numeric packageIds are therefore skipped.
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
            # Numeric IDs are pathway-bundle placeholders with no detail page.
            if re.match(r"^\d+$", pkg_id):
                continue
            seen.add(pkg_id)
            csl   = pkg.get("courseSimpleList") or []
            last  = csl[-1] if csl else {}
            title = _sanitise(last.get("title") or src.get("title", ""))
            results.append({"title": title, "id": pkg_id})

    if _is_valid_base(hit):
        results.append({"title": _sanitise(src.get("title", "")), "id": hit["_id"]})

    return results


async def fetch_tafensw_links(
    cfg: Any,
    *,
    emit=None,
) -> list[dict]:
    """Return bare link dicts for all TAFE NSW international courses.

    Each returned dict: {"name": ..., "url": ...}
    No searchstax_result key → normal per-course extraction runs on each URL.
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
        for page in range(_MAX_PAGES):
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
                    url  = f"{base_url}/international/courses/{slug}--{eid}"
                    links.append({"name": entry["title"], "url": url})

            fetched += len(hits)
            if total is not None and fetched >= total:
                break

    await _emit(
        f"[TAFENSW] {len(links)} international course URLs built from API "
        f"(API total={total})."
    )
    log.info("[TAFENSW] %d links built from API (total=%s)", len(links), total)
    return links

"""Melbourne Polytechnic international course discovery provider.

The /search/?studentType=1 page is a React SPA (bundle.js) that populates
results client-side via an Umbraco JSON API.  Scrape.do render only returns
navigation links; BFS finds 0 course cards.

This provider calls the internal API directly:
  POST /umbraco/api/courseSearchApi/Search
  Body: {"studentType": 1, "query": "", "page": 1, "pageSize": 200}

Returns {"items": [...], "totalItems": 48, "totalPages": 1, ...}.
Each item carries a ``url`` field (e.g. ``/study/diploma/auslan/``) that is
the canonical course detail page.  This provider builds full URLs from those
and returns bare link dicts so normal per-course HTML extraction runs.

Usage in YAML:
  discovery:
    melbournepolytechnic_api:
      base_url: "https://www.melbournepolytechnic.edu.au"
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_PATH  = "/umbraco/api/courseSearchApi/Search"
_PAGE_SIZE = 200
_TIMEOUT   = 20.0


async def fetch_melbournepolytechnic_links(
    cfg: Any,
    *,
    emit=None,
) -> list[dict]:
    """Return bare link dicts for all Melbourne Polytechnic international courses.

    Each returned dict: {"name": ..., "url": ...}
    No searchstax_result key → normal per-course extraction runs on each URL.
    """
    base_url: str = getattr(cfg, "base_url", "https://www.melbournepolytechnic.edu.au").rstrip("/")

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit(msg)
            except Exception:  # noqa: BLE001
                pass

    await _emit("[MELBPOLY] Fetching international course list from Umbraco API...")

    links: list[dict] = []
    seen_urls: set[str] = set()

    hdrs = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": f"{base_url}/search/?studentType=1&query=&activeTab=0&page=1",
    }

    page = 1
    total_pages = 1  # updated from first response

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers=hdrs,
    ) as client:
        while page <= total_pages:
            body = {"studentType": 1, "query": "", "page": page, "pageSize": _PAGE_SIZE}
            try:
                resp = await client.post(_API_PATH, json=body)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("[MELBPOLY] API fetch failed (page=%d): %s", page, exc)
                await _emit(f"[MELBPOLY] API fetch failed (page={page}): {exc}")
                break

            if page == 1:
                total_pages = data.get("totalPages", 1)
                log.info(
                    "[MELBPOLY] API: totalItems=%s totalPages=%s",
                    data.get("totalItems"), total_pages,
                )

            for item in data.get("items") or []:
                rel_url = (item.get("url") or "").strip()
                if not rel_url:
                    continue
                full_url = base_url + rel_url if rel_url.startswith("/") else rel_url
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                name = (item.get("name") or "").strip()
                links.append({"name": name, "url": full_url})

            page += 1

    await _emit(
        f"[MELBPOLY] {len(links)} international course URLs built from Umbraco API."
    )
    log.info("[MELBPOLY] %d links built from Umbraco API", len(links))
    return links

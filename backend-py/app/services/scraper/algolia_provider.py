"""Generic Algolia search-API discovery provider.

Queries an Algolia index (paginated) and returns discovery links —
plain ``{name, url}`` dicts that feed the normal per-course extraction
pipeline.  No per-course HTML fetch is skipped; this provider only
replaces the HTML-crawl discovery phase.

Currently wired for Western Sydney University (wsu_prod_courses index)
but the implementation is university-agnostic — any YAML with a
``discovery.algolia`` block will use it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Optional

import httpx

from app.services.scraper.config.schema import AlgoliaDiscoveryConfig

log = logging.getLogger("scraper.algolia_provider")

_ALGOLIA_BASE = "https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"


async def fetch_algolia_links(
    cfg: AlgoliaDiscoveryConfig,
    emit: Callable[..., Coroutine[Any, Any, None]],
) -> list[dict]:
    """Paginate through an Algolia index and return discovery link dicts.

    Each dict has the shape ``{name: str, url: str}`` which is all the
    orchestrator needs to feed per-course extraction.

    Args:
        cfg:  AlgoliaDiscoveryConfig loaded from the per-uni YAML.
        emit: async SSE emitter (same signature as in orchestrator).

    Returns:
        List of ``{name, url}`` dicts (deduplicated by URL).
    """
    endpoint = _ALGOLIA_BASE.format(app_id=cfg.app_id, index=cfg.index_name)
    headers = {
        "X-Algolia-Application-Id": cfg.app_id,
        "X-Algolia-API-Key": cfg.api_key,
        "Content-Type": "application/json",
    }

    facet_filters: list[list[str]] = []
    if cfg.facet_filter:
        facet_filters = [[cfg.facet_filter]]

    links: list[dict] = []
    seen_urls: set[str] = set()
    page = 0
    total_pages: Optional[int] = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body = {
                "query": "",
                "hitsPerPage": cfg.hits_per_page,
                "page": page,
                "attributesToRetrieve": [cfg.url_field, cfg.name_field],
            }
            if facet_filters:
                body["facetFilters"] = facet_filters

            log.info(
                "[ALGOLIA] querying index=%s page=%d facet=%s",
                cfg.index_name, page, cfg.facet_filter or "(none)",
            )
            try:
                resp = await client.post(endpoint, headers=headers, content=json.dumps(body))
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[ALGOLIA] page %d fetch failed: %s", page, exc)
                await emit(
                    "status",
                    f"[DISCOVER] Algolia: page {page} fetch failed — {exc}",
                    phase="discover",
                )
                break

            if total_pages is None:
                total_pages = data.get("nbPages", 1)
                total_hits = data.get("nbHits", 0)
                log.info(
                    "[ALGOLIA] index=%s total_hits=%d total_pages=%d",
                    cfg.index_name, total_hits, total_pages,
                )
                await emit(
                    "status",
                    f"[DISCOVER] Algolia: {total_hits} courses found across {total_pages} page(s)",
                    phase="discover",
                )

            hits = data.get("hits", [])
            for hit in hits:
                url = (hit.get(cfg.url_field) or hit.get("url") or "").strip()
                name = (hit.get(cfg.name_field) or "").strip()
                if not url or not url.startswith("http"):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                links.append({"name": name, "url": url})

            page += 1
            if page >= (total_pages or 1):
                break

    log.info("[ALGOLIA] discovery complete: %d unique course links", len(links))
    await emit(
        "status",
        f"[DISCOVER] Algolia: {len(links)} unique course links extracted",
        phase="discover",
    )
    return links

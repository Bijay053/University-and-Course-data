"""JSON / REST API discovery provider.

When a university's advanced recipe sets ``discovery_strategy: json_api``
the orchestrator calls this module instead of running BFS/browser/sitemap
discovery.  The provider:

  1. Fetches ``api.endpoint`` (GET or POST, optional extra headers).
  2. Navigates to ``api.root_path`` (dot-separated) to reach the course array.
  3. For each item builds a course URL from ``api.course_url_template`` using
     all JSON keys as template variables (Python str.format_map).
  4. Applies ``api.fields`` to map JSON keys → standard scraper field names.
  5. Returns a list of link dicts: {name, url, [json_result]} that feed the
     normal dedup + staging loop in run_scrape.

If ``api.course_url_template`` is absent, ``url`` is taken directly from the
item (checking common key names: url, link, href, course_url, page_url).

Pagination is supported via an optional ``api.pagination`` block::

    pagination:
      type: offset          # only type currently supported
      page_param: page      # query-param name for page number (0- or 1-based)
      size_param: limit     # query-param name for page size
      page_size: 100
      max_pages: 50         # safety cap

When pagination is absent, the provider fetches the endpoint once.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("scraper.json_api_discovery")

_DEFAULT_TIMEOUT = 30.0
_URL_KEYS = ("url", "link", "href", "course_url", "page_url", "courseUrl", "pageUrl")


def _navigate(obj: Any, path: str) -> Any:
    """Navigate dot-separated path into nested dicts/lists.

    E.g. path='data.courses' on {'data': {'courses': [...]}} → [...]
    """
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)]
        else:
            return None
        if obj is None:
            return None
    return obj


def _build_url(item: dict, template: Optional[str]) -> Optional[str]:
    """Build a course URL from template or fall back to well-known key names."""
    if template:
        try:
            # urllib-encode each value so slugs/ids don't break the URL
            encoded = {k: urllib.parse.quote(str(v), safe="") for k, v in item.items() if v is not None}
            return template.format_map(encoded)
        except (KeyError, ValueError) as exc:
            log.debug("URL template format failed: %s — falling back to item keys", exc)
    for key in _URL_KEYS:
        if key in item and item[key]:
            return str(item[key])
    return None


def _apply_field_mapping(item: dict, field_map: dict) -> dict:
    """Map JSON keys → standard field names using api.fields config."""
    result: dict = {}
    for std_field, json_key in field_map.items():
        val = item.get(json_key)
        if val is not None:
            result[std_field] = val
    return result


async def _fetch_page(
    client: httpx.AsyncClient,
    method: str,
    endpoint: str,
    headers: dict,
    extra_params: dict,
) -> Any:
    """Fetch one page and return parsed JSON (or None on error)."""
    try:
        if method.upper() == "POST":
            resp = await client.post(endpoint, json=extra_params, headers=headers, timeout=_DEFAULT_TIMEOUT)
        else:
            resp = await client.get(endpoint, params=extra_params or None, headers=headers, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("JSON API fetch failed [%s %s params=%s]: %s", method, endpoint, extra_params, exc)
        return None


async def fetch_json_api_links(
    recipe: dict,
    emit: Optional[Callable] = None,
) -> list[dict]:
    """Fetch all course links from a JSON API endpoint defined in a recipe config.

    Args:
        recipe: The recipe dict (scrape_config.recipe).
        emit:   Optional progress callback(msg: str).

    Returns:
        List of link dicts: [{name, url, json_result: {payload, evidence}}]
        Compatible with the orchestrator's staging loop.
    """
    api_cfg: dict = recipe.get("api") or {}
    endpoint: str = api_cfg.get("endpoint", "").strip()
    if not endpoint:
        log.warning("json_api_discovery: no api.endpoint in recipe — returning 0 links")
        return []

    method: str = api_cfg.get("method", "GET").upper()
    headers: dict = dict(api_cfg.get("headers") or {})
    root_path: Optional[str] = api_cfg.get("root_path") or None
    url_template: Optional[str] = api_cfg.get("course_url_template") or None
    field_map: dict = dict(api_cfg.get("fields") or {})

    pagination: dict = dict(api_cfg.get("pagination") or {})
    pag_type = pagination.get("type", "")
    page_param = pagination.get("page_param", "page")
    size_param = pagination.get("size_param", "limit")
    page_size = int(pagination.get("page_size", 100))
    max_pages = int(pagination.get("max_pages", 50))

    def _emit(msg: str) -> None:
        log.info("[JSON_API] %s", msg)
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    links: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(follow_redirects=True) as client:
        if pag_type == "offset":
            _emit(f"Paginating {endpoint} (up to {max_pages} pages of {page_size})")
            for page_num in range(max_pages):
                extra_params = {page_param: page_num, size_param: page_size}
                data = await _fetch_page(client, method, endpoint, headers, extra_params)
                if data is None:
                    break
                items = _navigate(data, root_path) if root_path else data
                if not isinstance(items, list) or not items:
                    _emit(f"No items on page {page_num} — stopping pagination")
                    break
                before = len(links)
                for item in items:
                    _process_item(item, url_template, field_map, links, seen_urls)
                _emit(f"Page {page_num}: +{len(links) - before} courses (total {len(links)})")
                await asyncio.sleep(0.1)
        else:
            _emit(f"Fetching {endpoint}")
            data = await _fetch_page(client, method, endpoint, headers, {})
            if data is None:
                return []
            items = _navigate(data, root_path) if root_path else data
            if not isinstance(items, list):
                log.error(
                    "json_api_discovery: root_path=%r did not resolve to a list "
                    "(got %s) — check root_path config",
                    root_path, type(items).__name__,
                )
                return []
            for item in items:
                _process_item(item, url_template, field_map, links, seen_urls)
            _emit(f"Fetched {len(links)} course links from {len(items)} JSON items")

    _emit(f"Done — {len(links)} unique course links")
    return links


def _process_item(
    item: dict,
    url_template: Optional[str],
    field_map: dict,
    links: list,
    seen_urls: set,
) -> None:
    """Convert one JSON item → link dict and append to links (dedup by URL)."""
    if not isinstance(item, dict):
        return

    url = _build_url(item, url_template)
    if not url or url in seen_urls:
        return
    seen_urls.add(url)

    # Map JSON keys to standard field names
    mapped = _apply_field_mapping(item, field_map) if field_map else {}

    name = (
        mapped.get("course_name")
        or item.get("title")
        or item.get("name")
        or item.get("course_name")
        or item.get("courseName")
        or "Unknown Course"
    )

    # Build a minimal payload from mapped fields so extraction can use them
    payload: dict = {}
    field_name_map = {
        "course_name": "course_name",
        "degree_level": "degree_level",
        "study_mode_raw": "study_mode",
        "duration": "duration",
        "campus": "course_location",
        "description": "description",
    }
    for src, dst in field_name_map.items():
        if src in mapped:
            payload[dst] = mapped[src]

    links.append({
        "name": str(name),
        "url": url,
        # json_result carries prebuilt payload + raw item for the extractor
        "json_result": {
            "raw": item,
            "mapped": mapped,
            "payload": payload,
        },
    })

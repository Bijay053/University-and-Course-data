"""Generic search-API provider for the autonomous scraping pipeline.

When :func:`site_probe.probe_site` detects a hosted search API (SearchStax,
Algolia, etc.) the probe writes ``_api_provider`` and ``_api_endpoint_hint``
into ``auto_config``.  On the *next* scrape the orchestrator reads those keys
and calls :func:`fetch_generic_api_links` here instead of running BFS / browser
discovery — no YAML configuration required.

Supported providers
-------------------
* **searchstax** — Lucene/Solr core hosted on SearchStax.  Paginated via
  ``rows`` + ``start`` parameters.  Filters to ``sectionType_s:course``;
  falls back to unfiltered if the filter returns 0 results (some cores use
  different section types).  Field mapping follows the same conventions as the
  HUD-specific ``searchstax_hud.py`` but is applied generically.
* **algolia** — Algolia search-as-a-service.  Uses the public search key
  extracted by the probe (stored in ``_api_auth_hint``) to browse the index.
  Field names are site-specific; Gemini maps them on first run.

Both providers return a list of link dicts compatible with the orchestrator's
``links`` list format.  Each dict has at minimum ``"name"`` and ``"url"``.
When the provider can extract more structured data (degree level, IELTS, etc.)
it embeds it under ``"auto_extracted"``; the orchestrator passes this to the
staging layer to skip per-course extraction for those fields.

Self-heal note
--------------
If avg completeness after staging is below the 70 % cascade threshold (see
orchestrator.py), the system automatically re-probes and re-scrapes.  The
generic provider is intentionally "best-effort" — it extracts what it can from
structured Solr/Algolia fields and the ``content`` text blob, and leaves the
rest blank for the completeness gate to catch.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

# ── Shared field patterns ─────────────────────────────────────────────────────
# These patterns are applied to the Solr ``content`` field (≈15 KB page text
# concatenated from the rendered page).  They are the same patterns used in
# the HUD provider and are generic enough to work across education sites.

_IELTS_RE = re.compile(
    r"IELTS\s*(?:overall|minimum|score|band)?[:\s]*(\d+(?:\.\d)?)",
    re.I,
)
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d)?)\s*(?:to\s*\d+(?:\.\d)?\s*)?year[s]?"
    r"|(\d+)\s*months?",
    re.I,
)
_DEGREE_LEVEL_MAP = {
    "undergraduate": "undergraduate",
    "postgraduate": "postgraduate",
    "phd": "doctorate",
    "doctorate": "doctorate",
    "research": "postgraduate",
    "foundation": "undergraduate",
    "diploma": "postgraduate",
    "certificate": "postgraduate",
    "master": "postgraduate",
    "bachelor": "undergraduate",
}


# ── SearchStax generic client ─────────────────────────────────────────────────

def _build_searchstax_base_url(endpoint_hint: str) -> str:
    """Convert a probe endpoint_hint to a full SearchStax select URL.

    The probe captures the endpoint as a regex match from the page source,
    e.g. ``searchcloud-1-eu-west-2.searchstax.com/29847/myuni-1234/``.
    We need ``https://<host>/emselect``.
    """
    hint = endpoint_hint.strip()
    # Strip any path suffix after the core identifier (keep host/tenant/core/)
    # Pattern: {host}/{tenant}/{core}/
    m = re.match(
        r"((?:https?://)?[\w.-]+\.searchstax\.com/\d+/[\w-]+)",
        hint,
        re.I,
    )
    base = m.group(1) if m else hint.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    return base.rstrip("/") + "/emselect"


async def fetch_searchstax_generic(
    endpoint_hint: str,
    token: str,
    emit: Callable[..., Any] | None = None,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict]:
    """Paginate a SearchStax Solr core and return link dicts.

    Parameters
    ----------
    endpoint_hint:
        The endpoint string captured by the probe (e.g. the regex match from
        the page source).
    token:
        Bearer token for ``Authorization: Token <token>`` header.
    emit:
        Optional logging callable (matches orchestrator's emit signature).
    page_size:
        Rows per page.  Default 100 matches HUD provider.
    max_pages:
        Hard ceiling on pagination to prevent runaway loops.
    """
    import httpx

    select_url = _build_searchstax_base_url(endpoint_hint)
    log.info("[GENERIC_SS] Querying SearchStax: %s", select_url)

    headers = {
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    }

    links: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        for page in range(max_pages):
            start = page * page_size
            params: dict[str, Any] = {
                "fq": "sectionType_s:course",
                "rows": page_size,
                "start": start,
                "wt": "json",
                "fl": "*",
            }
            try:
                resp = await client.get(select_url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[GENERIC_SS] fetch failed page=%d: %s", page, exc)
                break

            docs = (data.get("response") or {}).get("docs") or []

            # If the section-type filter returns nothing, retry without it once
            if page == 0 and not docs:
                log.info(
                    "[GENERIC_SS] sectionType_s:course returned 0 — retrying unfiltered"
                )
                params_nofilter = {k: v for k, v in params.items() if k != "fq"}
                try:
                    resp2 = await client.get(
                        select_url, params=params_nofilter, headers=headers
                    )
                    resp2.raise_for_status()
                    docs = (resp2.json().get("response") or {}).get("docs") or []
                    log.info("[GENERIC_SS] unfiltered returned %d docs", len(docs))
                except Exception as exc2:
                    log.error("[GENERIC_SS] unfiltered fetch failed: %s", exc2)
                    break

            if not docs:
                break

            num_found = (data.get("response") or {}).get("numFound", 0)
            if page == 0:
                log.info("[GENERIC_SS] numFound=%d page_size=%d", num_found, page_size)
                if emit:
                    emit("log", f"SearchStax: {num_found} docs found, paginating …")

            for doc in docs:
                link = _map_searchstax_doc(doc)
                if not link:
                    continue
                url = link["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                links.append(link)

            if len(docs) < page_size:
                break

    log.info("[GENERIC_SS] total links extracted: %d", len(links))
    return links


def _map_searchstax_doc(doc: dict) -> dict | None:
    """Map a Solr document to a link dict.

    Tries common field name patterns across different SearchStax education
    deployments.  Unknown fields are ignored — the completeness gate and
    self-heal loop will fill gaps on the next pass.
    """
    # Course URL — required
    url = doc.get("url") or doc.get("link") or doc.get("course_url_s") or ""
    if not url:
        return None

    # Course name — prefer explicit title fields, fall back to URL slug
    name = (
        doc.get("h1")
        or doc.get("searchTitle")
        or doc.get("title")
        or doc.get("course_title_s")
        or doc.get("name_s")
        or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    )

    # Degree level
    level_raw = (
        doc.get("study_level_s")
        or doc.get("level_s")
        or doc.get("degree_level_s")
        or doc.get("qualification_s")
        or ""
    ).lower()
    degree_level = _map_degree_level(level_raw)

    # Content blob for text extraction
    content = doc.get("content") or ""

    # IELTS from content
    ielts: float | None = None
    m = _IELTS_RE.search(content)
    if m:
        try:
            ielts = float(m.group(1))
        except ValueError:
            pass

    # Duration from content
    duration: str | None = None
    md = _DURATION_RE.search(content)
    if md:
        if md.group(1):
            y = float(md.group(1))
            duration = f"{y:.0f} year{'s' if y != 1 else ''}"
        elif md.group(2):
            duration = f"{md.group(2)} months"

    # Description: first non-empty paragraph from content
    description: str | None = None
    if content:
        paras = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 80]
        if paras:
            description = paras[0][:500]

    extracted: dict[str, Any] = {}
    if degree_level:
        extracted["degree_level"] = degree_level
    if ielts is not None:
        extracted["ielts_overall"] = ielts
    if duration:
        extracted["duration"] = duration
    if description:
        extracted["description"] = description

    return {
        "name": name,
        "url": url,
        "_auto_extracted": extracted,
        "_source": "generic_searchstax",
    }


def _map_degree_level(raw: str) -> str | None:
    for token, level in _DEGREE_LEVEL_MAP.items():
        if token in raw:
            return level
    return None


# ── Algolia generic client ────────────────────────────────────────────────────

async def fetch_algolia_generic(
    endpoint_hint: str,
    api_key_hint: str | None,
    emit: Callable[..., Any] | None = None,
) -> list[dict]:
    """Browse an Algolia index and return link dicts.

    The probe captures the Algolia app ID and index name from the page source.
    We need the public search API key (typically embedded in the JS bundle).
    Without a key we cannot query Algolia — this falls back gracefully to an
    empty list so the orchestrator cascades to BFS/browser discovery.
    """
    import httpx

    if not api_key_hint:
        log.warning("[GENERIC_ALGOLIA] No API key available — skipping Algolia fetch")
        return []

    # Extract app ID and index from the hint
    # hint format: "appid-dsn.algolia.net/1/indexes/indexname/"
    m = re.match(
        r"([\w-]+)-dsn\.algolia(?:net)?\.(?:com|net)/1/indexes/([\w-]+)",
        endpoint_hint,
        re.I,
    )
    if not m:
        log.warning("[GENERIC_ALGOLIA] Could not parse endpoint hint: %s", endpoint_hint[:80])
        return []

    app_id = m.group(1)
    index_name = m.group(2)
    browse_url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index_name}/browse"

    log.info("[GENERIC_ALGOLIA] Browsing index: %s / %s", app_id, index_name)

    links: list[dict] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(50):
            params: dict[str, Any] = {"hitsPerPage": 200}
            if cursor:
                params["cursor"] = cursor
            headers = {
                "X-Algolia-Application-Id": app_id,
                "X-Algolia-API-Key": api_key_hint,
            }
            try:
                resp = await client.get(browse_url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.error("[GENERIC_ALGOLIA] fetch failed: %s", exc)
                break

            hits = data.get("hits") or []
            for hit in hits:
                url = hit.get("url") or hit.get("link") or hit.get("permalink") or ""
                if not url:
                    continue
                name = (
                    hit.get("title")
                    or hit.get("name")
                    or hit.get("course_title")
                    or url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
                )
                links.append({
                    "name": name,
                    "url": url,
                    "_auto_extracted": {},
                    "_source": "generic_algolia",
                })

            cursor = data.get("cursor")
            if not cursor:
                break

    log.info("[GENERIC_ALGOLIA] total links extracted: %d", len(links))
    return links


# ── Phase 4B: field-mapping helpers ──────────────────────────────────────────

def _navigate_path(obj: Any, dot_path: str) -> Any:
    """Navigate a dot-notation path into a nested dict/list.

    An empty *dot_path* returns *obj* unchanged (identity path).
    """
    if not dot_path:
        return obj
    node: Any = obj
    for key in dot_path.split("."):
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and key.isdigit():
            idx = int(key)
            node = node[idx] if idx < len(node) else None
        else:
            return None
    return node


def _apply_field_mapping(
    item: dict,
    field_mapping: dict[str, str],
) -> dict[str, Any]:
    """Apply ``{internal_field: api_dot_path}`` to one result item.

    Returns a dict keyed by internal field names populated from the item.
    Fields that navigate to None or "" are omitted.
    """
    result: dict[str, Any] = {}
    for internal_field, api_path in field_mapping.items():
        value = _navigate_path(item, api_path)
        if value is not None and value != "":
            result[internal_field] = value
    return result


def _item_to_link(
    item: dict,
    field_mapping: dict[str, str],
    base_url: str = "",
) -> dict | None:
    """Convert one mapped item to a link dict.

    Returns None if neither ``url`` nor ``course_name`` is extractable.
    """
    mapped = _apply_field_mapping(item, field_mapping)
    url = str(mapped.get("url") or "").strip()
    name = str(mapped.get("course_name") or "").strip()

    if not url and not name:
        return None

    # Make relative URLs absolute
    if url and not url.startswith("http") and base_url:
        from urllib.parse import urljoin
        url = urljoin(base_url, url)

    link: dict[str, Any] = {"url": url, "name": name}
    # Forward all other mapped fields as auto_extracted (skips per-course scrape)
    auto_extracted = {k: v for k, v in mapped.items() if k not in ("url", "course_name")}
    if auto_extracted:
        link["auto_extracted"] = auto_extracted
    return link


# ── Phase 4B: Generic REST JSON fetcher ──────────────────────────────────────

async def fetch_rest_json_links(
    endpoint: str,
    field_mapping: dict[str, str],
    results_path: str = "",
    emit: Callable[..., Any] | None = None,
    page_size: int = 100,
    max_pages: int = 30,
) -> list[dict]:
    """Fetch a generic REST JSON API using a pre-computed field mapping.

    Tries three pagination strategies in order:
    1. ``page`` + ``per_page`` (most common)
    2. ``offset`` + ``limit``
    3. Single request (no pagination params)

    Parameters
    ----------
    endpoint:
        Base URL of the API endpoint (captured by XHR interceptor).
    field_mapping:
        ``{internal_field: api_dot_path}`` from ``ApiFieldMapping.field_mapping``.
    results_path:
        Dot-path to the results array in the response (e.g. ``"data"``, ``""``).
    """
    import httpx
    from urllib.parse import urlparse, urlunparse, urlencode, parse_qs

    log.info("[GENERIC_REST] endpoint=%s results_path=%r", endpoint[:80], results_path)
    if emit:
        emit("log", f"Generic REST: fetching {endpoint[:60]} …")

    parsed = urlparse(endpoint)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    links: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        timeout=30.0, verify=False,
        headers={"Accept": "application/json"},
    ) as client:
        # Strategy A: page + per_page pagination
        for page in range(max_pages):
            try:
                resp = await client.get(
                    endpoint,
                    params={"page": page + 1, "per_page": page_size, "limit": page_size},
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as exc:
                log.warning("[GENERIC_REST] page %d fetch failed: %s", page, exc)
                break

            items_root = _navigate_path(body, results_path) if results_path else body
            if isinstance(items_root, list):
                items = items_root
            elif isinstance(items_root, dict):
                # Find first list value
                items = next(
                    (v for v in items_root.values() if isinstance(v, list)), []
                )
            else:
                items = []

            if not items:
                break

            if page == 0 and emit:
                emit("log", f"Generic REST: {len(items)} items in page 1")

            for item in items:
                if not isinstance(item, dict):
                    continue
                link = _item_to_link(item, field_mapping, base_url)
                if not link:
                    continue
                key = link.get("url") or link.get("name", "")
                if key in seen:
                    continue
                seen.add(key)
                links.append(link)

            if len(items) < page_size:
                break  # last page

    log.info("[GENERIC_REST] extracted %d links", len(links))
    return links


# ── Phase 4B: Generic Elasticsearch / OpenSearch fetcher ─────────────────────

async def fetch_elasticsearch_links(
    endpoint: str,
    field_mapping: dict[str, str],
    emit: Callable[..., Any] | None = None,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict]:
    """Paginate an Elasticsearch / OpenSearch ``/_search`` endpoint.

    Uses ``from`` + ``size`` pagination with a ``match_all`` query.
    Fields are extracted from ``_source`` of each hit.
    """
    import httpx
    from urllib.parse import urlparse, urlunparse

    # Ensure endpoint ends in /_search
    search_url = endpoint
    if "/_search" not in search_url:
        search_url = search_url.rstrip("/") + "/_search"

    log.info("[GENERIC_ES] endpoint=%s", search_url[:80])
    if emit:
        emit("log", f"Generic Elasticsearch: fetching {search_url[:60]} …")

    base_url = urlunparse(urlparse(search_url)[:2] + ("", "", "", ""))
    links: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        timeout=30.0, verify=False,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    ) as client:
        for page in range(max_pages):
            body = {
                "from": page * page_size,
                "size": page_size,
                "query": {"match_all": {}},
            }
            try:
                resp = await client.post(search_url, json=body)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                log.warning("[GENERIC_ES] page %d failed: %s", page, exc)
                break

            hits_wrapper = data.get("hits") or {}
            hits = hits_wrapper.get("hits") or [] if isinstance(hits_wrapper, dict) else []

            if not hits:
                break
            if page == 0:
                total = (hits_wrapper.get("total") or {})
                n = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
                log.info("[GENERIC_ES] total=%d page_size=%d", n, page_size)
                if emit:
                    emit("log", f"Elasticsearch: {n} documents found")

            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                # Merge _source fields into top-level for mapping
                source = hit.get("_source") or {}
                merged = {**hit, **source}
                link = _item_to_link(merged, field_mapping, base_url)
                if not link:
                    continue
                key = link.get("url") or link.get("name", "")
                if key in seen:
                    continue
                seen.add(key)
                links.append(link)

            if len(hits) < page_size:
                break

    log.info("[GENERIC_ES] extracted %d links", len(links))
    return links


# ── Phase 4B: Generic GraphQL fetcher ────────────────────────────────────────

_GQL_COURSE_QUERIES = [
    # Try common course-list query shapes used by education CMS platforms
    "{ courses { id name url title level fee duration } }",
    "{ programmes { id title url level tuitionFee duration } }",
    "{ programs { id name url degreeLevel internationalFee duration } }",
    "{ allCourses { id name url } }",
]


async def fetch_graphql_links(
    endpoint: str,
    field_mapping: dict[str, str],
    emit: Callable[..., Any] | None = None,
) -> list[dict]:
    """Attempt a generic GraphQL query to retrieve course listings.

    Tries heuristic query templates in order; returns links from the first
    successful response.  This is intentionally best-effort — many GraphQL APIs
    require introspection or site-specific queries.  If all templates fail,
    returns [] and the completeness gate triggers a re-probe.
    """
    import httpx

    log.info("[GENERIC_GQL] endpoint=%s", endpoint[:80])
    if emit:
        emit("log", f"Generic GraphQL: probing {endpoint[:60]} …")

    async with httpx.AsyncClient(
        timeout=20.0, verify=False,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    ) as client:
        for query in _GQL_COURSE_QUERIES:
            try:
                resp = await client.post(endpoint, json={"query": query})
                if resp.status_code not in (200, 201):
                    continue
                data = resp.json()
                if "errors" in data and "data" not in data:
                    continue
                gql_data = data.get("data") or {}
                # Find the first list value under data
                items = next(
                    (v for v in gql_data.values() if isinstance(v, list)), []
                )
                if not items:
                    continue

                log.info("[GENERIC_GQL] got %d items with query: %s", len(items), query[:60])
                if emit:
                    emit("log", f"GraphQL: {len(items)} course items found")

                links: list[dict] = []
                seen: set[str] = set()
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    link = _item_to_link(item, field_mapping)
                    if not link:
                        continue
                    key = link.get("url") or link.get("name", "")
                    if key in seen:
                        continue
                    seen.add(key)
                    links.append(link)

                if links:
                    return links
            except Exception as exc:
                log.debug("[GENERIC_GQL] query failed: %s", exc)

    log.warning("[GENERIC_GQL] no usable response from %s — returning []", endpoint[:60])
    return []


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def fetch_generic_api_links(
    provider: str,
    endpoint_hint: str,
    auto_config: dict,
    emit: Callable[..., Any] | None = None,
) -> list[dict]:
    """Top-level entry point called by the orchestrator.

    Dispatches based on ``provider`` (set from HTML-scan or XHR classification).
    Phase 4B adds ``_api_type`` / ``_field_mapping`` keys to auto_config to
    enable REST / Elasticsearch / GraphQL dispatch without provider-specific code.

    Parameters
    ----------
    provider:
        Provider name from ``auto_config._api_provider``
        (e.g. ``"searchstax"``, ``"algolia"``, ``"elasticsearch"``, ``"rest_json"``).
    endpoint_hint:
        Raw endpoint string from ``auto_config._api_endpoint_hint``.
    auto_config:
        The full auto_config dict.  May contain:
        - ``_api_token_env`` / ``_api_auth_hint`` for auth.
        - ``_api_type`` — explicit type override (Phase 4B XHR classification).
        - ``_field_mapping`` — field mapping from schema analyzer (Phase 4B).
        - ``_results_path`` — dot-path to results array (Phase 4B).
    emit:
        Optional logging callable.
    """
    # Phase 4B: explicit api_type from XHR classification overrides provider
    api_type = auto_config.get("_api_type") or provider
    field_mapping: dict[str, str] = auto_config.get("_field_mapping") or {}
    results_path: str = auto_config.get("_results_path") or ""

    if api_type == "searchstax" or provider == "searchstax":
        token_env = auto_config.get("_api_token_env") or "GENERIC_SEARCHSTAX_TOKEN"
        token = os.environ.get(token_env, "")
        if not token:
            for env_name in ("HUD_SEARCHSTAX_TOKEN", "SEARCHSTAX_TOKEN"):
                token = os.environ.get(env_name, "")
                if token:
                    break
        if not token:
            log.warning(
                "[GENERIC_SS] No token found in env vars — attempting unauthenticated"
            )
        return await fetch_searchstax_generic(endpoint_hint, token, emit=emit)

    if api_type in ("algolia",) or provider in ("algolia",):
        api_key = auto_config.get("_api_auth_hint") or os.environ.get("ALGOLIA_API_KEY", "")
        return await fetch_algolia_generic(endpoint_hint, api_key, emit=emit)

    # Phase 4B: XHR-discovered API types
    if api_type == "elasticsearch" and field_mapping:
        return await fetch_elasticsearch_links(
            endpoint_hint, field_mapping, emit=emit,
        )

    if api_type == "graphql" and field_mapping:
        return await fetch_graphql_links(endpoint_hint, field_mapping, emit=emit)

    if api_type in ("rest_json", "solr") and field_mapping:
        return await fetch_rest_json_links(
            endpoint_hint, field_mapping,
            results_path=results_path,
            emit=emit,
        )

    if field_mapping and endpoint_hint:
        # Generic fallback: try REST JSON with whatever field mapping we have
        log.info(
            "[GENERIC_API] provider=%r api_type=%r — falling back to REST JSON with field mapping",
            provider, api_type,
        )
        return await fetch_rest_json_links(
            endpoint_hint, field_mapping,
            results_path=results_path,
            emit=emit,
        )

    log.warning(
        "[GENERIC_API] Unknown provider %r / api_type %r and no field_mapping — no links fetched",
        provider, api_type,
    )
    return []


# ── YAML-driven generic API (discovery.generic_search_api) ───────────────────

async def fetch_yaml_api_links(cfg: Any, emit: Callable[..., Any] | None = None) -> list[dict]:
    """Fetch course links from a YAML-configured JSON/REST API endpoint.

    Called by the orchestrator when ``discovery.generic_search_api`` is set
    in the per-uni YAML.  Runs BEFORE BFS/browser discovery — API-first.

    Parameters
    ----------
    cfg:
        ``GenericSearchApiConfig`` instance (from schema.py).
    emit:
        Optional logging callable (matches orchestrator emit signature).
    """
    import httpx

    def _emit(msg: str) -> None:
        log.info("[YAML_API] %s", msg)
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    _emit(f"Starting YAML generic_search_api: {cfg.method} {cfg.url}")

    # ── Build allow/block regex sets ──────────────────────────────────────────
    _allow_res = []
    for pat in (cfg.allow_url_patterns or []):
        try:
            _allow_res.append(re.compile(pat, re.I))
        except re.error as e:
            log.warning("[YAML_API] bad allow_url_pattern %r: %s", pat, e)
    _block_res = []
    for pat in (cfg.block_url_patterns or []):
        try:
            _block_res.append(re.compile(pat, re.I))
        except re.error as e:
            log.warning("[YAML_API] bad block_url_pattern %r: %s", pat, e)

    def _keep_url(url: str) -> bool:
        if _block_res and any(r.search(url) for r in _block_res):
            return False
        if _allow_res and not any(r.search(url) for r in _allow_res):
            return False
        return True

    def _resolve_url(url: str) -> str:
        if url and url.startswith("/") and cfg.normalize_relative_urls and cfg.base_url:
            return cfg.base_url.rstrip("/") + url
        return url

    def _dig(obj: Any, path: str) -> Any:
        """Navigate dot-separated path into a JSON structure."""
        for key in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(key)
            elif isinstance(obj, list) and key.isdigit():
                obj = obj[int(key)]
            else:
                return None
        return obj

    def _first_field(item: dict, fields: list[str]) -> str:
        for f in fields:
            v = item.get(f)
            if v and isinstance(v, str):
                return v.strip()
        return ""

    # ── Pagination loop ───────────────────────────────────────────────────────
    links: list[dict] = []
    offset = 0
    page_size = cfg.page_size or int(cfg.params.get(cfg.page_size_param, "0") or 0)
    paginate = page_size > 0

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for page_num in range(cfg.max_pages):
            req_params = dict(cfg.params)
            if paginate:
                req_params[cfg.page_size_param] = str(page_size)
                req_params[cfg.offset_param] = str(offset)

            try:
                if cfg.method.upper() == "POST":
                    resp = await client.post(cfg.url, headers=cfg.headers, json=req_params)
                else:
                    resp = await client.get(cfg.url, headers=cfg.headers, params=req_params)
                resp.raise_for_status()
            except Exception as exc:
                log.error("[YAML_API] request failed (page %d): %s", page_num, exc)
                break

            try:
                data = resp.json()
            except Exception as exc:
                log.error("[YAML_API] JSON decode failed: %s", exc)
                break

            items: list[dict] = []
            if cfg.root_path:
                items = _dig(data, cfg.root_path) or []
            elif isinstance(data, list):
                items = data
            else:
                # Try common wrapper keys
                for k in ("results", "items", "data", "courses", "docs"):
                    if isinstance(data.get(k), list):
                        items = data[k]
                        break

            if not items:
                _emit(f"Page {page_num}: 0 items — stopping pagination")
                break

            page_links = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_url = _first_field(item, cfg.url_fields)
                name = _first_field(item, cfg.title_fields) or raw_url
                if not raw_url:
                    continue
                url = _resolve_url(raw_url)
                if not _keep_url(url):
                    continue
                links.append({"name": name, "url": url})
                page_links += 1

                if cfg.max_courses and len(links) >= cfg.max_courses:
                    _emit(f"Reached max_courses cap ({cfg.max_courses})")
                    return links

            _emit(f"Page {page_num}: {page_links} links kept (total: {len(links)})")

            if not paginate or len(items) < page_size:
                break  # last page
            offset += page_size

    # Deduplicate by URL (preserve order)
    seen: set[str] = set()
    deduped = []
    for lk in links:
        if lk["url"] not in seen:
            seen.add(lk["url"])
            deduped.append(lk)

    _emit(f"Done: {len(deduped)} unique course links from YAML generic_search_api")
    return deduped

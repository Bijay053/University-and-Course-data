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


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def fetch_generic_api_links(
    provider: str,
    endpoint_hint: str,
    auto_config: dict,
    emit: Callable[..., Any] | None = None,
) -> list[dict]:
    """Top-level entry point called by the orchestrator.

    Parameters
    ----------
    provider:
        Provider name from ``auto_config._api_provider``
        (e.g. ``"searchstax"``, ``"algolia"``).
    endpoint_hint:
        Raw endpoint string from ``auto_config._api_endpoint_hint``.
    auto_config:
        The full auto_config dict.  May contain ``_api_token_env`` (name of the
        env var holding the auth token) or ``_api_auth_hint``.
    emit:
        Optional logging callable.
    """
    if provider == "searchstax":
        # Resolve token: env var override → auto_config hint → None (public core)
        token_env = auto_config.get("_api_token_env") or "GENERIC_SEARCHSTAX_TOKEN"
        token = os.environ.get(token_env, "")
        if not token:
            # Try known fallback env vars
            for env_name in ("HUD_SEARCHSTAX_TOKEN", "SEARCHSTAX_TOKEN"):
                token = os.environ.get(env_name, "")
                if token:
                    break
        if not token:
            # If no token, attempt unauthenticated (some Solr cores are public)
            log.warning(
                "[GENERIC_SS] No token found in env vars — attempting unauthenticated"
            )
        return await fetch_searchstax_generic(endpoint_hint, token, emit=emit)

    if provider in ("algolia",):
        api_key = auto_config.get("_api_auth_hint") or os.environ.get("ALGOLIA_API_KEY", "")
        return await fetch_algolia_generic(endpoint_hint, api_key, emit=emit)

    log.warning("[GENERIC_API] Unknown provider %r — no links fetched", provider)
    return []

"""Auto-configuration generator — produces a UniConfig-compatible dict
from a :class:`SiteProfile` using Gemini for intelligent analysis.

The generated config is stored in ``universities.scrape_config`` under
the ``auto_config`` key.  The loader merges it between the global
defaults and any per-uni YAML override, so the YAML always wins.

Flow
----
1. :func:`generate_config` receives a ``SiteProfile`` and optional
   sample HTML from one or two course pages.
2. Heuristic rules fill in the easy cases (Cloudflare → wayback,
   search API found → skip everything else, etc.).
3. Gemini is called with a structured prompt that includes the profile
   + sample HTML to refine allow/block URL patterns, detect fee-page
   locations, and set extraction hints.
4. The result is merged, validated against the Pydantic schema, and
   returned as a plain dict ready to store in JSONB.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


# ── Platform type derivation ──────────────────────────────────────────────────

def _derive_platform_type(profile: "SiteProfile") -> str:  # type: ignore[name-defined]
    """Derive a reusable platform-type key from a SiteProfile.

    Priority (highest → lowest):
      1. Explicit API provider  (searchstax, algolia, solr, …)
      2. CMS/platform fingerprint  (Phase 4A: wordpress:elementor, drupal, …)
      3. Library-stack situation   (existing behaviour pre-Phase-4A)
      4. Recommended strategy      (static_html, browser, wayback, …)

    Used as the key in ``scraper_patterns`` so rules learned from one university
    are automatically seeded when a future university shares the same platform.
    The more specific the key, the more targeted the pattern reuse.
    """
    # 1 — Explicit API provider (most specific)
    if getattr(profile, "detected_apis", None):
        provider = profile.detected_apis[0].provider
        if provider:
            return provider.lower().strip()

    # 2 — CMS/platform fingerprint (Phase 4A — more specific than situation)
    cms = getattr(profile, "cms_platform", None)
    if cms:
        return cms.lower().strip()

    # 3 — Library-stack situation (pre-Phase-4A behaviour)
    ls = getattr(profile, "library_stack", None)
    if ls:
        situation = getattr(ls, "situation", None) or ""
        if situation:
            return situation.lower().strip()

    # 4 — Strategy fallback
    return (getattr(profile, "recommended_strategy", None) or "").lower().strip()


# ── Default config templates per strategy ────────────────────────────────────

def _base_config(profile: "SiteProfile") -> dict[str, Any]:  # type: ignore[name-defined]
    from app.services.scraper.site_probe import (
        STRATEGY_BLOCKED,
        STRATEGY_BROWSER,
        STRATEGY_PROXY,
        STRATEGY_SEARCH_API,
        STRATEGY_SITEMAP_FIRST,
        STRATEGY_STATIC_HTML,
        STRATEGY_WAYBACK,
    )

    strategy = profile.recommended_strategy
    parsed = urlparse(profile.url)
    tld = parsed.netloc.lower()
    is_uk = tld.endswith(".ac.uk") or tld.endswith(".co.uk")
    is_au = tld.endswith(".edu.au") or tld.endswith(".ac.au")

    config: dict[str, Any] = {
        "discovery": {},
        "extraction": {
            "fees": {
                "default_currency": "GBP" if is_uk else ("AUD" if is_au else "USD"),
            }
        },
        "_auto_generated": True,
        "_strategy": strategy,
        # Library situation computed by library_strategy.recommend_library_stack().
        # Stored here so the UI and downstream tooling can read it from auto_config
        # without re-computing it from the probe signals.
        "_library_situation": (
            profile.library_stack.situation if profile.library_stack else None
        ),
        # Phase 3: platform type key used by pattern_store for learning/seeding
        "_platform_type": _derive_platform_type(profile),
        "_probe_summary": {
            "cloudflare": profile.is_cloudflare_blocked,
            "bot_protected": profile.is_bot_protected,
            "js_spa": profile.is_js_spa,
            "has_sitemap": profile.has_sitemap,
            "sitemap_course_count": profile.sitemap_course_count,
            "wayback_count": profile.wayback_course_count,
            "detected_apis": [a.provider for a in profile.detected_apis],
            "confidence": profile.strategy_confidence,
        },
    }

    disc = config["discovery"]
    extr = config["extraction"]

    if strategy == STRATEGY_SEARCH_API and profile.detected_apis:
        api = profile.detected_apis[0]
        disc["use_stealth_browser"] = False
        disc["always_sitemap_supplement"] = False
        config["_api_provider"] = api.provider
        config["_api_endpoint_hint"] = api.endpoint_hint
        log.info(
            "[AUTO_CONFIG] Search API strategy: provider=%s endpoint=%s",
            api.provider,
            api.endpoint_hint[:80],
        )

    elif strategy == STRATEGY_WAYBACK:
        disc["use_wayback"] = True
        disc["use_stealth_browser"] = False
        disc["scrape_do_fallback"] = False
        extr["skip_browser_rescue"] = True
        extr.setdefault("fees", {})["force_central_fee_stage"] = True
        log.info("[AUTO_CONFIG] Wayback strategy for %s", profile.url)

    elif strategy in (STRATEGY_PROXY,):
        disc["scrape_do_fallback"] = True
        disc["use_stealth_browser"] = False
        extr["skip_browser_rescue"] = True
        log.info("[AUTO_CONFIG] Proxy strategy for %s", profile.url)

    elif strategy == STRATEGY_BROWSER:
        disc["use_stealth_browser"] = True
        disc["always_sitemap_supplement"] = profile.has_sitemap
        log.info("[AUTO_CONFIG] Browser strategy for %s", profile.url)

    elif strategy == STRATEGY_SITEMAP_FIRST:
        disc["always_sitemap_supplement"] = True
        disc["use_stealth_browser"] = False
        if profile.sitemap_url:
            disc["sitemap_url"] = profile.sitemap_url
        log.info(
            "[AUTO_CONFIG] Sitemap-first strategy: %d course URLs",
            profile.sitemap_course_count,
        )

    else:  # STRATEGY_STATIC_HTML or fallback
        disc["use_stealth_browser"] = False
        if profile.has_sitemap and profile.sitemap_course_count > 50:
            disc["always_sitemap_supplement"] = True

    if strategy == STRATEGY_BLOCKED:
        config["_blocked"] = True
        config["_notes"] = profile.notes

    return config


async def generate_config(
    profile: "SiteProfile",  # type: ignore[name-defined]
    sample_html: str | None = None,
    sample_urls: list[str] | None = None,
    learned_patterns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a UniConfig-compatible dict for this site profile.

    Parameters
    ----------
    profile:
        Result from :func:`site_probe.probe_site`.
    sample_html:
        HTML of one course page (optional).  Used by Gemini to detect
        fee location, IELTS format, and URL patterns.
    sample_urls:
        A handful of discovered course URLs (optional).  Used to derive
        ``allow_url_patterns``.

    Returns
    -------
    dict
        Ready to store as ``universities.scrape_config["auto_config"]``.
    """
    config = _base_config(profile)

    # Apply Gemini refinement (non-fatal if unavailable)
    if sample_html or sample_urls:
        try:
            config = await _gemini_refine(config, profile, sample_html, sample_urls or [])
        except Exception as exc:
            log.warning("[AUTO_CONFIG] Gemini refinement failed (using heuristic only): %s", exc)

    # Derive allow_url_patterns from sample URLs if not already set
    if sample_urls and "allow_url_patterns" not in config.get("discovery", {}):
        patterns = _derive_url_patterns(sample_urls)
        if patterns:
            config.setdefault("discovery", {})["allow_url_patterns"] = patterns
            log.info("[AUTO_CONFIG] derived allow_url_patterns: %s", patterns)

    # ── Phase 2: generate per-field extraction rules from sample HTML ────────
    # Asks Gemini to produce CSS/XPath/regex rules for each review field.
    # Stored under auto_config["extraction_rules"] — applied as Stage 0
    # inside extract_course() BEFORE any regex heuristics or per-course Gemini.
    # This is how per-course Gemini cost drops to zero for well-configured sites.
    if sample_html:
        try:
            from app.services.scraper.ai_extractor_gen import generate_and_store_rules
            config = await generate_and_store_rules(
                profile, sample_html, config, learned_patterns=learned_patterns
            )
        except Exception as _gen_exc:
            log.warning(
                "[AUTO_CONFIG] extraction rule generation failed (non-fatal): %s", _gen_exc
            )

    return config


async def _gemini_refine(
    config: dict[str, Any],
    profile: "SiteProfile",  # type: ignore[name-defined]
    sample_html: str | None,
    sample_urls: list[str],
) -> dict[str, Any]:
    """Call Gemini to refine the heuristic config with site-specific details."""
    from app.services.ai import gemini_client

    # Build a compact profile summary for the prompt
    profile_summary = {
        "url": profile.url,
        "strategy": profile.recommended_strategy,
        "is_cloudflare_blocked": profile.is_cloudflare_blocked,
        "is_js_spa": profile.is_js_spa,
        "has_sitemap": profile.has_sitemap,
        "sitemap_course_count": profile.sitemap_course_count,
        "wayback_course_count": profile.wayback_course_count,
        "detected_apis": [a.provider for a in profile.detected_apis],
        "notes": profile.notes[:5],
    }

    # Truncate sample HTML to keep prompt compact
    html_excerpt = ""
    if sample_html:
        # Strip script/style tags
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", sample_html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        html_excerpt = text[:3000]

    url_list = "\n".join(sample_urls[:10])

    prompt = f"""You are configuring a web scraper for a university course catalogue.

SITE PROFILE:
{json.dumps(profile_summary, indent=2)}

SAMPLE COURSE URLS (up to 10):
{url_list}

SAMPLE PAGE TEXT (truncated to 3000 chars):
{html_excerpt}

Generate a JSON scraper configuration that improves on the base heuristics.

REQUIRED OUTPUT — valid JSON only, no markdown, no explanation:
{{
  "allow_url_patterns": ["<regex pattern matching real course URLs>"],
  "block_url_patterns": ["<regex pattern for non-course pages to skip>"],
  "fees_on_course_page": true_or_false,
  "ielts_on_course_page": true_or_false,
  "fee_page_hint": "<URL or path pattern for shared fee page, or null>",
  "english_page_hint": "<URL or path pattern for shared English-requirements page, or null>",
  "notes": "<one-sentence observation about this site>"
}}

RULES:
- allow_url_patterns: regex that matches ONLY course-detail pages, NOT listing/category pages
- block_url_patterns: listing pages, marketing pages, /apply/, /about/, /contact/ etc.
- If you cannot determine a field confidently, use null
- URL patterns should use Python re syntax (no anchors needed, substring match)
- Respond ONLY with valid JSON"""

    resp = await gemini_client.generate(
        prompt=prompt,
        call_type="auto_config",
    )

    if resp.skipped or not resp.text:
        log.info("[AUTO_CONFIG] Gemini skipped (budget/quota) — using heuristic config only")
        return config

    # Parse Gemini response
    try:
        raw = resp.text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
        gemini_cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("[AUTO_CONFIG] Gemini returned non-JSON: %s — %s", exc, resp.text[:200])
        return config

    # Merge Gemini refinements into config
    disc = config.setdefault("discovery", {})
    extr = config.setdefault("extraction", {})

    if gemini_cfg.get("allow_url_patterns"):
        disc["allow_url_patterns"] = gemini_cfg["allow_url_patterns"]

    if gemini_cfg.get("block_url_patterns"):
        disc["block_url_patterns"] = gemini_cfg["block_url_patterns"]

    fees_on_page = gemini_cfg.get("fees_on_course_page")
    if fees_on_page is False:
        extr.setdefault("fees", {})["force_central_fee_stage"] = True
        fee_page = gemini_cfg.get("fee_page_hint")
        if fee_page:
            extr.setdefault("fees", {})["central_page"] = fee_page

    ielts_on_page = gemini_cfg.get("ielts_on_course_page")
    english_page = gemini_cfg.get("english_page_hint")
    if ielts_on_page is False and english_page:
        extr.setdefault("english", {})["central_page"] = english_page

    if gemini_cfg.get("notes"):
        config.setdefault("_notes", [])
        if isinstance(config["_notes"], list):
            config["_notes"].append(gemini_cfg["notes"])
        else:
            config["_notes"] = [gemini_cfg["notes"]]

    log.info(
        "[AUTO_CONFIG] Gemini refined: allow=%s fees_on_page=%s",
        disc.get("allow_url_patterns"),
        fees_on_page,
    )
    return config


def _derive_url_patterns(urls: list[str]) -> list[str]:
    """Derive allow_url_patterns from a list of sample course URLs.

    Finds common path prefixes / structural patterns that identify
    course-detail pages.

    Examples
    --------
    ['https://example.edu/courses/postgraduate/master-of-science',
     'https://example.edu/courses/undergraduate/bachelor-of-arts']
    → ['/courses/(postgraduate|undergraduate)/[^/]+/?$']
    """
    if not urls:
        return []

    paths = [urlparse(u).path for u in urls]

    # Find common prefix depth
    parts_list = [p.strip("/").split("/") for p in paths]
    if not parts_list:
        return []

    min_depth = min(len(p) for p in parts_list)
    common_depth = 0
    for i in range(min_depth):
        values = {p[i] for p in parts_list}
        if len(values) == 1:
            common_depth = i + 1
        else:
            break

    if common_depth == 0:
        # No common prefix — just use a broad match
        return []

    common_path = "/" + "/".join(parts_list[0][:common_depth])
    # The URL must have at least one more segment after the common prefix
    pattern = re.escape(common_path) + r"/.+"
    return [pattern]


async def fetch_sample_course_html(url: str, timeout: float = 10.0) -> str:
    """Fetch one course page to provide sample HTML to the config generator."""
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html",
        }
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=True, timeout=timeout, verify=False
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.text[:50_000]
    except Exception as exc:
        log.debug("[AUTO_CONFIG] sample fetch failed for %s: %s", url, exc)
    return ""

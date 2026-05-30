"""Site intelligence probe — auto-detects the optimal scraping strategy.

Given a university website URL, this module analyses the site and returns
a :class:`SiteProfile` with:

* Whether the live site is accessible (Cloudflare / WAF check)
* Whether the site is a JS SPA (React / Vue / Angular)
* Any hidden search APIs discovered in the page source
  (SearchStax, Algolia, Elasticsearch, Coveo, Funnelback)
* Sitemap availability and course-URL count
* Wayback Machine archive availability
* A recommended strategy and an escalation ladder for self-healing

The probe is intentionally lightweight and synchronous-friendly — it
uses httpx for HTTP (not Playwright) so it runs fast as a pre-scrape
step without needing the browser pool.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services.scraper.library_strategy import LibraryStack, recommend_library_stack

log = logging.getLogger(__name__)

# ── Strategy constants ───────────────────────────────────────────────────────
STRATEGY_STATIC_HTML = "static_html"
STRATEGY_SITEMAP_FIRST = "sitemap_first"
STRATEGY_SEARCH_API = "search_api"
STRATEGY_WAYBACK = "wayback"
STRATEGY_BROWSER = "browser"
STRATEGY_PROXY = "proxy"
STRATEGY_BLOCKED = "blocked"

_STRATEGY_LADDER = [
    STRATEGY_STATIC_HTML,
    STRATEGY_SITEMAP_FIRST,
    STRATEGY_WAYBACK,
    STRATEGY_SEARCH_API,
    STRATEGY_BROWSER,
    STRATEGY_PROXY,
    STRATEGY_BLOCKED,
]

# ── Known search-API detection patterns ─────────────────────────────────────
_SEARCH_API_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "searchstax",
        "SearchStax Solr",
        re.compile(r"searchcloud-[\w-]+\.searchstax\.com/[\d]+/([\w-]+)/", re.I),
    ),
    (
        "algolia",
        "Algolia",
        re.compile(r"([\w-]+)-dsn\.algolia(?:net)?\.(?:com|net)/1/indexes", re.I),
    ),
    (
        "elasticsearch",
        "Elasticsearch",
        re.compile(r"https?://[\w.:-]+/_(?:search|msearch)", re.I),
    ),
    (
        "coveo",
        "Coveo",
        re.compile(r"platform(?:cdn)?\.cloud\.coveo\.com|usageanalytics\.coveo\.com", re.I),
    ),
    (
        "funnelback",
        "Funnelback",
        re.compile(r"funnelback\.com/s/search\.(?:json|html)|squiz\.net/s/search", re.I),
    ),
    (
        "solr",
        "Apache Solr",
        re.compile(r"/solr/[\w-]+/select\?", re.I),
    ),
    (
        "typesense",
        "Typesense",
        re.compile(r"typesense\.org|/collections/[\w-]+/documents/search", re.I),
    ),
    (
        "meilisearch",
        "Meilisearch",
        re.compile(r"meilisearch|/indexes/[\w-]+/search", re.I),
    ),
]

# ── Cloudflare / WAF detection ───────────────────────────────────────────────
_CF_HEADERS = {"cf-ray", "cf-cache-status", "cf-mitigated"}
_CF_BODY_PATTERNS = re.compile(
    r"just a moment|checking your browser|enable javascript and cookies"
    r"|cloudflare|cf-browser-verification|cf_chl_",
    re.I,
)
_OTHER_WAF_PATTERNS = re.compile(
    r"access denied|403 forbidden|this request has been blocked"
    r"|bot detected|automated access|security check",
    re.I,
)

# ── JS SPA detection ─────────────────────────────────────────────────────────
_SPA_MARKERS = re.compile(
    r'<div[^>]+id=["\'](?:root|app|__next|nuxt)["\']'
    r"|react-dom|__NEXT_DATA__|__nuxt__|vue\.config"
    r"|angular\.json|ember\.js|svelte",
    re.I,
)
_SPA_SCRIPT_PATTERNS = re.compile(
    r"chunk\.\w+\.js|runtime\.\w+\.js|main\.\w+\.js"
    r"|_next/static|_nuxt/|assets/index\.",
    re.I,
)

# ── Course-URL detection for Wayback ─────────────────────────────────────────
_COURSE_URL_PATTERNS = re.compile(
    r"/(?:courses?|programs?|degrees?|study|postgraduate|undergraduate|masters?|bachelors?)/",
    re.I,
)

# ── Common fetch headers ─────────────────────────────────────────────────────
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class DetectedAPI:
    provider: str
    label: str
    endpoint_hint: str
    auth_hint: str | None = None
    field_hints: dict[str, str] = field(default_factory=dict)


@dataclass
class SiteProfile:
    url: str
    probed_at: str

    # Live-site accessibility
    static_accessible: bool = False
    static_status_code: int = 0
    static_html_bytes: int = 0
    static_text_snippet: str = ""
    is_cloudflare_blocked: bool = False
    is_bot_protected: bool = False

    # Site type
    is_js_spa: bool = False
    spa_framework: str | None = None

    # Phase 4A: CMS/platform fingerprint (set by _detect_cms_platform)
    # More specific than library_stack.situation — used as pattern_store key.
    cms_platform: str | None = None

    # Search APIs embedded in the page source
    detected_apis: list[DetectedAPI] = field(default_factory=list)

    # Discovery helpers
    has_sitemap: bool = False
    sitemap_url: str | None = None
    sitemap_course_count: int = 0
    wayback_available: bool = False
    wayback_course_count: int = 0
    wayback_sample_urls: list[str] = field(default_factory=list)

    # Sample course URLs found during probe
    sample_course_urls: list[str] = field(default_factory=list)

    # Recommended strategy
    recommended_strategy: str = STRATEGY_STATIC_HTML
    strategy_confidence: float = 0.5
    strategy_ladder: list[str] = field(default_factory=list)

    # Recommended Python library stack (populated after _select_strategy)
    library_stack: LibraryStack | None = None

    # Human-readable notes for logging / UI
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "probed_at": self.probed_at,
            "static_accessible": self.static_accessible,
            "static_status_code": self.static_status_code,
            "static_html_bytes": self.static_html_bytes,
            "is_cloudflare_blocked": self.is_cloudflare_blocked,
            "is_bot_protected": self.is_bot_protected,
            "is_js_spa": self.is_js_spa,
            "spa_framework": self.spa_framework,
            "cms_platform": self.cms_platform,
            "detected_apis": [
                {
                    "provider": a.provider,
                    "label": a.label,
                    "endpoint_hint": a.endpoint_hint,
                    "auth_hint": a.auth_hint,
                    "field_hints": a.field_hints,
                }
                for a in self.detected_apis
            ],
            "has_sitemap": self.has_sitemap,
            "sitemap_url": self.sitemap_url,
            "sitemap_course_count": self.sitemap_course_count,
            "wayback_available": self.wayback_available,
            "wayback_course_count": self.wayback_course_count,
            "wayback_sample_urls": self.wayback_sample_urls[:10],
            "sample_course_urls": self.sample_course_urls[:10],
            "recommended_strategy": self.recommended_strategy,
            "strategy_confidence": self.strategy_confidence,
            "strategy_ladder": self.strategy_ladder,
            "library_stack": self.library_stack.to_dict() if self.library_stack else None,
            "notes": self.notes,
        }


# ── Core probe logic ─────────────────────────────────────────────────────────

async def probe_site(url: str, timeout: float = 15.0) -> SiteProfile:
    """Probe *url* and return a fully-populated :class:`SiteProfile`.

    This is the top-level entry point.  It runs all detection stages and
    computes a recommended strategy + escalation ladder.
    """
    profile = SiteProfile(
        url=url,
        probed_at=datetime.now(timezone.utc).isoformat(),
    )

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hostname = parsed.netloc.lower().removeprefix("www.")

    # Stage 1: Live-site fetch
    html = await _fetch_html(url, profile, timeout=timeout)

    # Stage 2: Cloudflare / WAF check
    if html:
        _detect_protection(html, profile)

    # Stage 3: JS SPA detection (only if site responded)
    if html and not profile.is_cloudflare_blocked:
        _detect_spa(html, profile)

    # Stage 3.5: CMS/platform fingerprinting (Phase 4A)
    if html and not profile.is_cloudflare_blocked:
        _detect_cms_platform(html, profile)

    # Stage 4: Hidden search-API detection in page source
    if html:
        _detect_search_apis(html, profile, origin=origin)

    # Stage 5: Sitemap probe
    await _probe_sitemap(origin, profile, timeout=timeout)

    # Stage 6: Wayback CDX probe (async; quick)
    await _probe_wayback(hostname, profile, timeout=timeout)

    # Stage 7: Recommend strategy + build escalation ladder
    _select_strategy(profile)

    # Stage 8: Recommend Python library stack based on site signals
    profile.library_stack = recommend_library_stack(profile)

    log.info(
        "[PROBE] %s → strategy=%s confidence=%.2f blocked=%s spa=%s "
        "apis=%d sitemap=%s wayback=%d",
        url,
        profile.recommended_strategy,
        profile.strategy_confidence,
        profile.is_cloudflare_blocked,
        profile.is_js_spa,
        len(profile.detected_apis),
        profile.has_sitemap,
        profile.wayback_course_count,
    )
    return profile


async def _fetch_html(url: str, profile: SiteProfile, timeout: float) -> str:
    """Fetch the URL with httpx and populate basic accessibility fields."""
    try:
        import httpx
        async with httpx.AsyncClient(
            headers=_FETCH_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            verify=False,
        ) as client:
            resp = await client.get(url)
            profile.static_status_code = resp.status_code
            html = resp.text or ""
            profile.static_html_bytes = len(resp.content)
            profile.static_accessible = resp.status_code < 400

            # Check Cloudflare headers directly on response
            resp_headers_lower = {k.lower() for k in resp.headers.keys()}
            if resp_headers_lower & _CF_HEADERS:
                profile.notes.append("Cloudflare headers detected in HTTP response")
                if resp.status_code in (403, 429) or "cf-mitigated" in resp_headers_lower:
                    profile.is_cloudflare_blocked = True

            profile.static_text_snippet = html[:500]
            return html
    except Exception as exc:
        profile.static_accessible = False
        profile.static_status_code = 0
        profile.notes.append(f"HTTP fetch failed: {exc!s:.120}")
        log.debug("[PROBE] fetch failed for %s: %s", url, exc)
        return ""


def _detect_protection(html: str, profile: SiteProfile) -> None:
    """Detect Cloudflare challenge and other WAF blocks in body."""
    if _CF_BODY_PATTERNS.search(html[:3000]):
        profile.is_cloudflare_blocked = True
        profile.notes.append("Cloudflare challenge page detected in body")
    elif profile.static_status_code == 403 and _OTHER_WAF_PATTERNS.search(html[:3000]):
        profile.is_bot_protected = True
        profile.notes.append("Bot-protection/WAF block detected (non-Cloudflare)")
    elif profile.static_html_bytes < 2000 and profile.static_status_code in (200,):
        profile.notes.append(
            f"Very small response ({profile.static_html_bytes}B) — "
            "may be a redirect shell or empty SPA entry point"
        )


def _detect_spa(html: str, profile: SiteProfile) -> None:
    """Detect JS single-page applications."""
    if _SPA_MARKERS.search(html):
        profile.is_js_spa = True
        if "react" in html.lower() or "__NEXT_DATA__" in html:
            profile.spa_framework = "react"
        elif "vue" in html.lower() or "__nuxt__" in html:
            profile.spa_framework = "vue"
        elif "angular" in html.lower():
            profile.spa_framework = "angular"
        profile.notes.append(
            f"JS SPA detected (framework={profile.spa_framework or 'unknown'})"
        )
    elif _SPA_SCRIPT_PATTERNS.search(html) and profile.static_html_bytes < 30_000:
        profile.is_js_spa = True
        profile.notes.append("Chunked JS bundle pattern detected — likely SPA")


# ── Phase 4A: CMS platform fingerprinting ────────────────────────────────────

def _detect_cms_platform(html: str, profile: SiteProfile) -> None:  # noqa: PLR0912
    """Detect CMS/platform from HTML source fingerprints (Phase 4A).

    Runs after _detect_spa so spa_framework is already set.
    Result stored in profile.cms_platform — used as the pattern_store key by
    the Phase 3 learning layer (more specific than library_stack.situation).

    Priority order:
      WordPress variants → Drupal → TerminalFour → ModernCampus →
      CourseLeaf → Sitecore → SharePoint → Joomla → SilverStripe
    """
    h = html[:60_000]  # first 60 KB contains all fingerprints

    # ── WordPress and sub-variants ────────────────────────────────────────
    is_wp = (
        "/wp-content/" in h
        or "/wp-includes/" in h
        or 'content="WordPress' in h
        or "/wp-json/" in h
    )
    if is_wp:
        hl = h.lower()
        if "elementor" in hl:
            profile.cms_platform = "wordpress:elementor"
        elif "et-divi" in h or "et_theme_builder" in h or '"divi"' in hl:
            profile.cms_platform = "wordpress:divi"
        elif "/wp-json/acf/" in h or "acf-field" in hl:
            profile.cms_platform = "wordpress:acf"
        else:
            profile.cms_platform = "wordpress"
        profile.notes.append(f"CMS fingerprint: {profile.cms_platform}")
        return

    # ── Drupal ────────────────────────────────────────────────────────────
    if (
        "Drupal.settings" in h
        or "sites/default/files" in h
        or 'content="Drupal' in h
        or "drupal.org" in h
        or "data-drupal-" in h
    ):
        profile.cms_platform = "drupal"
        profile.notes.append("CMS fingerprint: drupal")
        return

    # ── TerminalFour ──────────────────────────────────────────────────────
    hl = h.lower()
    if (
        "t4tag" in hl
        or "t4:content" in h
        or "/SiteManager/" in h
        or "terminal four" in hl
        or "terminalfour" in hl
        or "mediasourcecms" in hl
    ):
        profile.cms_platform = "terminalfour"
        profile.notes.append("CMS fingerprint: terminalfour")
        return

    # ── ModernCampus / Omni CMS ───────────────────────────────────────────
    if (
        "omni-cms" in hl
        or "moderncampus" in hl
        or "omnicampus" in hl
        or "oucampus" in hl
        or "ou-campus" in hl
    ):
        profile.cms_platform = "moderncampus"
        profile.notes.append("CMS fingerprint: moderncampus")
        return

    # ── CourseLeaf ────────────────────────────────────────────────────────
    if (
        "courseleaf" in hl
        or "leepfrog" in hl
        or 'class="clf-' in h
        or "/coursedog/" in h
    ):
        profile.cms_platform = "courseleaf"
        profile.notes.append("CMS fingerprint: courseleaf")
        return

    # ── Sitecore ──────────────────────────────────────────────────────────
    if (
        "/-/jssmedia/" in h
        or "/sitecore/shell/" in h
        or "SitecoreContent" in h
        or "Sitecore.Context" in h
        or "data-sc-" in h
    ):
        profile.cms_platform = "sitecore"
        profile.notes.append("CMS fingerprint: sitecore")
        return

    # ── SharePoint ────────────────────────────────────────────────────────
    if (
        "_layouts/15/" in h
        or "sharepoint.com" in hl
        or "sp.init.js" in h
        or "MSOLayout" in h
    ):
        profile.cms_platform = "sharepoint"
        profile.notes.append("CMS fingerprint: sharepoint")
        return

    # ── Joomla ───────────────────────────────────────────────────────────
    if "/components/com_" in h or 'content="Joomla' in h or "Joomla!" in h:
        profile.cms_platform = "joomla"
        profile.notes.append("CMS fingerprint: joomla")
        return

    # ── SilverStripe ─────────────────────────────────────────────────────
    if "SilverStripe" in h or "silverstripe" in hl:
        profile.cms_platform = "silverstripe"
        profile.notes.append("CMS fingerprint: silverstripe")
        return


def _detect_search_apis(html: str, profile: SiteProfile, origin: str) -> None:
    """Scan page source for known hosted-search API references."""
    for provider, label, pattern in _SEARCH_API_PATTERNS:
        m = pattern.search(html)
        if m:
            endpoint_hint = m.group(0)
            api = DetectedAPI(
                provider=provider,
                label=label,
                endpoint_hint=endpoint_hint,
            )
            profile.detected_apis.append(api)
            profile.notes.append(f"Detected {label} search API: {endpoint_hint[:80]}")
            log.info("[PROBE] detected %s at %s", label, endpoint_hint[:80])


async def _probe_sitemap(origin: str, profile: SiteProfile, timeout: float) -> None:
    """Check robots.txt for a Sitemap: directive, then try /sitemap.xml."""
    try:
        import httpx
        async with httpx.AsyncClient(
            headers=_FETCH_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            verify=False,
        ) as client:
            # Check robots.txt first
            sitemap_url: str | None = None
            try:
                robots_resp = await client.get(f"{origin}/robots.txt")
                if robots_resp.status_code == 200:
                    for line in robots_resp.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            sitemap_url = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass

            # Fall back to standard location
            if not sitemap_url:
                sitemap_url = f"{origin}/sitemap.xml"

            # Probe the sitemap
            try:
                sm_resp = await client.get(sitemap_url)
                if sm_resp.status_code == 200 and (
                    "<urlset" in sm_resp.text or "<sitemapindex" in sm_resp.text
                ):
                    profile.has_sitemap = True
                    profile.sitemap_url = sitemap_url
                    # Count course-like URLs
                    urls_in_sitemap = re.findall(r"<loc>(.*?)</loc>", sm_resp.text, re.I)
                    course_urls = [u for u in urls_in_sitemap if _COURSE_URL_PATTERNS.search(u)]
                    profile.sitemap_course_count = len(course_urls)
                    profile.sample_course_urls.extend(course_urls[:5])
                    profile.notes.append(
                        f"Sitemap found at {sitemap_url} "
                        f"({profile.sitemap_course_count} course-like URLs)"
                    )
            except Exception as exc:
                log.debug("[PROBE] sitemap probe failed for %s: %s", sitemap_url, exc)
    except Exception as exc:
        log.debug("[PROBE] sitemap stage failed: %s", exc)


async def _probe_wayback(hostname: str, profile: SiteProfile, timeout: float) -> None:
    """Quick Wayback CDX check to see if course snapshots exist."""
    try:
        import httpx

        # Try courses/* and programs/* prefixes
        prefixes = [f"{hostname}/courses/*", f"{hostname}/programs/*"]
        async with httpx.AsyncClient(timeout=timeout) as client:
            for prefix in prefixes:
                cdx_url = (
                    "https://web.archive.org/cdx/search/cdx"
                    f"?url={prefix}&output=json&fl=original&collapse=urlkey"
                    "&filter=statuscode:200&limit=50"
                )
                try:
                    resp = await client.get(cdx_url)
                    if resp.status_code == 200:
                        rows = resp.json()
                        # rows[0] is the header row ["original"]
                        urls = [r[0] for r in rows[1:] if r] if len(rows) > 1 else []
                        course_urls = [u for u in urls if _COURSE_URL_PATTERNS.search(u)]
                        if course_urls:
                            profile.wayback_available = True
                            profile.wayback_course_count += len(course_urls)
                            profile.wayback_sample_urls.extend(course_urls[:5])
                except Exception as exc:
                    log.debug("[PROBE] Wayback CDX failed for prefix %s: %s", prefix, exc)

        if profile.wayback_available:
            profile.notes.append(
                f"Wayback CDX: {profile.wayback_course_count} archived course URLs found"
            )
    except Exception as exc:
        log.debug("[PROBE] Wayback stage failed: %s", exc)


def _select_strategy(profile: SiteProfile) -> None:
    """Select the recommended strategy and build the escalation ladder."""
    ladder: list[str] = []

    # Search API: fastest, most reliable — always try first if detected
    if profile.detected_apis:
        ladder.append(STRATEGY_SEARCH_API)

    # Static HTML: best default if site is accessible and not SPA-only
    if profile.static_accessible and not profile.is_cloudflare_blocked:
        if profile.sitemap_course_count > 20:
            ladder.append(STRATEGY_SITEMAP_FIRST)
        ladder.append(STRATEGY_STATIC_HTML)

    # Wayback: good fallback for blocked sites with historical snapshots
    if profile.wayback_available and profile.wayback_course_count >= 10:
        ladder.append(STRATEGY_WAYBACK)

    # Browser: for JS-heavy sites where static is inadequate
    if profile.is_js_spa and not profile.is_cloudflare_blocked:
        ladder.append(STRATEGY_BROWSER)

    # Proxy: last resort for Cloudflare-blocked sites
    if profile.is_cloudflare_blocked or profile.is_bot_protected:
        ladder.append(STRATEGY_PROXY)
        if STRATEGY_WAYBACK not in ladder and profile.wayback_available:
            ladder.append(STRATEGY_WAYBACK)

    # Always cap with blocked as the sentinel
    if not ladder:
        ladder.append(STRATEGY_BLOCKED)
    elif STRATEGY_BLOCKED not in ladder:
        ladder.append(STRATEGY_BLOCKED)

    profile.strategy_ladder = ladder

    # Pick the top of the ladder as the recommendation
    if ladder[0] == STRATEGY_BLOCKED:
        profile.recommended_strategy = STRATEGY_BLOCKED
        profile.strategy_confidence = 0.0
        profile.notes.append("No viable scraping strategy detected")
        return

    profile.recommended_strategy = ladder[0]

    # Confidence scoring
    if profile.recommended_strategy == STRATEGY_SEARCH_API:
        profile.strategy_confidence = 0.95
    elif profile.recommended_strategy == STRATEGY_SITEMAP_FIRST:
        profile.strategy_confidence = 0.85
    elif profile.recommended_strategy == STRATEGY_STATIC_HTML:
        if profile.static_html_bytes > 50_000:
            profile.strategy_confidence = 0.80
        else:
            profile.strategy_confidence = 0.65
    elif profile.recommended_strategy == STRATEGY_WAYBACK:
        profile.strategy_confidence = 0.70
    elif profile.recommended_strategy == STRATEGY_BROWSER:
        profile.strategy_confidence = 0.60
    elif profile.recommended_strategy == STRATEGY_PROXY:
        profile.strategy_confidence = 0.40
    else:
        profile.strategy_confidence = 0.20


def next_strategy(current: str, ladder: list[str]) -> str | None:
    """Return the next strategy to try after *current* fails, or None."""
    try:
        idx = ladder.index(current)
        candidates = [s for s in ladder[idx + 1:] if s != STRATEGY_BLOCKED]
        return candidates[0] if candidates else None
    except ValueError:
        return None

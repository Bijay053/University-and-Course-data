"""Scraping library strategy advisor.

Given a :class:`~app.services.scraper.site_probe.SiteProfile`, this module
selects the optimal Python library stack for scraping that site and returns a
structured :class:`LibraryStack` recommendation.

The recommendation is pure rule-based (no Gemini) so it is instant and free.
It feeds into :func:`site_probe.probe_site` and is stored alongside the rest
of the probe result in ``universities.probe_result``.

Knowledge base covers:
  Full frameworks  : Scrapy, pyspider, autoscraper
  HTTP clients     : requests, httpx, aiohttp, urllib/urllib3, curl_cffi, pycurl
  HTML/XML parsers : BeautifulSoup, lxml, parsel, selectolax, pyquery, html5lib
  Browser          : Playwright, scrapy-playwright, Selenium, Pyppeteer,
                     scrapy-splash/Splash, DrissionPage, nodriver
  Anti-bot/stealth : cloudscraper, undetected-chromedriver, fake-useragent
  Content extract  : trafilatura, newspaper3k, readability-lxml, extruct
  All-in-one       : requests-html, MechanicalSoup
  Data cleaning    : price-parser, dateparser, pandas, openpyxl
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.scraper.site_probe import SiteProfile


# ── Library knowledge base ────────────────────────────────────────────────────
# Grouped for documentation / UI tooltips.  Values are short descriptive strings
# so the frontend can render them without a separate lookup.

LIBRARY_KB: dict[str, dict[str, str]] = {
    # Full frameworks
    "scrapy": {
        "category": "Full framework",
        "desc": "High-throughput async scraping framework with built-in middleware",
    },
    "pyspider": {
        "category": "Full framework",
        "desc": "Web scraping framework with a web UI and task scheduler",
    },
    "autoscraper": {
        "category": "Full framework",
        "desc": "Smart scraper that learns patterns from one example",
    },
    # HTTP clients
    "requests": {
        "category": "HTTP client",
        "desc": "Synchronous HTTP; simple and battle-tested",
    },
    "httpx": {
        "category": "HTTP client",
        "desc": "Async-first HTTP/2 client; drop-in requests upgrade",
    },
    "aiohttp": {
        "category": "HTTP client",
        "desc": "Async HTTP client/server; great for high concurrency",
    },
    "urllib": {
        "category": "HTTP client",
        "desc": "Python stdlib HTTP; no dependencies",
    },
    "curl_cffi": {
        "category": "HTTP client",
        "desc": "libcurl binding with browser TLS fingerprint — bypasses TLS-based bot detection",
    },
    "pycurl": {
        "category": "HTTP client",
        "desc": "Low-level libcurl binding",
    },
    # HTML/XML parsers
    "beautifulsoup": {
        "category": "HTML/XML parser",
        "desc": "Tolerant HTML parser; beginner-friendly; slower on large pages",
    },
    "lxml": {
        "category": "HTML/XML parser",
        "desc": "Fast C-based XPath/CSS parser; best for well-formed HTML",
    },
    "parsel": {
        "category": "HTML/XML parser",
        "desc": "Scrapy's CSS/XPath selector library; works standalone",
    },
    "selectolax": {
        "category": "HTML/XML parser",
        "desc": "Fastest pure-Python CSS selector; ideal for static HTML at scale",
    },
    "pyquery": {
        "category": "HTML/XML parser",
        "desc": "jQuery-like API over lxml",
    },
    "html5lib": {
        "category": "HTML/XML parser",
        "desc": "Browser-grade parser; slowest but most tolerant of broken HTML",
    },
    # Browser automation
    "playwright": {
        "category": "Browser automation",
        "desc": "Modern async browser automation (Chromium/Firefox/WebKit)",
    },
    "scrapy-playwright": {
        "category": "Browser automation",
        "desc": "Playwright integration for Scrapy spiders",
    },
    "selenium": {
        "category": "Browser automation",
        "desc": "Classic browser automation; wide ecosystem",
    },
    "pyppeteer": {
        "category": "Browser automation",
        "desc": "Python port of Puppeteer (Chromium DevTools Protocol)",
    },
    "scrapy-splash": {
        "category": "Browser automation",
        "desc": "Scrapy + Lua-scriptable headless browser (Splash service)",
    },
    "drissionpage": {
        "category": "Browser automation",
        "desc": "Merges requests and Playwright in one API; stealthy",
    },
    "nodriver": {
        "category": "Browser automation",
        "desc": "Undetectable Chrome automation without chromedriver",
    },
    # Anti-bot / stealth
    "cloudscraper": {
        "category": "Anti-bot/stealth",
        "desc": "Bypasses Cloudflare JS challenges using requests",
    },
    "undetected-chromedriver": {
        "category": "Anti-bot/stealth",
        "desc": "Selenium ChromeDriver patched to evade bot detection",
    },
    "fake-useragent": {
        "category": "Anti-bot/stealth",
        "desc": "Realistic rotating User-Agent strings",
    },
    # Content extraction
    "trafilatura": {
        "category": "Content extraction",
        "desc": "Fast main-content extraction; great for article/programme pages",
    },
    "newspaper3k": {
        "category": "Content extraction",
        "desc": "Article and news extraction with NLP",
    },
    "readability-lxml": {
        "category": "Content extraction",
        "desc": "Mozilla Readability algorithm; strips navigation/ads",
    },
    "extruct": {
        "category": "Content extraction",
        "desc": "Extracts embedded structured data (JSON-LD, Microdata, OpenGraph)",
    },
    # All-in-one
    "requests-html": {
        "category": "All-in-one",
        "desc": "requests + PyQuery + Pyppeteer in one package",
    },
    "mechanicalsoup": {
        "category": "All-in-one",
        "desc": "Stateful browser simulation over requests + BeautifulSoup",
    },
    # Data cleaning
    "price-parser": {
        "category": "Data cleaning",
        "desc": "Robust fee/currency extraction from free text",
    },
    "dateparser": {
        "category": "Data cleaning",
        "desc": "Parses dates in 200+ languages and formats",
    },
    "pandas": {
        "category": "Data cleaning",
        "desc": "DataFrame operations; ideal for table/CSV normalisation",
    },
    "openpyxl": {
        "category": "Data cleaning",
        "desc": "Read/write Excel files (XLSX)",
    },
}

# ── Strategy profiles ─────────────────────────────────────────────────────────
# Pre-defined stacks keyed by situation name.  recommend_library_stack() picks
# the right profile then may patch fields based on the SiteProfile signals.

_STACKS: dict[str, dict] = {
    "hidden_api": {
        "fetch_library": ["httpx", "aiohttp"],
        "parser": ["json (stdlib)"],
        "fallback": ["requests"],
        "antibot": ["fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser", "pandas"],
        "reason": (
            "Site embeds a hosted search API (SearchStax / Algolia / etc.). "
            "Query the API directly — no HTML parsing needed. "
            "httpx/aiohttp handles async pagination cleanly."
        ),
    },
    "cloudflare_stealth": {
        "fetch_library": ["curl_cffi"],
        "parser": ["selectolax", "lxml"],
        "fallback": ["playwright", "nodriver"],
        "antibot": ["cloudscraper", "fake-useragent", "undetected-chromedriver"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Site is Cloudflare-protected. "
            "curl_cffi impersonates real browser TLS fingerprints and bypasses "
            "most CF challenges without a headless browser. "
            "Playwright/nodriver as last resort for JS challenges."
        ),
    },
    "browser_automation": {
        "fetch_library": ["playwright"],
        "parser": ["parsel", "selectolax"],
        "fallback": ["scrapy-playwright", "drissionpage"],
        "antibot": ["fake-useragent", "undetected-chromedriver"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Site is a JavaScript SPA (React/Vue/Angular). "
            "Static HTTP fetches return an empty shell. "
            "Playwright renders JS and exposes the real DOM."
        ),
    },
    "large_structured": {
        "fetch_library": ["scrapy"],
        "parser": ["parsel"],
        "fallback": ["httpx"],
        "antibot": ["fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser", "pandas"],
        "reason": (
            "Site has a large sitemap (50+ course URLs) with structured, "
            "server-rendered HTML. Scrapy's async request pipeline and built-in "
            "deduplication handles this efficiently at scale."
        ),
    },
    "sitemap_first": {
        "fetch_library": ["httpx"],
        "parser": ["selectolax", "lxml"],
        "fallback": ["playwright"],
        "antibot": ["fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Site has a sitemap with course URLs and static HTML pages. "
            "httpx + selectolax is the fastest combination for this pattern."
        ),
    },
    "static_html": {
        "fetch_library": ["httpx", "requests"],
        "parser": ["selectolax", "lxml"],
        "fallback": ["playwright"],
        "antibot": ["fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Normal server-rendered HTML website. "
            "httpx handles async fetching; selectolax/lxml are the fastest "
            "CSS/XPath parsers for well-formed HTML."
        ),
    },
    "wayback_archive": {
        "fetch_library": ["httpx", "requests"],
        "parser": ["selectolax", "lxml"],
        "fallback": ["trafilatura"],
        "antibot": [],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Live site is inaccessible; using Wayback Machine archived snapshots. "
            "Standard HTTP + fast parser is sufficient — no bot detection needed "
            "for archive.org."
        ),
    },
    "messy_article": {
        "fetch_library": ["httpx", "requests"],
        "parser": ["trafilatura", "readability-lxml"],
        "fallback": ["beautifulsoup"],
        "antibot": ["fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Article-style pages with heavy navigation/ads. "
            "trafilatura and readability-lxml strip boilerplate and extract "
            "main content reliably."
        ),
    },
    "pdf_heavy": {
        "fetch_library": ["httpx"],
        "parser": ["pdfplumber / pymupdf (PDF)", "pandas"],
        "fallback": ["camelot-py"],
        "antibot": [],
        "data_cleaning": ["price-parser", "dateparser", "pandas", "openpyxl"],
        "reason": (
            "Site publishes fee tables or entry requirements primarily in PDFs. "
            "pdfplumber/pymupdf extracts text and table data; pandas normalises "
            "the tabular output."
        ),
    },
    "blocked": {
        "fetch_library": ["curl_cffi", "aiohttp"],
        "parser": ["selectolax"],
        "fallback": ["nodriver", "playwright"],
        "antibot": ["cloudscraper", "undetected-chromedriver", "fake-useragent"],
        "data_cleaning": ["price-parser", "dateparser"],
        "reason": (
            "Site appears completely blocked. "
            "Try curl_cffi for TLS fingerprint spoofing, then nodriver/Playwright "
            "with stealth plugins. Consider proxy rotation."
        ),
    },
}


# ── Public dataclass ──────────────────────────────────────────────────────────

@dataclass
class LibraryStack:
    """Recommended Python library stack for scraping a site."""

    situation: str
    fetch_library: list[str] = field(default_factory=list)
    parser: list[str] = field(default_factory=list)
    fallback: list[str] = field(default_factory=list)
    antibot: list[str] = field(default_factory=list)
    data_cleaning: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "situation": self.situation,
            "fetch_library": self.fetch_library,
            "parser": self.parser,
            "fallback": self.fallback,
            "antibot": self.antibot,
            "data_cleaning": self.data_cleaning,
            "reason": self.reason,
        }


# ── Core advisor ──────────────────────────────────────────────────────────────

def recommend_library_stack(profile: "SiteProfile") -> LibraryStack:
    """Return the optimal :class:`LibraryStack` for *profile*.

    Decision tree (highest priority first):

    1. Hidden search API detected (SearchStax / Algolia / Solr / etc.)
       → ``hidden_api`` — query the API directly, no HTML parsing.

    2. Cloudflare-blocked or bot-protected
       → ``cloudflare_stealth`` — curl_cffi + stealth tools.
         Sub-case: Wayback snapshots available → prefer ``wayback_archive``
         as the first attempt (no stealth needed).

    3. JS SPA without a usable search API
       → ``browser_automation`` — Playwright.

    4. Large structured sitemap (≥ 50 course URLs, static HTML)
       → ``large_structured`` — Scrapy + parsel.

    5. Sitemap present with course URLs (< 50 or unconfirmed static)
       → ``sitemap_first`` — httpx + selectolax.

    6. Static HTML, accessible, no sitemap
       → ``static_html`` — httpx + selectolax/lxml.

    7. Wayback only (live site down)
       → ``wayback_archive``.

    8. Nothing worked
       → ``blocked`` — stealth tools as last resort.
    """
    # 1. Hidden search API
    if profile.detected_apis:
        return _make(
            "hidden_api",
            extra_reason=_api_names(profile),
        )

    # 2. Cloudflare / WAF — but check if Wayback is a better first option
    if profile.is_cloudflare_blocked or profile.is_bot_protected:
        if profile.wayback_available and profile.wayback_course_count >= 10:
            return _make(
                "wayback_archive",
                extra_reason=(
                    f"Wayback has {profile.wayback_course_count} archived course URLs — "
                    "try archive first to avoid stealth complexity."
                ),
            )
        return _make("cloudflare_stealth")

    # 3. JS SPA (no API detected)
    if profile.is_js_spa:
        return _make(
            "browser_automation",
            extra_reason=(
                f"SPA framework: {profile.spa_framework or 'unknown'}"
                if profile.spa_framework
                else None
            ),
        )

    # 4. Large sitemap + static
    if profile.has_sitemap and profile.sitemap_course_count >= 50:
        return _make(
            "large_structured",
            extra_reason=f"Sitemap has {profile.sitemap_course_count} course URLs.",
        )

    # 5. Sitemap present but smaller
    if profile.has_sitemap and profile.sitemap_course_count > 0:
        return _make(
            "sitemap_first",
            extra_reason=f"Sitemap has {profile.sitemap_course_count} course URLs.",
        )

    # 6. Accessible static HTML
    if profile.static_accessible and not profile.is_cloudflare_blocked:
        return _make("static_html")

    # 7. Wayback only
    if profile.wayback_available:
        return _make(
            "wayback_archive",
            extra_reason=(
                f"Live site inaccessible (status {profile.static_status_code}); "
                f"{profile.wayback_course_count} Wayback snapshots available."
            ),
        )

    # 8. Blocked
    return _make("blocked")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make(situation: str, extra_reason: str | None = None) -> LibraryStack:
    template = _STACKS[situation]
    reason = template["reason"]
    if extra_reason:
        reason = f"{reason} ({extra_reason})"
    return LibraryStack(
        situation=situation,
        fetch_library=list(template["fetch_library"]),
        parser=list(template["parser"]),
        fallback=list(template["fallback"]),
        antibot=list(template["antibot"]),
        data_cleaning=list(template["data_cleaning"]),
        reason=reason,
    )


def _api_names(profile: "SiteProfile") -> str:
    return ", ".join(a.label for a in profile.detected_apis)

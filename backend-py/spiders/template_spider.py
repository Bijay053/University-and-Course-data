"""Template Scrapy spider — reference for all supported patterns.

QUICK START
-----------
1. Copy this file to ``backend-py/spiders/<uni_slug>_spider.py``.
2. Choose ONE of the pattern classes below (A–E) and delete the others.
3. Fill in the CONFIGURE block at the top.
4. Add to ``scraper_config/unis/<slug>.yaml``:

       discovery:
         scrapy:
           spider: <uni_slug>_spider
           settings:
             DOWNLOAD_DELAY: 1
             CONCURRENT_REQUESTS: 4

5. Trigger a scrape from the portal — items flow into the normal staging pipeline.

OUTPUT MODES
------------
• Discovery-only (minimum): yield ``{"name": ..., "url": ...}``.
  The orchestrator re-fetches each URL and runs normal extractors.
• Rich / pre-extracted: also yield ``"payload"`` + ``"evidence"`` keys.
  Orchestrator skips re-fetch entirely — identical to SearchStax HUD.

AVAILABLE PACKAGES (already installed)
---------------------------------------
  scrapy            — core framework (HTTP, link following, pipelines)
  scrapy-playwright — JS rendering via Playwright inside Scrapy
  beautifulsoup4    — HTML parsing (use alongside scrapy selectors)
  httpx             — async HTTP (use outside scrapy for API calls)
  requests          — sync HTTP (for one-off API calls in start_requests)
  pandas            — Excel / CSV parsing for structured data sources
  tenacity          — retry logic for flaky API / network calls
  price-parser      — parses fee strings like "£16,500 per year" → 16500.0

PATTERNS IN THIS FILE
---------------------
A — HTML crawl + BeautifulSoup  (server-rendered sites)
B — JSON / REST API             (sites that expose an internal API)
C — Scrapy-Playwright           (JS-rendered / Cloudflare-lite SPAs)
D — Excel / CSV feed            (universities that publish a data export)
E — Combined (API discovery + HTML rich extraction)
"""
from __future__ import annotations

import re
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE THIS SECTION — shared by all pattern classes below
# ─────────────────────────────────────────────────────────────────────────────

_UNI_NAME         = "Example University"
_ALLOWED_DOMAIN   = "example.edu"            # no leading www.
_BASE_URL         = "https://www.example.edu"
_COURSES_LIST_URL = f"{_BASE_URL}/courses/"  # listing / index page

# Regex matched against each href to decide if it is a course-detail page.
# Examples:
#   r"/courses/[a-z0-9-]+/?$"
#   r"/study/(undergraduate|postgraduate)/[^/]+/?$"
_COURSE_URL_RE    = r"/courses/[a-z0-9-]+/?$"

# For API / Excel patterns
_API_BASE         = f"{_BASE_URL}/api"
_EXCEL_FEED_URL   = f"{_BASE_URL}/downloads/courses.xlsx"

_CURRENCY         = "GBP"          # ISO currency for fee values
_LOCATION         = "Example City" # Default campus name

# ─────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN A — HTML crawl with BeautifulSoup (server-rendered sites)
# ══════════════════════════════════════════════════════════════════════════════

import scrapy
from bs4 import BeautifulSoup  # beautifulsoup4


class HtmlCrawlSpider(scrapy.Spider):
    """Crawl a paginated listing page; visit each course URL.

    Uses Scrapy's built-in selectors for fast XPath/CSS extraction and
    BeautifulSoup for complex nested HTML structures where CSS selectors
    become unwieldy (e.g. dl/dt/dd requirement tables).

    Copy this class when the site is server-rendered HTML (no JS needed).
    """

    name = "template_html"
    allowed_domains = [_ALLOWED_DOMAIN]
    start_urls = [_COURSES_LIST_URL]

    custom_settings = {
        "DOWNLOAD_DELAY": 0.5,
        "CONCURRENT_REQUESTS": 8,
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }

    def parse(self, response):
        """Follow course links; handle pagination."""
        for href in response.css("a::attr(href)").getall():
            if re.search(_COURSE_URL_RE, href or ""):
                yield response.follow(href, callback=self.parse_course)

        # Follow "next page" link if present
        next_href = response.css('a[rel="next"]::attr(href), a.pagination__next::attr(href)').get()
        if next_href:
            yield response.follow(next_href, callback=self.parse)

    def parse_course(self, response):
        """Extract course data.  Yields discovery-only item by default.

        Switch to rich mode by populating the commented-out payload block.
        """
        # Scrapy CSS selectors — fast and sufficient for most fields
        name = (
            response.css("h1.course-title::text, h1::text").get("")
        ).strip()

        # BeautifulSoup — useful for messy nested structures
        soup = BeautifulSoup(response.text, "lxml")
        description_tag = soup.find("div", class_=re.compile(r"course.?overview|about.?course", re.I))
        description = description_tag.get_text(" ", strip=True)[:800] if description_tag else None

        # Parse fee string using price-parser (see Pattern B for more detail)
        fee_raw = response.css(".course-fee, .tuition-fee::text").get("").strip()
        fee_value = _parse_fee(fee_raw)

        # ── Discovery-only output ────────────────────────────────────────────
        # Delete everything below and use the rich-mode block when
        # you can reliably extract all fields from the page.
        yield {"name": name or response.url, "url": response.url}

        # ── Rich mode (uncomment to skip re-fetch + extraction) ──────────────
        # yield {
        #     "name": name,
        #     "url": response.url,
        #     "payload": {
        #         "course_name": name,
        #         "description": description,
        #         "course_location": _LOCATION,
        #         "international_fee": fee_value,
        #         "fee_term": "Year",
        #         "currency": _CURRENCY,
        #     },
        #     "evidence": [
        #         _ev("course_name", name, "scrapy:h1", response.url, "h1 text", 0.9),
        #         _ev("international_fee", fee_value, "scrapy:fee_selector",
        #             response.url, f"Raw fee text: {fee_raw}", 0.75),
        #     ],
        # }


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN B — JSON / REST API  (internal API endpoint)
# ══════════════════════════════════════════════════════════════════════════════

import requests as _requests  # sync HTTP — fine in start_requests()
from tenacity import retry, stop_after_attempt, wait_exponential


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def _api_get(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    """GET JSON from an API endpoint with automatic retries on transient errors.

    tenacity retries up to 3 times with exponential back-off (1 s → 2 s → 4 s).
    Raises after the third failure so the spider can handle it.
    """
    resp = _requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


class ApiSpider(scrapy.Spider):
    """Pull course data from an internal JSON API (no HTML crawling needed).

    Many modern university sites query an internal REST API from their
    frontend JavaScript.  Check DevTools → Network → XHR/Fetch while
    navigating the course catalogue to find the endpoint.

    This spider queries the API directly (using ``requests`` + ``tenacity``
    for retries) and yields rich items — no per-course page fetch needed.
    """

    name = "template_api"
    start_urls = ["about:blank"]  # not used; we call the API in start_requests

    def start_requests(self):
        """Fetch all courses from the API (paginated)."""
        page = 1
        while True:
            try:
                data = _api_get(
                    f"{_API_BASE}/courses",
                    params={"page": page, "per_page": 100, "international": "true"},
                    headers={"Accept": "application/json"},
                )
            except Exception as exc:
                self.logger.error("API request failed after retries: %s", exc)
                break

            courses = data.get("results") or data.get("courses") or data.get("data") or []
            if not courses:
                break

            for course in courses:
                yield scrapy.Request(
                    url=course.get("url") or course.get("link") or _BASE_URL,
                    callback=self.parse_course,
                    cb_kwargs={"api_data": course},
                    dont_filter=True,
                )

            # Stop when we've read all pages
            if not data.get("next") and page >= data.get("total_pages", 1):
                break
            page += 1

    def parse_course(self, response, api_data: dict):
        """Build rich item from API data (no HTML parsing needed)."""
        name = api_data.get("title") or api_data.get("name") or ""
        url  = response.url

        # Parse the fee string the API returns (e.g. "£16,500 per year")
        fee_str = api_data.get("international_fee") or api_data.get("fee") or ""
        fee_value = _parse_fee(str(fee_str))

        yield {
            "name": name,
            "url": url,
            "payload": {
                "course_name": name,
                "degree_level": _map_level(api_data.get("level") or api_data.get("study_level") or ""),
                "course_location": api_data.get("campus") or _LOCATION,
                "study_mode": api_data.get("mode") or api_data.get("study_mode"),
                "international_fee": fee_value,
                "fee_term": "Year",
                "currency": _CURRENCY,
                "description": api_data.get("description") or api_data.get("summary"),
            },
            "evidence": [
                _ev("course_name", name, "scrapy:api", url, f"API title: {name}", 0.95),
                _ev("international_fee", fee_value, "scrapy:api:fee", url,
                    f"API fee field: {fee_str}", 0.8),
            ],
        }


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN C — Scrapy-Playwright  (JS-rendered / Cloudflare-lite SPAs)
# ══════════════════════════════════════════════════════════════════════════════
#
# YAML settings required:
#
#   discovery:
#     scrapy:
#       spider: my_spider
#       settings:
#         DOWNLOAD_HANDLERS:
#           https: scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler
#           http:  scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler
#         TWISTED_REACTOR: twisted.internet.asyncioreactor.AsyncioSelectorReactor
#         PLAYWRIGHT_BROWSER_TYPE: chromium
#         PLAYWRIGHT_LAUNCH_OPTIONS: '{"headless": true}'
#         DOWNLOAD_DELAY: 1
#         CONCURRENT_REQUESTS: 2
#
# Also run once on the server: playwright install chromium

from scrapy_playwright.page import PageMethod  # scrapy-playwright


class PlaywrightSpider(scrapy.Spider):
    """Spider that uses a real Chromium browser via scrapy-playwright.

    Use for React / Vue SPAs where the course list is rendered client-side
    and a lightweight Cloudflare check only requires a real browser UA
    (not full Cloudflare Enterprise Bot Management).

    For sites with Cloudflare Enterprise (blocks all DCs), this will also
    fail — use the Wayback CDX + scrape.do path instead.
    """

    name = "template_playwright"
    start_urls = [_COURSES_LIST_URL]

    def start_requests(self):
        for url in self.start_urls:
            yield scrapy.Request(
                url,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_methods": [
                        # Wait until the course list is populated
                        PageMethod("wait_for_selector", ".course-card, .course-item", timeout=15000),
                        # Optionally scroll to load lazy-rendered items
                        PageMethod("evaluate", "window.scrollTo(0, document.body.scrollHeight)"),
                        PageMethod("wait_for_timeout", 1000),
                    ],
                },
            )

    async def parse(self, response, **kwargs):
        """Page is fully rendered — parse like normal HTML."""
        page = response.meta.get("playwright_page")
        if page:
            await page.close()  # always close to free browser resources

        for href in response.css("a::attr(href)").getall():
            if re.search(_COURSE_URL_RE, href or ""):
                yield response.follow(
                    href,
                    callback=self.parse_course,
                    meta={
                        "playwright": True,
                        "playwright_page_methods": [
                            PageMethod("wait_for_selector", "h1", timeout=10000),
                        ],
                    },
                )

    async def parse_course(self, response, **kwargs):
        page = response.meta.get("playwright_page")
        if page:
            await page.close()

        name = response.css("h1::text").get("").strip()
        yield {"name": name or response.url, "url": response.url}


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN D — Excel / CSV feed  (universities that publish a data export)
# ══════════════════════════════════════════════════════════════════════════════

import io
import pandas as pd  # pandas


class ExcelFeedSpider(scrapy.Spider):
    """Download and parse an Excel or CSV course list published by the university.

    Some universities offer a downloadable spreadsheet of their full course
    catalogue (common for Course Seeker / QTAC data, or internal programme
    guides).  This spider fetches it once, uses pandas to parse every row,
    and yields a rich item for each — no HTML crawling at all.

    YAML:  No special settings needed; DOWNLOAD_DELAY and CONCURRENT_REQUESTS
    can both be 1 (only one file downloaded).
    """

    name = "template_excel"
    start_urls = [_EXCEL_FEED_URL]

    def parse(self, response):
        """Parse the downloaded Excel/CSV file row by row."""
        raw = io.BytesIO(response.body)

        # Auto-detect format from content-type or URL extension
        url = response.url.lower()
        if url.endswith(".csv") or "text/csv" in response.headers.get("Content-Type", b"").decode():
            df = pd.read_csv(raw)
        else:
            # openpyxl engine handles .xlsx; xlrd handles legacy .xls
            df = pd.read_excel(raw, engine="openpyxl")

        # Normalise column names: lowercase, replace spaces with underscores
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

        for _, row in df.iterrows():
            name = str(row.get("course_name") or row.get("programme") or row.get("title") or "").strip()
            url  = str(row.get("url") or row.get("link") or row.get("website") or "").strip()
            if not name or not url:
                continue

            fee_raw  = str(row.get("international_fee") or row.get("fee") or "")
            fee_value = _parse_fee(fee_raw)

            yield {
                "name": name,
                "url": url,
                "payload": {
                    "course_name": name,
                    "degree_level": _map_level(str(row.get("level") or row.get("study_level") or "")),
                    "international_fee": fee_value,
                    "fee_term": "Year",
                    "currency": _CURRENCY,
                    "study_mode": str(row.get("mode") or row.get("study_mode") or "") or None,
                    "duration": _safe_float(row.get("duration_years")),
                    "duration_term": "Years" if row.get("duration_years") else None,
                    "course_location": str(row.get("campus") or _LOCATION),
                },
                "evidence": [
                    _ev("course_name", name, "scrapy:excel", url, f"Excel row: {name}", 0.95),
                    _ev("international_fee", fee_value, "scrapy:excel:fee", url,
                        f"Excel fee column: {fee_raw}", 0.8),
                ],
            }


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN E — API discovery + per-course HTML rich extraction (combined)
# ══════════════════════════════════════════════════════════════════════════════

class ApiDiscoverHtmlExtractSpider(scrapy.Spider):
    """Use an API to discover course URLs, then scrape each page for full data.

    Best of both worlds:
    - API gives a reliable, complete URL list (no BFS needed).
    - Per-course HTML pages carry IELTS, entry requirements, and intakes
      which the API doesn't return.

    Uses ``tenacity`` for the API call and ``requests`` for a synchronous
    initial fetch in ``start_requests``.
    """

    name = "template_combined"
    start_urls = ["about:blank"]

    def start_requests(self):
        try:
            data = _api_get(f"{_API_BASE}/courses", params={"format": "json"})
        except Exception as exc:
            self.logger.error("API discovery failed: %s", exc)
            return

        for item in data.get("courses") or []:
            url = item.get("url") or item.get("link")
            if url:
                yield scrapy.Request(
                    url,
                    callback=self.parse_course,
                    cb_kwargs={"api_item": item},
                )

    def parse_course(self, response, api_item: dict):
        soup = BeautifulSoup(response.text, "lxml")
        name = (response.css("h1::text").get() or api_item.get("title") or "").strip()

        # IELTS from page text
        ielts_m = re.search(r"IELTS\D{0,30}?(\d(?:\.\d)?)\s*overall", response.text, re.I)
        ielts = float(ielts_m.group(1)) if ielts_m else None

        # Fee from API (more reliable than parsing HTML for fee strings)
        fee_value = _parse_fee(str(api_item.get("fee") or ""))

        # Intakes from page
        intakes = re.findall(
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b",
            response.text, re.I,
        )
        intake_months = list(dict.fromkeys(m.capitalize() for m in intakes)) or None

        yield {
            "name": name,
            "url": response.url,
            "payload": {
                "course_name": name,
                "degree_level": _map_level(api_item.get("level") or ""),
                "international_fee": fee_value,
                "fee_term": "Year",
                "currency": _CURRENCY,
                "course_location": _LOCATION,
                "ielts_overall": ielts,
                "intake_months": intake_months,
            },
            "evidence": [
                _ev("course_name", name, "scrapy:h1", response.url, f"h1: {name}", 0.9),
                *([_ev("ielts_overall", ielts, "scrapy:regex", response.url,
                        f"IELTS {ielts} overall", 0.85)] if ielts else []),
                *([_ev("international_fee", fee_value, "scrapy:api:fee", response.url,
                        f"API fee: {api_item.get('fee')}", 0.8)] if fee_value else []),
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS  (used across all patterns above)
# ─────────────────────────────────────────────────────────────────────────────

from price_parser import Price  # price-parser


def _parse_fee(raw: str) -> Optional[float]:
    """Parse a fee string to a float using price-parser.

    Examples:
      "£16,500 per year"     → 16500.0
      "AUD 38,500"           → 38500.0
      "$42,000"              → 42000.0
      ""  / "TBC" / "N/A"   → None
    """
    if not raw or not raw.strip():
        return None
    p = Price.fromstring(raw)
    if p.amount is not None:
        try:
            return float(p.amount)
        except (TypeError, ValueError):
            pass
    return None


def _map_level(raw: str) -> Optional[str]:
    """Map a raw study-level string to a staging degree_level value."""
    low = (raw or "").lower()
    if any(k in low for k in ("phd", "doctor", "research degree")):
        return "Doctorate"
    if any(k in low for k in ("master", "msc", "mba", "meng", "postgrad")):
        return "Master's"
    if any(k in low for k in ("bachelor", "bsc", "ba ", "beng", "honours", "undergrad")):
        return "Bachelor's"
    if any(k in low for k in ("grad cert", "graduate cert")):
        return "Graduate Certificate"
    if any(k in low for k in ("grad dip", "graduate dip")):
        return "Graduate Diploma"
    if any(k in low for k in ("postgrad cert", "pg cert", "pgcert")):
        return "Postgraduate Certificate"
    if any(k in low for k in ("postgrad dip", "pg dip", "pgdip")):
        return "Postgraduate Diploma"
    if "diploma" in low:
        return "Diploma"
    if "certificate" in low:
        return "Certificate"
    if "foundation" in low:
        return "Foundation"
    return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _ev(
    field_key: str,
    value: Any,
    method: str,
    source_url: str,
    snippet: str,
    confidence: float,
) -> dict:
    """Build a single evidence row for the staging evidence panel."""
    return {
        "field_key": field_key,
        "value": value,
        "normalized": value,
        "source_url": source_url,
        "page_type": "course",
        "method": method,
        "snippet": snippet,
        "confidence": confidence,
        "decision_status": "selected",
    }

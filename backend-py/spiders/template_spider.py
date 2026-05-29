"""Template Scrapy spider for university course discovery.

USAGE
-----
1. Copy this file to backend-py/spiders/<uni_slug>_spider.py.
2. Fill in the CONFIGURE section below (allowed_domains, start_urls, URL rule).
3. Add to the university's YAML:

       discovery:
         scrapy:
           spider: <uni_slug>_spider      # filename without .py
           settings:
             DOWNLOAD_DELAY: 1           # seconds between requests (polite)
             CONCURRENT_REQUESTS: 4      # parallel downloads

4. Trigger a scrape job from the portal — the spider runs in a subprocess
   and its items flow into the normal staging + review pipeline.

HOW IT WORKS
------------
- Discovery-only mode (default): yield {"name": ..., "url": ...}.
  The orchestrator then fetches each URL and runs the normal extractors
  (IELTS regex, fee page, Gemini fallback, etc.).
- Rich mode (optional): yield {"name": ..., "url": ..., "payload": {...},
  "evidence": [...]}.  The orchestrator skips per-course extraction
  entirely — identical to the SearchStax Huddersfield provider.
  Use this when the listing pages already carry all the data you need
  (e.g. API endpoints, JSON feeds, structured search results).

TIPS
----
- Use scrapy.Spider for simple single-level scraping (one listing page).
- Use CrawlSpider + Rule/LinkExtractor for recursive multi-page crawling.
- Use response.follow() inside parse() for manual pagination.
- For JS-rendered sites, consider scrapy-playwright middleware instead.
  See https://github.com/scrapy-plugins/scrapy-playwright for setup.
- Scrapy settings can be overridden per-university via YAML (settings:
  block) without touching this file.
"""
from __future__ import annotations

import scrapy


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURE THIS SECTION
# ─────────────────────────────────────────────────────────────────────────────

_UNI_NAME = "Example University"            # Human label (not used in staging)
_ALLOWED_DOMAIN = "example.edu"             # University hostname (no www.)
_START_URL = "https://www.example.edu/courses/"  # Listing page to start from

# Regex that must match a URL for the spider to follow it and call parse_course.
# Anchored to the path portion.  Examples:
#   r"/courses/[a-z0-9-]+/?$"     — leaf course pages
#   r"/study/(undergraduate|postgraduate)/.+/$"
#   r"/(bachelor|master|phd)s?/.+?"
_COURSE_URL_RE = r"/courses/[a-z0-9-]+/?$"

# ─────────────────────────────────────────────────────────────────────────────


class TemplateCourseSpider(scrapy.Spider):
    """Generic crawling spider.  Follows all <a> links on the start page(s)
    whose href matches _COURSE_URL_RE, then calls parse_course on each.
    """

    name = "template"
    allowed_domains = [_ALLOWED_DOMAIN]
    start_urls = [_START_URL]

    custom_settings = {
        "DOWNLOAD_DELAY": 0.5,
        "CONCURRENT_REQUESTS": 8,
        "ROBOTSTXT_OBEY": False,           # already set globally by bridge
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
        """Follow links that look like course pages; hand off to parse_course."""
        import re
        for href in response.css("a::attr(href)").getall():
            if re.search(_COURSE_URL_RE, href or ""):
                yield response.follow(href, callback=self.parse_course)

        # Pagination: follow "next page" links on listing pages.
        next_page = response.css('a[rel="next"]::attr(href)').get()
        if next_page:
            yield response.follow(next_page, callback=self.parse)

    def parse_course(self, response):
        """Extract the course name from the page and yield a discovery item.

        This is DISCOVERY-ONLY mode — the orchestrator will re-fetch
        response.url and run the normal extractors (IELTS, fee, etc.).

        To switch to RICH mode (skip re-fetch + extraction), also populate
        'payload' and 'evidence' keys — see the module docstring example.
        """
        name = (
            response.css("h1::text").get()
            or response.css("title::text").get()
            or response.url
        ).strip()

        yield {
            "name": name,
            "url": response.url,
            # Uncomment + populate the block below for rich / pre-extracted mode:
            # "payload": {
            #     "course_name": name,
            #     "degree_level": "Bachelor's",
            #     "course_location": "Example City",
            #     "study_mode": "Full-time",
            #     "duration": 3.0,
            #     "duration_term": "Years",
            #     "international_fee": 25000.0,
            #     "fee_term": "Year",
            #     "currency": "AUD",
            #     "ielts_overall": 6.5,
            #     "intake_months": ["February", "July"],
            #     "description": "...",
            # },
            # "evidence": [
            #     {
            #         "field_key": "course_name",
            #         "value": name,
            #         "normalized": name,
            #         "source_url": response.url,
            #         "page_type": "course",
            #         "method": "scrapy:h1",
            #         "snippet": f"h1 text: {name}",
            #         "confidence": 0.9,
            #         "decision_status": "selected",
            #     },
            # ],
        }

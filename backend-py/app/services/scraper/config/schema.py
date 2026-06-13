"""Pydantic models for per-university scraper configuration.

Schema is split into two top-level sections as proposed:

  discovery:   Settings that are safe to replay against an unknown university
               during Tier-3 playbook matching (URL filters, sitemap options,
               subdomain probes).  These do not assume anything about the
               university's content structure.

  extraction:  Settings that are specific to how a known university structures
               its pages (fee pages, English requirements, text-cleaning
               patterns, filters).  These MUST NOT be replayed against unknown
               unis in Tier-3 because they encode knowledge about a specific
               site's layout.

This split enables the Week-3 tiered-fallback feature to load a known
university's ``discovery`` section and replay it against a new university
without accidentally importing extraction assumptions (e.g. a
``trust_vision_ocr: false`` override that was tuned to prevent ACAP-specific
hallucinations from polluting a brand-new university's scrape).
"""
from __future__ import annotations

from typing import Optional, Union

from pydantic import BaseModel, Field


# ── Discovery ───────────────────────────────────────────────────────────────

class SearchStaxConfig(BaseModel):
    """Custom data-source provider: a SearchStax-hosted Solr course index.

    Some universities (e.g. University of Huddersfield, Queen Margaret
    University) serve their course catalogue from a SearchStax Solr core —
    the same endpoint their live React SPA queries client-side.  When this
    block is present the orchestrator skips HTML discovery + per-course
    extraction entirely and builds fully-formed staged-course records
    straight from the Solr docs.  No page fetching is needed because the
    ``content`` field carries the full page text (IELTS, entry reqs, etc.).

    Token resolution order (first non-empty wins):
      1. ``authorization_token`` — literal value in this YAML block
      2. env var named by ``token_env`` (e.g. ``HUD_SEARCHSTAX_TOKEN``)
      3. ``token`` — legacy literal field
      4. ``SEARCHSTAX_TOKEN`` — global fallback env var (set once, works for
         all universities that don't specify their own token field)

    The read tokens the SPA ships to every browser are not server secrets,
    but operators should rotate them periodically.  For production, prefer
    ``token_env`` or the global ``SEARCHSTAX_TOKEN`` env var over committing
    a literal value.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Set to false to temporarily disable the SearchStax provider "
            "without removing the config block.  When false the orchestrator "
            "falls through to BFS / browser discovery."
        ),
    )
    endpoint: Union[str, list[str]] = Field(
        description=(
            "Full Solr select URL (e.g. '.../emselect').  May be a single URL "
            "string or a list of URLs when the university splits its catalogue "
            "across multiple Solr models (e.g. one endpoint for UG, another for "
            "PG).  The provider pages through each endpoint in turn and merges "
            "all results."
        ),
    )

    @property
    def endpoints(self) -> list[str]:
        """Always return endpoint(s) as a list, regardless of YAML spelling."""
        if isinstance(self.endpoint, list):
            return [e for e in self.endpoint if e]
        return [self.endpoint] if self.endpoint else []

    authorization_token: Optional[str] = Field(
        default=None,
        description=(
            "SearchStax read token used in the 'Authorization: Token <t>' "
            "header.  Priority 1 in the token resolution chain.  The SPA "
            "ships this to every browser so it is not a server secret, but "
            "prefer token_env for production to avoid committing it to YAML."
        ),
    )
    token: Optional[str] = Field(
        default=None,
        description=(
            "Legacy literal token field (priority 3).  Prefer "
            "authorization_token (priority 1) or token_env (priority 2)."
        ),
    )
    token_env: Optional[str] = Field(
        default=None,
        description=(
            "Name of an environment variable to read the token from (priority "
            "2).  E.g. 'HUD_SEARCHSTAX_TOKEN' or 'QMU_SEARCHSTAX_TOKEN'.  "
            "When unset, the global 'SEARCHSTAX_TOKEN' env var is tried last."
        ),
    )
    filter_query: str = Field(
        default="sectionType_s:course",
        description=(
            "Solr fq applied to restrict the result set to courses.  The "
            "provider automatically retries without this filter when the "
            "initial query returns 0 results (some cores use different section "
            "types).  Set to empty string '' to skip the filter entirely."
        ),
    )
    extra_params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Additional Solr query parameters merged into every paginated "
            "request.  Use for university-specific API flags that the generic "
            "provider does not set by default.  "
            "Example (QMU): {model: 'coursefinder-ug', language: 'en', "
            "spellcheck.correct: 'true', hl.fragsize: '200'}. "
            "Values override built-in defaults (q, fq, rows, start, fl, wt) "
            "when the same key is present."
        ),
    )
    use_generic_mapper: bool = Field(
        default=False,
        description=(
            "When True, use the generic Solr-field mapper from "
            "generic_search_api instead of the Huddersfield-specific mapper "
            "(which applies HUD fee bands + name reformatting).  Set to True "
            "for any university that is NOT Huddersfield.  The generic mapper "
            "extracts name, URL, degree level, IELTS, duration, and "
            "description from common Solr field names and the content blob."
        ),
    )
    page_size: int = Field(
        default=100,
        description="Number of Solr docs fetched per paginated request.",
    )
    max_courses: Optional[int] = Field(
        default=None,
        description="Optional cap on the number of course docs staged (debug).",
    )
    links_only: bool = Field(
        default=False,
        description=(
            "When True the SearchStax provider is used for URL *discovery* only: "
            "each Solr doc is mapped to a bare {name, url} link dict without "
            "a 'searchstax_result' key.  The orchestrator then runs normal "
            "per-course HTTP/browser extraction for each discovered URL "
            "(fees, IELTS, etc. are fetched from the live page).  "
            "Use this for universities like WLV whose Solr docs do NOT contain "
            "fees or IELTS and whose live pages are fully reachable by the "
            "browser pool.  Contrast with the default mode (links_only=False) "
            "used by Huddersfield where Solr has the full page-text content "
            "field and no per-course fetch is needed."
        ),
    )
    fee_year: int = Field(
        default=2025,
        description="Academic fee year written into every staged fee row.",
    )
    currency: str = Field(
        default="GBP",
        description="ISO currency code written into every staged fee row.",
    )
    central_fee_page: Optional[str] = Field(
        default=None,
        description=(
            "URL of the central fee schedule page used as the source_url for "
            "the international_fee evidence row (the fee is band-derived, not "
            "on the course page)."
        ),
    )
    field_map: dict[str, Union[str, list[str]]] = Field(
        default_factory=dict,
        description=(
            "Maps logical field names to the university-specific Solr field "
            "names used by this SearchStax core.  Required when the Solr core "
            "uses non-standard field names (e.g. Durham uses 'courseUrl_t' "
            "instead of the WLV default 'url_t'). "
            "Fallback keys (duration_fallback, intake_fallback, mode_fallback, "
            "location_fallback) accept a string OR a list of strings tried in "
            "order — the first non-empty Solr field wins.  This lets a single "
            "YAML config handle universities whose Solr index uses different "
            "field schemas for different course sub-types (e.g. WLV clearing vs "
            "non-clearing courses). "
            "Recognised logical keys and their built-in defaults: "
            "  url          → 'url_t'           (canonical course page URL) "
            "  name         → 'title_t'         (course display name) "
            "  degree_type  → 'award_s'         (degree abbreviation, e.g. 'MSc') "
            "  degree_level → 'study_level_s'   (Undergraduate / Postgraduate) "
            "  study_mode   → 'mode_s'          (Full-time / Part-time) "
            "  duration     → 'duration_t'      (e.g. '3 years full-time') "
            "  intake_dates → 'start_dates_s'   (e.g. 'September 2026') "
            "  category     → 'subject_s'       (department / subject area) "
            "  location     → no default (omitted if not set); multi-valued "
            "                 fields joined with ', ' into course_location. "
            "                 location_override still wins if both are set. "
            "Example (Durham University): "
            "  field_map: {url: courseUrl_t, name: Degreename_t, "
            "  degree_type: Degreetype_ss, degree_level: DegreeCourseLevel_ss, "
            "  study_mode: DegreeStudyOptions_ss, duration: DegreeDuration_ss, "
            "  intake_dates: DegreeStartDate_ss, category: Department_ss}"
        ),
    )
    url_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of Solr field names to try when extracting the "
            "course page URL.  The first non-empty value wins.  Use when the "
            "university's Solr core uses a non-standard URL field name and "
            "you need to try multiple fallback fields.  "
            "Takes priority over field_map['url'].  "
            "Example (QMUL): url_fields: [coursepageurl_t]"
        ),
    )
    title_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of Solr field names used to build the course title.  "
            "title_fields[0] is the main course title (mapped to field_map.name).  "
            "title_fields[1], if present, is the degree-type prefix (e.g. 'MSc', "
            "'BSc (Hons)') that is prepended to the title when it is not already "
            "the opening token — mapped to field_map.degree_type.  "
            "Takes priority over field_map['name'] / field_map['degree_type'].  "
            "Example (QMUL): title_fields: [coursetitle_t, awardname_t]"
        ),
    )
    field_map_as_payload: bool = Field(
        default=False,
        description=(
            "When True (and links_only is False), use the field_map to build a "
            "searchstax_result payload directly from Solr doc fields — no "
            "per-course page fetch is needed.  Use for universities (e.g. Durham) "
            "whose Solr docs contain structured metadata (name, level, duration, "
            "mode, intakes, category) but NOT fees or IELTS. "
            "Fees are then supplied by extraction.fees.degree_level_defaults. "
            "field_map keys translated to payload: "
            "  name         → course_name "
            "  degree_type  → degree_level  (e.g. 'BA (Hons)', 'MSc') "
            "  degree_level → academic_level (normalised to Undergraduate/Postgraduate) "
            "  study_mode   → study_mode "
            "  duration     → duration "
            "  intake_dates → intake_months (month names extracted from 'Month YYYY') "
            "  category     → category "
            "location_override → course_location (when set)"
        ),
    )
    location_override: Optional[str] = Field(
        default=None,
        description=(
            "Hard-coded course_location value written into every staged course "
            "when field_map_as_payload is True (e.g. 'Durham, UK'). "
            "Ignored in links_only mode."
        ),
    )
    url_base: Optional[str] = Field(
        default=None,
        description=(
            "Base URL used to construct a full course URL when the Solr url "
            "field contains a bare course code rather than a real HTTP URL "
            "(e.g. 'WR006J01UMU' → 'https://www.wlv.ac.uk/courses/wr006j01umu'). "
            "When set and the resolved url value contains no '://', the provider "
            "builds: url_base.rstrip('/') + '/' + code.lower(). "
            "Ignored when the url field already contains a full URL."
        ),
    )
    exclude_part_time: bool = Field(
        default=False,
        description=(
            "When True (field_map_as_payload mode only): courses whose ONLY "
            "study mode is Part-time are dropped entirely. Courses offered in "
            "both Full-time and Part-time have Part-time stripped so only "
            "Full-time is staged."
        ),
    )
    max_fulltime_duration_years: Optional[int] = Field(
        default=None,
        description=(
            "When set with exclude_part_time, rejects any course whose parsed "
            "duration (in years) exceeds this threshold. Used to filter out "
            "part-time-only programs (e.g. PhD research = 6/8 years) whose "
            "Solr duration_t field lacks a 'part-time' qualifier so the text "
            "filter cannot detect them. Typical value: 4 (WLV)."
        ),
    )
    location_strip_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "List of string prefixes to strip from each raw Solr location value "
            "before joining into course_location. Useful when the Solr facet label "
            "prepends a category name (e.g. 'University: City Campus' → 'City Campus'). "
            "Stripping is case-sensitive and applied to every value in the "
            "multi-valued location field."
        ),
    )
    exclude_title_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "Case-insensitive title prefixes that identify professional/CPD modules "
            "that should be rejected immediately — before any HTTP fetch, Gemini call, "
            "or browser pass.  A course is skipped when its Solr title (or the "
            "constructed '<award> <title>' name) starts with any listed prefix. "
            "Intended for links_only mode where Solr surfaces CPD/practitioner "
            "courses alongside degree programmes, e.g. 'Postgraduate Credit', "
            "'Undergraduate Credit', 'CPD', 'Workshop'.\n"
            "Example (WLV):\n"
            "  exclude_title_prefixes:\n"
            "    - 'Postgraduate Credit'\n"
            "    - 'Undergraduate Credit'\n"
            "    - 'CPD'\n"
        ),
    )
    exclude_title_substrings: list[str] = Field(
        default_factory=list,
        description=(
            "Like exclude_title_prefixes but matches anywhere in the title "
            "(case-insensitive substring check).  Use for keywords that appear "
            "mid-title, e.g. '(V300)' or 'Non-Medical Prescribing'."
        ),
    )


class ScrapyConfig(BaseModel):
    """Run a Scrapy spider for course URL discovery (or full extraction).

    The spider runs in a subprocess — isolating Scrapy's Twisted event loop
    from the asyncio/Celery stack.  Output items are written to a temp JSON
    lines file by Scrapy's built-in feed exporter, then read back by the
    bridge (``scrapy_bridge.py``) and fed into the normal staging pipeline.

    Spider files must live in ``backend-py/spiders/<spider>.py``.  Copy
    ``backend-py/spiders/template_spider.py`` as a starting point.

    Minimum item shape (discovery-only mode)::

        {"name": "Course Name", "url": "https://..."}

    Rich mode (bypasses per-course extraction, like SearchStax)::

        {"name": "...", "url": "...", "payload": {...}, "evidence": [...]}

    ``payload`` keys match the ``scraped_courses`` staging columns.
    """

    spider: str = Field(
        description=(
            "Spider filename without the .py extension. "
            "File must exist at backend-py/spiders/<spider>.py."
        ),
    )
    settings: dict = Field(
        default_factory=dict,
        description=(
            "Scrapy SETTINGS overrides passed via -s KEY=VALUE on the CLI. "
            "E.g. {'DOWNLOAD_DELAY': 1, 'CONCURRENT_REQUESTS': 4}."
        ),
    )
    max_courses: Optional[int] = Field(
        default=None,
        description="Optional cap on returned links (useful for debug runs).",
    )
    timeout_seconds: int = Field(
        default=600,
        description="Maximum seconds the spider subprocess may run before it is killed.",
    )


class GenericSearchApiConfig(BaseModel):
    """YAML-driven generic JSON/REST API discovery.

    Use when a university's course catalogue is served from a JSON/REST API
    (SearchStax Solr, Algolia, custom REST endpoint) and you want API-first
    discovery to run BEFORE BFS/browser tiers.

    Unlike ``discovery.searchstax`` (which uses the HUD-specific or generic
    Solr mapper), this config lets you specify the full HTTP request shape:
    method, URL, headers, query params, and how to extract URLs and names from
    the response.  The orchestrator runs this tier immediately after SearchStax
    and before BFS/browser discovery — API discovery always wins when it
    returns ≥1 link.

    Example (SearchStax Solr core for a non-HUD university)::

        discovery:
          generic_search_api:
            url: "https://searchcloud-1.searchstax.com/29847/myuni-1234/emselect"
            headers:
              authorization: "Token <your-token>"
            params:
              q: "*"
              rows: "250"
            root_path: "response.docs"
            url_fields: [url, page_url]
            title_fields: [title, name]
            allow_url_patterns:
              - '^https://www\\.myuni\\.edu\\.au/courses/[a-z0-9-]+/?$'
            normalize_relative_urls: true
            base_url: "https://www.myuni.edu.au"
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Set false to disable without removing the config block. "
            "When false the orchestrator falls through to BFS/browser."
        ),
    )
    fetch_via_browser: bool = Field(
        default=False,
        description=(
            "When True, use a Playwright browser to call the API endpoint instead of "
            "making raw HTTP requests. The browser navigates to the university homepage "
            "first so the server sets its session cookies, then calls the API via "
            "JavaScript fetch() — which inherits those cookies. This is required for "
            "session-bound APIs (e.g. Optimizely CMS) whose pagination is tied to a "
            "server-side session and ignores the page/offset params from external clients."
        ),
    )
    browser_seed_url: Optional[str] = Field(
        default=None,
        description=(
            "URL to navigate to before making API calls in browser mode. "
            "Triggers the server's session-cookie handshake. Defaults to the "
            "university's scrape_url (homepage) when not set."
        ),
    )
    method: str = Field(
        default="GET",
        description="HTTP method for the API request: GET or POST.",
    )
    url: str = Field(
        description="Full URL of the JSON/REST API endpoint.",
    )
    additional_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Extra API URLs to call with the same settings (method, headers, params, "
            "root_path, url_fields, pagination, etc.). Results are merged and "
            "deduplicated. Use when a university splits its catalogue across separate "
            "UG and PG endpoints. Each URL gets its own full pagination cycle."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "HTTP headers to send with every request. "
            "E.g. {'authorization': 'Token abc123', 'accept': 'application/json'}. "
            "Prefer using an environment variable for tokens — do not commit "
            "literal tokens to the YAML file."
        ),
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Query parameters (GET) or body fields (POST) sent with every request. "
            "E.g. {'q': '*', 'rows': '250', 'model': 'coursefinder-ug'}."
        ),
    )
    root_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-separated path into the JSON response to reach the array of course "
            "items. E.g. 'response.docs', 'data.items', 'results'. "
            "None = the response itself is the array."
        ),
    )
    url_fields: list[str] = Field(
        default_factory=lambda: ["url", "course_url", "page_url", "link", "path"],
        description=(
            "Ordered list of JSON field names to try when extracting the course URL "
            "from each item. The first non-empty value wins."
        ),
    )
    title_fields: list[str] = Field(
        default_factory=lambda: ["title", "name", "course_name"],
        description=(
            "Ordered list of JSON field names to try when extracting the course name. "
            "The first non-empty value wins."
        ),
    )
    allow_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex whitelist applied to extracted URLs. If non-empty, only URLs "
            "matching at least one pattern are kept."
        ),
    )
    block_url_patterns: list[str] = Field(
        default_factory=list,
        description="Regex blocklist applied to extracted URLs. Matching URLs are dropped.",
    )
    normalize_relative_urls: bool = Field(
        default=True,
        description=(
            "When True, relative URLs (starting with /) are prepended with base_url "
            "to make them absolute."
        ),
    )
    base_url: Optional[str] = Field(
        default=None,
        description=(
            "Origin URL used to resolve relative URLs "
            "(e.g. 'https://www.jcu.edu.au'). Required when normalize_relative_urls=True "
            "and the API returns relative paths."
        ),
    )
    page_size: Optional[int] = Field(
        default=None,
        description=(
            "When set, paginate the API using page_size_param and offset_param. "
            "None = single request (rely on rows param in params instead)."
        ),
    )
    page_size_param: str = Field(
        default="rows",
        description="Query parameter name for page size (used when page_size is set).",
    )
    offset_param: str = Field(
        default="start",
        description="Query parameter name for offset / page start.",
    )
    page_number_param: Optional[str] = Field(
        default=None,
        description=(
            "When set, use 1-based page-number pagination instead of offset pagination. "
            "The value is the query parameter name sent to the API (e.g. 'currentPage', "
            "'page', 'PageNumber'). Requires page_size and page_size_param to also be set. "
            "Use has_next_field to let the API signal the last page."
        ),
    )
    has_next_field: Optional[str] = Field(
        default=None,
        description=(
            "Dot-separated path into the JSON response (at root level, NOT inside root_path) "
            "pointing to a boolean field that is true when more pages exist. "
            "E.g. 'result.hasNextPage', 'meta.has_more'. "
            "When set the paginator stops as soon as this field is false/absent, "
            "regardless of page_size comparisons."
        ),
    )
    max_pages: int = Field(
        default=20,
        description="Hard ceiling on pagination rounds to prevent runaway loops.",
    )
    max_courses: Optional[int] = Field(
        default=None,
        description="Optional cap on extracted links (useful for debug runs).",
    )
    body: Optional[dict] = Field(
        default=None,
        description=(
            "JSON body template sent with every POST request. "
            "Merged with pagination updates before each call. "
            "Use this instead of params when the API expects application/json body "
            "(e.g. Elastic App Search, Algolia, some Solr variants). "
            "Example: {query: '', page: {current: 1, size: 100}}"
        ),
    )
    body_pagination: Optional["BodyPaginationConfig"] = Field(
        default=None,
        description=(
            "Configures pagination when page number / size must be embedded inside "
            "the JSON request body rather than as query-string parameters. "
            "Required for Elastic App Search and similar APIs. "
            "See BodyPaginationConfig for field details."
        ),
    )


class BodyPaginationConfig(BaseModel):
    """Pagination config for APIs that embed page number/size in the request body.

    Used with Elastic App Search, Algolia, and any REST endpoint where pagination
    is controlled by nested JSON body fields rather than query-string params::

        body_pagination:
          current_path: page.current    # dot-path in body to set current page
          size_path: page.size          # dot-path in body to set page size
          total_pages_path: meta.page.total_pages  # dot-path in response for stop signal
    """

    current_path: str = Field(
        description=(
            "Dot-path inside the request body to update with the current page number. "
            "E.g. 'page.current' → sets body['page']['current'] = 1, 2, 3 …"
        ),
    )
    size_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-path inside the request body to set the page size. "
            "E.g. 'page.size'. Omit if page size is already in the body template "
            "and should not be overridden."
        ),
    )
    total_pages_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-path in the API response pointing to the total number of pages. "
            "E.g. 'meta.page.total_pages' (Elastic App Search). "
            "When set the paginator stops as soon as current_page >= total_pages, "
            "preventing extra empty requests."
        ),
    )
    total_results_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-path in the API response pointing to the total result count. "
            "E.g. 'meta.page.total_results'. Informational only — used in log output."
        ),
    )


# Resolve forward reference: GenericSearchApiConfig.body_pagination uses
# BodyPaginationConfig which is defined AFTER it (alphabetical order would
# require the reverse, but BodyPaginationConfig is logically subordinate).
GenericSearchApiConfig.model_rebuild()


class SsrPropListingPageConfig(BaseModel):
    """One listing page entry for the generic SSR-prop discovery provider."""

    url: str = Field(description="Listing page URL to fetch (plain httpx, no browser).")
    url_prefix: str = Field(
        description=(
            "Base URL prepended to each slug to form the course URL. "
            "E.g. 'https://www.lancaster.ac.uk/study/undergraduate/courses/'. "
            "The trailing slash is normalised automatically."
        )
    )
    label: str = Field(
        default="",
        description="Human-readable label for logs, e.g. 'Undergraduate'.",
    )


class SsrPropDiscoveryConfig(BaseModel):
    """Generic discovery from server-rendered JSON props embedded in HTML.

    Activated by ``discovery.ssr_prop_discovery`` in any university's YAML.
    The scraper fetches each listing page with plain httpx, locates an HTML
    attribute (e.g. ``:courses-data``) whose value is a JSON array of course
    objects, extracts slugs, and builds course URLs — no JavaScript required.
    A Playwright browser fallback is used automatically when the prop is absent.

    Lancaster example::

        discovery:
          ssr_prop_discovery:
            listing_pages:
              - url: "https://www.lancaster.ac.uk/study/undergraduate/courses/"
                url_prefix: "https://www.lancaster.ac.uk/study/undergraduate/courses/"
                label: "Undergraduate"
            prop_attr: ":courses-data"
            slug_field: "slug"
            name_field: "title"
            url_suffix: "/{year}/"
            year_field: "entryYear"
            year_filter_prefix: "{short_year}/"
            browser_fallback_xpath: "//nav[contains(@class, 'a-z')]//li/a/@href"
            browser_wait_selector: "nav.a-z"
    """

    listing_pages: list[SsrPropListingPageConfig] = Field(
        description=(
            "One entry per catalogue/listing page to fetch. "
            "Each has: url (listing page), url_prefix (base for course URLs), label (log label)."
        )
    )
    prop_attr: str = Field(
        default=":courses-data",
        description=(
            "Name of the HTML attribute containing the JSON courses array. "
            "Vue prop example: ':courses-data'. "
            "React/Next.js example: 'data-courses' or 'data-initial-props'."
        ),
    )
    slug_field: str = Field(
        default="slug",
        description="Field in each JSON object whose value is the URL slug.",
    )
    name_field: str = Field(
        default="title",
        description="Field in each JSON object whose value is the course name.",
    )
    url_suffix: str = Field(
        default="/{year}/",
        description=(
            "Appended after '{url_prefix}/{slug}' to form the final course URL. "
            "Supports tokens: {year} (4-digit, e.g. 2026), {short_year} (2-digit, e.g. 26)."
        ),
    )
    year_field: Optional[str] = Field(
        default=None,
        description=(
            "Optional. When set, only JSON objects whose year_field value starts "
            "with year_filter_prefix are included. Leave null to include all objects."
        ),
    )
    year_filter_prefix: str = Field(
        default="{short_year}/",
        description=(
            "Prefix matched against year_field. Tokens: {year} and {short_year}. "
            "Lancaster uses '{short_year}/' because its format is '26/27'. "
            "A university with plain '2026' values would use '{year}'."
        ),
    )
    browser_fallback_xpath: Optional[str] = Field(
        default=None,
        description=(
            "XPath evaluated in a Playwright browser when the SSR prop is missing. "
            "Lancaster: '//nav[contains(@class, \"a-z\")]//li/a/@href'. "
            "Leave null to skip browser fallback."
        ),
    )
    browser_wait_selector: Optional[str] = Field(
        default=None,
        description=(
            "CSS selector to wait for before evaluating browser_fallback_xpath. "
            "Lancaster: 'nav.a-z'. Only used when browser_fallback_xpath is set."
        ),
    )
    course_url_pattern: Optional[str] = Field(
        default=None,
        description=(
            "Optional regex to filter hrefs extracted by the browser fallback. "
            "Lancaster: '/study/(undergraduate|postgraduate)/courses/[^/]+/20\\\\d{2}/?$'."
        ),
    )


class DiscoveryConfig(BaseModel):
    """Safe to replay against unknown universities (Tier-3 playbook matching)."""

    searchstax: Optional[SearchStaxConfig] = Field(
        default=None,
        description=(
            "When present, route discovery + extraction through the SearchStax "
            "Solr provider instead of HTML crawling. See SearchStaxConfig."
        ),
    )
    ssr_prop_discovery: Optional[SsrPropDiscoveryConfig] = Field(
        default=None,
        description=(
            "Generic SSR-prop discovery. When present, fetches each listing page "
            "configured under ssr_prop_discovery.listing_pages, extracts a JSON "
            "courses array from a server-rendered HTML attribute (e.g. :courses-data "
            "for Vue, data-courses for React), and builds course URLs — no browser "
            "required.  A Playwright browser fallback is triggered automatically if "
            "the prop is missing.  See SsrPropDiscoveryConfig for full field docs "
            "and examples."
        ),
    )
    lancaster_listing: bool = Field(
        default=False,
        description=(
            "Lancaster University shorthand for ssr_prop_discovery. "
            "Sets up Lancaster's two listing pages (:courses-data Vue prop) "
            "automatically. Prefer ssr_prop_discovery for new universities. "
            "Use lancaster_listing_year to pin a specific entry year."
        ),
    )
    lancaster_listing_year: Optional[int] = Field(
        default=None,
        description=(
            "4-digit entry year for lancaster_listing discovery (e.g. 2026). "
            "Defaults to the current calendar year when not set."
        ),
    )
    scrapy: Optional[ScrapyConfig] = Field(
        default=None,
        description=(
            "When present, run the named Scrapy spider for discovery. "
            "Spider output feeds the normal staging pipeline. "
            "Falls through to BFS/sitemap if the spider returns 0 links. "
            "See ScrapyConfig and backend-py/spiders/template_spider.py."
        ),
    )
    generic_search_api: Optional[GenericSearchApiConfig] = Field(
        default=None,
        description=(
            "When present, query the configured JSON/REST API for course links "
            "BEFORE BFS/browser discovery. Supports SearchStax Solr, Algolia, "
            "and any custom REST endpoint that returns a JSON array. "
            "The API tier runs immediately after SearchStax — if it returns ≥1 "
            "link, BFS and browser tiers are skipped. Falls through to BFS if "
            "0 links returned. See GenericSearchApiConfig for full field docs."
        ),
    )
    auto_api_discovery: bool = Field(
        default=False,
        description=(
            "Enable autonomous XHR/Fetch API discovery. When true, and when all "
            "preceding discovery tiers (YAML API, auto_config API, BFS, Scrapy) "
            "return fewer than 10 course links, the scraper: "
            "(1) opens the university's course-listing page in a headless browser, "
            "(2) intercepts all JSON XHR/Fetch calls made during page load, "
            "(3) classifies each call (SearchStax, Algolia, Elasticsearch, Solr, REST), "
            "(4) generates a GenericSearchApiConfig for the best candidate (confidence ≥ 0.45), "
            "(5) immediately uses that config to fetch course links for the current run, "
            "(6) persists the discovered endpoint URL + auth hint to auto_config so "
            "    future scrapes use it without re-running XHR capture. "
            "Typically adds 15-25 s to the first scrape run; zero overhead on subsequent "
            "runs (auto_config path bypasses discovery entirely). "
            "The captured Authorization header value is stored in auto_config under "
            "'_auto_api_auth' — it is a read-only public search key (SearchStax / Algolia), "
            "NOT a user credential. Treat it as semi-public but rotate via the "
            "provider console if the key is ever compromised. "
            "When false (default), autonomous API detection never runs."
        ),
    )
    fallback_subdomains: list[str] = Field(
        default_factory=list,
        description=(
            "Additional subdomains to probe when the primary URL yields <5 candidates. "
            "E.g. ['handbook.{domain}', 'courses.{domain}', 'international.{domain}']."
        ),
    )
    allowed_extra_hostnames: list[str] = Field(
        default_factory=list,
        description=(
            "Hostnames (in addition to the scrape URL's own apex domain) whose links "
            "are permitted to pass the domain safety guard.  Use for universities that "
            "legitimately host course pages on a second domain, e.g. a subdomain CDN or "
            "a partner site.  Entries are matched as suffix (apex-domain level), so "
            "'portal.myuni.edu.au' matches any *.portal.myuni.edu.au link. "
            "Do NOT add a foreign university's domain here — that is a misconfiguration."
        ),
    )
    always_sitemap_supplement: bool = Field(
        default=False,
        description=(
            "Always merge sitemap results with BFS candidates even when BFS exceeded "
            "the fallback threshold.  Needed for JS-rendered SPAs (Torrens, CDU) and "
            "deep-faculty sites where BFS burns its page budget on info pages (AUT, ACU)."
        ),
    )
    block_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns.  Any discovered URL matching one of these is dropped "
            "before extraction.  E.g. '/handbook/handbook-20' blocks old ACU handbooks."
        ),
    )
    year_dedup_mode: str = Field(
        default="none",
        description=(
            "Post-discovery year-based URL deduplication mode.  When not 'none', the "
            "orchestrator groups discovered URLs by their course slug stripped of any "
            "year suffix (e.g. /courses/marketing-msc-2026-27 and "
            "/courses/marketing-msc-2027-28 → same slug group) and keeps ONE URL per "
            "group according to this mode:\n"
            "  'keep_latest'          — keep the highest year (most recent intake)\n"
            "  'keep_preferred_year'  — keep year_dedup_preferred_year; fall back to latest\n"
            "  'keep_current'         — keep the year closest to the calendar year\n"
            "  'none' / 'keep_all'    — disabled (default)\n"
            "URLs with no year in their path are always kept regardless of mode.\n"
            "Courses whose slug exists in only ONE year are always kept.\n"
            "This is the YAML alternative to the UI-recipe course_year block — use "
            "it when you want year dedup without creating a UI recipe."
        ),
    )
    year_dedup_preferred_year: Optional[int] = Field(
        default=None,
        description=(
            "When year_dedup_mode='keep_preferred_year', this is the year to prefer. "
            "If the preferred year is not found for a course slug, falls back to the "
            "latest year available.  Example: 2027"
        ),
    )
    allow_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns (whitelist).  If non-empty, only URLs matching at least "
            "one pattern are kept.  Empty list = allow everything."
        ),
    )
    allow_blocked_listing_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Substring or regex patterns that, when matched against a URL, override "
            "the global ``is_blocked_page`` classifier and treat the URL as a "
            "discovery/listing page: it is crawled for outbound course links but is "
            "NOT added to the course candidate set for extraction. "
            "Use when a university's real listing pages sit under globally-blocked "
            "paths such as /student-life/, /faculties/, /schools/ or /subject-areas/ "
            "that is_blocked_page classifies as campus_page or category_landing_page. "
            "The override is host-scoped: only URLs on the same hostname as the "
            "university's scrape_url are affected. "
            "Emits: [DISCOVER] YAML listing override: bypassed <reason> for <path>"
        ),
    )
    listing_only_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns.  Any discovered URL matching one of these is crawled "
            "for outbound links but never added to the course candidate set for "
            "extraction — even if the page classifier identifies it as a detail page. "
            "Use for pagination, search-result, and category-index URLs that contain "
            "course links but are not courses themselves.  "
            "Complements allow_blocked_listing_patterns (which is only needed when "
            "the URL would also be blocked by is_blocked_page)."
        ),
    )
    course_detail_url_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex whitelist applied AFTER discovery as the final extraction gate. "
            "When non-empty, any candidate URL that does NOT match at least one "
            "pattern is dropped before extraction begins.  "
            "Unlike allow_url_patterns (which restricts BFS crawling), "
            "course_detail_url_patterns lets the BFS crawl listing and category "
            "pages freely while restricting extraction to URLs that match the "
            "expected course-detail URL shape. "
            "Emits: [DISCOVER] YAML course detail filter: dropped <url> / kept <url>. "
            "Example (Bath Spa): ['^https://www\\.bathspa\\.ac\\.uk/courses/"
            "(ug|pg|ify|phd|edd|pgce)-[a-z0-9-]+/?$']"
        ),
    )
    sitemap_url: Optional[str] = Field(
        default=None,
        description="Explicit sitemap URL.  Overrides the auto-detected sitemap.",
    )
    use_wayback: Optional[bool] = Field(
        default=None,
        description=(
            "Tri-state Wayback Machine control. "
            "null/unset (default) = run Wayback only when all other discovery returns 0 links (fallback-only mode). "
            "true = always run Wayback after BFS+browser and merge results (supplemental mode, e.g. QUT). "
            "false = never run Wayback, even as a fallback (use for Cloudflare-blocked sites where archive.org has no useful coverage, e.g. JCU)."
        ),
    )
    skip_browser_discovery: bool = Field(
        default=False,
        description=(
            "Skip the generic Playwright browser-discovery fallback entirely, "
            "even when BFS returns 0 links.  Enable for universities whose live "
            "site is Cloudflare Enterprise-walled at the datacenter IP level so "
            "the browser ALSO receives 403 / 'Just a moment…' — the 90-second "
            "discovery timeout fires with zero results and only wastes time.  "
            "JCU is the canonical example: set skip_browser_discovery=true and "
            "use_wayback=true so the orchestrator goes BFS → 0 → Wayback CDX "
            "instead of BFS → 0 → 90s browser hang → Wayback CDX."
        ),
    )
    bfs_page_budget: Optional[int] = Field(
        default=None,
        description=(
            "Override the default BFS page budget (12 fast / 25 full).  "
            "Raise for sites with many listing pages (e.g. UOW ~62 pages)."
        ),
    )
    max_candidates: Optional[int] = Field(
        default=None,
        description=(
            "Override the default candidate cap (20 fast / 200 full).  Raise "
            "when a sitemap publishes more allow_url_patterns-matching URLs "
            "than the default cap can hold — without this, late-listed "
            "courses get truncated (e.g. CQU sitemap has 199 unique HE "
            "courses but BFS already filled 54 slots, so 53 of the late "
            "alphabet — including cv82 master-of-engineering at index 168 "
            "of the allow-filtered sitemap — were dropped at the 200 cap)."
        ),
    )
    always_browser_discover: bool = Field(
        default=False,
        description=(
            "Run Playwright browser-based discovery in ADDITION to BFS (merging "
            "results) rather than only as a zero-result fallback.  Enable for "
            "Cloudflare-protected sites where BFS succeeds on HTTP-accessible "
            "faculties but silently misses faculties that return 403 for plain "
            "HTTP (e.g. UTAS arts-soc which returns 403 for curl but loads fine "
            "in a real browser).  Host-specific seed URLs in "
            "_HOST_EXTRA_SEEDS (browser_discover_generic.py) are consumed "
            "during this pass to sweep the full faculty A-Z catalogue."
        ),
    )
    seed_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Course-listing page URLs injected into the browser BFS queue with "
            "the highest priority (+300 score bonus) BEFORE any generic nav "
            "crawl begins.  Use when you know the exact listing pages (e.g. "
            "/study/undergraduate/courses, /study/postgraduate/courses) and "
            "want the crawler to visit them first rather than guessing from the "
            "homepage.  Unlike extra_course_urls these are LISTING pages — the "
            "crawler follows links from them to reach individual course pages.  "
            "If enough course links are found from seeds, weak generic BFS pages "
            "are skipped automatically."
        ),
    )
    extra_course_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit course page URLs injected directly into the discovered "
            "link set after all discovery tiers complete.  Bypasses BFS, "
            "sitemap, browser-discover, and Wayback entirely.  Use as a "
            "surgical fallback for known-CRICOS courses that all discovery "
            "tiers consistently miss (e.g. a single URL that lives behind "
            "Cloudflare and is not reachable by any crawler)."
        ),
    )
    use_stealth_browser: bool = Field(
        default=False,
        description=(
            "Route browser discovery AND per-course HTML fetches through the "
            "patchright + Xvfb stealth stack (see stealth_browser.py).  Enable "
            "ONLY for hosts where regular headless Playwright fails the "
            "Cloudflare challenge with HTTP 403 / 'Just a moment...' (verified "
            "for Macquarie/www.mq.edu.au on 2026-05-25).  Stealth adds ~2-4s "
            "per page (Xvfb display + persistent-context launch) so do NOT "
            "enable fleet-wide.  Requires the `patchright` python package and "
            "the `xorg.xvfb` system dependency."
        ),
    )
    must_contain: list[str] = Field(
        default_factory=list,
        description=(
            "Substring whitelist (case-insensitive) applied AFTER block_url_patterns. "
            "If non-empty, any candidate URL that does not contain at least one of "
            "these substrings is dropped. Simpler than allow_url_patterns (no regex) "
            "for the common case where you just want '/courses/' or '/study/' in "
            "the path. Empty list (default) = no-op. Per-uni opt-in via YAML."
        ),
    )
    browser_time_budget_s: int = Field(
        default=90,
        description=(
            "Hard wall-clock time limit (seconds) for the browser-based discovery "
            "pass.  When elapsed time exceeds this value the nav BFS loop stops "
            "and returns whatever course links have been found so far.  Default 90 s. "
            "Raise for sites with many listing pages; lower for sites with fast "
            "JS hydration that rarely need more than a handful of nav visits."
        ),
    )
    browser_early_stop_courses: int = Field(
        default=100,
        description=(
            "Stop following nav pages once this many course links have been found. "
            "Avoids spending minutes on low-value navigation after a catalogue-size "
            "listing page has already been harvested.  Default 100.  Set higher "
            "for large universities with 200+ courses split across many listing pages."
        ),
    )
    block_nav_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Substring patterns (case-insensitive).  Any nav-candidate URL whose "
            "path contains one of these strings is discarded without visiting.  "
            "Extends the global blocklist of low-value paths (apprenticeships, fees, "
            "news, events, accommodation, etc.).  E.g. ['/open-evenings', '/scholarships']."
        ),
    )
    expected_min_courses: Optional[int] = Field(
        default=None,
        description=(
            "Minimum number of courses expected from discovery.  When set, the "
            "orchestrator emits a WARNING if the discovered count falls below this "
            "threshold (e.g. 'Discovery incomplete: expected 100+, found 12').  "
            "Does not block the job — use it as an early-warning signal that "
            "seed_urls or config changes are needed."
        ),
    )
    render_listing_pages: list[str] = Field(
        default_factory=list,
        description=(
            "List of course listing/search page URLs to fetch via Scrape.do headless "
            "Chrome during the discovery phase.  Each page is rendered with JS enabled "
            "and course links matching allow_url_patterns are extracted and added to "
            "the candidate pool.  Use for Angular/React SPA catalogues that paginate "
            "results and are only fully accessible after JavaScript execution — e.g. "
            "a search page that shows page=0..N of results.  Runs AFTER BFS/browser "
            "discovery so it supplements (not replaces) standard tiers.  Each URL "
            "costs one Scrape.do render call (~$0.006).  Example:\n"
            "  render_listing_pages:\n"
            "    - 'https://example.com/courses/search?page=0'\n"
            "    - 'https://example.com/courses/search?page=1'"
        ),
    )
    render_listing_pages_static: bool = Field(
        default=False,
        description=(
            "When True, fetch render_listing_pages with Scrape.do render=False "
            "(static, ~1 credit) instead of render=True (headless Chrome, ~5 "
            "credits).  Use when the SPA's search results are server-side "
            "rendered and the course links are present in the raw HTML without "
            "JS execution — confirmed by comparing render=False vs render=True "
            "link counts.  Saves ~80% of the per-listing-page Scrape.do cost.  "
            "Has no effect unless render_listing_pages is non-empty."
        ),
    )


# ── Extraction sub-configs ───────────────────────────────────────────────────

class FeesConfig(BaseModel):
    central_page: Optional[str] = Field(
        default=None,
        description="URL of the university-wide fee schedule page.",
    )
    fees_pdf_url: Optional[str] = Field(
        default=None,
        description="URL of the university-wide fee schedule PDF.",
    )
    force_central_fee_stage: bool = Field(
        default=False,
        description=(
            "When true, sets has_central_fee_page=True in every course payload "
            "so courses without an individual fee listing still pass the "
            "no_international_fee staging gate and land in the review queue. "
            "Use for universities (e.g. UTAS) that publish fees on a separate "
            "central schedule rather than on each course page."
        ),
    )
    default_currency: str = Field(
        default="AUD",
        description="ISO currency code used when no currency marker is found on the page.",
    )
    credit_points_per_unit: Optional[int] = Field(
        default=None,
        description=(
            "Number of credit points per unit of study.  When set, per-unit fees are "
            "multiplied by this value to derive the full-course fee.  "
            "None = use the extracted credit-point count from the page."
        ),
    )
    course_pdf_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-uni alias map: DB course name (lower-cased) → the course "
            "name as it actually appears in the fee schedule PDF. Used by "
            "match_course_in_pdf_table to augment / replace the DB course's "
            "token bag so fuzzy matching can clear the ≥2-distinctive-token "
            "floor when the PDF row carries a qualifier (e.g. "
            "'Master of Design (Non-Cognate)') the DB course name lacks. "
            "Empty by default — opt-in per university only. "
            "Match is case-insensitive on the key."
        ),
    )
    pdf_row_pattern: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni regex override for the per-course PDF data row. "
            "When None (default), _pick_per_course_amounts uses the shared "
            "_PDF_DATA_ROW_RE pattern (CRICOS + duration + units + 3 dollar "
            "amounts — Torrens / ASA layout). Set this when a university's "
            "fee PDF uses a fundamentally different column layout (e.g. "
            "Federation: CRICOS + 2 dollar amounts, no per-unit / total). "
            "Pattern MUST define a named group ``cricos``; ``per_unit``, "
            "``annual``, ``total`` are looked up via ``groupdict().get()`` "
            "so missing groups are tolerated. Compiled with re.I."
        ),
    )
    pdf_fee_term: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni override for the fee_term emitted by per-course PDF "
            "rows. When None (default), the term is auto-derived from the "
            "annual vs total comparison. Set to ``\"Annual\"`` for PDFs "
            "(e.g. Federation) whose row only carries an annual figure, or "
            "``\"Full Course\"`` for whole-of-programme schedules."
        ),
    )
    fee_url_suffix: Optional[str] = Field(
        default=None,
        description=(
            "Raw string appended to each course URL before the static HTTP "
            "fetch.  Use when the international-student fee view is gated "
            "behind a query flag that cannot be expressed as a key=value pair "
            "(e.g. '?international' on JCU — a valueless query flag that renders "
            "the International Fast Facts panel).  The suffix is appended only "
            "if the URL does not already contain it.  Empty / None = no change."
        ),
    )
    prefer_annual_over_total: bool = Field(
        default=False,
        description=(
            "When the per-course PDF row exposes BOTH an annual figure and "
            "a total-course figure (e.g. Torrens: $31,600 annual / $94,800 "
            "total over 3 years), store the annual value as "
            "``international_fee`` with ``fee_term=\"Annual\"`` instead of "
            "the total / ``\"Full Course\"`` default. The default (False) "
            "preserves the original behaviour of preferring the total "
            "course tuition. Enable per-uni only when annual fees are the "
            "value the operator wants displayed in the catalogue."
        ),
    )
    prefer_year_one_over_total: bool = Field(
        default=False,
        description=(
            "When the per-course PAGE publishes BOTH a 'Year 1 fee' "
            "(e.g. 'Indicative year 1 fee $47,116') and a 'Total course "
            "fee' (e.g. 'Total indicative course fee $141,348') label, "
            "store the year-1 amount as ``international_fee`` with "
            "``fee_term=\"Annual\"`` instead of the total / "
            "``\"Full Course\"`` default. The default (False) preserves "
            "the original page-regex preference for the larger total. "
            "Enable per-uni only when the year-1 figure is the value the "
            "operator wants displayed in the catalogue (e.g. Curtin "
            "publishes 3-year Bachelor degrees with both labels and the "
            "operator-facing column reads ``$X/Annual`` not ``$X/Full "
            "Course``). Independent of ``prefer_annual_over_total`` which "
            "covers the PDF-row path; this knob covers the page-regex "
            "extractor in ``extractors/fee.py``."
        ),
    )
    pdf_overrides_page_regex: bool = Field(
        default=False,
        description=(
            "When True, a successful per-course PDF row match OVERWRITES "
            "any existing payload values for ``international_fee``, "
            "``currency``, ``fee_term`` and ``fee_year`` even if those "
            "slots were already filled by an earlier extractor (page "
            "regex / Gemini prose / sibling cache). The default (False) "
            "keeps the historical course-page-wins behaviour where the "
            "PDF only fills empty slots. Enable for universities whose "
            "per-course HTML pages contain marketing prose with stale or "
            "non-tuition dollar amounts (e.g. Torrens: ``\"will cost an "
            "international student $82,800\"`` for a course whose "
            "official 2026 PDF schedule lists $31,600/Annual). Treats "
            "the central PDF schedule as the authoritative tuition "
            "source for the affected uni."
        ),
    )
    pdf_parser: Optional[str] = Field(
        default=None,
        description=(
            "Per-uni override for the per-course PDF parser strategy. "
            "When None (default), the legacy regex-based ``_pick_per_course_amounts`` "
            "is used over pypdf-extracted text — works well for PDFs whose course "
            "names fit on the same line as the CRICOS+fee data. Set to "
            "``\"columnar\"`` to switch to ``_pick_per_course_amounts_columnar``, "
            "which uses ``pdftotext -layout`` (poppler) and a CRICOS-anchored, "
            "column-position-aware row parser. The columnar parser correctly "
            "handles fee schedules where course titles wrap across 2-3 lines "
            "(e.g. Torrens: ``Diploma of Branded`` / ``Fashion Design`` on "
            "separate lines) — pypdf flattens these and the legacy line-anchored "
            "regex misses them entirely. Requires ``pdftotext`` to be on PATH; "
            "falls back to legacy on subprocess failure."
        ),
    )
    # ── Fee source preference / rejection ────────────────────────────────────
    reject_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Case-insensitive keywords that, when present in the text surrounding "
            "an extracted fee amount, indicate the fee is a *domestic* or "
            "CSP/HECS fee that must NOT be published as an international fee. "
            "Typical values: ``[\"Commonwealth Supported\", \"CSP\", \"HECS\", "
            "\"Domestic\", \"HECS-HELP\", \"Indicative domestic\"]``. "
            "When the winning fee amount's snippet matches ANY keyword, the fee "
            "is discarded and ``international_fee`` is left null (→ DQ warning). "
            "Empty by default — opt-in per university."
        ),
    )
    international_fee_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases that positively confirm a fee evidence snippet is for "
            "international students.  When non-empty: if the evidence snippet "
            "contains at least one of these phrases, the fee is kept even when "
            "a reject_keyword is also present (international marker wins). "
            "This allows the pipeline to correctly handle pages that show both "
            "domestic and international fees — e.g. 'UK fee: £9,250 / "
            "International fee: £15,000' — by keeping the clearly-labelled "
            "international figure.  "
            "Typical values: ['International', 'Overseas', 'Non UK', "
            "'EU and international'].  "
            "Logged as: [FEE_KEEP] kept international fee by YAML evidence"
        ),
    )
    prefer_international: bool = Field(
        default=False,
        description=(
            "When True, if both an international fee AND a domestic/CSP fee were "
            "found on the same page, the pipeline always selects the international "
            "one even if the domestic value was extracted first or has higher "
            "confidence.  Pairs with ``reject_keywords`` to handle universities "
            "that publish domestic fees first and international fees below (e.g. "
            "JCU tab pages, Newcastle right-sidebar, Murdoch toggle). "
            "Default False preserves the existing 'highest confidence wins' logic."
        ),
    )
    allow_bucket_match: bool = Field(
        default=False,
        description=(
            "When True, a degree-level-only (bucket) match from the central fee "
            "page is applied to the course payload when no per-programme name "
            "match is available.  The fee is set with confidence=0.30 and a "
            "scrape warning is appended so reviewers know it is an imprecise "
            "estimate.  Default False (skip bucket matches to avoid wrong data). "
            "Enable for universities that use a standardised per-credit-point "
            "rate for all programmes at the same level — e.g. University of "
            "Canterbury (NZ) — where the bucket fee IS the correct fee."
        ),
    )
    follow_links: list[str] = Field(
        default_factory=list,
        description=(
            "When ``international_fee`` is still blank after all extractors and "
            "after fee rejection, scan the course HTML for ``<a>`` elements whose "
            "link text matches any of these phrases (case-insensitive), fetch the "
            "linked page, and re-run the fee extractor.  Mirrors the "
            "``extraction.english.follow_links`` mechanism.  Any matched fee is "
            "also filtered through ``reject_keywords`` so domestic/CSP amounts on "
            "the linked page are discarded automatically.  "
            "Typical values for JCU: "
            "``['fees and scholarships', 'international student fees', "
            "'fees for your course']``.  Empty by default — opt-in per university."
        ),
    )
    # ── Fee calculation / sanity rules (mirrored into recipe_rules) ──────────
    # These are YAML-side equivalents of the RecipeConfig fee fields so that
    # per-university fee calculation behaviour can be configured in the uni YAML
    # without needing a DB-stored recipe. The orchestrator merges them into the
    # recipe dict at scrape time (YAML wins over DB recipe when both are set).
    fee_calculation_mode: Optional[str] = Field(
        default=None,
        description=(
            "How fee amounts are processed after extraction. "
            "'use_source_value_only' — store as-is (default). "
            "'full_course_to_annual' — divide a Full Course total by duration "
            "to get the per-year figure (e.g. $166,500 / 3yr → $55,500). "
            "For courses < 1 year, the full-course amount is kept as-is. "
            "Set fee_prevent_full_course_rollup: false when using this mode."
        ),
    )
    fee_prevent_full_course_rollup: Optional[bool] = Field(
        default=None,
        description=(
            "Override the recipe-level fee_prevent_full_course_rollup flag. "
            "Set false when fee_calculation_mode='full_course_to_annual' so the "
            "Full Course → Annual conversion can see the original fee_term. "
            "When None (default), the recipe setting or global default applies."
        ),
    )
    max_annual_fee: Optional[int] = Field(
        default=None,
        description=(
            "Discard any extracted Annual fee above this AUD threshold — these "
            "are likely total-course amounts misidentified as per-year by Gemini "
            "(e.g. returning a 3-year $117k total as 'Annual'). "
            "Typical safe cap for Australian universities: 80000."
        ),
    )
    degree_level_defaults: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Fallback international fee (in ``default_currency``) applied per "
            "degree level when no course-specific fee is found after all "
            "extractors run.  Keys are normalised tier names: ``undergraduate``, "
            "``postgraduate``, ``doctorate``.  Values are annual fee amounts as "
            "integers (e.g. ``26400`` for £26,400).  Only fills "
            "``international_fee`` when the slot is still null; a value from any "
            "extractor always wins.  Recorded in evidence with method "
            "``uni_config:fee_default`` and confidence 0.35."
        ),
    )


class BandSpec(BaseModel):
    """Score values for one named English-requirement band (e.g. 'Band 2')."""

    ielts_overall: Optional[float] = Field(
        default=None,
        description="IELTS overall band score for this band level.",
    )
    ielts_each: Optional[float] = Field(
        default=None,
        description=(
            "Minimum score for each IELTS component (listening, reading, "
            "speaking, writing).  Stored in all four ielts_* per-band fields."
        ),
    )
    pte_overall: Optional[int] = Field(
        default=None,
        description="PTE Academic overall score for this band level.",
    )
    toefl_overall: Optional[int] = Field(
        default=None,
        description="TOEFL iBT overall score for this band level.",
    )
    cambridge_overall: Optional[int] = Field(
        default=None,
        description="Cambridge C1 Advanced (CAE) overall score for this band level.",
    )
    duolingo_overall: Optional[int] = Field(
        default=None,
        description=(
            "Duolingo English Test (DET) minimum overall score for this band level. "
            "When set and ielts_overall matches the per-course IELTS score, the "
            "band-mapping pipeline writes this value to duolingo_overall in the "
            "staged course record with method='yaml_band_mapping'."
        ),
    )


class DegreeEnglishDefaults(BaseModel):
    """Per-degree-level English score defaults for universities whose UG and PG
    entry requirements differ (e.g. Waikato: UG IELTS 6.0, PG IELTS 6.5).

    Keys under ``extraction.english.degree_level_defaults`` in the YAML are
    normalised degree tiers:
      - ``undergraduate``  — Bachelors, Honours, Diploma, Certificate (non-graduate)
      - ``postgraduate``   — Masters, Graduate Diploma, Graduate Certificate
      - ``doctorate``      — Doctorate / PhD

    Any tier NOT listed falls back to the flat ``default_ielts`` / ``default_pte``
    / ``default_toefl`` fields on :class:`EnglishConfig`.
    """

    ielts: Optional[float] = Field(default=None, description="IELTS Academic overall score default for this tier.")
    pte: Optional[int] = Field(default=None, description="PTE Academic overall score default for this tier.")
    toefl: Optional[int] = Field(default=None, description="TOEFL iBT overall score default for this tier.")
    duolingo: Optional[int] = Field(default=None, description="Duolingo English Test score default for this tier.")


class EnglishConfig(BaseModel):
    central_page: Optional[str] = Field(
        default=None,
        description="URL of the university-wide English requirements page.",
    )
    requirements_pdf_url: Optional[str] = Field(
        default=None,
        description="URL of the English requirements PDF.",
    )
    course_english_priority: bool = Field(
        default=False,
        description=(
            "When True, the central-page English values are used ONLY as a "
            "fallback — they never overwrite a value already extracted from the "
            "individual course page, regardless of the extraction method. "
            "Default (False) allows the central page to override low-confidence "
            "per-course values (ai_fallback / gemini_primary). "
            "Enable for universities (e.g. JCU) where individual course pages "
            "carry per-course IELTS requirements that differ from the "
            "institution-wide default — the central cached value (e.g. 5.5) "
            "would otherwise silently overwrite per-course values (e.g. 7.0)."
        ),
    )
    trust_vision_ocr: bool = Field(
        default=True,
        description=(
            "Set to false for universities where Gemini vision consistently "
            "hallucinates IELTS/PTE scores from images (e.g. ACAP).  "
            "Disabling falls back to HTML extraction only."
        ),
    )
    skip_vision_when_core_found: bool = Field(
        default=False,
        description=(
            "When True, skip vision OCR entirely for a course page when both "
            "ielts_overall and international_fee are already populated in the "
            "payload before the vision pass runs.  Avoids the Gemini image-scan "
            "overhead (6 candidate images, tier1_skipped checks, API call) on "
            "courses where the two most expensive fields are already known.\n"
            "Safe to enable when IELTS comes from a reliable pre-filled source "
            "(default_ielts, central page, regex) AND fees from degree_level_defaults "
            "or a fee-listing page — vision cannot improve on those sources.\n"
            "Example: UEL — IELTS=6.0 (default_ielts) + fee from degree_level_defaults "
            "are both set before vision; the full image scan adds nothing."
        ),
    )
    trust_tier1_vision_ocr_english: bool = Field(
        default=False,
        description=(
            "Week 1 Prompt 7 Part B — default is now FALSE globally.  Tier-1 "
            "vision OCR (images NOT anchored inside an English / Entry "
            "Requirements DOM section) is treated as 'tier 5' evidence: the "
            "OCR still runs and is logged, but extracted values do not "
            "contribute to row finalisation.  Only tier-0 images (DOM-anchored) "
            "are promoted to tier-4 evidence and may write to the payload.  "
            "Set to true on a per-uni basis only for universities whose "
            "requirements live exclusively inside images that the DOM-section "
            "detector misses (e.g. ASAHE), and only after manual spot-check."
        ),
    )
    default_ielts: Optional[float] = Field(
        default=None,
        description=(
            "Institutional IELTS default to apply when no per-course value is found. "
            "Only set when the university publicly states a single entry standard. "
            "Used as a fallback when degree_level_defaults is set but the course's "
            "degree level does not match any key."
        ),
    )
    default_pte: Optional[int] = Field(
        default=None,
        description="Institutional PTE Academic default (same conditions as default_ielts).",
    )
    default_toefl: Optional[int] = Field(
        default=None,
        description="Institutional TOEFL iBT default.",
    )
    degree_level_defaults: dict[str, DegreeEnglishDefaults] = Field(
        default_factory=dict,
        description=(
            "Per-degree-level English score defaults. Keys are normalised tiers: "
            "``undergraduate``, ``postgraduate``, ``doctorate``. "
            "When set, the tier matching the course's degree_level overrides the "
            "flat default_ielts / default_pte / default_toefl values. "
            "Courses whose degree_level cannot be mapped to a tier fall back to "
            "the flat defaults. Example::\n\n"
            "  degree_level_defaults:\n"
            "    undergraduate: {ielts: 6.0, pte: 50, toefl: 80}\n"
            "    postgraduate:  {ielts: 6.5, pte: 58, toefl: 90}\n"
            "    doctorate:     {ielts: 6.5, pte: 58, toefl: 90}\n"
        ),
    )
    test_blocklist: list[str] = Field(
        default_factory=list,
        description=(
            "Per-uni list of English-test names to drop from extracted results "
            "(case-insensitive match on test name: ielts / pte / toefl / "
            "cambridge / duolingo / kite). Use when a university page "
            "incidentally mentions a test it does not actually accept (e.g. a "
            "marketing page links 'KITE' as a competitor product, polluting "
            "the extracted English requirements). Empty by default."
        ),
    )
    suppress_pte: bool = Field(
        default=False,
        description=(
            "When True, skip PTE score extraction entirely for this university. "
            "Use for universities whose pages mention 'PTE' incidentally "
            "(e.g. 'Year 11 PTE' or competitor product references) causing "
            "false-positive PTE scores to be staged. "
            "YAML alternative to adding the hostname to _NO_PTE_HOSTS in "
            "english_test.py — no code change needed."
        ),
    )
    follow_links: list[str] = Field(
        default_factory=list,
        description=(
            "Link-text patterns (case-insensitive, substring match) that the "
            "scraper should follow when IELTS / PTE / TOEFL scores are missing "
            "after the main course-page extraction.  The scraper looks for "
            "<a> elements whose visible text contains any listed phrase, fetches "
            "the linked page, and re-runs the English extractor against it. "
            "Typical values: ``[\"English Language Requirements\", "
            "\"Minimum English Requirements\", \"Entry Requirements\"]``. "
            "Fetched pages are added to the evidence store so the review panel "
            "shows the source URL. Empty by default — opt-in per university."
        ),
    )
    band_mapping: dict[str, BandSpec] = Field(
        default_factory=dict,
        description=(
            "Named band → score mapping for universities that publish English "
            "requirements as labelled bands rather than direct IELTS scores "
            "(e.g. JCU uses 'Band 1', 'Band 2', 'Band 3'). "
            "Keys are the exact band labels as they appear on the course page "
            "(case-insensitive match at runtime). "
            "Values are :class:`BandSpec` dicts with ``ielts_overall``, "
            "``ielts_each``, ``pte_overall``, ``toefl_overall``. "
            "When a course page contains a recognised band label and IELTS is "
            "still blank after all other extractors have run, the mapped scores "
            "are applied with ``method='yaml_band_mapping'``. "
            "Empty by default — opt-in per university."
        ),
    )
    band_reference_url: Optional[str] = Field(
        default=None,
        description=(
            "URL of the page that defines the band-to-score mapping "
            "(e.g. the university's admissions policy schedule).  Stored as "
            "``source_url`` in the evidence row so reviewers can verify the "
            "mapping without a code search."
        ),
    )


class DomesticOnlyFilter(BaseModel):
    enabled: bool = Field(
        default=True,
        description=(
            "When true, courses detected as domestic-only are dropped during staging. "
            "Default: True (fail-open — the filter ran for every university before the "
            "per-uni YAML gate was added; changing this default to False broke that "
            "behaviour for any uni with a YAML config that does not explicitly opt in). "
            "Set to false ONLY for universities where _DOMESTIC_ONLY_RE produces "
            "confirmed false positives (see: adelaide.yaml, anu.yaml, auckland.yaml)."
        ),
    )
    require_international_evidence: bool = Field(
        default=False,
        description=(
            "When True, a course is rejected unless at least one phrase from "
            "international_markers is found in the scraped page text.  Stronger "
            "than the default domestic-marker reject-on-match rule."
        ),
    )
    international_markers: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases that positively identify a course as open to international "
            "students.  When found in the page, the course is kept even if "
            "domestic_markers are also present.  "
            "Typical values: ['International students', 'International fee', "
            "'international applicants', 'overseas students']."
        ),
    )
    domestic_markers: list[str] = Field(
        default_factory=list,
        description=(
            "Phrases that identify a course as domestic-only.  When found in "
            "the page and enabled=True, the course is rejected. "
            "Typical values: ['UK students', 'Home students', 'Domestic students', "
            "'Commonwealth Supported', 'CSP', 'HECS', 'Student Finance England']."
        ),
    )


class OnlineOnlyFilter(BaseModel):
    enabled: bool = Field(
        default=True,
        description=(
            "When true, courses with all-online delivery are dropped during "
            "staging.  Default true mirrors the historical hard-coded reject "
            "in guards.should_stage_course.  Distance-education-heavy unis "
            "(e.g. CSU, OUA) opt out by setting this to false in their "
            "per-uni YAML."
        ),
    )
    reject_if_mode_is_exactly: list[str] = Field(
        default_factory=list,
        description=(
            "Only reject when study_mode exactly matches one of these values "
            "(case-insensitive).  When non-empty, overrides the default "
            "behaviour of rejecting ANY Online classification.  "
            "E.g. ['Online', 'Distance learning'] to reject fully-online but "
            "keep Blended."
        ),
    )
    keep_if_location_present: bool = Field(
        default=True,
        description=(
            "When True, a course classified as Online is kept if a non-empty "
            "course_location is also present (physical campus confirmed). "
            "Default True prevents over-rejection of blended courses whose "
            "study_mode extractor fired on nav/footer 'online' text."
        ),
    )


class StudyModeConfig(BaseModel):
    """YAML controls for study-mode extraction behaviour."""

    online_only_requires_strong_evidence: bool = Field(
        default=False,
        description=(
            "When True, the bare \\bonline\\b keyword fallback (confidence 0.5) "
            "is suppressed.  Only authoritative structured labels (id-span, "
            "data-attribute, DOM label) or high-specificity phrases like "
            "'fully online', '100% online', 'distance learning' will set "
            "study_mode='Online'.  Enable for universities whose pages contain "
            "'online' in navigation, footer menus, or utility copy (e.g. "
            "'apply online', 'UC Online', 'online reporting') that should NOT "
            "mark a campus course as Online."
        ),
    )
    prefer_location_over_online_keyword: bool = Field(
        default=False,
        description=(
            "When True and a non-empty course_location was extracted on the "
            "same course page, a study_mode='Online' result derived only from "
            "the bare \\bonline\\b keyword fallback (confidence 0.5) is "
            "suppressed in favour of 'On Campus'.  Has no effect on structured "
            "labels or high-specificity patterns.  Use together with "
            "online_only_requires_strong_evidence for maximum noise reduction."
        ),
    )
    strong_online_markers: list[str] = Field(
        default_factory=lambda: [
            "delivered fully online",
            "100% online",
            "online only",
            "distance learning",
            "study online",
            "fully online",
            "entirely online",
            "online delivery only",
        ],
        description=(
            "Phrases that constitute 'strong evidence' of a fully online course. "
            "Used when online_only_requires_strong_evidence=True to distinguish "
            "genuinely online courses from pages that incidentally contain 'online'."
        ),
    )
    ignore_online_keywords_in: list[str] = Field(
        default_factory=list,
        description=(
            "UI section labels whose content should be excluded from online-mode "
            "keyword scanning (e.g. 'navigation', 'footer', 'global menu', "
            "'UC Online', 'online reporting').  Informational only — use "
            "suppress_nav_rule instead to actually suppress the rule extractor."
        ),
    )
    suppress_nav_rule: bool = Field(
        default=False,
        description=(
            "When True, skip the regex/rule-based study-mode classification "
            "entirely for this university.  Use when site-wide navigation or "
            "footer text contains the word 'online' that triggers false Online "
            "detections (e.g. 'apply online', 'UC Online', 'study online' nav "
            "links).  Gemini and location_derived still run and classify "
            "study_mode correctly without the noisy rule result. "
            "YAML alternative to adding the hostname to "
            "_STUDY_MODE_RULE_SUPPRESSED_HOSTS in study_mode.py."
        ),
    )
    suppress_on_campus: bool = Field(
        default=False,
        description=(
            "When True, any 'On Campus' value in study_mode (from any source "
            "— rule, Gemini, or location_derived) is replaced with None after "
            "all extractors have run.  Use for UK universities where "
            "'On Campus' is the delivery location (already captured in "
            "course_location) rather than the study mode; the expected study "
            "mode for those universities is 'Full-time' or 'Part-time', not "
            "a location label.  Prevents the Mode column in the review UI "
            "from duplicating the Course Location column."
        ),
    )


class IntakeConfig(BaseModel):
    start_dates_only: bool = Field(
        default=False,
        description=(
            "When True, restrict intake extraction to a dedicated 'Start dates' "
            "section on the course page.  The extractor anchors on the text "
            "heading 'Start dates' / 'Next start date' / 'Start dates for YYYY' "
            "and scans only the following ``start_dates_window_chars`` characters "
            "for month patterns.  This prevents exam calendars, application "
            "deadlines, and academic-calendar tables from injecting spurious "
            "months into the intake list. "
            "Enable for universities that publish a structured 'Start dates' "
            "section with 'Semester X – DD Month' or 'Starts – DD Month' entries "
            "(e.g. University of Auckland)."
        ),
    )
    start_dates_window_chars: int = Field(
        default=600,
        description=(
            "Number of characters to scan after the 'Start dates' anchor when "
            "start_dates_only=True.  600 chars covers 3-4 semester entries "
            "including multi-year listings.  Raise if the section is unusually "
            "long or the scraper misses late-listed entries."
        ),
    )
    rolling_enrollment_label: Optional[str] = Field(
        default=None,
        description=(
            "When set AND the page text contains any of "
            "rolling_enrollment_markers AND no intake months were "
            "extracted by any other path, write [label] into "
            "intake_months. Used for research degrees (PhD / MPhil) "
            "where the university accepts continuous enrolment "
            "rather than fixed semester intakes — e.g. Curtin's "
            "'Enrolment shall be continuous' wording. Off by default; "
            "opt-in per uni so undergraduate/postgrad pages without "
            "an extracted intake still raise the no_intake_months "
            "warning."
        ),
    )
    rolling_enrollment_markers: list[str] = Field(
        default_factory=list,
        description=(
            "Lower-cased substrings searched in the page body to "
            "trigger the rolling_enrollment_label fallback. Match is "
            "case-insensitive substring (no regex). Empty by default."
        ),
    )
    use_default_when_missing: bool = Field(
        default=False,
        description=(
            "When True and intake_months is still empty after all extractors "
            "(including rolling_enrollment fallback), apply the default_by_level "
            "lookup for the course's degree level.  The synthetic intake row is "
            "marked with default_source_note so reviewers know it was not "
            "extracted from the course page."
        ),
    )
    default_by_level: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "YAML-level default intake month(s) per degree tier, applied when "
            "use_default_when_missing=True and the page has no extractable intake. "
            "Keys are normalised degree tiers: 'undergraduate', 'postgraduate', "
            "'doctorate'.  Values are month name lists. "
            "Example: {undergraduate: [September], postgraduate: [September, January]}"
        ),
    )
    default_source_note: str = Field(
        default="YAML default intake",
        description=(
            "Evidence note written into the intake evidence row when a YAML "
            "default is applied.  Shown in the admin review panel so operators "
            "know the intake was not extracted from the course page."
        ),
    )


class FiltersConfig(BaseModel):
    domestic_only: DomesticOnlyFilter = Field(
        default_factory=DomesticOnlyFilter,
    )
    online_only: OnlineOnlyFilter = Field(
        default_factory=OnlineOnlyFilter,
    )
    reject_parttime_only: bool = Field(
        default=False,
        description=(
            "When True, courses where the course-length cell shows Part-time only "
            "(no Full-time option) are rejected during staging.  International "
            "students on a student visa must typically enrol full-time, so part-time-"
            "only courses are not applicable to them.\n\n"
            "Enable only for universities that clearly label both Part-time and "
            "Full-time per course — e.g. WLV, where 'Course length: Part-time "
            "(1 year)' vs 'Part-time (8 years), Full-time (4 years)' are "
            "unambiguously distinguishable.  Off by default so no existing "
            "university is affected."
        ),
    )
    broken_cms_retry_strip_query: bool = Field(
        default=False,
        description=(
            "When the global broken-CMS short-circuit fires AND the URL "
            "carries a query string, retry the bare URL once before "
            "giving up. Use only when the uni serves a real, fully-"
            "international-eligible page at the bare URL but a "
            "YAML-driven query rewrite (e.g. CQU "
            "?audience=INTERNATIONAL on most Bachelors and Masters) "
            "returns a 200-OK 137-char branded error template. Off by "
            "default — preserves the strict skip-on-broken-CMS "
            "behaviour for every other uni."
        ),
    )


class LocationCleaningConfig(BaseModel):
    strip_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns applied to raw location strings before parsing. "
            "Each matching fragment is stripped.  Order matters — patterns are "
            "applied left to right.  "
            "E.g. ACAP: [r'\\^\\s*\\^.*$'] strips '^ ^Available in Perth' cruft."
        ),
    )
    allowed_values: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of valid campus/location strings (case-insensitive substring "
            "match against the extracted value).  When non-empty, only values that "
            "contain at least one entry are kept; all others are cleared to blank.  "
            "Use for universities where the extractor may pick up person names or "
            "testimonial copy from other page sections.  "
            "E.g. BCU: ['City Centre', 'City South', 'Margaret Street', 'Online']."
        ),
    )


class DurationCleaningConfig(BaseModel):
    split_on_slash: bool = Field(
        default=False,
        description=(
            "Split raw duration strings on '/' before parsing.  Needed for KBS/Torrens "
            "compound patterns like 'X years / Y subjects / Z trimesters'."
        ),
    )
    prefer_fulltime: bool = Field(
        default=False,
        description=(
            "When True, demote any label-matched (Pattern-0) duration sentence where "
            "'part-time' appears BEFORE the matched number so that the Pattern-1 "
            "('full-time N units') match wins instead.  Use for universities whose "
            "course-length cell lists Part-time first, then Full-time — e.g. "
            "'Part-time (8 years), Full-time (4 years)' — so the per-page extractor "
            "picks the correct full-time value instead of the first (part-time) number."
        ),
    )
    reject_sentence_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns (case-insensitive) applied to each candidate sentence "
            "before it enters the duration tournament.  Any sentence matching one "
            "of these patterns is skipped entirely — it contributes no candidates. "
            "Also applied to the raw value text of any structural DOM match "
            "(strong/dt/th) so that a labeled 'Duration' cell whose value reads "
            "'up to N years' (a max-completion-time phrase) is not returned early "
            "as the program duration.\n\n"
            "Use when a university's pages expose maximum-completion-time or "
            "candidature-cap text using phrasing that the global "
            "_DURATION_RESEARCH_CAP_RE and _DURATION_ANTI_CONTEXT filters don't "
            "catch — e.g. 'up to 10 years to complete' or 'up to 35 months'.\n\n"
            "Patterns are compiled with re.IGNORECASE.  Empty by default — no "
            "sentences are rejected.  Do NOT replicate global anti-context patterns "
            "here; only add patterns that are specific to this university's CMS "
            "phrasing and would create false positives on other universities."
        ),
    )


class FieldOverride(BaseModel):
    """Hard-set a payload field when a course URL matches a regex.

    Use as a surgical last-mile fix when an extractor consistently writes
    a wrong value for a small set of pages and per-uni tweaks to the
    extractor are not justified. E.g. a single department page whose
    ``course_location`` always comes out wrong.
    """

    url_regex: str = Field(
        description="Regex applied (case-insensitive) to the course URL. Must match for the override to fire.",
    )
    field: str = Field(
        description="Payload key to overwrite (e.g. 'course_location', 'study_mode', 'duration_term').",
    )
    value: Optional[str] = Field(
        default=None,
        description="Value to write. Use null/empty to clear the field instead.",
    )


class UrlRewrite(BaseModel):
    """Per-host URL rewriting applied right before each course page is fetched.

    Many universities gate the international-student view (fees, IELTS,
    intakes, campus list) behind a query flag like ``?international=true``
    or ``?audience=INTERNATIONAL``. Without that flag the page renders the
    domestic CSP view and every fee comes out at the domestic price.

    Each entry matches a request when ``host`` matches the URL netloc
    (case-insensitive; bare and ``www.`` variants both match) AND, if
    ``path_contains`` is set, the URL path includes that substring. When
    a rewrite fires it parses ``append_query`` as a query string and adds
    each key only if NOT already present (idempotent).
    """

    host: str = Field(
        description=(
            "Hostname this rewrite applies to (e.g. 'www.cqu.edu.au'). "
            "The bare apex form (cqu.edu.au) is also matched automatically."
        ),
    )
    path_contains: Optional[str] = Field(
        default=None,
        description=(
            "Optional substring the URL path must contain for the rewrite to "
            "fire (e.g. '/courses/'). Useful when a uni mixes course and "
            "non-course pages on the same host. None = no path filter."
        ),
    )
    append_query: str = Field(
        description=(
            "Query string to merge into the URL (e.g. 'audience=INTERNATIONAL' "
            "or 'students=international&year=2026'). Each key is only added "
            "if it is not already present in the URL."
        ),
    )


class CourseNameConfig(BaseModel):
    strip_title_suffixes: list[str] = Field(
        default_factory=list,
        description=(
            "Literal substrings stripped from the raw H1/title text before the "
            "standard suffix-detection regex runs.  Use when a university's CMS "
            "appends a fixed provider string that the generic regex cannot match "
            "automatically.  Matching is case-sensitive and checked from the END "
            "of the raw text.  "
            "Example (UWA): [' : the University of Western Australia']"
        ),
    )
    prefer_title_over_h1: bool = Field(
        default=False,
        description=(
            "When True, prefer the cleaned page <title> over the <h1> as the "
            "primary course-name candidate.  Use for universities (e.g. Bath Spa) "
            "whose CMS puts only the bare subject name in H1 ('Business and "
            "Management') while the full degree title ('Business and Management "
            "degree - BA (Hons)') appears only in the page <title>.  Combine with "
            "strip_title_suffixes to remove the provider suffix from the title."
        ),
    )
    h1_css_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector used to find the course-name H1 instead of a bare "
            "soup.find('h1').  Required when the page has multiple H1 elements "
            "and the first one is NOT the course title (e.g. Lancaster whose "
            "cookie-consent modal injects <h1>Our use of cookies</h1> before "
            "the main content area).  "
            "Example (Lancaster): 'div.course-title h1' — targets the H1 "
            "inside the .course-title div which always holds the real degree name.  "
            "Falls back to soup.find('h1') if the selector matches nothing."
        ),
    )
    university_aliases: list[str] = Field(
        default_factory=list,
        description=(
            "University name aliases that must be stripped from scraped course "
            "titles.  The full university name from the DB is always tried; "
            "these aliases add extra patterns (abbreviations, brand variants). "
            "Example (UEL): ['University of East London', 'UEL']. "
            "All aliases are matched case-insensitively against the separators "
            "' | ', ' - ', ' – ', ' — ', ' at ', ' @ ', ' : ' at the END of "
            "the course name."
        ),
    )
    remove_separator_suffix: list[str] = Field(
        default_factory=list,
        description=(
            "Additional separator strings (e.g. ['|', ' - ', ' at ']) checked "
            "before the default separator set.  Rarely needed — the default set "
            "already covers pipe, dash, en/em-dash, colon, 'at', '@', bullet. "
            "Provided for edge-case CMS variants."
        ),
    )


class TextCleaningConfig(BaseModel):
    location: LocationCleaningConfig = Field(
        default_factory=LocationCleaningConfig,
    )
    duration: DurationCleaningConfig = Field(
        default_factory=DurationCleaningConfig,
    )
    global_substring_blocklist: list[str] = Field(
        default_factory=list,
        description=(
            "Substrings (case-insensitive) stripped from EVERY string field on "
            "the staged payload immediately before DB write. Useful for stock "
            "boilerplate that pollutes multiple fields (e.g. 'Apply Now', "
            "'Find out more', a stray cookie banner fragment). Whitespace is "
            "collapsed after stripping. Empty by default."
        ),
    )
    field_overrides: list[FieldOverride] = Field(
        default_factory=list,
        description=(
            "List of per-URL hard overrides. Each entry has url_regex, field, "
            "and value. When the course URL matches the regex, the named "
            "payload field is set to the supplied value (overwriting whatever "
            "extractors wrote). Apply sparingly — extractor / YAML fixes are "
            "preferred. Empty by default."
        ),
    )


class StagingConfig(BaseModel):
    reject_if_missing: list[str] = Field(
        default_factory=lambda: ["course_name"],
        description=(
            "Fields that must be non-null/non-empty for a staged course to be accepted. "
            "A course missing any of these fields is rejected at the staging gate."
        ),
    )
    skip_degree_qualifier_check: bool = Field(
        default=False,
        description=(
            "When True, the name-based degree-qualifier check (category_landing_page "
            "rejection) is skipped.  Use for universities (e.g. ARU/Writtle) whose "
            "SPA pages populate course_name from JSON metadata without the degree "
            "prefix in the name — the URL-based block_url_patterns already filters "
            "non-course pages so the name check is redundant."
        ),
    )
    skip_duplicate_fee_check: bool = Field(
        default=False,
        description=(
            "When True, the cross-course duplicate_fee_detected data-quality check "
            "is suppressed.  Use when the university publishes a genuine flat-rate "
            "international fee (same amount for all or most courses) that would "
            "otherwise trigger a false-positive CRITICAL selector-scope warning "
            "(e.g. UEL charges £16,020 for every undergraduate course)."
        ),
    )
    require_international_fee: bool = Field(
        default=True,
        description=(
            "When False, courses without an international_fee are staged for human "
            "review instead of being auto-rejected (no_international_fee).  Use when "
            "the fee is reliably published on the institution's website but the "
            "extractor cannot reach it (e.g. fee behind a JS tab that stealth browser "
            "hasn't rendered yet)."
        ),
    )
    reject_slug_name_with_no_data: bool = Field(
        default=False,
        description=(
            "When True, reject courses whose name is clearly slug-derived (starts with "
            "a URL-level prefix such as 'Ug ', 'Pg ', 'Ify ', 'Pgce ', 'Phd ', 'Edd ') "
            "AND have zero meaningful data (international_fee, study_mode, duration, "
            "and degree_level are all null).  This catches courses whose page silently "
            "redirected to a '404 / course-not-found' page during extraction — the "
            "extractor found no title/H1, the name fell back to the URL slug, and "
            "nothing else was populated.  Courses that have even one data field "
            "(e.g. PGCE courses with a fee) are kept."
        ),
    )


# ── Top-level ExtractionConfig ───────────────────────────────────────────────

class ExtractionConfig(BaseModel):
    """Per-university only.  Must NOT be replayed against unknown unis in Tier-3."""

    fees: FeesConfig = Field(default_factory=FeesConfig)
    english: EnglishConfig = Field(default_factory=EnglishConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    study_mode: StudyModeConfig = Field(default_factory=StudyModeConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    text_cleaning: TextCleaningConfig = Field(default_factory=TextCleaningConfig)
    course_name: CourseNameConfig = Field(default_factory=CourseNameConfig)
    staging: StagingConfig = Field(default_factory=StagingConfig)
    url_rewrites: list[UrlRewrite] = Field(
        default_factory=list,
        description=(
            "Per-host query-param rewrites applied to every course URL "
            "before fetch. Used to switch a site into its international-"
            "student view (e.g. ?audience=INTERNATIONAL on CQU). Empty "
            "by default."
        ),
    )
    default_course_location: Optional[str] = Field(
        default=None,
        description=(
            "Fallback value written to course_location when every extractor "
            "(regex, browser, Gemini, PDF) returns an empty string.  Useful "
            "for universities whose Cloudflare-protected pages occasionally "
            "deliver partial HTML that omits the Location panel — UTAS is the "
            "canonical example.  Set to the primary campus city/name (e.g. "
            "'Hobart') so that the online-only staging guard does not reject "
            "legitimate on-campus courses whose location text was simply "
            "missing from the crawled HTML."
        ),
    )
    campus_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "When non-empty, any staged course whose course_location does NOT "
            "contain at least one of these strings (case-insensitive) is flagged "
            "as a critical data-quality failure. Useful for universities with a "
            "fixed set of known campuses (e.g. JCU: Townsville, Cairns, Brisbane, "
            "Singapore). Leave empty (default) to disable allowlist checking."
        ),
    )
    prefer_blended_over_on_campus: bool = Field(
        default=False,
        description=(
            "When True, and the study-mode rule extractor emitted 'Online' at low "
            "confidence (≤0.50) AND a physical campus was also found in "
            "course_location, the pipeline sets study_mode='Blended' instead of "
            "the default 'On Campus'.  Use for universities that genuinely offer "
            "courses both online AND on-campus (e.g. JCU: 'Online: Jan, May' AND "
            "'Townsville: Jan, May' on the same page).  Default False preserves "
            "the original Bug-1 fix behaviour (low-conf online + campus → On Campus)."
        ),
    )
    skip_browser_rescue: bool = Field(
        default=False,
        description=(
            "When True, skip the sparse-static rescue browser refetch even "
            "when both international_fee and duration are blank after the "
            "Gemini-primary pass.  Enable for universities whose live site is "
            "protected by Cloudflare Enterprise — Playwright is also IP-blocked "
            "and the browser rescue wastes 10-30 s per course returning "
            "rendered=0B.  Notre Dame is the canonical example: the rescue "
            "adds ~30 s latency per course with zero benefit, inflating a "
            "210-course job from ~6 minutes to 106 minutes."
        ),
    )
    skip_per_course_browser: bool = Field(
        default=False,
        description=(
            "When True, skip ALL per-course browser fetches for this university "
            "(equivalent to adding the host to _SKIP_BROWSER_HOSTS in code). "
            "Use when: (a) the static HTML already contains all required fields "
            "and the browser always times out wasting 60s × n_courses, or (b) "
            "Cloudflare WAF blocks the headless browser just as it blocks plain "
            "HTTP — rendered=0B every time. "
            "YAML alternative to engineering changes in per_course_browser.py. "
            "Example: extraction.skip_per_course_browser: true"
        ),
    )
    browser_wait_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Override the Playwright wait_until strategy for this university. "
            "Accepted values: 'networkidle' | 'domcontentloaded'. "
            "YAML alternative to adding the host to _NETWORKIDLE_HOSTS or "
            "_DCL_SETTLE_MS_OVERRIDES in per_course_browser.py code. "
            "Use 'networkidle' when critical data (fees, IELTS, intakes) is "
            "loaded via XHR after the initial render and domcontentloaded fires "
            "before the content is present. "
            "Use 'domcontentloaded' (plus browser_dcl_settle_ms if needed) when "
            "networkidle never fires because persistent analytics / chat widgets "
            "keep the network permanently busy (e.g. UWA, some Sitecore sites). "
            "When None (default), the global host-list logic in "
            "per_course_browser.py selects the strategy."
        ),
    )
    browser_dcl_settle_ms: Optional[int] = Field(
        default=None,
        description=(
            "Extra settle delay in milliseconds after domcontentloaded fires, "
            "before the HTML is captured. Only used when browser_wait_strategy "
            "is 'domcontentloaded' (or the host falls into that bucket via the "
            "global host lists). YAML alternative to _DCL_SETTLE_MS_OVERRIDES. "
            "Default (when None): 1500 ms (the global _DEFAULT_SETTLE_MS). "
            "Set to 4000–6000 for React/Sitecore SPAs that need a few extra "
            "seconds for JS hydration after DCL fires. "
            "Example: extraction.browser_dcl_settle_ms: 5000"
        ),
    )
    max_parallel_fetch: Optional[int] = Field(
        default=None,
        description=(
            "Cap the asyncio.Semaphore concurrency used during the extraction "
            "gather phase for this university.  When None (default), the global "
            "_MAX_PARALLEL_FETCH value (currently 4) is used.  Set to 2 or 1 "
            "for Cloudflare-heavy sites (e.g. UTAS) where 4 simultaneous "
            "browser sessions trigger aggressive 429 rate-limiting, causing "
            "10-minute stalls that multiply across every URL batch.  Lower "
            "values reduce throughput per-batch but eliminate the 600s "
            "cooldown penalties, typically making the overall run faster."
        ),
    )
    # ── YAML-driven browser interaction actions ───────────────────────────────
    # Allows clients to configure interactive browser behaviour without code changes.
    # Executed in order after the initial page load and settle delay, before
    # the HTML is captured for extraction.
    #
    # Supported action keys (all optional, only one per dict entry):
    #   click_text:  str  — click the first visible element whose text
    #                        matches (case-insensitive, partial OK if unique).
    #                        "International" uses the smart toggle JS.
    #   click_css:   str  — click the first element matching this CSS selector.
    #   wait_for:   dict  — wait for a condition; supported sub-keys:
    #                         text: str    — wait until page contains this text
    #                         selector: str — wait until selector is visible
    #   expand_text: str  — click accordion / <details> / "Show more" whose
    #                        trigger text contains this phrase.
    #   scroll_to:   str  — scroll to a CSS selector or anchor (e.g. "#fees").
    #
    # Example (JCU international fee tab):
    #   extraction:
    #     actions:
    #       - click_text: "International"
    #       - wait_for:
    #           text: "International Students"
    #
    # After clicking, the scraper waits for networkidle (up to 5s) + 1.2s settle.
    scrape_do_render: bool = Field(
        default=False,
        description=(
            "When True, fetch_html() detects React/SPA shells (200 response "
            "but near-zero visible text) and retries via the Scrape.do paid "
            "proxy with render=true (headless Chrome rendering).  Also enables "
            "Scrape.do render as tier-5 after the free tier-4 static fetch "
            "when Cloudflare blocks all free options.  Requires SCRAPE_DO_TOKEN "
            "env var.  Use for Cloudflare-Enterprise sites whose httpx/cffi "
            "responses are empty SPA shells (Canterbury, Sunderland, etc.)."
        ),
    )
    scrape_do_skip_fallbacks: bool = Field(
        default=False,
        description=(
            "When True (and scrape_do_render is also True), fetch_html() skips "
            "the httpx and curl_cffi fallback attempts entirely and goes straight "
            "to Scrape.do headless render for every course page.  Use for Angular/"
            "React SPA sites behind Cloudflare WAF where direct HTTP always returns "
            "a challenge page (e.g. UWL) — skipping the two doomed attempts saves "
            "~1-2s per course which compounds to several minutes across a full run. "
            "Has no effect unless SCRAPE_DO_TOKEN is set and scrape_do_render is True."
        ),
    )
    scrape_do_static: bool = Field(
        default=False,
        description=(
            "When True, fetch_html() skips httpx/cffi entirely and routes each "
            "per-course fetch through Scrape.do's residential proxy in static "
            "(non-rendering) mode (~$0.0005/call).  Use for SSR universities "
            "whose pages return geo-targeted content for US IPs even though the "
            "HTTP response is 200 OK — the residential proxy provides a non-US IP "
            "so the actual course page is returned instead of a country-welcome "
            "overlay.  Lancaster is the canonical case: fetching from a US IP "
            "returns 'We welcome applications from the United States of America' "
            "with 'Our Use of Cookies' as the extracted course name.  Does NOT "
            "execute JavaScript — use scrape_do_render for JS-rendered pages."
        ),
    )
    scrape_do_geo: str = Field(
        default="",
        description=(
            "ISO 3166-1 alpha-2 country code passed to Scrape.do as the "
            "'geoCode' parameter, pinning the exit-node IP to that country. "
            "Applies to both scrape_do_static and scrape_do_render calls. "
            "Example: 'NP' (Nepal) makes the university site see a Nepalese "
            "visitor — useful when fee/requirement pages show international "
            "student content only for certain origin countries. "
            "Leave empty to let Scrape.do choose the nearest residential IP."
        ),
    )
    skip_ai_when_text_empty: bool = Field(
        default=False,
        description=(
            "When True, skip ALL AI calls (Gemini primary + AI fallback) for a "
            "course page whose extracted visible text_len is zero after the "
            "initial static/scrape.do fetch.  An empty-text page means the SPA "
            "shell was fetched but JS hydration did not produce visible content — "
            "sending it to Gemini wastes tokens and returns nothing useful. "
            "With this flag set, the pipeline skips Gemini/AI and allows the "
            "per-course browser (Playwright) to attempt a render instead "
            "(if skip_per_course_browser is false). "
            "Enable for React-SPA sites that already use scrape_do_render: true "
            "but still receive 0-text shells for some pages (e.g. Canterbury CC). "
            "Example: extraction.skip_ai_when_text_empty: true"
        ),
    )
    retry_on_cloudflare: bool = Field(
        default=False,
        description=(
            "When True, the per-course browser fetch is retried once when "
            "the first attempt returns None (rendered=0B / Cloudflare block). "
            "Use for CF-protected sites where the first browser request sets "
            "cf_clearance and the second request passes through. "
            "YAML alternative to adding the hostname to _BROWSER_RETRY_HOSTS "
            "in per_course_browser.py — no code change needed."
        ),
    )
    force_browser: bool = Field(
        default=False,
        description=(
            "When True, always run the Playwright browser for every course page "
            "even when static HTML appears to have populated fields like english "
            "test scores.  Use for sites whose static HTML contains misleading "
            "site-wide IELTS/English statements — the browser's course-specific "
            "result overrides the generic static value. "
            "YAML alternative to _FORCE_BROWSER_HOSTS in per_course_browser.py."
        ),
    )
    skip_initial_http_fetch: bool = Field(
        default=False,
        description=(
            "When True, skip the initial httpx HTTP fetch entirely and go straight "
            "to the Playwright browser for every course page.  Use for universities "
            "whose pages are ALWAYS Cloudflare/bot-protected — every HTTP attempt "
            "returns a 403/challenge that wastes a round-trip before the browser "
            "fallback fires.  Pair with force_browser=true and max_parallel_fetch "
            "to eliminate the wasted HTTP overhead and cut per-course latency.\n"
            "Example: UEL — 100% Cloudflare-protected, HTTP always blocked."
        ),
    )
    needs_international_toggle: bool = Field(
        default=False,
        description=(
            "When True, the browser clicks an 'International' student-type "
            "toggle on each course page before scraping.  Use for universities "
            "whose pages default to Domestic view (domestic fees, shorter "
            "duration) and require a toggle click to reveal international fees, "
            "IELTS requirements, and intakes. "
            "YAML alternative to _INTERNATIONAL_TOGGLE_HOSTS in per_course_browser.py."
        ),
    )
    actions: list[dict] = Field(
        default_factory=list,
        description="Ordered list of browser interaction steps to execute after page load.",
    )
    auto_interact_all: bool = Field(
        default=False,
        description=(
            "When True, after the Playwright browser loads the page and runs any "
            "YAML-configured actions, a generic pass automatically clicks every "
            "collapsed accordion, closed <details> element, and "
            "[aria-expanded='false'] button visible on the page. "
            "Use for pages where IELTS requirements, fees, or intake dates are "
            "hidden inside expandable sections that the scraper cannot see in the "
            "static HTML.  Runs before `html = page.content()` so the saved "
            "snapshot and all extractors see the fully-expanded DOM. "
            "Enable via the UI Quick Settings panel or YAML: "
            "extraction.auto_interact_all: true"
        ),
    )


# ── Merged UniConfig ─────────────────────────────────────────────────────────

class UniConfig(BaseModel):
    """Fully-merged per-university configuration (defaults → per-uni YAML → DB overrides).

    Instances are created by ``loader.load_uni_config`` and stored in the
    ``current_uni_config`` contextvar for the duration of a scrape job.
    Extractors that have been migrated to config-driven behaviour call
    ``get_uni_config()`` to read it.
    """

    slug: str = Field(description="Short identifier derived from hostname, e.g. 'acu', 'aut'.")
    name: str = Field(description="Human-readable university name.")
    university_id: Optional[int] = Field(default=None)
    base_url: str = Field(description="Origin URL, e.g. 'https://www.acu.edu.au'.")
    scrape_url: str = Field(description="Discovery entry-point URL.")

    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)

    def for_tier3_replay(self) -> "UniConfig":
        """Return a config containing ONLY the discovery section.

        Safe to replay against unknown universities in Tier-3 playbook matching.

        The returned config has:
          - ``discovery``: copied from self (URL filters, sitemap options, etc.)
          - ``extraction``: bare defaults (no fees config, no english config, no
            filters, no text_cleaning overrides)

        Why this matters
        ----------------
        ACAP's ``filters.domestic_only.enabled = true`` was tuned specifically
        for ACAP's page structure, where "domestic students only" appears in the
        main content.  On an unknown university's page that same text might live
        in a sidebar, footer, or not exist at all — silently rejecting all courses
        and making the result look like a discovery failure.

        The ``for_tier3_replay()`` boundary makes this exclusion explicit in code
        rather than relying on developer discipline.  Any code that builds a Tier-3
        temporary playbook config MUST call this method — not use the raw UniConfig.

        The ``filters`` section is under ``extraction:`` (applied at extraction time
        for all currently implemented filters) and is excluded here along with the
        rest of ``extraction:``.  This choice is documented explicitly so it survives
        refactoring: if ``filters`` is ever moved to a third top-level section, that
        section must also be excluded from the Tier-3 replay payload.
        """
        return UniConfig(
            slug=self.slug,
            name=self.name,
            university_id=self.university_id,
            base_url=self.base_url,
            scrape_url=self.scrape_url,
            discovery=self.discovery.model_copy(),
            # extraction intentionally omitted — ExtractionConfig() defaults apply.
        )


# ── Advanced Scraping Recipe ─────────────────────────────────────────────────
# Stored in scrape_config.recipe (JSONB).  Covers all 17 portal-configurable
# features so operators can convert existing manual spiders into DB-stored
# recipes without touching YAML or Python.

class RecipeApiConfig(BaseModel):
    """JSON / REST API endpoint that serves the full course catalogue."""
    endpoint: str = Field(default="", description="Full URL of the JSON feed.")
    method: str = Field(default="GET", description="HTTP method (GET or POST).")
    headers: dict = Field(default_factory=dict, description="Extra request headers.")
    root_path: Optional[str] = Field(
        default=None,
        description=(
            "Dot-separated path into the JSON response to reach the course array. "
            "E.g. 'courses', 'data.items', 'results'."
        ),
    )
    course_url_template: Optional[str] = Field(
        default=None,
        description=(
            "Python str.format_map template built from JSON field values. "
            "E.g. 'https://courses.hud.ac.uk/2025-26/{study_mode}/{study_level}/{urltitle}'"
        ),
    )
    fields: dict = Field(
        default_factory=dict,
        description=(
            "Mapping of standard scraper field → JSON key name. "
            "Standard keys: course_name, degree_level, study_mode_raw, "
            "full_time, part_time, url_slug, duration, campus, description."
        ),
    )
    pagination: Optional[dict] = Field(
        default=None,
        description=(
            "Optional pagination config: {type: 'offset', page_param: 'page', "
            "size_param: 'limit', page_size: 100, max_pages: 50}."
        ),
    )


class RecipeFieldSelector(BaseModel):
    """Extraction rule for a single field on the course detail page."""
    xpath: Optional[str] = Field(default=None, description="XPath expression.")
    css: Optional[str] = Field(default=None, description="CSS selector.")
    regex: Optional[str] = Field(default=None, description="Regex (first capture group used).")
    attribute: Optional[str] = Field(
        default=None,
        description="HTML attribute to extract (default: text content).",
    )
    transform: list = Field(
        default_factory=list,
        description="List of transforms: 'strip', 'lower', 'upper', {'regex_replace': {pattern, replacement}}.",
    )


class RecipeFeeRule(BaseModel):
    """Fee band: apply this amount when course name contains one of the keywords."""
    amount: float = Field(description="Annual fee in fee_currency.")
    keywords: list[str] = Field(default_factory=list, description="Case-insensitive match substrings.")


class RecipeIeltsConfig(BaseModel):
    """Regex rules for extracting IELTS / English scores from page text."""
    overall_regex: Optional[str] = Field(
        default=None,
        description="Regex with one capture group for the overall score. E.g. r'(\\d\\.?\\d*)\\s*overall'.",
    )
    band_regex: Optional[str] = Field(
        default=None,
        description="Regex for per-band / each-component minimum.",
    )
    source_xpath: Optional[str] = Field(
        default=None,
        description="XPath to limit the text region searched for IELTS scores.",
    )


class RecipeIntakeConfig(BaseModel):
    """Rules for extracting intake / start-date months."""
    xpath: Optional[str] = Field(default=None, description="XPath for start-date text.")
    regex: Optional[str] = Field(default=None, description="Regex with month-name capture group.")
    month_map: dict = Field(
        default_factory=dict,
        description="Map raw text → canonical month name. E.g. {'Autumn': 'March', 'Spring': 'July'}.",
    )


class RecipeCampusConfig(BaseModel):
    """Campus / location normalization rules."""
    default_city: Optional[str] = Field(default=None, description="Used when no campus is found.")
    valid_campuses: list[str] = Field(
        default_factory=list,
        description="Allowlist — courses at other campuses are dropped when non-empty.",
    )
    online_only_reject: bool = Field(
        default=False,
        description="Drop courses whose only delivery is Online/Distance.",
    )


class RecipeConfig(BaseModel):
    """Full advanced scraping recipe — admin-configurable without code.

    Stored at scrape_config.recipe (JSONB).  The orchestrator reads this
    block before all other discovery tiers and routes accordingly.

    Covers the 17 features requested:
      1  seed_urls                  8  regex extraction rules
      2  api (JSON endpoint)        9  fee mapping rules
      3  api.root_path             10  ielts / english rules
      4  api.course_url_template   11  intake rules
      5  api.fields                12  campus / location rules
      6  selectors (detail page)   13  online_only_reject
      7  XPath / CSS selectors     14  must_contain / block_url_patterns
                                   15  expected_min_courses
                                   16  fallback_strategy
                                   17  minimum_completeness
    """

    # ── 1. Discovery strategy ──
    discovery_strategy: str = Field(
        default="auto",
        description="auto | json_api | bfs | browser | sitemap",
    )
    seed_urls: list[str] = Field(
        default_factory=list,
        description="Course-listing page URLs fed to browser BFS with +300 priority.",
    )
    extra_course_urls: list[str] = Field(
        default_factory=list,
        description="Individual course URLs injected directly after discovery.",
    )
    expected_min_courses: Optional[int] = Field(
        default=None, description="Alert threshold — raise a warning if fewer courses found."
    )
    expected_max_courses: Optional[int] = Field(
        default=None, description="Sanity cap — flag if more courses found than expected."
    )
    fallback_strategy: str = Field(
        default="bfs",
        description="Strategy used when json_api returns 0 results: bfs | browser | sitemap | none.",
    )

    # ── 2-5. JSON API endpoint ──
    api: Optional[RecipeApiConfig] = Field(
        default=None, description="JSON API config (used when discovery_strategy=json_api).",
    )

    # ── 14. URL filters ──
    must_contain: list[str] = Field(
        default_factory=list,
        description="Drop any discovered URL that does NOT contain one of these substrings.",
    )
    block_url_patterns: list[str] = Field(
        default_factory=list,
        description="Regex patterns — drop any discovered URL matching any of these.",
    )

    # ── 6-8. Course detail page selectors ──
    fetch_detail_page: bool = Field(
        default=True,
        description="Fetch the individual course page for selector-based extraction.",
    )
    selectors: dict = Field(
        default_factory=dict,
        description=(
            "Per-field extraction rules. Keys are standard field names "
            "(course_name, degree_level, duration, intake_month, description, "
            "international_fee, ielts_overall, study_mode, course_location, "
            "entry_requirements, academic_level, other_requirement). "
            "Values are RecipeFieldSelector dicts."
        ),
    )

    # ── 10. IELTS / English ──
    ielts: Optional[RecipeIeltsConfig] = Field(default=None)

    # ── 11. Intake ──
    intake: Optional[RecipeIntakeConfig] = Field(default=None)

    # ── 9. Fee rules ──
    fee_currency: str = Field(default="AUD", description="ISO currency code (GBP, AUD, NZD…).")
    fee_year: Optional[int] = Field(default=None, description="Academic year for fee data.")
    fee_rules_undergraduate: list[RecipeFeeRule] = Field(
        default_factory=list,
        description="UG fee bands, checked most-specific first.",
    )
    fee_rules_postgraduate: list[RecipeFeeRule] = Field(
        default_factory=list,
        description="PG fee bands, checked most-specific first.",
    )

    # ── 12-13. Campus ──
    campus: Optional[RecipeCampusConfig] = Field(default=None)

    # ── 15-17. Quality ──
    minimum_completeness: int = Field(
        default=85, description="Auto-publish threshold (0-100).",
    )
    required_fields: list[str] = Field(
        default_factory=list,
        description="Fields that must be non-empty for a course to be accepted.",
    )

    # ── 18. Fee Rule Engine ──────────────────────────────────────────────────
    fee_source_urls: list[str] = Field(
        default_factory=list,
        description=(
            "URL(s) of the university's international fee schedule page(s). "
            "The scraper fetches these pages and matches fees to each course. "
            "E.g. ['https://www.scu.edu.au/study/international-courses-and-fees/']."
        ),
    )
    fee_match_by: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of strategies used to match fee rows to courses. "
            "Options: 'course_name', 'cricos_code', 'degree_level', 'campus', 'duration'. "
            "Default (empty) uses the global matching logic."
        ),
    )
    fee_term: Optional[str] = Field(
        default=None,
        description=(
            "Force the fee term for all courses at this university. "
            "Options: 'Annual', 'Per Unit', 'Full Course'. "
            "When set, overrides whatever term the extractor derives from the page."
        ),
    )
    fee_calculation_mode: str = Field(
        default="use_source_value_only",
        description=(
            "How fee amounts are calculated after extraction. "
            "'use_source_value_only' — store the fee exactly as found on the fee page (default). "
            "'full_course_to_annual' — divide by duration to get annual equivalent. "
            "'per_unit_to_annual' — multiply per-unit fee by credit-point load. "
            "'annual_to_full_course' — multiply annual fee by duration. "
            "Default 'use_source_value_only' prevents the Full Course rollup bug."
        ),
    )
    fee_prevent_full_course_rollup: bool = Field(
        default=True,
        description=(
            "Prevent the scraper from multiplying an annual fee by the course duration "
            "to produce a Full Course total. When True (default), the extracted value "
            "is stored as-is and fee_term is set to 'Annual'. "
            "Set False when fee_calculation_mode='full_course_to_annual' so the "
            "conversion can see the original 'Full Course' term."
        ),
    )
    max_annual_fee: Optional[int] = Field(
        default=None,
        description=(
            "If set, any extracted Annual fee above this threshold (AUD) is discarded "
            "and treated as a likely total-course value misidentified as per-year "
            "(e.g. Gemini returning a 3-year total as 'Annual'). "
            "Typical safe cap for Australian universities: 80000."
        ),
    )

    # ── 19. IELTS Component Mapping ──────────────────────────────────────────
    ielts_component_mapping: dict = Field(
        default_factory=dict,
        description=(
            "Maps IELTS overall band score → minimum component (each-band) score. "
            "Used when the course page shows only an overall score but the university "
            "requires minimum per-band scores. "
            "E.g. {'6.0': 5.5, '6.5': 6.0, '7.0': 6.5, '7.5': 7.0}. "
            "When matched, Reading/Writing/Listening/Speaking are all set to the mapped value."
        ),
    )

    # ── 20. Course Name Cleanup ──────────────────────────────────────────────
    course_name_remove_after: list[str] = Field(
        default_factory=list,
        description=(
            "Strip everything from the first occurrence of any of these strings "
            "(inclusive) onwards. Applied left-to-right. "
            "E.g. ['|', ' - Southern Cross University'] strips site name suffixes."
        ),
    )
    course_name_remove_year_suffix: bool = Field(
        default=False,
        description="Remove a trailing 4-digit year (e.g. 'Master of Science 2025' → 'Master of Science').",
    )
    course_name_remove_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns applied to the course name (case-insensitive). "
            "Each match is replaced with an empty string. "
            "E.g. [r'\\s*\\(.*?\\)\\s*$'] strips trailing parenthetical suffixes."
        ),
    )

    # ── 21. Location Cleanup ─────────────────────────────────────────────────
    location_allowed_values: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of valid campus/location strings (case-insensitive substring match). "
            "If non-empty, only location values that match at least one entry are kept; "
            "non-matching locations are cleared. "
            "E.g. ['Gold Coast', 'Lismore', 'Online', 'Brisbane']."
        ),
    )
    location_reject_values: list[str] = Field(
        default_factory=list,
        description=(
            "If any of these strings appears (case-insensitive) in the extracted location, "
            "the location is cleared entirely. Used to reject nav/footer contamination. "
            "E.g. ['How to Apply', 'Teaching period', 'Student Services']."
        ),
    )
    location_replace: dict = Field(
        default_factory=dict,
        description=(
            "String replacement map applied to location values before allow/reject filtering. "
            "Keys are strings to replace, values are replacements (use '' to delete). "
            "E.g. {'Teaching period': '', 'SCU Online': 'Online'}."
        ),
    )

    # ── 22. Study Mode Rules ─────────────────────────────────────────────────
    study_mode_from_location: bool = Field(
        default=False,
        description=(
            "Derive study_mode from the cleaned location value when study_mode is blank. "
            "Checks location for online keywords; if found with campus values → Blended, "
            "only online → Online, only campus → On Campus."
        ),
    )
    study_mode_online_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Keywords that indicate online delivery when found in the location string. "
            "Default (empty) uses ['online', 'distance', 'virtual']. "
            "Only used when study_mode_from_location=true."
        ),
    )

    # ── 23. Validation Rules ─────────────────────────────────────────────────
    block_publish_if: list[str] = Field(
        default_factory=list,
        description=(
            "List of conditions that block a course from auto-publishing even if it "
            "meets the completeness threshold. "
            "Options: 'fee_missing', 'fee_term_wrong', 'ielts_component_missing', "
            "'invalid_location', 'online_only', 'course_name_too_short', "
            "'course_name_too_long', 'degree_level_missing'."
        ),
    )

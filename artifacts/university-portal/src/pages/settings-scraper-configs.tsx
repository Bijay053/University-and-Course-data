import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import { SettingsTabs } from "@/components/settings-tabs";
import { Plus, Save, Trash2, Sparkles, Search, RefreshCw, X, Play, Loader2, CheckCircle2, AlertCircle, Clock, GitCompare, Code, History, RotateCcw, Download, Clipboard, Check, Wand2, Undo2, Bot, ShieldAlert, TriangleAlert, Info, ChevronDown, ChevronUp, Bug, Layers, Filter, Trash } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchWithAuth } from "@/lib/api";
import { CountrySelect } from "@/components/country-select";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

const SAMPLE_YAML = `# University Full Name
# Hostname: www.example.edu.au
# Country: Australia  |  Currency: AUD
#
# Bug history / rationale:
#   (add notes here as you discover site-specific quirks)

# ── BEFORE WRITING ANY YAML ──────────────────────────────────────────────────
#
# 1. Run a scrape first with NO custom YAML.
# 2. Check the discovery count (Discovered N raw candidate course link(s)).
# 3. Check the staged courses — look for 0 results or missing fields.
# 4. Identify the ACTUAL symptom before touching any setting.
# 5. Apply the smallest possible override that fixes only that symptom.
#
# Most universities need only 1–3 custom settings. Avoid adding fields
# 'just in case' — every extra setting adds a new failure mode.
#
# Biggest mistake: fixing a discovery problem with extraction settings,
# or fixing an extraction problem by changing discovery filters.
# Fix the layer where the symptom appears, not a neighbouring layer.
#
# ── WHEN TO EDIT THIS FILE ────────────────────────────────────────────────────
# Edit when a scrape of THIS university produces wrong or missing data.
# Safe pattern:
#   1. Run scrape → inspect staged rows in the portal
#   2. Find your symptom in the Quick Reference table at the bottom
#   3. Uncomment the relevant field → re-run this university only
#
# Do NOT touch scraper_config/defaults.yaml — that affects every university.

# ── DISCOVERY ─────────────────────────────────────────────────────────────────

discovery:

  # ── API discovery (use ONE of the three options below) ────────────────────

  # Option A — Autonomous XHR capture (AI finds the API endpoint for you).
  # Set true when BFS finds < 10 courses and you do not know the API URL.
  # The engine opens the course listing page in Playwright, intercepts all JSON
  # calls, picks the best candidate (SearchStax / Algolia / Solr / REST), and
  # immediately fetches courses from it.  The endpoint is then saved to auto_config
  # so future scrapes skip the XHR capture entirely.
  # [AUTO_API] log lines will show what the AI captured and how many URLs it added.
  auto_api_discovery: false

  # Option B — YAML-driven API (use after auto_api_discovery found the endpoint,
  # OR when you already know the URL from DevTools).
  # Runs before BFS/browser; if it returns ≥1 link those tiers are skipped.
  #
  # ┌─ Sub-option B1: Direct HTTP (works for public/open APIs) ─────────────────
  # generic_search_api:
  #   enabled: true
  #   method: GET
  #   url: "https://searchcloud-1.searchstax.com/29847/<core-id>/emselect"
  #   headers:
  #     authorization: "Token <your-public-search-key>"  # never commit session tokens
  #   params:
  #     q: "*"
  #     rows: "250"
  #     model: "coursefinder-ug"
  #   root_path: "response.docs"        # dot-path to the course array in the JSON
  #   url_fields: [url, course_url, link, path]
  #   title_fields: [title, name, course_name]
  #   normalize_relative_urls: true
  #   base_url: "https://www.example.edu.au"
  #   page_size: 250
  #   page_size_param: "rows"
  #   offset_param: "start"             # use this OR page_number_param, not both
  #   max_pages: 20
  #
  #   # Multiple API endpoints for the same university (e.g. UG and PG split across
  #   # separate Funnelback models or separate search APIs).
  #   # Links from every additional_url are merged with the primary url and deduplicated.
  #   # additional_urls:
  #   #   - url: "https://www.example.edu.au/api/search"
  #   #     params:
  #   #       q: "*"
  #   #       rows: "250"
  #   #       model: "coursefinder-pg"   # same keys as the primary; override only what differs
  #   #     allow_url_patterns:          # per-endpoint inclusion filter (optional)
  #   #       - /postgraduate/
  #
  # ┌─ Sub-option B2: Browser-based fetch (session-bound / Optimizely CMS APIs) ─
  # Use when: DevTools shows the API works fine in-browser, but hitting the same
  # URL with curl/Python always returns the same page 1 regardless of page params.
  # Root cause: the CMS ties pagination to a server-side session — the session
  # cookie is only issued when a real browser visits the site first.
  # Fix: fetch_via_browser: true — launches a headless browser, navigates to
  # browser_seed_url (triggers the cookie), then calls the API via JS fetch()
  # from inside the browser (inherits the cookie) so pagination works correctly.
  #
  # HOW TO IDENTIFY A SESSION-BOUND API:
  #   1. Open DevTools → Network tab → filter XHR/Fetch
  #   2. Find the course-search API call — copy the URL
  #   3. Paste into a new tab or run: curl "<url>" — if it only ever returns
  #      page 1 (same 20 items regardless of currentPage param) → session-bound.
  #   4. Set fetch_via_browser: true below.
  #
  # generic_search_api:
  #   enabled: true
  #   url: "https://www.example.edu.au/api/CourseApi/course-search"
  #   params:
  #     PageId: "12"          # CMS content node ID — copy from DevTools, do NOT guess
  #     PageSize: "20"
  #   page_size: 20
  #   page_size_param: "PageSize"       # query-string param that sets items-per-page
  #   page_number_param: "currentPage"  # query-string param that advances the page
  #   has_next_field: "result.hasNextPage"  # dot-path to the boolean stop signal
  #   root_path: "result.items"         # dot-path to the array of course objects
  #   url_fields:
  #     - "link.href"                   # dot-path supported: item.link.href
  #   title_fields:
  #     - "header"
  #   normalize_relative_urls: true
  #   base_url: "https://www.example.edu.au"
  #   allow_url_patterns:
  #     - "/courses/"
  #   max_pages: 20
  #   fetch_via_browser: true           # ← enables browser-mode for session-bound APIs
  #   browser_seed_url: "https://www.example.edu.au/study/course-search"
  #                                     # page to visit first — triggers session cookie
  #                                     # defaults to url if not set

  # ┌─ Sub-option B3: JSON body POST (Elastic App Search / SilverStripe) ──────────
  # Use when: the API requires application/json body for BOTH the request payload AND
  # pagination — NOT query-string parameters.  Elastic App Search works this way:
  # pagination is { "page": { "current": 2, "size": 100 } } in the JSON body,
  # not ?page.current=2 in the URL.
  #
  # HOW TO IDENTIFY AN ELASTIC APP SEARCH API:
  #   1. DevTools → Network → filter XHR/Fetch
  #   2. Find a POST to .../_search/api/as/v1/engines/<engine>/search.json
  #   3. Click it → Payload tab — you will see JSON body with "query" + "page" keys
  #   4. Check the Authorization header carefully (see TWO VARIANTS below)
  #   5. Response: { results: [...], meta: { engine: { name }, page: { total_pages } } }
  #   6. Check which field holds the course URL (common: url.raw, page_link.raw, link.raw)
  #      Look at the Payload tab's response to find the right field name.
  #
  # ── VARIANT A: Server-side proxy alias (most SilverStripe / Incapsula sites) ──
  # How to recognise: DevTools shows "Bearer --search--" or "Bearer <placeholder>".
  # The site runs a reverse proxy at /_search/ that maps a placeholder engine name
  # (e.g. --engine--) and placeholder token (--search--) to the real credentials
  # server-side.  The client (browser and scraper) never sees the real token.
  #
  # Solution: use the placeholder URL as-is, NO Authorization header in YAML,
  # set fetch_via_browser: true so the scraper obtains Incapsula/Imperva session
  # cookies first (by navigating to browser_seed_url), then calls the API as POST.
  #
  # HOW THE BROWSER POST WORKS:
  #   When body is set AND fetch_via_browser: true, the scraper does NOT build
  #   a GET query-string URL.  Instead it runs a JS fetch() inside the browser:
  #     fetch(url, { method:'POST', credentials:'include',
  #                  headers: <cfg.headers>, body: JSON.stringify(<body>) })
  #   The cfg.headers dict (content-type, x-swiftype-client, etc.) is forwarded
  #   automatically.  body_pagination updates body["page"]["current"] per page.
  #   Pagination stops when current_page >= meta.page.total_pages (total_pages_path).
  #
  # IMPORTANT: copy the EXACT body from DevTools Payload tab — the source_class /
  # type filter is essential or the API returns mixed content types.
  # Check the response to confirm which field holds the URL (url.raw vs page_link.raw).
  #
  # generic_search_api:
  #   enabled: true
  #   method: POST
  #   url: "https://www.example.ac.nz/_search/api/as/v1/engines/--engine--/search.json"
  #   headers:
  #     content-type: "application/json"
  #     x-swiftype-client: "elastic-app-search-javascript"  # copy from DevTools
  #     x-swiftype-client-version: "8.13.0"
  #   # COPY EXACT BODY from DevTools Payload tab — do not guess filters/sort
  #   body:
  #     query: ""
  #     filters:
  #       all:
  #         - source_class:
  #             - "App\\Pages\\QualificationPage"  # YAML needs double-backslash
  #     page:
  #       current: 1      # body_pagination.current_path increments this per page
  #       size: 100       # body_pagination.size_path sets this to page_size
  #     sort:
  #       - _score: "desc"
  #       - title: "asc"
  #   page_size: 100
  #   body_pagination:
  #     current_path: page.current          # dot-path into body dict; updated per page
  #     size_path: page.size                # sets body["page"]["size"] = page_size
  #     total_pages_path: meta.page.total_pages      # stops when current >= total_pages
  #     total_results_path: meta.page.total_results  # logged for diagnostics only
  #   root_path: "results"                  # Elastic App Search wraps items in "results"
  #   url_fields:
  #     - "page_link.raw"     # SilverStripe sites often use page_link.raw (relative)
  #     - "url.raw"           # fallback: standard Elastic App Search absolute URL
  #   title_fields:
  #     - "title.raw"
  #   normalize_relative_urls: true   # needed when page_link.raw returns relative paths
  #   base_url: "https://www.example.ac.nz"
  #   allow_url_patterns:
  #     - "/qualifications/"
  #   max_pages: 10
  #   fetch_via_browser: true         # REQUIRED: gets Incapsula cookies + sends body POST
  #   browser_seed_url: "https://www.example.ac.nz/study/qualifications/"
  #                                   # navigated first to acquire session cookies
  #
  # ── VARIANT B: Real Bearer token (public search key, not a user credential) ──
  # How to recognise: DevTools shows a real "Bearer search-key-abc123xyz..." value.
  # This is a read-only public search key — safe to store as a Replit secret.
  # Set fetch_via_browser: false (plain httpx POST is enough).
  # Replace <engine> in the URL with the actual name from DevTools.
  #
  # generic_search_api:
  #   enabled: true
  #   method: POST
  #   url: "https://api.example.com/api/as/v1/engines/<engine>/search.json"
  #   headers:
  #     authorization: "Bearer \${MY_UNI_EAS_TOKEN}"  # set as Replit secret
  #     content-type: "application/json"
  #   body:
  #     query: ""
  #     page:
  #       current: 1
  #       size: 100
  #   page_size: 100
  #   body_pagination:
  #     current_path: page.current
  #     size_path: page.size
  #     total_pages_path: meta.page.total_pages
  #   root_path: "results"
  #   url_fields:
  #     - "url.raw"
  #   title_fields:
  #     - "title.raw"
  #   normalize_relative_urls: false  # hosted Elastic returns absolute URLs
  #   max_pages: 10
  #
  # seed_urls:
  #   - https://www.example.ac.nz/study/qualifications/
  # # When API returns 0 links, BFS starts from seed_urls[0] instead of homepage.

  # Option C — SearchStax Solr provider (Huddersfield / WLV style, full provider).
  # Use when the site is a React SPA that queries a Solr/SearchStax core client-side.
  # HOW TO IDENTIFY: DevTools → Network → filter XHR → find a call to
  #   searchcloud-*.searchstax.com/.../emselect  → copy the endpoint URL.
  # searchstax:
  #   enabled: true
  #   endpoint: "https://searchcloud-1-eu-west-2.searchstax.com/29847/<core-id>/emselect"
  #   token_env: "MY_UNI_SEARCHSTAX_TOKEN"  # env var name — never commit literal tokens
  #   filter_query: "sectionType_s:course"   # Solr fq filter — copy from DevTools request
  #   currency: "GBP"                        # NZD | AUD | USD | GBP | EUR
  #
  #   # When the Solr url_t field contains bare course codes rather than full URLs
  #   # (e.g. WLV SITS codes like "WR006J01UMU"), prepend url_base to build real links:
  #   # url_base: "https://www.example.ac.uk/courses"
  #
  #   # Strip category-label prefixes from the Solr location field values before storing.
  #   # Example: "University: City Campus" → "City Campus"
  #   # location_strip_prefixes:
  #   #   - "University: "
  #   #   - "Campus: "
  #
  #   # Drop "Part-time" from mixed-mode courses; skip exclusively Part-time courses.
  #   # Use for international-student portals where only Full-time enrolment is offered:
  #   # exclude_part_time: true
  #
  #   # When Solr documents already contain full course data (duration, mode, intakes,
  #   # fees, IELTS) there is no need to fetch each course HTML page separately.
  #   # Set both flags below so the orchestrator uses the Solr payload directly:
  #   # links_only: false          # false = run per-course extraction pass
  #   # field_map_as_payload: true # true  = use Solr doc fields as the payload (no HTTP)
  #
  #   # Map semantic field names to the actual Solr field names for this university.
  #   # Only override fields whose Solr name differs from the HUD defaults.
  #   # Defaults: name→title_t, url→url_t, award→award_s
  #   # field_map:
  #   #   degree_level:  level_s
  #   #   study_mode:    multi_mode_ss
  #   #   duration:      multi_duration_ss
  #   #   intake_dates:  multi_course_start_date_ss
  #   #   category:      subject_area_ss
  #   #   location:      multi_location_ss
  #
  #   # Pagination (defaults usually fine — only change if Solr returns < all courses):
  #   # page_size: 200
  #   # max_pages: 20

  # ── BFS / Sitemap / Browser ────────────────────────────────────────────────

  # Always merge sitemap results with BFS (for JS SPAs or deep-faculty sites):
  # always_sitemap_supplement: true

  # Probe extra subdomains when BFS finds fewer than 5 candidates:
  # fallback_subdomains:
  #   - handbook.{domain}
  #   - study.{domain}

  # Drop URLs matching these regex patterns:
  # block_url_patterns:
  #   - /news/
  #   - /events/

  # Keep ONLY URLs matching at least one pattern (cuts Gemini cost significantly):
  # allow_url_patterns:
  #   - /courses/
  #   - /programs/
  #
  # IMPORTANT: allow_url_patterns is an INCLUSION filter — if no discovered URL
  # matches at least one pattern, the scraper stages 0 courses:
  #   Discovered 175 raw candidate course link(s)
  #   URL filter dropped 175 / 175 URLs (100%)
  #   Found: 0
  # Always test your regex against a sample of real discovered URLs before enabling.
  # Prefer must_contain (below) when a simple substring is enough.

  # Simpler and usually safer than allow_url_patterns.
  # Use when a unique URL path segment reliably identifies course pages.
  # Unlike allow_url_patterns, no regex knowledge needed — just a substring.
  # must_contain:
  #   - /courses/

  # Override auto-detected sitemap:
  # sitemap_url: https://www.example.edu.au/custom-sitemap.xml

  # Raise BFS page budget for sites with many listing pages (default 25 full):
  # bfs_page_budget: 80

  # Enable Playwright browser discovery in addition to BFS (for Cloudflare sites):
  # always_browser_discover: true

  # Use stealth Playwright stack (for hosts where regular headless fails Cloudflare):
  # use_stealth_browser: true

  # Fallback to Wayback Machine when all live-site discovery fails:
  # use_wayback: true

  # Surgical fallback — inject specific course URLs directly, bypassing all discovery:
  # extra_course_urls:
  #   - https://www.example.edu.au/courses/some-hidden-course


# ── EXTRACTION ────────────────────────────────────────────────────────────────

extraction:

  # ── Per-course browser controls (now YAML-configurable, no code changes needed) ──

  # Skip ALL per-course Playwright fetches for this university.
  # Use when static HTML already contains all required fields AND browser always times out:
  # skip_per_course_browser: true

  # Override the Playwright wait strategy for per-course fetches.
  # 'networkidle'      — wait for XHR/fetch to settle (use when fees load via AJAX).
  # 'domcontentloaded' — use when analytics widgets prevent networkidle from ever firing.
  # browser_wait_strategy: networkidle

  # Extra settle delay (ms) after domcontentloaded fires (only with 'domcontentloaded'):
  # browser_dcl_settle_ms: 4000

  # ── Fees ──────────────────────────────────────────────────────────────────
  fees:
    default_currency: "AUD"   # NZD for New Zealand, GBP for UK

    # University-wide fee schedule page (used when fees are not per-course):
    # central_page: https://www.example.edu.au/fees

    # University-wide fee schedule PDF:
    # fees_pdf_url: https://www.example.edu.au/fees-schedule.pdf

    # Mark all courses as having a central fee page (staging gate won't reject them):
    # force_central_fee_stage: true
    #
    # NOTE: This only lets courses pass staging when no per-course fee is found.
    # It does NOT copy the central fee to every course record.
    # If the central page publishes only broad tuition buckets (e.g. "UG: $18k/yr"),
    # leave international_fee blank rather than forcing a possibly wrong amount.
    # Use reject_keywords (below) to discard domestic rates from the central page.

    # Per-unit fee multiplier (null = auto-extract credit points from course page):
    # credit_points_per_unit: 6

    # Prefer Year-1 fee over total-course fee when both are present:
    # prefer_year_one_over_total: true

    # Column-aware PDF parser (for PDFs with multi-line course names in fee tables):
    # pdf_parser: "columnar"

    # Per-course fee keyword rejection — discard an extracted fee when its evidence
    # snippet contains any listed keyword (e.g. to avoid staging domestic rates):
    # reject_keywords:
    #   - "Kentucky residents"    # precise domestic marker — safe
    #   - "In-state"              # precise domestic marker — safe
    #   - "Commonwealth Supported"
    #   - "CSP"
    #   - "HECS"
    #
    # IMPORTANT: Avoid broad words like "Full-time" or "credit hours" because
    # they can also appear beside valid international fees (e.g.
    # "Full-time international student: $28,000/year") and will silently discard
    # the fee you actually want. Use the most specific domestic phrase possible.

  # ── English requirements ───────────────────────────────────────────────────
  english:
    # University-wide English requirements page:
    # central_page: https://www.example.edu.au/english-requirements

    # Stop Gemini vision from hallucinating IELTS scores from decorative images:
    # trust_vision_ocr: false

    # Institutional defaults applied when no per-course value is found:
    # default_ielts: 6.5
    # default_pte: 58
    # default_toefl: 80
    #
    # Per-degree-level defaults (more precise than the flat defaults above).
    # Applied when no per-course English value is found AND the course has a
    # known degree_level.  Overrides default_ielts / default_pte / default_toefl
    # for matched tiers.  Use when UG and PG have different published requirements.
    #
    # Supported tiers: undergraduate | postgraduate | doctorate
    #
    # degree_level_defaults:
    #   undergraduate:
    #     ielts: 6.0
    #     pte: 50
    #     toefl: 80
    #   postgraduate:
    #     ielts: 6.5
    #     pte: 58
    #     toefl: 90
    #   doctorate:
    #     ielts: 6.5
    #     pte: 58
    #     toefl: 90

    # Drop test names the university doesn't actually accept (suppress false positives):
    # test_blocklist:
    #   - pte
    #   - kite

  # ── Intake ────────────────────────────────────────────────────────────────
  intake:
    # For research degrees with rolling enrolment (PhD/MPhil):
    # rolling_enrollment_label: "Rolling"
    # rolling_enrollment_markers:
    #   - "enrolment shall be continuous"
    #   - "rolling admission"
    #   - "applications accepted year-round"
    #
    # IMPORTANT: Only use phrases that specifically mean continuous/rolling intake.
    # Do NOT use generic page text like "Apply Now", "Admission Requirements",
    # or "accepted to university" — those appear on normal fixed-intake pages too
    # and will stamp "Rolling" on every course that has no detected intake dates.

  # ── Filters ───────────────────────────────────────────────────────────────
  filters:
    domestic_only:
      enabled: false    # true = drop courses without international student data
    online_only:
      enabled: true     # false for distance-education-heavy universities (e.g. CSU)

  # ── URL rewrites — switch site to international view before fetching ─────────
  # url_rewrites:
  #   - host: www.example.edu.au
  #     append_query: "international=true"

  # ── Text cleaning ──────────────────────────────────────────────────────────
  text_cleaning:
    location:
      # strip_patterns:
      #   - '\\bDelivery\\s*method\\b'
    duration:
      # reject_sentence_patterns:
      #   - 'up to \\d+ years to complete'
    # global_substring_blocklist:
    #   - "Apply Now"
    #   - "Find out more"

  # ── Course name ────────────────────────────────────────────────────────────
  course_name:
    # strip_title_suffixes:
    #   - " : the University of Western Australia"

  # ── Concurrency ────────────────────────────────────────────────────────────
  # Lower for Cloudflare-heavy sites that rate-limit aggressively:
  # max_parallel_fetch: 2

  # Fallback location written when the Location panel is occasionally missing:
  # default_course_location: "Sydney"

# ── QUICK REFERENCE — symptom → YAML field ────────────────────────────────────
# Symptom                                        Fix
# ─────────────────────────────────────────────────────────────────────────────
# BFS finds 0–9 courses (JS SPA, hidden API)     auto_api_discovery: true
# You already know the API endpoint URL           generic_search_api (Option B above)
# Site uses SearchStax Solr directly             searchstax (Option C above)
# SearchStax url_t holds SITS codes, not URLs    searchstax.url_base
# Location shows "Category: Value" prefix        searchstax.location_strip_prefixes
# Mode shows Part-time for intl-only courses     searchstax.exclude_part_time: true
# Solr doc has all fields (no HTML fetch needed) searchstax.field_map_as_payload: true + links_only: false
# Solr field names differ from HUD defaults      searchstax.field_map (6 overrideable fields)
# Discovery finds nav/news pages, not courses    must_contain / block_url_patterns
# Sitemap not auto-discovered                    sitemap_url
# BFS finds < 5 courses (different subdomain)    fallback_subdomains
# Cloudflare blocks plain-HTTP BFS               always_browser_discover: true
# Cloudflare blocks headless Playwright too      use_stealth_browser: true
# Per-course browser always times out (0 bytes)  skip_per_course_browser: true
# Fees load after page load via AJAX             browser_wait_strategy: networkidle
# Analytics widget prevents networkidle          browser_wait_strategy: domcontentloaded
# All courses staged as no_international_fee     fees.force_central_fee_stage: true
# Fee PDF has multi-line course names            fees.pdf_parser: "columnar"
# Page shows Year-1 fee; we want annual total    fees.prefer_year_one_over_total: true
# IELTS hallucinated from decorative images      english.trust_vision_ocr: false
# PTE/TOEFL on pages that do not list it         english.test_blocklist
# UG/PG have different IELTS/PTE requirements    english.degree_level_defaults
# API has separate UG and PG endpoints           generic_search_api.additional_urls
# Duration shows max-candidature time            text_cleaning.duration.reject_sentence_patterns
# Location panel blank on Cloudflare-heavy site  default_course_location
# Location string has CMS junk suffix            text_cleaning.location.strip_patterns
# Course title ends with " : University of X"   course_name.strip_title_suffixes
# PhD shows no intake months                     intake.rolling_enrollment_label
# Domestic-only courses are being staged         filters.domestic_only.enabled: true
# Site uses per-unit fees                        fees.credit_points_per_unit
# International view needs a query parameter     url_rewrites
# ─────────────────────────────────────────────────────────────────────────────
# NOT YAML-fixable (escalate to engineering):
#   Cloudflare WAF blocks even stealth browser   → new extraction route needed
#   Fees only behind a JS calculator (no HTML)   → new XHR extractor needed
#   English requirements behind a login wall     → manual data entry
#   CRICOS 0% even though page shows CRICOS text → regex fix in cricos_code.py
`;

function downloadSampleYaml() {
  const blob = new Blob([SAMPLE_YAML], { type: "text/yaml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "sample-scraper-config.yaml";
  a.click();
  URL.revokeObjectURL(url);
}

interface ConfigEntry {
  slug: string;
  title: string;
  yaml: string;
  university_id: number | null;
  university_name: string | null;
}

interface RegressionAlert {
  id: number;
  university_id: number;
  job_id: string | null;
  alert_type: string;
  severity: "critical" | "high" | "medium";
  previous_value: number | null;
  current_value: number | null;
  delta: number | null;
  probable_causes: string[];
  status: "open" | "acknowledged" | "resolved";
  snapshot_date: string | null;
  created_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

const ALERT_TYPE_LABELS: Record<string, string> = {
  course_count_drop:     "Course Count Drop",
  overall_health_drop:   "Overall Health Drop",
  discovery_health_drop: "Discovery Health Drop",
  extraction_health_drop:"Extraction Health Drop",
  fee_coverage_drop:     "Fee Coverage Drop",
  english_coverage_drop: "English Coverage Drop",
  intake_coverage_drop:  "Intake Coverage Drop",
};

function alertSeverityMeta(severity: "critical" | "high" | "medium") {
  if (severity === "critical") return {
    bgCls:    "bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-800",
    badgeCls: "bg-red-100 dark:bg-red-900/60 text-red-700 dark:text-red-300 border-red-300 dark:border-red-700",
    textCls:  "text-red-800 dark:text-red-200",
    dotCls:   "bg-red-500",
    iconCls:  "text-red-600 dark:text-red-400",
  };
  if (severity === "high") return {
    bgCls:    "bg-orange-50 dark:bg-orange-950/30 border-orange-200 dark:border-orange-800",
    badgeCls: "bg-orange-100 dark:bg-orange-900/60 text-orange-700 dark:text-orange-300 border-orange-300 dark:border-orange-700",
    textCls:  "text-orange-800 dark:text-orange-200",
    dotCls:   "bg-orange-500",
    iconCls:  "text-orange-600 dark:text-orange-400",
  };
  return {
    bgCls:    "bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-800",
    badgeCls: "bg-amber-100 dark:bg-amber-900/60 text-amber-700 dark:text-amber-300 border-amber-300 dark:border-amber-700",
    textCls:  "text-amber-800 dark:text-amber-200",
    dotCls:   "bg-amber-500",
    iconCls:  "text-amber-600 dark:text-amber-400",
  };
}

function alertChangeLine(alert: RegressionAlert): string {
  const label = ALERT_TYPE_LABELS[alert.alert_type] ?? alert.alert_type;
  const prev = alert.previous_value ?? 0;
  const cur  = alert.current_value  ?? 0;
  if (alert.alert_type === "course_count_drop") {
    const pct = prev > 0 ? Math.round(100 * (prev - cur) / prev) : 0;
    return `Course count dropped ${pct}%: ${prev} → ${cur} courses`;
  }
  const pts = Math.round(Math.abs(alert.delta ?? 0));
  return `${label}: ${prev}% → ${cur}% (−${pts} pts)`;
}

interface ValidationMetrics {
  completeness:    number;
  fee_coverage:    number;
  english_coverage:number;
  intake_coverage: number;
  sample_count:    number;
}

interface AutoRepairSuggestion {
  id:                    number;
  university_id:         number;
  regression_alert_id:   number | null;
  issue_summary:         string | null;
  root_cause_category:   string | null;
  fix_recommendation:    string | null;
  fix_yaml_snippet:      string | null;
  safe_fix:              { type: string; key: string; value?: unknown } | null;
  risk_label:            "low" | "medium" | "developer_required" | null;
  developer_note:        string | null;
  fail_reason:           string | null;
  evidence:              { type: string; label: string; value: string; source: string }[];
  validation_result:     {
    before?: ValidationMetrics;
    after?:  ValidationMetrics;
    production_completeness?: number;
    confidence?: string;
    method?: string;
    skip_reason?: string | null;
    url_simulation?: {
      method:               "url_simulation";
      sample_size:          number;
      total_raw:            number;
      before_pass:          number;
      after_pass:           number;
      improvement:          number;
      sample_dropped_before: string[];
      sample_rescued:       string[];
      confidence:           "high" | "medium" | "low";
    } | null;
  } | null;
  confidence:   "high" | "medium" | "low" | null;
  status:       "pending" | "ready" | "developer_required" | "applied" | "dismissed" | "failed";
  created_at:   string;
  applied_at:   string | null;
  dismissed_at: string | null;
  applied_by:   string | null;
  old_config:   Record<string, unknown> | null;
  new_config:   Record<string, unknown> | null;
}

interface UniversityHealth {
  university_id: number;
  total_courses: number;
  last_imported: number | null;
  last_total_found: number | null;
  last_job_at: string | null;
  discovery_health: number;
  extraction_health: number;
  fee_coverage: number;
  english_coverage: number;
  intake_coverage: number;
  overall_health: number;
  top_issue: { metric: string; score: number; label: string } | null;
  trend_overall: number | null;
  trend_discovery: number | null;
  trend_extraction: number | null;
  trend_fee: number | null;
  trend_english: number | null;
  trend_intake: number | null;
  trend_snapshot_date: string | null;
}

function healthScoreMeta(score: number) {
  if (score >= 85) return { label: "Healthy",      textCls: "text-green-700 dark:text-green-400",   bgCls: "bg-green-50 dark:bg-green-950/30 border-green-200 dark:border-green-800",   barCls: "bg-green-500"  };
  if (score >= 70) return { label: "Watch",         textCls: "text-amber-700 dark:text-amber-400",   bgCls: "bg-amber-50 dark:bg-amber-950/30 border-amber-200 dark:border-amber-800",   barCls: "bg-amber-500"  };
  if (score >= 50) return { label: "Needs Review",  textCls: "text-orange-700 dark:text-orange-400", bgCls: "bg-orange-50 dark:bg-orange-950/30 border-orange-200 dark:border-orange-800", barCls: "bg-orange-500" };
  return              { label: "Critical",     textCls: "text-red-700 dark:text-red-400",     bgCls: "bg-red-50 dark:bg-red-950/30 border-red-200 dark:border-red-800",     barCls: "bg-red-500"    };
}

interface GenerateForm {
  university_name: string;
  website_url: string;
  country: string;
  notes: string;
}

type JobStatus = "queued" | "running" | "done" | "awaiting_approval" | "failed" | "cancelled";

interface TriggerState {
  jobId: string;
  status: JobStatus;
  universityId?: number;
  universityName?: string;
  error?: string;
  imported?: number;
  totalFound?: number;
}

interface HistoryEntry {
  id: number;
  slug: string;
  yaml_content: string;
  saved_by: string | null;
  saved_at: string | null;
}

type EditorView = "editor" | "diff" | "history" | "debugger";

// ── Debugger types ─────────────────────────────────────────────────────────
interface EffectiveConfigLayer {
  value: unknown;
  source: "defaults_yaml" | "db_legacy" | "db_auto" | "yaml" | "admin_config";
}
interface AdminOverrideFlat { path: string; value: unknown; }
interface EffectiveConfigResult {
  university_id: number;
  university_name: string;
  slug: string;
  yaml_slug: string | null;
  has_yaml: boolean;
  layers_present: string[];
  annotated_config: Record<string, unknown>;
  admin_config_raw: Record<string, unknown>;
  admin_overrides_flat: AdminOverrideFlat[];
  has_admin_overrides: boolean;
}
interface RejectionSummaryItem {
  reason: string;
  reason_label: string;
  count: number;
  severity: "critical" | "warning" | "info";
  config_key: string | null;
}
interface RejectionItem {
  reason: string;
  reason_label: string;
  description: string;
  config_key: string | null;
  severity: "critical" | "warning" | "info";
  course_name: string;
  url: string;
  ts: string | null;
}
interface RejectionLogResult {
  university_id: number;
  job_id: string | null;
  total: number;
  summary: RejectionSummaryItem[];
  rejections: RejectionItem[];
}

// ── Extraction Debugger types ────────────────────────────────────────────────
interface EvidenceCandidate {
  candidate_value: string | null;
  normalized_value: string | null;
  extraction_method: string | null;
  confidence: number | null;
  selected: boolean;
  snippet: string | null;
  source_url: string | null;
  page_type: string | null;
  validation_status: string | null;
}
interface ExtractionPipelineField {
  field_key: string;
  field_label: string;
  final_value: string | null;
  extraction_method: string | null;
  confidence: number | null;
  snippet: string | null;
  source_url: string | null;
  candidates_count: number;
  candidates: EvidenceCandidate[];
  has_conflict: boolean;
  missing: boolean;
}
interface ScrapedCourseItem {
  id: number;
  course_name: string;
  status: string;
  completeness: number | null;
  auto_publish_status: string | null;
  study_mode: string | null;
  degree_level: string | null;
  international_fee: number | null;
  ielts_overall: number | null;
  course_website: string | null;
  extraction_method: Record<string, string> | null;
}
interface ScrapedCoursesResult {
  university_id: number;
  job_id: string | null;
  courses: ScrapedCourseItem[];
}
interface ExtractionTraceResult {
  university_id: number;
  course_id: number;
  course_name: string;
  completeness: number | null;
  status: string;
  course_website: string | null;
  pipeline: ExtractionPipelineField[];
}

// ── Discovery Debugger types ──────────────────────────────────────────────────
interface DiscoveryEventItem {
  kind: string;
  phase: string;
  dropped: number;
  kept: number;
  drop_pct: number | null;
  message: string;
  dropped_sample: string[];
  pattern_breakdown: Record<string, number>;
}
interface DiscoverySummary {
  total_blocked_by_block_patterns: number;
  total_blocked_by_allow_patterns: number;
  total_blocked_by_must_contain: number;
  pages_classified: number;
  pattern_breakdown: Record<string, number>;
  blocked_samples: string[];
  allow_dropped_samples: string[];
  must_contain_dropped_samples: string[];
}
interface DiscoveryStatsResult {
  university_id: number;
  job_id: string | null;
  summary: DiscoverySummary;
  events: DiscoveryEventItem[];
}
interface UrlTestResult {
  accepted: boolean;
  blocked_by: string | null;
  matched_pattern: string | null;
  reason: string;
  block_patterns: string[];
  allow_patterns: string[];
  must_contain: string[];
}

interface DiagnosisIssue {
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
}

interface DiagnosisResult {
  university_found: boolean;
  university_name: string;
  university_id: number | null;
  last_job: {
    job_id: string;
    status: string;
    total_found: number;
    imported: number;
    errors: number;
    created_at: string | null;
    raw_discovered: number;
    after_filter: number;
    filter_drop_count: number;
  } | null;
  issues: DiagnosisIssue[];
  changes: string[];
  summary: string;
  yaml: string;
  has_changes: boolean;
}

// ── AI Root Cause Analysis types ─────────────────────────────────────────────
interface AiEvidenceItem {
  type: "job_stat" | "rejection" | "config" | "alert" | "extraction" | "discovery";
  label: string;
  value: string;
  source: string;
}

interface AiSafeFix {
  action: "clear_admin_override" | "set_admin_override";
  key: string;
  value?: string | number | boolean | null;
  description: string;
}

interface AiRootCauseResult {
  issue_summary: string;
  root_cause_category: "discovery" | "filtering" | "extraction" | "config_conflict" | "api" | "pdf" | "browser" | "staging_gate" | "healthy";
  confidence: "high" | "medium" | "low";
  evidence: AiEvidenceItem[];
  fix_recommendation: string;
  fix_yaml_snippet: string | null;
  safe_fix: AiSafeFix | null;
  risk_label: "low" | "medium" | "developer_required";
  developer_required: boolean;
  developer_note: string | null;
  context_used: string[];
  university_id: number;
  university_name: string;
  last_job_id: string | null;
  last_job_created_at: string | null;
  config_last_saved_at: string | null;
  config_is_stale: boolean;
  filter_sim: { total: number; passing: number; blocked: number; pass_pct: number; has_filters: boolean } | null;
}

interface TestDiscoveryUrlResult {
  seed_url: string;
  status_code: number;
  raw_candidates: number;
  raw_course_count: number;
  raw_listing_count: number;
  raw_category_count: number;
  raw_other_count: number;
  after_filter: number;
  dropped: number;
  drop_rate_pct: number;
  sample_passing: string[];
  sample_dropped: string[];
  classified_passing: Record<string, string[]>;
  course_count: number;
  listing_count: number;
  category_count: number;
  ok: boolean;
  error?: string;
  warning?: string;
}

interface ConfigConflict {
  field: string;
  location: "discovery";
  yaml_values: string[];
  admin_value: [];
}

interface TestDiscoveryResult {
  ok: boolean;
  error?: string;
  total_raw: number;
  total_passing: number;
  total_dropped: number;
  agg_drop_rate_pct: number;
  warnings: string[];
  safety_score: number;
  safety_level: "safe" | "warning" | "dangerous";
  agg_status: "ok" | "warning" | "critical";
  seed_results: TestDiscoveryUrlResult[];
  filter_config: { allow_url_patterns: string[]; must_contain: string[]; block_url_patterns: string[] };
  config_conflicts?: ConfigConflict[];
  admin_config_raw?: Record<string, unknown>;
}

interface FullValidationUrlResult {
  url: string;
  passes_filter: boolean;
  blocked_by: string | null;
  status_code: number;
  page_type: "course" | "listing" | "category" | "unknown";
  course_name_extracted: boolean;
  course_name_value: string | null;
  fee_extracted: boolean;
  fee_value: string | null;
  english_extracted: boolean;
  english_value: string | null;
  intake_extracted: boolean;
  duration_extracted: boolean;
  degree_level_extracted: boolean;
  fields_found: number;
  fields_total: number;
  completeness_pct: number;
  will_stage: boolean;
  rejection_reason: string | null;
  ok: boolean;
  error?: string;
  text_length?: number;
}

interface FullValidationResult {
  ok: boolean;
  error?: string;
  results: FullValidationUrlResult[];
  summary: {
    total: number;
    passed_filter: number;
    course_pages: number;
    listing_pages: number;
    avg_course_completeness_pct: number;
  };
}

const TERMINAL_STATUSES: JobStatus[] = ["done", "awaiting_approval", "failed", "cancelled"];

function JobStatusBadge({ state, compact = false }: { state: TriggerState; compact?: boolean }) {
  const { status, imported, totalFound, error } = state;
  if (status === "queued") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-amber-600", compact ? "text-[10px]" : "text-xs")}>
        <Clock className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && "Queued"}
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-blue-600", compact ? "text-[10px]" : "text-xs")}>
        <Loader2 className={cn("animate-spin", compact ? "w-3 h-3" : "w-3.5 h-3.5")} />
        {!compact && (totalFound ? `Running… ${imported ?? 0}/${totalFound}` : "Running…")}
      </span>
    );
  }
  if (status === "done" || status === "awaiting_approval") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-green-600", compact ? "text-[10px]" : "text-xs")}>
        <CheckCircle2 className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && (status === "awaiting_approval" ? "Awaiting approval" : `Done${imported != null ? ` — ${imported} staged` : ""}`)}
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span className={cn("inline-flex items-center gap-1 text-red-600", compact ? "text-[10px]" : "text-xs")} title={error}>
        <AlertCircle className={compact ? "w-3 h-3" : "w-3.5 h-3.5"} />
        {!compact && "Failed"}
      </span>
    );
  }
  return null;
}

// ── Diff engine ───────────────────────────────────────────────────────────────

type DiffOp = "equal" | "insert" | "delete";

interface DiffLine {
  op: DiffOp;
  text: string;
  oldLineNo: number | null;
  newLineNo: number | null;
}

function lcs(a: string[], b: string[]): number[][] {
  const m = a.length;
  const n = b.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] + 1 : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  return dp;
}

function computeDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText === "" ? [] : oldText.split("\n");
  const newLines = newText === "" ? [] : newText.split("\n");

  const dp = lcs(oldLines, newLines);

  const result: DiffLine[] = [];
  let i = oldLines.length;
  let j = newLines.length;
  const ops: Array<[DiffOp, string]> = [];

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      ops.push(["equal", oldLines[i - 1]]);
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      ops.push(["insert", newLines[j - 1]]);
      j--;
    } else {
      ops.push(["delete", oldLines[i - 1]]);
      i--;
    }
  }

  ops.reverse();

  let oldNo = 1;
  let newNo = 1;
  for (const [op, text] of ops) {
    if (op === "equal") {
      result.push({ op, text, oldLineNo: oldNo++, newLineNo: newNo++ });
    } else if (op === "delete") {
      result.push({ op, text, oldLineNo: oldNo++, newLineNo: null });
    } else {
      result.push({ op, text, oldLineNo: null, newLineNo: newNo++ });
    }
  }

  return result;
}

const CONTEXT_LINES = 3;

function collapseDiff(lines: DiffLine[]): Array<DiffLine | { type: "hunk"; count: number }> {
  const changed = new Set<number>();
  lines.forEach((l, idx) => {
    if (l.op !== "equal") {
      for (let k = Math.max(0, idx - CONTEXT_LINES); k <= Math.min(lines.length - 1, idx + CONTEXT_LINES); k++) {
        changed.add(k);
      }
    }
  });

  const result: Array<DiffLine | { type: "hunk"; count: number }> = [];
  let skipCount = 0;

  for (let idx = 0; idx < lines.length; idx++) {
    if (changed.has(idx)) {
      if (skipCount > 0) {
        result.push({ type: "hunk", count: skipCount });
        skipCount = 0;
      }
      result.push(lines[idx]);
    } else {
      skipCount++;
    }
  }

  if (skipCount > 0) result.push({ type: "hunk", count: skipCount });

  return result;
}

// ── Diff viewer component ─────────────────────────────────────────────────────

function DiffViewer({ oldYaml, newYaml, oldLabel = "saved", newLabel = "current edit" }: { oldYaml: string; newYaml: string; oldLabel?: string; newLabel?: string }) {
  const diffLines = computeDiff(oldYaml, newYaml);
  const hasChanges = diffLines.some(l => l.op !== "equal");

  if (!hasChanges) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        No changes — {oldLabel} matches {newLabel}.
      </div>
    );
  }

  const collapsed = collapseDiff(diffLines);

  const added = diffLines.filter(l => l.op === "insert").length;
  const removed = diffLines.filter(l => l.op === "delete").length;

  return (
    <div className="flex-1 overflow-auto font-mono text-xs">
      <div className="sticky top-0 bg-muted/80 border-b px-4 py-1.5 text-xs flex gap-3 z-10 backdrop-blur-sm">
        <span className="text-muted-foreground mr-1">{oldLabel} → {newLabel}</span>
        <span className="text-green-600 dark:text-green-400">+{added} added</span>
        <span className="text-red-600 dark:text-red-400">−{removed} removed</span>
      </div>

      <table className="w-full border-collapse">
        <colgroup>
          <col className="w-10" />
          <col className="w-10" />
          <col />
        </colgroup>
        <tbody>
          {collapsed.map((item, idx) => {
            if ("type" in item) {
              return (
                <tr key={idx} className="bg-blue-50 dark:bg-blue-950/30">
                  <td colSpan={3} className="px-4 py-0.5 text-blue-500 dark:text-blue-400 select-none">
                    @@ {item.count} unchanged line{item.count !== 1 ? "s" : ""} hidden
                  </td>
                </tr>
              );
            }

            const { op, text, oldLineNo, newLineNo } = item;
            const rowCls =
              op === "insert"
                ? "bg-green-50 dark:bg-green-950/30"
                : op === "delete"
                  ? "bg-red-50 dark:bg-red-950/30"
                  : "";
            const gutterCls =
              op === "insert"
                ? "text-green-600 dark:text-green-400 bg-green-100 dark:bg-green-900/40"
                : op === "delete"
                  ? "text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/40"
                  : "text-muted-foreground";
            const prefix = op === "insert" ? "+" : op === "delete" ? "−" : " ";

            return (
              <tr key={idx} className={rowCls}>
                <td className={cn("px-2 text-right select-none border-r", gutterCls)}>
                  {oldLineNo ?? ""}
                </td>
                <td className={cn("px-2 text-right select-none border-r", gutterCls)}>
                  {newLineNo ?? ""}
                </td>
                <td className="px-3 py-px whitespace-pre-wrap break-all">
                  <span
                    className={
                      op === "insert"
                        ? "text-green-700 dark:text-green-300"
                        : op === "delete"
                          ? "text-red-700 dark:text-red-300"
                          : ""
                    }
                  >
                    {prefix} {text}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── localStorage draft helpers ────────────────────────────────────────────────

const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
const DRAFT_PREFIX = "scraper-draft:";

interface DraftEntry { yaml: string; savedAt: string; }

function isDraftEntry(v: unknown): v is DraftEntry {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).yaml === "string" &&
    typeof (v as Record<string, unknown>).savedAt === "string"
  );
}

function writeDraft(slug: string, yaml: string): void {
  try {
    const entry: DraftEntry = { yaml, savedAt: new Date().toISOString() };
    localStorage.setItem(`${DRAFT_PREFIX}${slug}`, JSON.stringify(entry));
  } catch { /* storage quota */ }
}

function readDraft(slug: string): string | null {
  try {
    const raw = localStorage.getItem(`${DRAFT_PREFIX}${slug}`);
    if (raw === null) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (isDraftEntry(parsed)) {
        const age = Date.now() - new Date(parsed.savedAt).getTime();
        if (isNaN(age) || age > DRAFT_TTL_MS) {
          localStorage.removeItem(`${DRAFT_PREFIX}${slug}`);
          return null; // expired or invalid date
        }
        return parsed.yaml;
      }
      // Valid JSON but not a DraftEntry (e.g. a legacy bare string like "foo",
      // a number, or a plain object without the right shape) — treat as current
      return raw;
    } catch {
      return raw; // not valid JSON — treat as current bare string
    }
  } catch { return null; }
}

function removeDraft(slug: string): void {
  try { localStorage.removeItem(`${DRAFT_PREFIX}${slug}`); } catch { /* ignore */ }
}

function pruneOldDrafts(): void {
  try {
    const toDelete: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(DRAFT_PREFIX)) continue;
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      try {
        const parsed: unknown = JSON.parse(raw);
        if (isDraftEntry(parsed)) {
          const age = Date.now() - new Date(parsed.savedAt).getTime();
          if (isNaN(age) || age > DRAFT_TTL_MS) toDelete.push(key);
        }
        // Non-DraftEntry JSON or bare string — leave it for readDraft to handle
      } catch { /* not valid JSON — keep */ }
    }
    toDelete.forEach(k => localStorage.removeItem(k));
  } catch { /* ignore */ }
}

// ── YAML key diff helpers ─────────────────────────────────────────────────────

function extractYamlKeys(yaml: string): Record<string, string> {
  const result: Record<string, string> = {};
  const lines = yaml.split("\n");
  const stack: { indent: number; path: string }[] = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || trimmed.startsWith("-")) continue;

    const indent = line.length - line.trimStart().length;
    const match = trimmed.match(/^([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.*?)(?:\s*#.*)?$/);
    if (!match) continue;

    const key = match[1];
    let value = match[2].trim();

    while (stack.length > 0 && stack[stack.length - 1].indent >= indent) {
      stack.pop();
    }

    const path = stack.length > 0 ? `${stack[stack.length - 1].path}.${key}` : key;

    if (path.split(".").length <= 3) {
      if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
      if (value.startsWith("'") && value.endsWith("'")) value = value.slice(1, -1);
      if (value && value !== "|" && value !== ">" && !value.startsWith("&")) {
        result[path] = value;
      }
      stack.push({ indent, path });
    }
  }

  return result;
}

interface KeyChange {
  path: string;
  changeType: "added" | "removed" | "modified";
  oldValue?: string;
  newValue?: string;
}

function computeKeyChanges(oldYaml: string, newYaml: string): KeyChange[] {
  const oldKeys = extractYamlKeys(oldYaml);
  const newKeys = extractYamlKeys(newYaml);
  const changes: KeyChange[] = [];
  const allPaths = new Set([...Object.keys(oldKeys), ...Object.keys(newKeys)]);

  for (const path of allPaths) {
    const inOld = path in oldKeys;
    const inNew = path in newKeys;
    if (!inOld) {
      changes.push({ path, changeType: "added", newValue: newKeys[path] });
    } else if (!inNew) {
      changes.push({ path, changeType: "removed", oldValue: oldKeys[path] });
    } else if (oldKeys[path] !== newKeys[path]) {
      changes.push({ path, changeType: "modified", oldValue: oldKeys[path], newValue: newKeys[path] });
    }
  }

  const order = { modified: 0, added: 1, removed: 2 };
  changes.sort((a, b) => {
    const diff = order[a.changeType] - order[b.changeType];
    return diff !== 0 ? diff : a.path.localeCompare(b.path);
  });

  return changes;
}

function ChangedKeysPanel({ oldYaml, newYaml }: { oldYaml: string; newYaml: string }) {
  const changes = useMemo(() => computeKeyChanges(oldYaml, newYaml), [oldYaml, newYaml]);

  if (changes.length === 0) return null;

  return (
    <div className="border-b bg-muted/20 flex-shrink-0">
      <div className="px-4 py-2 flex items-center gap-2 border-b bg-muted/30">
        <GitCompare className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
        <span className="text-xs font-medium text-muted-foreground">
          Changed keys
        </span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-medium ml-auto">
          {changes.length} {changes.length === 1 ? "key" : "keys"}
        </span>
      </div>
      <div className="max-h-40 overflow-y-auto px-4 py-2 flex flex-col gap-1">
        {changes.map((c) => (
          <div key={c.path} className="flex items-baseline gap-2 text-xs font-mono leading-relaxed">
            <span
              className={cn(
                "flex-shrink-0 w-4 text-center font-bold",
                c.changeType === "added" ? "text-green-600 dark:text-green-400" :
                c.changeType === "removed" ? "text-red-600 dark:text-red-400" :
                "text-amber-600 dark:text-amber-400"
              )}
            >
              {c.changeType === "added" ? "+" : c.changeType === "removed" ? "−" : "~"}
            </span>
            <span className="text-foreground/80 font-medium">{c.path}</span>
            {c.changeType === "modified" && c.oldValue !== undefined && c.newValue !== undefined && (
              <span className="flex items-center gap-1 text-muted-foreground">
                <span className="text-red-600 dark:text-red-400 line-through">{c.oldValue}</span>
                <span>→</span>
                <span className="text-green-600 dark:text-green-400">{c.newValue}</span>
              </span>
            )}
            {c.changeType === "added" && c.newValue !== undefined && (
              <span className="text-green-600 dark:text-green-400">{c.newValue}</span>
            )}
            {c.changeType === "removed" && c.oldValue !== undefined && (
              <span className="text-red-600 dark:text-red-400 line-through">{c.oldValue}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── History panel ─────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "unknown";
  const date = new Date(iso);
  const now = Date.now();
  const diffMs = now - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

function formatAbsoluteTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return date.toLocaleString();
}

function formatSavedBy(savedBy: string | null): string {
  if (!savedBy) return "unknown";
  if (savedBy.startsWith("restore:")) return `↩ restored by ${savedBy.slice(8)}`;
  return savedBy;
}

interface HistoryPanelProps {
  history: HistoryEntry[];
  loading: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  savedYaml: string;
  selectedEntry: HistoryEntry | null;
  compareEntry: HistoryEntry | null;
  onSelectEntry: (entry: HistoryEntry | null) => void;
  onSetCompareEntry: (entry: HistoryEntry | null) => void;
  onRestore: (entry: HistoryEntry) => void;
  onLoadMore: () => void;
  restoringId: number | null;
}

function HistoryPanel({ history, loading, hasMore, loadingMore, savedYaml, selectedEntry, compareEntry, onSelectEntry, onSetCompareEntry, onRestore, onLoadMore, restoringId }: HistoryPanelProps) {
  const selected = selectedEntry;
  const [search, setSearch] = useState("");
  const [keyFilter, setKeyFilter] = useState("");

  const filtered = useMemo(() => {
    let entries = history;
    const q = search.trim().toLowerCase();
    if (q) {
      entries = entries.filter(e => {
        const byUser = (e.saved_by ?? "").toLowerCase().includes(q);
        const byDate =
          formatAbsoluteTime(e.saved_at).toLowerCase().includes(q) ||
          formatRelativeTime(e.saved_at).toLowerCase().includes(q);
        return byUser || byDate;
      });
    }
    const k = keyFilter.trim();
    if (k) {
      entries = entries.filter(e => e.yaml_content.includes(k));
    }
    return entries;
  }, [history, search, keyFilter]);

  const isFiltered = search.trim() !== "" || keyFilter.trim() !== "";

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        Loading history…
      </div>
    );
  }

  if (history.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
        No save history yet — history is recorded each time you save.
      </div>
    );
  }

  return (
    <div className="flex-1 flex overflow-hidden">
      {/* Left: history list */}
      <div className="w-64 flex-shrink-0 border-r flex flex-col">
        {/* Search / filter bar */}
        <div className="p-2 border-b space-y-1.5 flex-shrink-0">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <Input
              className="h-7 pl-7 pr-7 text-xs"
              placeholder="Filter by user or date…"
              value={search}
              onChange={e => setSearch(e.target.value)}
            />
            {search && (
              <button
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => setSearch("")}
                aria-label="Clear search"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          <div className="relative">
            <Code className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
            <Input
              className="h-7 pl-7 pr-7 text-xs"
              placeholder="YAML key (e.g. default_ielts)…"
              value={keyFilter}
              onChange={e => setKeyFilter(e.target.value)}
            />
            {keyFilter && (
              <button
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => setKeyFilter("")}
                aria-label="Clear key filter"
              >
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          {isFiltered && (
            <p className="text-[10px] text-muted-foreground text-center">
              {filtered.length} of {history.length} {history.length === 1 ? "entry" : "entries"}
            </p>
          )}
        </div>

        {/* Shift-click hint */}
        <div className="px-3 py-1.5 border-b bg-muted/20 flex-shrink-0">
          <p className="text-[10px] text-muted-foreground leading-tight">
            Click to compare vs. current.{" "}
            <span className="font-medium">Shift-click</span> a second entry to compare two versions.
          </p>
        </div>

        {/* Entry list */}
        <div className="flex-1 overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              No entries match your filter.
            </div>
          ) : (
            filtered.map((entry) => {
              const isSelected = selected?.id === entry.id;
              const isCompare = compareEntry?.id === entry.id;
              const isCurrent = entry.id === history[0]?.id;
              const isHighlighted = isSelected || isCompare;
              return (
                <button
                  key={entry.id}
                  onClick={(e) => {
                    if (e.shiftKey) {
                      if (isCompare) {
                        onSetCompareEntry(null);
                      } else if (isSelected) {
                        onSelectEntry(null);
                        onSetCompareEntry(null);
                      } else if (selected) {
                        onSetCompareEntry(entry);
                      } else {
                        onSelectEntry(entry);
                      }
                    } else {
                      if (isSelected && !compareEntry) {
                        onSelectEntry(null);
                      } else {
                        onSelectEntry(entry);
                        onSetCompareEntry(null);
                      }
                    }
                  }}
                  className={cn(
                    "w-full text-left px-3 py-2.5 border-b last:border-b-0 transition-colors",
                    isSelected ? "bg-blue-50 dark:bg-blue-950/30" :
                    isCompare ? "bg-purple-50 dark:bg-purple-950/30" :
                    "hover:bg-muted/50",
                  )}
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-xs font-medium truncate" title={formatAbsoluteTime(entry.saved_at)}>
                      {formatRelativeTime(entry.saved_at)}
                    </span>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      {isSelected && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium">
                          A
                        </span>
                      )}
                      {isCompare && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300 font-medium">
                          B
                        </span>
                      )}
                      {isCurrent && !isHighlighted && (
                        <span className="text-[10px] px-1 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300">
                          latest
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="text-[11px] text-muted-foreground truncate mt-0.5">
                    {formatSavedBy(entry.saved_by)}
                  </div>
                  <div className="text-[10px] text-muted-foreground/70 mt-0.5">
                    {entry.yaml_content.split("\n").length} lines
                  </div>
                </button>
              );
            })
          )}
        </div>

        {/* Load more */}
        {hasMore && (
          <div className="p-2 border-t flex-shrink-0">
            <Button
              size="sm"
              variant="outline"
              className="w-full h-7 text-xs"
              onClick={onLoadMore}
              disabled={loadingMore}
            >
              {loadingMore
                ? <><Loader2 className="h-3 w-3 mr-1 animate-spin" />Loading…</>
                : "Load more"}
            </Button>
          </div>
        )}
      </div>

      {/* Right: diff or prompt */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {selected && compareEntry ? (() => {
          const aDate = selected.saved_at ? new Date(selected.saved_at).getTime() : 0;
          const bDate = compareEntry.saved_at ? new Date(compareEntry.saved_at).getTime() : 0;
          const oldEntry = aDate <= bDate ? selected : compareEntry;
          const newEntry = aDate <= bDate ? compareEntry : selected;
          return (
            <>
              <div className="px-3 py-2 border-b flex items-center justify-between bg-muted/30 gap-2 flex-wrap">
                <div className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <span className="inline-flex items-center gap-1">
                    <span className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium text-[10px]">A</span>
                    <span className="font-medium" title={formatAbsoluteTime(oldEntry.saved_at)}>{formatRelativeTime(oldEntry.saved_at)}</span>
                  </span>
                  <span className="text-muted-foreground/50">→</span>
                  <span className="inline-flex items-center gap-1">
                    <span className="px-1 py-0.5 rounded bg-purple-100 dark:bg-purple-900 text-purple-700 dark:text-purple-300 font-medium text-[10px]">B</span>
                    <span className="font-medium" title={formatAbsoluteTime(newEntry.saved_at)}>{formatRelativeTime(newEntry.saved_at)}</span>
                  </span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 text-[11px] gap-1 text-muted-foreground"
                    onClick={() => onSetCompareEntry(null)}
                  >
                    <X className="w-3 h-3" />
                    Clear B
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 text-[11px] gap-1"
                    onClick={() => onRestore(oldEntry)}
                    disabled={restoringId !== null}
                  >
                    {restoringId === oldEntry.id
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <RotateCcw className="w-3 h-3" />}
                    {restoringId === oldEntry.id ? "Restoring…" : "Restore A"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 text-[11px] gap-1"
                    onClick={() => onRestore(newEntry)}
                    disabled={restoringId !== null}
                  >
                    {restoringId === newEntry.id
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <RotateCcw className="w-3 h-3" />}
                    {restoringId === newEntry.id ? "Restoring…" : "Restore B"}
                  </Button>
                </div>
              </div>
              <ChangedKeysPanel oldYaml={oldEntry.yaml_content} newYaml={newEntry.yaml_content} />
              <DiffViewer
                oldYaml={oldEntry.yaml_content}
                newYaml={newEntry.yaml_content}
                oldLabel={`${formatRelativeTime(oldEntry.saved_at)} (older)`}
                newLabel={`${formatRelativeTime(newEntry.saved_at)} (newer)`}
              />
            </>
          );
        })() : selected ? (
          <>
            <div className="px-3 py-2 border-b flex items-center justify-between bg-muted/30">
              <div className="text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1">
                  <span className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium text-[10px]">A</span>
                  <span className="font-medium" title={formatAbsoluteTime(selected.saved_at)}>
                    {formatRelativeTime(selected.saved_at)}
                  </span>
                </span>
                {" · "}
                {formatSavedBy(selected.saved_by)}
              </div>
              <Button
                size="sm"
                variant="outline"
                className="h-6 text-[11px] gap-1"
                onClick={() => onRestore(selected)}
                disabled={restoringId === selected.id}
              >
                {restoringId === selected.id
                  ? <Loader2 className="w-3 h-3 animate-spin" />
                  : <RotateCcw className="w-3 h-3" />}
                {restoringId === selected.id ? "Restoring…" : "Restore this version"}
              </Button>
            </div>
            <ChangedKeysPanel oldYaml={selected.yaml_content} newYaml={savedYaml} />
            <DiffViewer
              oldYaml={selected.yaml_content}
              newYaml={savedYaml}
              oldLabel={formatRelativeTime(selected.saved_at)}
              newLabel="current saved"
            />
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm px-6 text-center">
            Select a history entry to compare it against the current saved version
          </div>
        )}
      </div>
    </div>
  );
}

// ── Debugger Panel ────────────────────────────────────────────────────────────

const SOURCE_META: Record<string, { label: string; color: string; dot: string }> = {
  defaults_yaml:  { label: "defaults.yaml",  color: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",   dot: "bg-slate-400" },
  db_legacy:      { label: "DB (legacy)",     color: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",       dot: "bg-blue-400" },
  db_auto:        { label: "DB (auto)",       color: "bg-cyan-100 text-cyan-700 dark:bg-cyan-900 dark:text-cyan-300",       dot: "bg-cyan-500" },
  yaml:           { label: "YAML file",       color: "bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300",   dot: "bg-green-500" },
  admin_config:   { label: "Admin override",  color: "bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300", dot: "bg-orange-500" },
};

const SEVERITY_META = {
  critical: { color: "text-red-600 dark:text-red-400",    bg: "bg-red-50 border-red-200 dark:bg-red-950/30 dark:border-red-800",    icon: <AlertCircle className="h-3.5 w-3.5 text-red-500 flex-shrink-0" /> },
  warning:  { color: "text-amber-600 dark:text-amber-400", bg: "bg-amber-50 border-amber-200 dark:bg-amber-950/30 dark:border-amber-800", icon: <TriangleAlert className="h-3.5 w-3.5 text-amber-500 flex-shrink-0" /> },
  info:     { color: "text-blue-600 dark:text-blue-400",  bg: "bg-blue-50 border-blue-200 dark:bg-blue-950/30 dark:border-blue-800",  icon: <Info className="h-3.5 w-3.5 text-blue-500 flex-shrink-0" /> },
};

function AnnotatedConfigTree({
  data,
  depth = 0,
  expandedSections,
  setExpandedSections,
  path = "",
}: {
  data: Record<string, unknown>;
  depth?: number;
  expandedSections: Record<string, boolean>;
  setExpandedSections: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  path?: string;
}) {
  return (
    <div className={cn("space-y-0.5", depth > 0 && "ml-4 border-l border-muted/50 pl-2")}>
      {Object.entries(data).map(([k, v]) => {
        const fullPath = path ? `${path}.${k}` : k;
        // Leaf: {value, source}
        if (v !== null && typeof v === "object" && "value" in (v as object) && "source" in (v as object)) {
          const leaf = v as { value: unknown; source: string };
          const sm = SOURCE_META[leaf.source] ?? SOURCE_META.defaults_yaml;
          const displayVal = typeof leaf.value === "object" ? JSON.stringify(leaf.value) : String(leaf.value ?? "");
          return (
            <div key={k} className="flex items-baseline gap-2 py-0.5 hover:bg-muted/30 rounded px-1 group">
              <span className="font-mono text-[11px] text-muted-foreground w-40 flex-shrink-0 truncate" title={k}>{k}</span>
              <span className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0 text-[10px] font-medium flex-shrink-0", sm.color)}>
                <span className={cn("w-1.5 h-1.5 rounded-full flex-shrink-0", sm.dot)} />
                {sm.label}
              </span>
              <span className="font-mono text-[11px] flex-1 truncate" title={displayVal}>{displayVal}</span>
            </div>
          );
        }
        // Section: nested dict
        if (v !== null && typeof v === "object" && !Array.isArray(v)) {
          const isOpen = expandedSections[fullPath] !== false; // default open
          return (
            <div key={k}>
              <button
                className="flex items-center gap-1 py-0.5 px-1 w-full text-left hover:bg-muted/30 rounded group"
                onClick={() => setExpandedSections(prev => ({ ...prev, [fullPath]: !isOpen }))}
              >
                {isOpen ? <ChevronDown className="h-3 w-3 text-muted-foreground" /> : <ChevronUp className="h-3 w-3 text-muted-foreground" style={{ transform: "rotate(180deg)" }} />}
                <span className="font-mono text-[11px] font-semibold text-foreground">{k}</span>
                <span className="text-[10px] text-muted-foreground ml-1">{Object.keys(v as object).length} keys</span>
              </button>
              {isOpen && (
                <AnnotatedConfigTree
                  data={v as Record<string, unknown>}
                  depth={depth + 1}
                  expandedSections={expandedSections}
                  setExpandedSections={setExpandedSections}
                  path={fullPath}
                />
              )}
            </div>
          );
        }
        // Fallback scalar
        return (
          <div key={k} className="flex items-baseline gap-2 py-0.5 px-1">
            <span className="font-mono text-[11px] text-muted-foreground w-40 flex-shrink-0 truncate">{k}</span>
            <span className="font-mono text-[11px]">{String(v ?? "")}</span>
          </div>
        );
      })}
    </div>
  );
}

interface DebuggerPanelProps {
  uniId: number | null;
  uniName: string;
  effectiveCfg: EffectiveConfigResult | null;
  effectiveCfgLoading: boolean;
  rejectionLog: RejectionLogResult | null;
  rejectionLogLoading: boolean;
  rejectionFilter: string | null;
  setRejectionFilter: (v: string | null) => void;
  debugTab: "config" | "overrides" | "rejections" | "extraction" | "discovery" | "ai_analysis";
  setDebugTab: (v: "config" | "overrides" | "rejections" | "extraction" | "discovery" | "ai_analysis") => void;
  cfgExpandedSections: Record<string, boolean>;
  setCfgExpandedSections: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  clearingOverrideKey: string | null;
  clearingAllOverrides: boolean;
  onClearOverrideKey: (key: string) => void;
  onClearAllOverrides: () => void;
  onRefresh: () => void;
  // Extraction Debugger
  scrapedCourses: ScrapedCoursesResult | null;
  scrapedCoursesLoading: boolean;
  extractionTrace: ExtractionTraceResult | null;
  extractionTraceLoading: boolean;
  selectedCourseId: number | null;
  setSelectedCourseId: (id: number | null) => void;
  onLoadExtractionTrace: (courseId: number) => void;
  // Discovery Debugger
  discoveryStats: DiscoveryStatsResult | null;
  discoveryStatsLoading: boolean;
  testUrl: string;
  setTestUrl: (v: string) => void;
  urlTestResult: UrlTestResult | null;
  urlTestLoading: boolean;
  onTestUrl: () => void;
  // AI Root Cause Analysis
  aiAnalysis: AiRootCauseResult | null;
  aiAnalysisLoading: boolean;
  aiAnalysisApplying: boolean;
  fixJustApplied: boolean;
  onRunAiAnalysis: () => void;
  onApplySafeFix: (fix: AiSafeFix) => void;
  // Live Test Discovery (Item 4)
  testDiscoveryResult: TestDiscoveryResult | null;
  testDiscoveryLoading: boolean;
  onRunTestDiscovery: () => void;
  // Full Validation (Item 3)
  fullValidationResult: FullValidationResult | null;
  fullValidationLoading: boolean;
  onRunFullValidation: (urls: string[]) => void;
  // Config override conflict clearing
  onClearConflict: (conflict: ConfigConflict, adminRaw: Record<string, unknown>) => void;
}

function DebuggerPanel({
  uniId, uniName, effectiveCfg, effectiveCfgLoading,
  rejectionLog, rejectionLogLoading, rejectionFilter, setRejectionFilter,
  debugTab, setDebugTab, cfgExpandedSections, setCfgExpandedSections,
  clearingOverrideKey, clearingAllOverrides, onClearOverrideKey, onClearAllOverrides, onRefresh,
  scrapedCourses, scrapedCoursesLoading, extractionTrace, extractionTraceLoading,
  selectedCourseId, setSelectedCourseId, onLoadExtractionTrace,
  discoveryStats, discoveryStatsLoading, testUrl, setTestUrl, urlTestResult, urlTestLoading, onTestUrl,
  aiAnalysis, aiAnalysisLoading, aiAnalysisApplying, fixJustApplied,
  onRunAiAnalysis, onApplySafeFix,
  testDiscoveryResult, testDiscoveryLoading, onRunTestDiscovery,
  fullValidationResult, fullValidationLoading, onRunFullValidation,
  onClearConflict,
}: DebuggerPanelProps) {
  const [aiEvidenceExpanded, setAiEvidenceExpanded] = useState(false);
  const [tdExpandedSeeds, setTdExpandedSeeds] = useState<Record<number, boolean>>({});

  if (!uniId) {
    return (
      <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm p-8 text-center">
        <div>
          <Bug className="h-8 w-8 mx-auto mb-3 opacity-30" />
          <p>This config is not linked to a university.</p>
          <p className="text-xs mt-1">Add <code className="font-mono bg-muted px-1 rounded">{'# Hostname: www.example.edu.au'}</code> to the YAML to enable the debugger.</p>
        </div>
      </div>
    );
  }

  const loading = effectiveCfgLoading || rejectionLogLoading;

  // Filtered rejections
  const visibleRejections = rejectionFilter
    ? (rejectionLog?.rejections ?? []).filter(r => r.reason === rejectionFilter)
    : (rejectionLog?.rejections ?? []);

  const TAB_BUTTONS: { id: "config" | "overrides" | "rejections" | "extraction" | "discovery" | "ai_analysis"; label: string; icon: React.ReactNode; badge?: number; badgeColor?: string }[] = [
    { id: "config",      label: "Effective Config", icon: <Layers className="h-3.5 w-3.5" /> },
    { id: "overrides",   label: "Admin Overrides",  icon: <ShieldAlert className="h-3.5 w-3.5" />,
      badge: effectiveCfg?.admin_overrides_flat?.length,
      badgeColor: effectiveCfg?.has_admin_overrides ? "bg-orange-500" : undefined },
    { id: "rejections",  label: "Rejection Log",    icon: <Filter className="h-3.5 w-3.5" />,
      badge: rejectionLog?.total,
      badgeColor: (rejectionLog?.total ?? 0) > 0 ? "bg-red-500" : undefined },
    { id: "extraction",  label: "Extraction",       icon: <Code className="h-3.5 w-3.5" />,
      badge: scrapedCourses?.courses?.length,
      badgeColor: (scrapedCourses?.courses?.length ?? 0) > 0 ? "bg-blue-500" : undefined },
    { id: "discovery",   label: "Discovery",        icon: <Search className="h-3.5 w-3.5" />,
      badge: discoveryStats ? (discoveryStats.summary.total_blocked_by_block_patterns + discoveryStats.summary.total_blocked_by_allow_patterns) : undefined,
      badgeColor: discoveryStats && (discoveryStats.summary.total_blocked_by_block_patterns + discoveryStats.summary.total_blocked_by_allow_patterns) > 0 ? "bg-amber-500" : undefined },
    { id: "ai_analysis", label: "AI Analysis",      icon: <Bot className="h-3.5 w-3.5" />,
      badge: aiAnalysis ? 1 : undefined,
      badgeColor: aiAnalysis
        ? (aiAnalysis.root_cause_category === "healthy" ? "bg-green-500"
          : aiAnalysis.risk_label === "developer_required" ? "bg-red-500"
          : "bg-violet-500")
        : undefined },
  ];

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-orange-50/30 dark:bg-orange-950/10">
      {/* Header */}
      <div className="px-4 py-2 border-b bg-orange-50 dark:bg-orange-950/30 flex items-center gap-3">
        <Bug className="h-4 w-4 text-orange-600 flex-shrink-0" />
        <div className="flex-1 min-w-0">
          <span className="text-xs font-semibold text-orange-900 dark:text-orange-200">Config Debugger</span>
          <span className="text-xs text-muted-foreground ml-2">→ {uniName}</span>
        </div>
        {/* Source legend */}
        <div className="hidden lg:flex items-center gap-2 flex-wrap">
          {Object.values(SOURCE_META).map(m => (
            <span key={m.label} className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0 text-[10px] font-medium", m.color)}>
              <span className={cn("w-1.5 h-1.5 rounded-full", m.dot)} />{m.label}
            </span>
          ))}
        </div>
        <button onClick={onRefresh} className="ml-2 p-1 rounded hover:bg-orange-100 dark:hover:bg-orange-900/40 text-orange-700 dark:text-orange-400" title="Refresh">
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
        </button>
      </div>

      {/* Sub-tabs */}
      <div className="flex border-b bg-background">
        {TAB_BUTTONS.map(tb => (
          <button
            key={tb.id}
            onClick={() => setDebugTab(tb.id)}
            className={cn(
              "flex items-center gap-1.5 px-4 py-2 text-xs border-b-2 transition-colors",
              debugTab === tb.id
                ? "border-orange-500 text-orange-700 dark:text-orange-300 font-medium"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tb.icon}
            {tb.label}
            {tb.badge != null && tb.badge > 0 && (
              <span className={cn("ml-0.5 inline-flex items-center justify-center rounded-full text-white text-[10px] px-1.5 min-w-[18px] h-[18px]", tb.badgeColor ?? "bg-muted-foreground")}>
                {tb.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-y-auto p-4">

        {/* ── Tab: Effective Config ─────────────────────────────────────── */}
        {debugTab === "config" && (
          <>
            {effectiveCfgLoading && (
              <div className="flex flex-col gap-3 animate-pulse">
                {[1,2,3,4].map(i => (
                  <div key={i} className="flex gap-3">
                    <div className="h-3 bg-muted rounded w-1/4" />
                    <div className="h-3 bg-muted rounded w-1/6" />
                    <div className="h-3 bg-muted/60 rounded w-1/3" />
                  </div>
                ))}
              </div>
            )}
            {!effectiveCfgLoading && !effectiveCfg && (
              <p className="text-sm text-muted-foreground">No data — click Refresh to load.</p>
            )}
            {effectiveCfg && !effectiveCfgLoading && (
              <div className="space-y-4">
                {/* Layers present */}
                <div className="flex flex-wrap gap-1.5 items-center">
                  <span className="text-xs text-muted-foreground mr-1">Active layers:</span>
                  {effectiveCfg.layers_present.map(l => {
                    const sm = SOURCE_META[l] ?? SOURCE_META.defaults_yaml;
                    return (
                      <span key={l} className={cn("inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium", sm.color)}>
                        <span className={cn("w-1.5 h-1.5 rounded-full", sm.dot)} />{sm.label}
                      </span>
                    );
                  })}
                  {effectiveCfg.has_yaml && (
                    <span className="text-[11px] text-muted-foreground ml-1">· YAML: <code className="font-mono">{effectiveCfg.yaml_slug}</code></span>
                  )}
                </div>
                {/* Tree */}
                <div className="border rounded-md bg-background overflow-hidden">
                  <div className="px-3 py-2 border-b bg-muted/20 text-[11px] text-muted-foreground font-medium tracking-wide">
                    MERGED SETTINGS (hover for source)
                  </div>
                  <div className="p-2 overflow-x-auto">
                    {Object.keys(effectiveCfg.annotated_config).length === 0
                      ? <p className="text-xs text-muted-foreground px-2 py-1">No settings found — only system defaults apply.</p>
                      : <AnnotatedConfigTree
                          data={effectiveCfg.annotated_config}
                          expandedSections={cfgExpandedSections}
                          setExpandedSections={setCfgExpandedSections}
                        />
                    }
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        {/* ── Tab: Admin Overrides ──────────────────────────────────────── */}
        {debugTab === "overrides" && (
          <>
            {effectiveCfgLoading && (
              <div className="flex flex-col gap-3 animate-pulse">
                {[1,2].map(i => (
                  <div key={i} className="h-10 bg-muted rounded w-full" />
                ))}
              </div>
            )}
            {!effectiveCfgLoading && effectiveCfg && (
              <>
                {!effectiveCfg.has_admin_overrides ? (
                  <div className="text-center py-8">
                    <CheckCircle2 className="h-8 w-8 mx-auto mb-2 text-green-500 opacity-60" />
                    <p className="text-sm text-muted-foreground">No admin overrides active.</p>
                    <p className="text-xs text-muted-foreground mt-1">Admin overrides are emergency fixes applied via the API — they take the highest priority and override the YAML file.</p>
                  </div>
                ) : (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <p className="text-xs text-muted-foreground">
                        Admin overrides take the <strong>highest priority</strong> — they override the YAML file and all other layers. Clear them when the underlying YAML has been fixed.
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs text-red-600 border-red-300 hover:bg-red-50 flex-shrink-0 ml-4"
                        onClick={onClearAllOverrides}
                        disabled={clearingAllOverrides}
                      >
                        {clearingAllOverrides ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> : <Trash className="h-3.5 w-3.5 mr-1" />}
                        Clear all
                      </Button>
                    </div>
                    <div className="border rounded-md overflow-hidden">
                      {effectiveCfg.admin_overrides_flat.map((ov, i) => (
                        <div
                          key={ov.path}
                          className={cn(
                            "flex items-center gap-3 px-3 py-2.5 text-sm",
                            i > 0 && "border-t",
                            "hover:bg-orange-50/60 dark:hover:bg-orange-950/20",
                          )}
                        >
                          <div className="flex-1 min-w-0">
                            <code className="font-mono text-xs text-orange-700 dark:text-orange-400">{ov.path}</code>
                            <span className="ml-3 text-xs text-muted-foreground font-mono truncate">
                              = {typeof ov.value === "object" ? JSON.stringify(ov.value) : String(ov.value ?? "")}
                            </span>
                          </div>
                          <button
                            className={cn(
                              "flex-shrink-0 text-muted-foreground hover:text-red-600 p-1 rounded transition-colors",
                              clearingOverrideKey === ov.path && "opacity-50 cursor-not-allowed",
                            )}
                            onClick={() => onClearOverrideKey(ov.path)}
                            disabled={clearingOverrideKey !== null}
                            title={`Remove "${ov.path}" override`}
                          >
                            {clearingOverrideKey === ov.path
                              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              : <X className="h-3.5 w-3.5" />}
                          </button>
                        </div>
                      ))}
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      After clearing, run a fresh scrape to confirm the YAML-only config behaves as expected.
                    </p>
                  </div>
                )}
              </>
            )}
          </>
        )}

        {/* ── Tab: Rejection Log ────────────────────────────────────────── */}
        {debugTab === "rejections" && (
          <>
            {rejectionLogLoading && (
              <div className="flex flex-col gap-3 animate-pulse">
                {[1,2,3].map(i => (
                  <div key={i} className="h-14 bg-muted rounded w-full" />
                ))}
              </div>
            )}
            {!rejectionLogLoading && rejectionLog && (
              <>
                {rejectionLog.total === 0 ? (
                  <div className="text-center py-8">
                    <CheckCircle2 className="h-8 w-8 mx-auto mb-2 text-green-500 opacity-60" />
                    <p className="text-sm text-muted-foreground">No rejections in the last scrape job.</p>
                    <p className="text-xs text-muted-foreground mt-1">Job ID: <code className="font-mono">{rejectionLog.job_id ?? "—"}</code></p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    <div className="flex items-center justify-between flex-wrap gap-2">
                      <span className="text-xs text-muted-foreground">
                        Job: <code className="font-mono">{rejectionLog.job_id ?? "—"}</code> · {rejectionLog.total} rejection{rejectionLog.total !== 1 ? "s" : ""}
                      </span>
                      {rejectionFilter && (
                        <button
                          className="text-xs text-blue-600 hover:underline flex items-center gap-1"
                          onClick={() => setRejectionFilter(null)}
                        >
                          <X className="h-3 w-3" /> Clear filter
                        </button>
                      )}
                    </div>

                    {/* Summary cards */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      {rejectionLog.summary.map(s => {
                        const sm2 = SEVERITY_META[s.severity] ?? SEVERITY_META.info;
                        const isActive = rejectionFilter === s.reason;
                        return (
                          <button
                            key={s.reason}
                            onClick={() => setRejectionFilter(isActive ? null : s.reason)}
                            className={cn(
                              "text-left border rounded-lg p-3 transition-all",
                              sm2.bg,
                              isActive && "ring-2 ring-orange-400",
                              "hover:opacity-90",
                            )}
                          >
                            <div className="flex items-center gap-2 mb-1">
                              {sm2.icon}
                              <span className={cn("text-xs font-semibold", sm2.color)}>{s.reason_label}</span>
                              <span className={cn("ml-auto text-lg font-bold leading-none", sm2.color)}>{s.count}</span>
                            </div>
                            {s.config_key && (
                              <div className="text-[11px] text-muted-foreground mt-1">
                                Fix: <code className="font-mono text-[11px]">{s.config_key}</code>
                              </div>
                            )}
                          </button>
                        );
                      })}
                    </div>

                    {/* Detail list */}
                    {visibleRejections.length > 0 && (
                      <div className="border rounded-md overflow-hidden">
                        <div className="px-3 py-1.5 border-b bg-muted/20 text-[11px] text-muted-foreground font-medium">
                          {rejectionFilter ? `Showing: ${rejectionLog.summary.find(s => s.reason === rejectionFilter)?.reason_label ?? rejectionFilter}` : "All rejections"} ({visibleRejections.length})
                        </div>
                        <div className="max-h-80 overflow-y-auto">
                          {visibleRejections.map((r, i) => {
                            const sm2 = SEVERITY_META[r.severity] ?? SEVERITY_META.info;
                            return (
                              <div key={i} className={cn("flex items-start gap-2 px-3 py-2 text-xs border-b last:border-b-0 hover:bg-muted/30")}>
                                {sm2.icon}
                                <div className="flex-1 min-w-0">
                                  {r.course_name && <div className="font-medium truncate">{r.course_name}</div>}
                                  {r.url && <div className="text-muted-foreground truncate font-mono text-[11px]">{r.url}</div>}
                                  <div className="text-muted-foreground mt-0.5">{r.description}</div>
                                  {r.config_key && (
                                    <div className="mt-0.5 text-[11px]">
                                      Fix key: <code className="font-mono bg-muted px-1 rounded">{r.config_key}</code>
                                    </div>
                                  )}
                                </div>
                                {r.ts && <span className="text-muted-foreground text-[10px] flex-shrink-0">{new Date(r.ts).toLocaleTimeString()}</span>}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
            {!rejectionLogLoading && !rejectionLog && (
              <p className="text-sm text-muted-foreground">No data — click Refresh to load.</p>
            )}
          </>
        )}

        {/* ── Tab: Extraction Debugger ───────────────────────────────────── */}
        {debugTab === "extraction" && (
          <div className="space-y-4">
            {/* Course list pane */}
            {!selectedCourseId ? (
              <>
                {scrapedCoursesLoading && (
                  <div className="flex flex-col gap-2 animate-pulse">
                    {[1,2,3,4,5].map(i => (
                      <div key={i} className="h-10 bg-muted rounded w-full" />
                    ))}
                  </div>
                )}
                {!scrapedCoursesLoading && !scrapedCourses && (
                  <div className="text-center py-8">
                    <Code className="h-8 w-8 mx-auto mb-2 opacity-30" />
                    <p className="text-sm text-muted-foreground">No data — click Refresh to load courses.</p>
                  </div>
                )}
                {!scrapedCoursesLoading && scrapedCourses && scrapedCourses.courses.length === 0 && (
                  <div className="text-center py-8">
                    <CheckCircle2 className="h-8 w-8 mx-auto mb-2 text-green-500 opacity-60" />
                    <p className="text-sm text-muted-foreground">No staged courses found for this university.</p>
                    <p className="text-xs text-muted-foreground mt-1">Job: <code className="font-mono">{scrapedCourses.job_id ?? "—"}</code></p>
                  </div>
                )}
                {scrapedCourses && scrapedCourses.courses.length > 0 && (
                  <>
                    <div className="flex items-center justify-between">
                      <span className="text-xs text-muted-foreground">
                        {scrapedCourses.courses.length} course{scrapedCourses.courses.length !== 1 ? "s" : ""} from last scrape job
                        {scrapedCourses.job_id && <> · <code className="font-mono">{scrapedCourses.job_id.slice(0, 12)}…</code></>}
                      </span>
                    </div>
                    <div className="border rounded-md overflow-hidden">
                      {scrapedCourses.courses.map((c, i) => {
                        const pct = c.completeness ?? 0;
                        const pctColor = pct >= 85 ? "text-green-600 dark:text-green-400" : pct >= 60 ? "text-amber-600 dark:text-amber-400" : "text-red-600 dark:text-red-400";
                        return (
                          <button
                            key={c.id}
                            onClick={() => { setSelectedCourseId(c.id); onLoadExtractionTrace(c.id); }}
                            className={cn(
                              "w-full text-left flex items-center gap-3 px-3 py-2 text-xs border-b last:border-b-0 hover:bg-blue-50/60 dark:hover:bg-blue-950/20 transition-colors",
                              i % 2 === 0 ? "" : "bg-muted/10",
                            )}
                          >
                            <div className="flex-1 min-w-0">
                              <span className="font-medium truncate block">{c.course_name}</span>
                              <span className="text-muted-foreground text-[11px]">{c.degree_level} · {c.study_mode ?? "—"} · {c.international_fee ? `$${c.international_fee.toLocaleString()}` : "no fee"}</span>
                            </div>
                            <span className={cn("text-[11px] font-mono font-semibold flex-shrink-0", pctColor)}>{pct}%</span>
                            <ChevronDown className="h-3 w-3 text-muted-foreground flex-shrink-0 rotate-[-90deg]" />
                          </button>
                        );
                      })}
                    </div>
                  </>
                )}
              </>
            ) : (
              /* Trace detail pane */
              <>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => { setSelectedCourseId(null); }}
                    className="flex items-center gap-1 text-xs text-blue-600 hover:underline"
                  >
                    <ChevronDown className="h-3 w-3 rotate-90" /> Back to course list
                  </button>
                </div>
                {extractionTraceLoading && (
                  <div className="flex flex-col gap-2 animate-pulse">
                    {[1,2,3,4,5,6].map(i => (
                      <div key={i} className="h-12 bg-muted rounded w-full" />
                    ))}
                  </div>
                )}
                {!extractionTraceLoading && extractionTrace && (
                  <>
                    <div className="flex items-center justify-between flex-wrap gap-2">
                      <div>
                        <p className="font-medium text-sm">{extractionTrace.course_name}</p>
                        <p className="text-[11px] text-muted-foreground mt-0.5">
                          Completeness: <span className={cn(
                            "font-semibold",
                            (extractionTrace.completeness ?? 0) >= 85 ? "text-green-600" : (extractionTrace.completeness ?? 0) >= 60 ? "text-amber-600" : "text-red-600",
                          )}>{extractionTrace.completeness ?? "?"}%</span>
                          {" · "}Status: <code className="font-mono">{extractionTrace.status}</code>
                          {extractionTrace.course_website && (
                            <> · <a href={extractionTrace.course_website} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate max-w-[20ch] inline-block align-bottom">source page ↗</a></>
                          )}
                        </p>
                      </div>
                    </div>
                    <div className="border rounded-md overflow-hidden">
                      <div className="grid grid-cols-[160px_1fr_1fr_90px] text-[10px] font-semibold text-muted-foreground uppercase tracking-wide px-3 py-1.5 border-b bg-muted/30">
                        <span>Field</span>
                        <span>Rule / Method</span>
                        <span>Final Value</span>
                        <span>Confidence</span>
                      </div>
                      <div className="max-h-[420px] overflow-y-auto">
                        {extractionTrace.pipeline.map((f, i) => {
                          const methodColor = f.extraction_method?.startsWith("gemini") ? "text-purple-600 dark:text-purple-400"
                            : f.extraction_method?.startsWith("regex") ? "text-blue-600 dark:text-blue-400"
                            : f.extraction_method?.startsWith("ai_") ? "text-indigo-600 dark:text-indigo-400"
                            : "text-slate-600 dark:text-slate-400";
                          return (
                            <div
                              key={f.field_key}
                              className={cn(
                                "grid grid-cols-[160px_1fr_1fr_90px] items-start gap-1 px-3 py-2 text-xs border-b last:border-b-0",
                                f.missing ? "bg-red-50/60 dark:bg-red-950/20" : i % 2 === 0 ? "" : "bg-muted/10",
                              )}
                              title={f.snippet ? `Snippet: ${f.snippet}` : undefined}
                            >
                              <span className={cn("font-medium text-[11px]", f.missing && "text-red-600 dark:text-red-400")}>
                                {f.field_label}
                                {f.candidates_count > 1 && (
                                  <span className="ml-1 text-[10px] text-muted-foreground">({f.candidates_count})</span>
                                )}
                              </span>
                              <span className={cn("font-mono text-[11px] truncate", methodColor)}>
                                {f.extraction_method ?? <span className="text-muted-foreground italic">—</span>}
                              </span>
                              <span className={cn("font-mono text-[11px] truncate", f.missing ? "text-muted-foreground italic" : "")}>
                                {f.missing ? "missing" : (f.final_value ?? "—")}
                              </span>
                              <span className="text-[11px] text-muted-foreground text-right">
                                {f.confidence != null ? `${Math.round(f.confidence * 100)}%` : "—"}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      Hover a row to see the raw snippet. <span className="font-mono text-purple-600">gemini</span> = AI extraction · <span className="font-mono text-blue-600">regex</span> = pattern · <span className="font-mono text-slate-600">*.rule</span> = rule-based.
                    </p>
                  </>
                )}
              </>
            )}
          </div>
        )}

        {/* ── Tab: Discovery Debugger ────────────────────────────────────── */}
        {debugTab === "discovery" && (
          <div className="space-y-4">
            {/* URL Tester */}
            <div className="border rounded-md overflow-hidden">
              <div className="px-3 py-2 border-b bg-muted/20 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide flex items-center gap-1.5">
                <Search className="h-3.5 w-3.5" /> URL Tester
              </div>
              <div className="p-3 space-y-2">
                <p className="text-xs text-muted-foreground">Test any URL against this university's current block, allow, and must_contain patterns.</p>
                <div className="flex gap-2">
                  <input
                    type="url"
                    value={testUrl}
                    onChange={e => setTestUrl(e.target.value)}
                    onKeyDown={e => e.key === "Enter" && onTestUrl()}
                    placeholder="https://www.university.edu.au/courses/master-of-science"
                    className="flex-1 h-8 rounded-md border px-3 text-xs bg-background focus:outline-none focus:ring-1 focus:ring-ring font-mono"
                  />
                  <Button size="sm" className="h-8 text-xs px-3" onClick={onTestUrl} disabled={urlTestLoading || !testUrl.trim()}>
                    {urlTestLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5 mr-1" />}
                    Test
                  </Button>
                </div>
                {urlTestResult && (
                  <div className={cn(
                    "border rounded-md p-3 mt-2 text-xs",
                    urlTestResult.accepted ? "bg-green-50 border-green-200 dark:bg-green-950/30 dark:border-green-800" : "bg-red-50 border-red-200 dark:bg-red-950/30 dark:border-red-800",
                  )}>
                    <div className="flex items-center gap-2 font-semibold mb-1">
                      {urlTestResult.accepted
                        ? <CheckCircle2 className="h-4 w-4 text-green-600" />
                        : <AlertCircle className="h-4 w-4 text-red-600" />}
                      <span className={urlTestResult.accepted ? "text-green-700 dark:text-green-300" : "text-red-700 dark:text-red-300"}>
                        {urlTestResult.accepted ? "ACCEPTED — URL passes all filters" : `BLOCKED by ${urlTestResult.blocked_by}`}
                      </span>
                    </div>
                    <p className="text-muted-foreground">{urlTestResult.reason}</p>
                    {urlTestResult.matched_pattern && (
                      <p className="mt-1">Pattern: <code className="font-mono bg-muted px-1 rounded">{urlTestResult.matched_pattern}</code></p>
                    )}
                    <div className="mt-2 grid grid-cols-3 gap-2 text-[11px]">
                      <div><span className="text-muted-foreground">Block patterns:</span> <strong>{urlTestResult.block_patterns.length}</strong></div>
                      <div><span className="text-muted-foreground">Allow patterns:</span> <strong>{urlTestResult.allow_patterns.length}</strong></div>
                      <div><span className="text-muted-foreground">Must contain:</span> <strong>{urlTestResult.must_contain.length}</strong></div>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Stats from last job */}
            {discoveryStatsLoading && (
              <div className="flex flex-col gap-2 animate-pulse">
                {[1,2,3].map(i => <div key={i} className="h-10 bg-muted rounded w-full" />)}
              </div>
            )}
            {!discoveryStatsLoading && !discoveryStats && (
              <p className="text-sm text-muted-foreground">No discovery stats — click Refresh to load.</p>
            )}
            {!discoveryStatsLoading && discoveryStats && (
              <div className="space-y-3">
                <div className="text-xs text-muted-foreground">
                  Job: <code className="font-mono">{discoveryStats.job_id?.slice(0, 16) ?? "—"}…</code>
                </div>

                {/* Summary tiles */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {[
                    { label: "Blocked (block patterns)", value: discoveryStats.summary.total_blocked_by_block_patterns, color: "text-red-600 dark:text-red-400" },
                    { label: "Blocked (allow whitelist)", value: discoveryStats.summary.total_blocked_by_allow_patterns, color: "text-amber-600 dark:text-amber-400" },
                    { label: "Blocked (must_contain)", value: discoveryStats.summary.total_blocked_by_must_contain, color: "text-orange-600 dark:text-orange-400" },
                    { label: "Pages classified", value: discoveryStats.summary.pages_classified, color: "text-blue-600 dark:text-blue-400" },
                  ].map(t => (
                    <div key={t.label} className="border rounded-md p-3 bg-background">
                      <div className={cn("text-2xl font-bold leading-none", t.color)}>{t.value}</div>
                      <div className="text-[11px] text-muted-foreground mt-1">{t.label}</div>
                    </div>
                  ))}
                </div>

                {/* Pattern breakdown */}
                {Object.keys(discoveryStats.summary.pattern_breakdown).length > 0 && (
                  <div className="border rounded-md overflow-hidden">
                    <div className="px-3 py-1.5 border-b bg-muted/20 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
                      Block Pattern Breakdown
                    </div>
                    <div className="divide-y max-h-48 overflow-y-auto">
                      {Object.entries(discoveryStats.summary.pattern_breakdown).map(([pat, cnt]) => (
                        <div key={pat} className="flex items-center gap-3 px-3 py-1.5 text-xs">
                          <code className="font-mono text-[11px] flex-1 truncate text-red-700 dark:text-red-400">{pat}</code>
                          <span className="font-semibold text-red-600 dark:text-red-400 flex-shrink-0">{cnt} blocked</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Sample blocked URLs */}
                {discoveryStats.summary.blocked_samples.length > 0 && (
                  <div className="border rounded-md overflow-hidden">
                    <div className="px-3 py-1.5 border-b bg-muted/20 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
                      Sample Blocked URLs (block_url_patterns)
                    </div>
                    <div className="divide-y max-h-40 overflow-y-auto">
                      {discoveryStats.summary.blocked_samples.slice(0, 15).map((url, i) => (
                        <div key={i} className="flex items-center gap-2 px-3 py-1 text-xs group">
                          <code className="font-mono text-[11px] flex-1 truncate text-muted-foreground">{url}</code>
                          <button
                            className="opacity-0 group-hover:opacity-100 text-xs text-blue-600 hover:underline flex-shrink-0"
                            onClick={() => setTestUrl(url)}
                          >
                            test ↑
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Allow-pattern drops */}
                {discoveryStats.summary.allow_dropped_samples.length > 0 && (
                  <div className="border rounded-md overflow-hidden">
                    <div className="px-3 py-1.5 border-b bg-amber-50 dark:bg-amber-950/20 border-amber-200 dark:border-amber-800 text-[11px] font-semibold text-amber-700 dark:text-amber-300 uppercase tracking-wide">
                      Dropped by allow_url_patterns whitelist ({discoveryStats.summary.allow_dropped_samples.length} sample)
                    </div>
                    <div className="divide-y max-h-40 overflow-y-auto">
                      {discoveryStats.summary.allow_dropped_samples.slice(0, 10).map((url, i) => (
                        <div key={i} className="flex items-center gap-2 px-3 py-1 text-xs group">
                          <code className="font-mono text-[11px] flex-1 truncate text-muted-foreground">{url}</code>
                          <button
                            className="opacity-0 group-hover:opacity-100 text-xs text-blue-600 hover:underline flex-shrink-0"
                            onClick={() => setTestUrl(url)}
                          >
                            test ↑
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ── Live Test Discovery ───────────────────────────────────────── */}
            <div className="border rounded-md overflow-hidden">
              <div className="flex items-center justify-between px-3 py-2 border-b bg-muted/30 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
                <span className="flex items-center gap-1.5"><Search className="h-3.5 w-3.5" /> Live Test Discovery</span>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs px-3"
                  onClick={onRunTestDiscovery}
                  disabled={testDiscoveryLoading}
                >
                  {testDiscoveryLoading
                    ? <><Loader2 className="h-3 w-3 mr-1 animate-spin" />Testing…</>
                    : <><Play className="h-3 w-3 mr-1" />Run Test</>}
                </Button>
              </div>
              {!testDiscoveryResult && !testDiscoveryLoading && (
                <p className="text-xs text-muted-foreground px-3 py-3">
                  Fetches seed URLs using the <em>current</em> config and classifies found links as course / listing / category pages.
                </p>
              )}
              {testDiscoveryLoading && (
                <div className="px-3 py-4 flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Fetching seed pages…
                </div>
              )}
              {!testDiscoveryLoading && testDiscoveryResult && (() => {
                const td = testDiscoveryResult;
                const conflicts = td.config_conflicts ?? [];
                const adminRaw = td.admin_config_raw ?? {};
                const safetyColors = {
                  safe: "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300",
                  warning: "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300",
                  dangerous: "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300",
                };
                return (
                  <div className="divide-y">
                    {/* ── Configuration Override Conflict warning ── */}
                    {conflicts.length > 0 && (
                      <div className="px-3 py-2.5 bg-red-50 dark:bg-red-950/30 border-b border-red-200 dark:border-red-800 space-y-2">
                        <div className="flex items-center gap-2">
                          <span className="text-red-600 dark:text-red-400 text-sm font-semibold">⚠ Configuration Override Conflict</span>
                          <span className="text-[10px] bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300 px-1.5 py-0.5 rounded font-mono">admin_config</span>
                        </div>
                        <p className="text-xs text-red-700 dark:text-red-300">
                          The database <code className="font-mono text-[11px]">admin_config</code> contains empty <code className="font-mono text-[11px]">[]</code> values for fields that your YAML defines. These stale empty overrides previously silenced YAML patterns entirely.
                        </p>
                        <div className="space-y-1.5">
                          {conflicts.map((c) => (
                            <div key={c.field} className="flex items-start justify-between gap-2 bg-red-100/60 dark:bg-red-900/30 rounded px-2 py-1.5">
                              <div className="space-y-0.5 min-w-0">
                                <div className="flex items-center gap-1.5">
                                  <code className="font-mono text-[11px] text-red-800 dark:text-red-200 font-semibold">{c.location}.{c.field}</code>
                                  <span className="text-[10px] text-red-600 dark:text-red-400">admin_config: [] overrides YAML: [{c.yaml_values.length} pattern{c.yaml_values.length !== 1 ? "s" : ""}]</span>
                                </div>
                                <div className="flex flex-wrap gap-1 mt-0.5">
                                  {c.yaml_values.slice(0, 3).map((v, i) => (
                                    <code key={i} className="text-[10px] bg-white/60 dark:bg-black/20 text-red-700 dark:text-red-300 px-1 py-0.5 rounded border border-red-200 dark:border-red-700 truncate max-w-[200px]">{v}</code>
                                  ))}
                                  {c.yaml_values.length > 3 && (
                                    <span className="text-[10px] text-red-500">+{c.yaml_values.length - 3} more</span>
                                  )}
                                </div>
                              </div>
                              <button
                                className="flex-shrink-0 text-[11px] font-medium text-white bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600 rounded px-2 py-1 transition-colors"
                                onClick={() => onClearConflict(c, adminRaw)}
                              >
                                Clear override
                              </button>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {/* Aggregated totals */}
                    <div className="px-3 py-2 flex flex-wrap gap-3 text-xs">
                      <span className="font-medium">{td.total_raw} raw</span>
                      <span className="text-green-700 dark:text-green-400 font-medium">→ {td.total_passing} passed filter</span>
                      {td.total_dropped > 0 && (
                        <span className="text-red-600 dark:text-red-400">{td.agg_drop_rate_pct}% dropped</span>
                      )}
                      <span className={cn("ml-auto inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium", safetyColors[td.safety_level])}>
                        {td.safety_level === "safe" ? "✅" : td.safety_level === "warning" ? "⚠️" : "🛑"} Score {td.safety_score}
                      </span>
                    </div>
                    {/* Per-seed results */}
                    {td.seed_results.map((sr, i) => (
                      <div key={i} className="px-3 py-2 text-xs space-y-1.5">
                        <div
                          className="flex items-center justify-between cursor-pointer"
                          onClick={() => setTdExpandedSeeds(s => ({ ...s, [i]: !s[i] }))}
                        >
                          <code className="font-mono text-[11px] truncate flex-1 text-muted-foreground max-w-xs">{sr.seed_url}</code>
                          <div className="flex items-center gap-2 ml-2 flex-shrink-0 flex-wrap">
                            {/* Raw breakdown — all discovered URLs by type */}
                            <span className="text-muted-foreground text-[10px]">{sr.raw_candidates} raw</span>
                            {(sr.raw_course_count ?? 0) > 0 && <span className="px-1.5 py-0.5 bg-blue-100 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 rounded text-[10px]">{sr.raw_course_count} course</span>}
                            {(sr.raw_listing_count ?? 0) > 0 && <span className="px-1.5 py-0.5 bg-amber-100 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 rounded text-[10px]">{sr.raw_listing_count} listing</span>}
                            {(sr.raw_category_count ?? 0) > 0 && <span className="px-1.5 py-0.5 bg-purple-100 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 rounded text-[10px]">{sr.raw_category_count} cat</span>}
                            {/* After-filter count */}
                            <span className="text-[10px]">→</span>
                            <span className={cn("text-[10px] font-medium", sr.after_filter > 0 ? "text-green-600 dark:text-green-400" : "text-red-500")}>
                              {sr.after_filter} pass filter
                            </span>
                            {sr.drop_rate_pct > 0 && <span className="text-orange-600 text-[10px]">{sr.drop_rate_pct}% dropped</span>}
                            {tdExpandedSeeds[i]
                              ? <ChevronUp className="h-3 w-3 text-muted-foreground" />
                              : <ChevronDown className="h-3 w-3 text-muted-foreground" />}
                          </div>
                        </div>
                        {tdExpandedSeeds[i] && (
                          <div className="space-y-1.5 pl-2 border-l border-muted">
                            {Object.entries(sr.classified_passing).map(([type, urls]) => (
                              <div key={type}>
                                <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground mb-0.5 capitalize">{type} ({urls.length})</p>
                                <div className="space-y-0.5">
                                  {urls.map((url, j) => (
                                    <div key={j} className="flex items-center gap-1.5 group">
                                      <code className="font-mono text-[10px] truncate flex-1 text-muted-foreground">{url}</code>
                                      <button
                                        className="opacity-0 group-hover:opacity-100 text-[10px] text-blue-600 hover:underline flex-shrink-0"
                                        onClick={() => setTestUrl(url)}
                                      >test ↑</button>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            ))}
                            {sr.sample_dropped.length > 0 && (
                              <div>
                                <p className="text-[10px] font-semibold uppercase tracking-wide text-red-500 mb-0.5">Dropped ({sr.dropped})</p>
                                <div className="space-y-0.5">
                                  {sr.sample_dropped.slice(0, 4).map((url, j) => (
                                    <code key={j} className="block font-mono text-[10px] truncate text-muted-foreground/70">{url}</code>
                                  ))}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                    {td.warnings && td.warnings.length > 0 && (
                      <div className="px-3 py-2 space-y-1">
                        {td.warnings.map((w, i) => (
                          <p key={i} className="text-[11px] text-amber-700 dark:text-amber-400 flex items-start gap-1">
                            <TriangleAlert className="h-3.5 w-3.5 flex-shrink-0 mt-0.5" />{w}
                          </p>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>

            {/* ── Full Validation ───────────────────────────────────────────── */}
            {testDiscoveryResult && testDiscoveryResult.total_passing > 0 && (
              <div className="border rounded-md overflow-hidden">
                <div className="flex items-center justify-between px-3 py-2 border-b bg-muted/30 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
                  <span className="flex items-center gap-1.5"><Layers className="h-3.5 w-3.5" /> Full Validation (up to 5 URLs)</span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-xs px-3"
                    disabled={fullValidationLoading}
                    onClick={() => {
                      const courseUrls = testDiscoveryResult.seed_results
                        .flatMap(sr => sr.classified_passing?.course ?? []);
                      const anyUrls = testDiscoveryResult.seed_results
                        .flatMap(sr => [
                          ...(sr.sample_passing ?? []),
                          ...((sr as any).browser_test?.sample_passing ?? []),
                        ]);
                      const urlsToTest = (courseUrls.length ? courseUrls : anyUrls)
                        .filter((u, i, a) => a.indexOf(u) === i)
                        .slice(0, 5);
                      onRunFullValidation(urlsToTest);
                    }}
                  >
                    {fullValidationLoading
                      ? <><Loader2 className="h-3 w-3 mr-1 animate-spin" />Validating…</>
                      : <><Play className="h-3 w-3 mr-1" />Validate</>}
                  </Button>
                </div>
                {!fullValidationResult && !fullValidationLoading && (
                  <p className="text-xs text-muted-foreground px-3 py-3">
                    Fetches up to 5 sample course pages from the live test above, applies the current filter, and estimates extractability.
                  </p>
                )}
                {fullValidationLoading && (
                  <div className="px-3 py-4 flex items-center gap-2 text-xs text-muted-foreground">
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    Fetching and analysing pages…
                  </div>
                )}
                {!fullValidationLoading && fullValidationResult && (() => {
                  const fv = fullValidationResult;
                  return (
                    <div className="divide-y">
                      {/* Summary row */}
                      <div className="px-3 py-2 flex flex-wrap gap-3 text-xs">
                        <span><strong>{fv.summary.passed_filter}</strong>/{fv.summary.total} pass filter</span>
                        <span className="text-blue-600 dark:text-blue-400"><strong>{fv.summary.course_pages}</strong> course pages</span>
                        {fv.summary.avg_course_completeness_pct > 0 && (
                          <span className={cn(
                            "font-medium",
                            fv.summary.avg_course_completeness_pct >= 60 ? "text-green-600 dark:text-green-400" : "text-orange-600 dark:text-orange-400",
                          )}>
                            ~{fv.summary.avg_course_completeness_pct}% avg extractability
                          </span>
                        )}
                      </div>
                      {/* Per-URL results */}
                      {fv.results.map((r, i) => {
                        const fieldRows: { label: string; ok: boolean; value?: string | null }[] = [
                          { label: "Course name", ok: r.course_name_extracted, value: r.course_name_value },
                          { label: "Fee", ok: r.fee_extracted, value: r.fee_value },
                          { label: "English req", ok: r.english_extracted, value: r.english_value },
                          { label: "Intake month", ok: r.intake_extracted },
                          { label: "Duration", ok: r.duration_extracted },
                          { label: "Degree level", ok: r.degree_level_extracted },
                        ];
                        return (
                        <div key={i} className="px-3 py-2.5 text-xs space-y-1.5">
                          <code className="font-mono text-[10px] text-muted-foreground truncate block mb-1">{r.url}</code>
                          {r.error && <p className="text-[10px] text-red-500">{r.error}</p>}
                          {/* Summary badges row */}
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className={cn(
                              "inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium",
                              r.passes_filter ? "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300" : "bg-red-100 text-red-600 dark:bg-red-950/40 dark:text-red-400",
                            )}>{r.passes_filter ? "✓ Passes URL filter" : `✗ Blocked (${r.blocked_by ?? "filter"})`}</span>
                            <span className={cn(
                              "inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium",
                              r.page_type === "course" ? "bg-blue-100 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300" :
                              r.page_type === "listing" ? "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300" :
                              "bg-muted text-muted-foreground",
                            )}>Page: {r.page_type}</span>
                            {r.ok && (
                              <span className={cn(
                                "inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium",
                                r.completeness_pct >= 67 ? "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300" : r.completeness_pct >= 33 ? "bg-orange-100 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300" : "bg-red-100 text-red-600",
                              )}>{r.fields_found}/{r.fields_total} fields</span>
                            )}
                          </div>
                          {/* Field extraction grid */}
                          {r.ok && (
                            <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 pl-1 border-l-2 border-muted">
                              {fieldRows.map(f => (
                                <div key={f.label} className="flex items-center gap-1 text-[10px]">
                                  <span className={f.ok ? "text-green-600 dark:text-green-400" : "text-muted-foreground/50"}>
                                    {f.ok ? "✓" : "✗"}
                                  </span>
                                  <span className={f.ok ? "text-foreground" : "text-muted-foreground/60"}>{f.label}</span>
                                  {f.ok && f.value && (
                                    <span className="text-muted-foreground/70 truncate ml-0.5 italic">{f.value}</span>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                          {/* Will stage verdict */}
                          <div className={cn(
                            "flex items-start gap-1.5 px-2 py-1 rounded text-[10px] font-medium",
                            r.will_stage ? "bg-green-50 dark:bg-green-950/30 text-green-700 dark:text-green-300" : "bg-red-50 dark:bg-red-950/30 text-red-600 dark:text-red-400",
                          )}>
                            <span className="flex-shrink-0">{r.will_stage ? "✅" : "❌"}</span>
                            <span>
                              {r.will_stage
                                ? "Will stage — passes filter, is a course page, has enough fields"
                                : `Will not stage${r.rejection_reason ? `: ${r.rejection_reason}` : ""}`}
                            </span>
                          </div>
                        </div>
                        );
                      })}
                    </div>
                  );
                })()}
              </div>
            )}
          </div>
        )}

        {/* ── Tab: AI Root Cause Analysis ──────────────────────────────────── */}
        {debugTab === "ai_analysis" && (
          <div className="space-y-4">
            {/* Run button */}
            <div className="flex items-center gap-3">
              <Button
                size="sm"
                className="h-8 text-xs px-4 bg-violet-600 hover:bg-violet-700 text-white"
                onClick={onRunAiAnalysis}
                disabled={aiAnalysisLoading}
              >
                {aiAnalysisLoading
                  ? <><Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />Analysing…</>
                  : <><Bot className="h-3.5 w-3.5 mr-1.5" />{aiAnalysis ? "Re-run Analysis" : "Run AI Analysis"}</>}
              </Button>
              {!aiAnalysis && !aiAnalysisLoading && (
                <p className="text-xs text-muted-foreground">
                  Reads all 7 data sources — config, overrides, rejection log, discovery, extraction, job stats, and YAML history — then identifies the root cause.
                </p>
              )}
            </div>

            {/* Fix just applied — persists until operator manually re-runs analysis */}
            {fixJustApplied && !aiAnalysisLoading && (
              <div className="flex items-start gap-2.5 p-3 rounded-md border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/20 text-xs">
                <CheckCircle2 className="h-4 w-4 flex-shrink-0 mt-0.5 text-amber-600 dark:text-amber-400" />
                <div>
                  <p className="font-semibold text-amber-800 dark:text-amber-200 mb-0.5">Fix saved but not validated yet</p>
                  <p className="text-amber-700 dark:text-amber-400">The config change has been saved. Trigger a new scrape to confirm the fix worked — the analysis below reflects the <em>previous</em> scrape run.</p>
                </div>
              </div>
            )}

            {/* Loading skeleton */}
            {aiAnalysisLoading && (
              <div className="space-y-3 animate-pulse">
                <div className="h-16 bg-muted rounded-md w-full" />
                <div className="grid grid-cols-3 gap-2">
                  {[1,2,3].map(i => <div key={i} className="h-8 bg-muted rounded" />)}
                </div>
                <div className="h-24 bg-muted rounded-md w-full" />
                <div className="h-32 bg-muted rounded-md w-full" />
              </div>
            )}

            {/* Result */}
            {!aiAnalysisLoading && aiAnalysis && (() => {
              const cat = aiAnalysis.root_cause_category;
              const healthy = cat === "healthy";
              const devRequired = aiAnalysis.developer_required;
              const risk = aiAnalysis.risk_label;

              const categoryColors: Record<string, string> = {
                healthy:       "bg-green-100 text-green-800 dark:bg-green-950/40 dark:text-green-300 border-green-200 dark:border-green-800",
                discovery:     "bg-amber-100 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300 border-amber-200 dark:border-amber-800",
                filtering:     "bg-orange-100 text-orange-800 dark:bg-orange-950/40 dark:text-orange-300 border-orange-200 dark:border-orange-800",
                extraction:    "bg-blue-100 text-blue-800 dark:bg-blue-950/40 dark:text-blue-300 border-blue-200 dark:border-blue-800",
                config_conflict: "bg-red-100 text-red-800 dark:bg-red-950/40 dark:text-red-300 border-red-200 dark:border-red-800",
                staging_gate:  "bg-purple-100 text-purple-800 dark:bg-purple-950/40 dark:text-purple-300 border-purple-200 dark:border-purple-800",
                api:           "bg-sky-100 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300 border-sky-200 dark:border-sky-800",
                pdf:           "bg-teal-100 text-teal-800 dark:bg-teal-950/40 dark:text-teal-300 border-teal-200 dark:border-teal-800",
                browser:       "bg-indigo-100 text-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-300 border-indigo-200 dark:border-indigo-800",
              };
              const riskColors: Record<string, string> = {
                low:                "bg-green-100 text-green-700 dark:bg-green-950/40 dark:text-green-300",
                medium:             "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300",
                developer_required: "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300",
              };
              const evidenceTypeIcon: Record<string, string> = {
                job_stat:   "📊",
                rejection:  "🚫",
                config:     "⚙️",
                alert:      "🔔",
                extraction: "🔍",
                discovery:  "🌐",
              };

              return (
                <div className="space-y-3">
                  {/* Stale config warning — last scrape ran before config was saved */}
                  {aiAnalysis.config_is_stale && (
                    <div className="flex items-start gap-2.5 p-3 rounded-md border border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-950/20 text-xs">
                      <TriangleAlert className="h-4 w-4 flex-shrink-0 mt-0.5 text-yellow-600 dark:text-yellow-400" />
                      <div className="min-w-0">
                        <p className="font-semibold text-yellow-800 dark:text-yellow-200 mb-0.5">Analysis may reflect old config</p>
                        <p className="text-yellow-700 dark:text-yellow-400">
                          Last scrape: <strong>{aiAnalysis.last_job_created_at}</strong> · Config last saved: <strong>{aiAnalysis.config_last_saved_at}</strong>.{" "}
                          The config changed after the last scrape — job statistics may not match the current config. The AI used a live filter simulation to compensate.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Issue summary banner */}
                  <div className={cn(
                    "border rounded-md p-4",
                    healthy
                      ? "bg-green-50 border-green-200 dark:bg-green-950/20 dark:border-green-800"
                      : devRequired
                        ? "bg-red-50 border-red-200 dark:bg-red-950/20 dark:border-red-800"
                        : "bg-violet-50 border-violet-200 dark:bg-violet-950/20 dark:border-violet-800",
                  )}>
                    <div className="flex items-start gap-3">
                      <span className="text-2xl flex-shrink-0 mt-0.5">
                        {healthy ? "✅" : devRequired ? "🛑" : "⚠️"}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-semibold leading-snug">{aiAnalysis.issue_summary}</p>
                        <div className="flex flex-wrap gap-2 mt-2">
                          <span className={cn("inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium border", categoryColors[cat] ?? categoryColors.discovery)}>
                            {cat.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}
                          </span>
                          <span className={cn("inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium", riskColors[risk] ?? riskColors.medium)}>
                            {risk === "developer_required" ? "🛑 Developer Required" : risk === "low" ? "✅ Low Risk" : "⚠️ Medium Risk"}
                          </span>
                          <span className="inline-flex items-center rounded px-2 py-0.5 text-[11px] font-medium bg-muted text-muted-foreground">
                            {aiAnalysis.confidence === "high" ? "🔵" : aiAnalysis.confidence === "medium" ? "🟡" : "🔴"} {aiAnalysis.confidence} confidence
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Evidence */}
                  <div className="border rounded-md overflow-hidden">
                    <button
                      className="w-full flex items-center justify-between px-3 py-2 bg-muted/20 hover:bg-muted/30 transition-colors"
                      onClick={() => setAiEvidenceExpanded(v => !v)}
                    >
                      <span className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wide flex items-center gap-1.5">
                        <Info className="h-3.5 w-3.5" /> Evidence ({aiAnalysis.evidence.length} items)
                      </span>
                      {aiEvidenceExpanded ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" /> : <ChevronUp className="h-3.5 w-3.5 text-muted-foreground rotate-180" />}
                    </button>
                    {aiEvidenceExpanded && (
                      <div className="divide-y">
                        {aiAnalysis.evidence.map((ev, i) => (
                          <div key={i} className="flex items-start gap-3 px-3 py-2 text-xs">
                            <span className="flex-shrink-0 text-sm">{evidenceTypeIcon[ev.type] ?? "•"}</span>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-baseline gap-2">
                                <span className="font-medium text-foreground">{ev.label}</span>
                                <span className="text-[10px] text-muted-foreground">{ev.source}</span>
                              </div>
                              <code className="text-[11px] text-muted-foreground font-mono break-all">{ev.value}</code>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Fix recommendation */}
                  <div className="border rounded-md overflow-hidden">
                    <div className="px-3 py-1.5 border-b bg-muted/20 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide flex items-center gap-1.5">
                      <Wand2 className="h-3.5 w-3.5" /> Recommended Fix
                    </div>
                    <div className="p-3 space-y-3">
                      <p className="text-xs leading-relaxed">{aiAnalysis.fix_recommendation}</p>

                      {/* YAML snippet */}
                      {aiAnalysis.fix_yaml_snippet && (
                        <div>
                          <p className="text-[11px] text-muted-foreground mb-1">Add to per-uni YAML config:</p>
                          <pre className="text-[11px] font-mono bg-muted/30 rounded p-3 overflow-x-auto whitespace-pre-wrap leading-relaxed border">
                            {aiAnalysis.fix_yaml_snippet}
                          </pre>
                        </div>
                      )}

                      {/* Developer note */}
                      {devRequired && aiAnalysis.developer_note && (
                        <div className="flex items-start gap-2 p-2.5 rounded-md bg-red-50 border border-red-200 dark:bg-red-950/20 dark:border-red-800 text-xs text-red-800 dark:text-red-300">
                          <TriangleAlert className="h-4 w-4 flex-shrink-0 mt-0.5" />
                          <div>
                            <p className="font-semibold mb-0.5">Developer Required</p>
                            <p>{aiAnalysis.developer_note}</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* One-click safe fix */}
                  {aiAnalysis.safe_fix && !devRequired && (
                    <div className={cn(
                      "border rounded-md p-4",
                      risk === "low"
                        ? "bg-green-50 border-green-200 dark:bg-green-950/20 dark:border-green-800"
                        : "bg-amber-50 border-amber-200 dark:bg-amber-950/20 dark:border-amber-800",
                    )}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-semibold mb-0.5 flex items-center gap-1.5">
                            <CheckCircle2 className="h-3.5 w-3.5 text-green-600 dark:text-green-400" />
                            One-Click Safe Fix
                            <span className={cn("inline-flex items-center rounded px-1.5 py-0 text-[10px] font-medium ml-1", riskColors[risk] ?? riskColors.medium)}>
                              {risk === "low" ? "Low risk" : "Medium risk"}
                            </span>
                          </p>
                          <p className="text-xs text-muted-foreground">{aiAnalysis.safe_fix.description}</p>
                          <p className="text-[11px] mt-1 font-mono text-muted-foreground">
                            Action: <strong>{aiAnalysis.safe_fix.action}</strong>
                            {" · "}Key: <code>{aiAnalysis.safe_fix.key}</code>
                            {aiAnalysis.safe_fix.value != null && <> · Value: <code>{String(aiAnalysis.safe_fix.value)}</code></>}
                          </p>
                        </div>
                        <Button
                          size="sm"
                          className={cn(
                            "h-8 text-xs px-4 flex-shrink-0",
                            risk === "low"
                              ? "bg-green-600 hover:bg-green-700 text-white"
                              : "bg-amber-600 hover:bg-amber-700 text-white",
                          )}
                          onClick={() => onApplySafeFix(aiAnalysis.safe_fix!)}
                          disabled={aiAnalysisApplying}
                        >
                          {aiAnalysisApplying
                            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            : <><CheckCircle2 className="h-3.5 w-3.5 mr-1" />Apply Fix</>}
                        </Button>
                      </div>
                    </div>
                  )}

                  {/* Data sources used */}
                  <div className="text-[10px] text-muted-foreground flex flex-wrap gap-1.5 items-center pt-1">
                    <span>Data sources:</span>
                    {aiAnalysis.context_used.map(s => (
                      <span key={s} className="inline-flex items-center rounded px-1.5 py-0 bg-muted text-[10px]">
                        {s.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })()}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────


export default function SettingsScraperConfigs() {
  const { toast } = useToast();
  const [configs, setConfigs] = useState<ConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [healthData, setHealthData] = useState<Record<string, UniversityHealth>>({});
  const [regressionAlerts, setRegressionAlerts]   = useState<Record<string, RegressionAlert[]>>({});
  const [repairSuggestions, setRepairSuggestions] = useState<Record<string, AutoRepairSuggestion[]>>({});
  const [repairEvidenceOpen, setRepairEvidenceOpen] = useState<Record<number, boolean>>({});
  const [repairApplying, setRepairApplying] = useState<Record<number, boolean>>({});
  const [repairConfirmSid, setRepairConfirmSid] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [editorYaml, setEditorYaml] = useState("");
  const [savedYaml, setSavedYaml] = useState("");
  const [editorSlug, setEditorSlug] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [filter, setFilter] = useState("");
  const [showNewModal, setShowNewModal] = useState(false);
  const [newModalMode, setNewModalMode] = useState<"ai" | "manual">("ai");
  const [manualSlug, setManualSlug] = useState("");
  const [manualYaml, setManualYaml] = useState(SAMPLE_YAML);
  const [copiedSample, setCopiedSample] = useState(false);
  const [pendingSlug, setPendingSlug] = useState<string | null>(null);
  const [draftBanner, setDraftBanner] = useState<{ slug: string; lineCount: number } | null>(null);
  const [generating, setGenerating] = useState(false);
  const [genStage, setGenStage] = useState("");
  const [view, setView] = useState<EditorView>("editor");
  const [genForm, setGenForm] = useState<GenerateForm>({
    university_name: "",
    website_url: "",
    country: "Australia",
    notes: "",
  });
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const draftTimerRef = useRef<number | null>(null);

  // ── History state ─────────────────────────────────────────────────────────
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const [historySelectedEntry, setHistorySelectedEntry] = useState<HistoryEntry | null>(null);
  const [historyCompareEntry, setHistoryCompareEntry] = useState<HistoryEntry | null>(null);
  const [restoringId, setRestoringId] = useState<number | null>(null);

  // ── Per-slug scrape job tracking ─────────────────────────────────────────
  const [triggerJobs, setTriggerJobs] = useState<Record<string, TriggerState>>({});
  const [triggering, setTriggering] = useState<string | null>(null);
  const pollTimerRef = useRef<number | null>(null);

  // ── AI YAML fix ──────────────────────────────────────────────────────────
  const [aiFixOpen, setAiFixOpen] = useState(false);
  const [aiFixPrompt, setAiFixPrompt] = useState("");
  const [aiFixing, setAiFixing] = useState(false);
  const [aiFixPrev, setAiFixPrev] = useState<string | null>(null);

  // ── Debugger panel state ──────────────────────────────────────────────────
  const [debugTab, setDebugTab] = useState<"config" | "overrides" | "rejections" | "extraction" | "discovery" | "ai_analysis">("config");
  const [effectiveCfg, setEffectiveCfg] = useState<EffectiveConfigResult | null>(null);
  const [effectiveCfgLoading, setEffectiveCfgLoading] = useState(false);
  const [rejectionLog, setRejectionLog] = useState<RejectionLogResult | null>(null);
  const [rejectionLogLoading, setRejectionLogLoading] = useState(false);
  const [rejectionFilter, setRejectionFilter] = useState<string | null>(null);
  const [clearingOverrideKey, setClearingOverrideKey] = useState<string | null>(null);
  const [clearingAllOverrides, setClearingAllOverrides] = useState(false);
  const [cfgExpandedSections, setCfgExpandedSections] = useState<Record<string, boolean>>({});
  // Extraction Debugger
  const [scrapedCourses, setScrapedCourses] = useState<ScrapedCoursesResult | null>(null);
  const [scrapedCoursesLoading, setScrapedCoursesLoading] = useState(false);
  const [extractionTrace, setExtractionTrace] = useState<ExtractionTraceResult | null>(null);
  const [extractionTraceLoading, setExtractionTraceLoading] = useState(false);
  const [selectedCourseId, setSelectedCourseId] = useState<number | null>(null);
  // Discovery Debugger
  const [discoveryStats, setDiscoveryStats] = useState<DiscoveryStatsResult | null>(null);
  const [discoveryStatsLoading, setDiscoveryStatsLoading] = useState(false);
  const [testUrl, setTestUrl] = useState("");
  const [urlTestResult, setUrlTestResult] = useState<UrlTestResult | null>(null);
  const [urlTestLoading, setUrlTestLoading] = useState(false);
  // AI Root Cause Analysis
  const [aiAnalysis, setAiAnalysis] = useState<AiRootCauseResult | null>(null);
  const [aiAnalysisLoading, setAiAnalysisLoading] = useState(false);
  const [aiAnalysisApplying, setAiAnalysisApplying] = useState(false);
  const [fixJustApplied, setFixJustApplied] = useState(false);
  // Live Test Discovery
  const [testDiscoveryResult, setTestDiscoveryResult] = useState<TestDiscoveryResult | null>(null);
  const [testDiscoveryLoading, setTestDiscoveryLoading] = useState(false);
  // Full Validation
  const [fullValidationResult, setFullValidationResult] = useState<FullValidationResult | null>(null);
  const [fullValidationLoading, setFullValidationLoading] = useState(false);

  // ── Quick Settings (central pages + auto_interact_all) ────────────────────
  const [quickSettings, setQuickSettings] = useState<{ central_english_url: string; central_english_ug_url: string; central_english_pg_url: string; central_fees_url: string; auto_interact_all: boolean } | null>(null);
  const [quickSettingsLoading, setQuickSettingsLoading] = useState(false);
  const [quickSettingsSaving, setQuickSettingsSaving] = useState(false);
  const [quickSettingsOpen, setQuickSettingsOpen] = useState(false);

  // ── AI Diagnose & Fix ─────────────────────────────────────────────────────
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnosisOpen, setDiagnosisOpen] = useState(false);
  const [diagnosisResult, setDiagnosisResult] = useState<DiagnosisResult | null>(null);
  const [diagnosisPrompt, setDiagnosisPrompt] = useState("");
  const [diagnosisExpanded, setDiagnosisExpanded] = useState<Record<number, boolean>>({});

  const fetchConfigs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const cfgs: ConfigEntry[] = data.configs ?? [];
      setConfigs(cfgs);
      // Fire-and-forget health + alerts fetch for all linked universities
      const ids = cfgs.map(c => c.university_id).filter((id): id is number => id != null);
      if (ids.length > 0) {
        const idStr = ids.join(",");
        fetchWithAuth(`${BASE}/api/settings/scraper-health?university_ids=${idStr}`)
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d?.health) setHealthData(d.health as Record<string, UniversityHealth>); })
          .catch(() => { /* health is non-critical */ });
        fetchWithAuth(`${BASE}/api/settings/regression-alerts?university_ids=${idStr}&status=open,acknowledged`)
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d?.alerts) setRegressionAlerts(d.alerts as Record<string, RegressionAlert[]>); })
          .catch(() => { /* alerts are non-critical */ });
        fetchWithAuth(`${BASE}/api/settings/auto-repair?university_ids=${idStr}&status=pending,ready,developer_required,failed`)
          .then(r => r.ok ? r.json() : null)
          .then(d => { if (d?.suggestions) setRepairSuggestions(d.suggestions as Record<string, AutoRepairSuggestion[]>); })
          .catch(() => { /* repair suggestions are non-critical */ });
      }
    } catch (err) {
      toast({ title: "Failed to load configs", description: (err as Error).message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  // ── Quick Settings callbacks ───────────────────────────────────────────────
  const fetchQuickSettings = useCallback(async (slug: string) => {
    setQuickSettingsLoading(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${slug}/quick-settings`);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setQuickSettings({
        central_english_url: data.central_english_url ?? "",
        central_english_ug_url: data.central_english_ug_url ?? "",
        central_english_pg_url: data.central_english_pg_url ?? "",
        central_fees_url: data.central_fees_url ?? "",
        auto_interact_all: data.auto_interact_all ?? false,
      });
    } catch (err) {
      toast({ title: "Failed to load quick settings", description: (err as Error).message, variant: "destructive" });
    } finally {
      setQuickSettingsLoading(false);
    }
  }, [toast]);

  const saveQuickSettings = useCallback(async (slug: string, patch: { central_english_url?: string; central_english_ug_url?: string; central_english_pg_url?: string; central_fees_url?: string; auto_interact_all?: boolean }) => {
    setQuickSettingsSaving(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${slug}/quick-settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const changed: string[] = data.changed ?? [];
      toast({ title: "Quick settings saved", description: changed.length ? changed.join("; ") : "No changes", variant: "default" });
    } catch (err) {
      toast({ title: "Failed to save quick settings", description: (err as Error).message, variant: "destructive" });
    } finally {
      setQuickSettingsSaving(false);
    }
  }, [toast]);

  const fetchHistory = useCallback(async (slug: string, opts?: { beforeId?: number; append?: boolean }) => {
    const isAppend = opts?.append ?? false;
    if (isAppend) {
      setHistoryLoadingMore(true);
    } else {
      setHistoryLoading(true);
      setHistory([]);
      setHistoryHasMore(false);
    }
    try {
      const params = new URLSearchParams();
      if (opts?.beforeId != null) params.set("before_id", String(opts.beforeId));
      const url = `${BASE}/api/settings/scraper-configs/${slug}/history${params.size ? `?${params}` : ""}`;
      const res = await fetchWithAuth(url);
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const newEntries: HistoryEntry[] = data.history ?? [];
      setHistory(prev => isAppend ? [...prev, ...newEntries] : newEntries);
      setHistoryHasMore(data.has_more ?? false);
    } catch (err) {
      toast({ title: "Failed to load history", description: (err as Error).message, variant: "destructive" });
    } finally {
      setHistoryLoading(false);
      setHistoryLoadingMore(false);
    }
  }, [toast]);

  useEffect(() => { void fetchConfigs(); }, [fetchConfigs]);

  // Auto-select slug from ?slug= query param (e.g. when linked from recipe editor)
  useEffect(() => {
    if (!configs.length) return;
    const params = new URLSearchParams(window.location.search);
    const slugParam = params.get("slug");
    if (slugParam && configs.find(c => c.slug === slugParam)) {
      selectConfig(slugParam);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configs]);

  // Prune stale drafts once on mount
  useEffect(() => { pruneOldDrafts(); }, []);

  // Debounced draft persistence to localStorage
  useEffect(() => {
    if (!editorSlug || editorYaml === savedYaml) return;
    if (draftTimerRef.current) clearTimeout(draftTimerRef.current);
    draftTimerRef.current = window.setTimeout(() => {
      writeDraft(editorSlug, editorYaml);
      draftTimerRef.current = null;
    }, 600);
    return () => {
      if (draftTimerRef.current) { clearTimeout(draftTimerRef.current); draftTimerRef.current = null; }
    };
  }, [editorYaml, editorSlug, savedYaml]);

  // Fetch history when user switches to history view
  useEffect(() => {
    if (view === "history" && selected) {
      void fetchHistory(selected);
    }
  }, [view, selected, fetchHistory]);

  // Load debugger data when switching to debugger view
  useEffect(() => {
    if (view === "debugger" && selected) {
      const cfg = configs.find(c => c.slug === selected);
      if (cfg?.university_id) {
        void fetchEffectiveConfig(cfg.university_id);
        void fetchRejectionLog(cfg.university_id);
        void fetchScrapedCourses(cfg.university_id);
        void fetchDiscoveryStats(cfg.university_id);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, selected]);

  const fetchEffectiveConfig = useCallback(async (uniId: number) => {
    setEffectiveCfgLoading(true);
    setEffectiveCfg(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/effective-config`);
      if (!res.ok) throw new Error(await res.text());
      setEffectiveCfg(await res.json());
    } catch (err) {
      toast({ title: "Failed to load effective config", description: (err as Error).message, variant: "destructive" });
    } finally {
      setEffectiveCfgLoading(false);
    }
  }, [toast]);

  const fetchRejectionLog = useCallback(async (uniId: number) => {
    setRejectionLogLoading(true);
    setRejectionLog(null);
    setRejectionFilter(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/rejection-log`);
      if (!res.ok) throw new Error(await res.text());
      setRejectionLog(await res.json());
    } catch (err) {
      toast({ title: "Failed to load rejection log", description: (err as Error).message, variant: "destructive" });
    } finally {
      setRejectionLogLoading(false);
    }
  }, [toast]);

  const fetchScrapedCourses = useCallback(async (uniId: number) => {
    setScrapedCoursesLoading(true);
    setScrapedCourses(null);
    setSelectedCourseId(null);
    setExtractionTrace(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/scraped-courses`);
      if (!res.ok) throw new Error(await res.text());
      setScrapedCourses(await res.json());
    } catch (err) {
      toast({ title: "Failed to load courses", description: (err as Error).message, variant: "destructive" });
    } finally {
      setScrapedCoursesLoading(false);
    }
  }, [toast]);

  const fetchExtractionTrace = useCallback(async (uniId: number, courseId: number) => {
    setExtractionTraceLoading(true);
    setExtractionTrace(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/scraped-courses/${courseId}/extraction-trace`);
      if (!res.ok) throw new Error(await res.text());
      setExtractionTrace(await res.json());
    } catch (err) {
      toast({ title: "Failed to load extraction trace", description: (err as Error).message, variant: "destructive" });
    } finally {
      setExtractionTraceLoading(false);
    }
  }, [toast]);

  const fetchDiscoveryStats = useCallback(async (uniId: number) => {
    setDiscoveryStatsLoading(true);
    setDiscoveryStats(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/discovery-stats`);
      if (!res.ok) throw new Error(await res.text());
      setDiscoveryStats(await res.json());
    } catch (err) {
      toast({ title: "Failed to load discovery stats", description: (err as Error).message, variant: "destructive" });
    } finally {
      setDiscoveryStatsLoading(false);
    }
  }, [toast]);

  const handleTestUrl = useCallback(async (uniId: number, url: string) => {
    if (!url.trim()) return;
    setUrlTestLoading(true);
    setUrlTestResult(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/test-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!res.ok) throw new Error(await res.text());
      setUrlTestResult(await res.json());
    } catch (err) {
      toast({ title: "URL test failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setUrlTestLoading(false);
    }
  }, [toast]);

  const runAiRootCause = useCallback(async (uniId: number) => {
    setAiAnalysisLoading(true);
    setAiAnalysis(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/ai-root-cause`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(await res.text());
      setAiAnalysis(await res.json());
    } catch (err) {
      toast({ title: "AI analysis failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setAiAnalysisLoading(false);
    }
  }, [toast]);

  const handleApplySafeFix = useCallback(async (uniId: number, fix: AiSafeFix) => {
    setAiAnalysisApplying(true);
    try {
      if (fix.action === "clear_admin_override") {
        const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/admin-config`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keys: [fix.key] }),
        });
        if (!res.ok) throw new Error(await res.text());
        toast({ title: "Fix applied", description: fix.description });
        setFixJustApplied(true);
        await fetchEffectiveConfig(uniId);
        await runAiRootCause(uniId);
      } else if (fix.action === "set_admin_override") {
        // Build nested object from dot-notation key
        const parts = fix.key.split(".");
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const nested: Record<string, any> = {};
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        let cur: Record<string, any> = nested;
        for (let i = 0; i < parts.length - 1; i++) {
          cur[parts[i]] = {};
          cur = cur[parts[i]];
        }
        cur[parts[parts.length - 1]] = fix.value;
        const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/agent-config`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(nested),
        });
        if (!res.ok) throw new Error(await res.text());
        toast({ title: "Fix applied", description: fix.description });
        setFixJustApplied(true);
        await fetchEffectiveConfig(uniId);
        await runAiRootCause(uniId);
      }
    } catch (err) {
      toast({ title: "Failed to apply fix", description: (err as Error).message, variant: "destructive" });
    } finally {
      setAiAnalysisApplying(false);
    }
  }, [toast, fetchEffectiveConfig, runAiRootCause]);

  const runTestDiscovery = useCallback(async (uniId: number) => {
    setTestDiscoveryLoading(true);
    setTestDiscoveryResult(null);
    setFullValidationResult(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/test-discovery`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(await res.text());
      setTestDiscoveryResult(await res.json());
    } catch (err) {
      toast({ title: "Test discovery failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setTestDiscoveryLoading(false);
    }
  }, [toast]);

  const clearConfigConflict = useCallback(async (uniId: number, conflict: ConfigConflict, currentAdminRaw: Record<string, unknown>) => {
    try {
      const cleaned = JSON.parse(JSON.stringify(currentAdminRaw));
      const disc = (cleaned.discovery ?? {}) as Record<string, unknown>;
      delete disc[conflict.field];
      if (Object.keys(disc).length === 0) delete cleaned.discovery;
      else cleaned.discovery = disc;
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/agent-config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cleaned),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: "Override cleared", description: `Removed empty admin_config.discovery.${conflict.field} — YAML values are now active.` });
      setTestDiscoveryResult(prev => prev ? {
        ...prev,
        config_conflicts: (prev.config_conflicts ?? []).filter(c => c.field !== conflict.field),
        admin_config_raw: cleaned,
      } : prev);
    } catch (err) {
      toast({ title: "Failed to clear override", description: (err as Error).message, variant: "destructive" });
    }
  }, [toast]);

  const runFullValidation = useCallback(async (uniId: number, urls: string[]) => {
    if (!urls.length) return;
    setFullValidationLoading(true);
    setFullValidationResult(null);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/full-validation`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls }),
      });
      if (!res.ok) throw new Error(await res.text());
      setFullValidationResult(await res.json());
    } catch (err) {
      toast({ title: "Full validation failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setFullValidationLoading(false);
    }
  }, [toast]);

  const handleClearOverrideKey = useCallback(async (uniId: number, key: string) => {
    setClearingOverrideKey(key);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/admin-config`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys: [key] }),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: "Override cleared", description: `Removed "${key}" from admin overrides.` });
      await fetchEffectiveConfig(uniId);
    } catch (err) {
      toast({ title: "Failed to clear override", description: (err as Error).message, variant: "destructive" });
    } finally {
      setClearingOverrideKey(null);
    }
  }, [toast, fetchEffectiveConfig]);

  const handleClearAllOverrides = useCallback(async (uniId: number) => {
    setClearingAllOverrides(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/universities/${uniId}/admin-config`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: "All overrides cleared", description: "Admin config overrides have been removed." });
      await fetchEffectiveConfig(uniId);
    } catch (err) {
      toast({ title: "Failed to clear overrides", description: (err as Error).message, variant: "destructive" });
    } finally {
      setClearingAllOverrides(false);
    }
  }, [toast, fetchEffectiveConfig]);

  // Clear history entry selection whenever the active config slug changes
  useEffect(() => {
    setHistorySelectedEntry(null);
    setHistoryCompareEntry(null);
  }, [selected]);

  // Poll all in-progress jobs
  useEffect(() => {
    const activeJobs = Object.entries(triggerJobs).filter(
      ([, state]) => !TERMINAL_STATUSES.includes(state.status),
    );
    if (activeJobs.length === 0) {
      if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null; }
      return;
    }
    if (pollTimerRef.current) return; // already polling
    pollTimerRef.current = window.setInterval(async () => {
      const current = Object.entries(triggerJobs).filter(
        ([, s]) => !TERMINAL_STATUSES.includes(s.status),
      );
      if (current.length === 0) {
        clearInterval(pollTimerRef.current!);
        pollTimerRef.current = null;
        return;
      }
      await Promise.all(current.map(async ([slug, state]) => {
        try {
          const res = await fetchWithAuth(`${BASE}/api/scrape/status/${state.jobId}`);
          if (!res.ok) return;
          const d = await res.json();
          setTriggerJobs(prev => ({
            ...prev,
            [slug]: {
              ...prev[slug],
              status: d.status as JobStatus,
              imported: d.imported ?? d.progress?.imported,
              totalFound: d.totalFound ?? d.progress?.total,
            },
          }));
        } catch { /* network hiccup — keep polling */ }
      }));
    }, 2500);
    return () => {
      if (pollTimerRef.current) { clearInterval(pollTimerRef.current); pollTimerRef.current = null; }
    };
  }, [triggerJobs]);

  const triggerScrape = async (slug: string) => {
    if (triggering === slug) return;
    setTriggering(slug);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${slug}/trigger`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) {
        toast({ title: "Trigger failed", description: data.detail ?? "Could not start scrape", variant: "destructive" });
        return;
      }
      setTriggerJobs(prev => ({
        ...prev,
        [slug]: {
          jobId: data.jobId ?? data.runtimeJobId,
          status: data.status ?? "queued",
          universityId: data.universityId,
          universityName: data.universityName,
        },
      }));
      toast({ title: "Scrape started", description: `Job queued for ${data.universityName ?? slug}` });
    } catch (err) {
      toast({ title: "Trigger failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setTriggering(null);
    }
  };

  const selectConfig = (slug: string) => {
    const cfg = configs.find(c => c.slug === slug);
    if (!cfg) return;
    const draft = readDraft(slug);
    setSelected(slug);
    setEditorSlug(slug);
    setSavedYaml(cfg.yaml);
    setView("editor");
    setHistory([]);
    if (draft !== null && draft !== cfg.yaml) {
      setEditorYaml(draft);
      setDraftBanner({ slug, lineCount: draft.split("\n").length });
    } else {
      setEditorYaml(cfg.yaml);
      setDraftBanner(null);
    }
    // Load quick settings for this slug (non-blocking)
    setQuickSettings(null);
    setQuickSettingsOpen(false);
    void fetchQuickSettings(slug);
  };

  const handleSelectConfig = (slug: string) => {
    if (slug === selected) return;
    if (editorYaml !== savedYaml) {
      setPendingSlug(slug);
    } else {
      selectConfig(slug);
    }
  };

  const confirmDiscard = () => {
    if (pendingSlug) {
      selectConfig(pendingSlug);
      setPendingSlug(null);
    }
  };

  const discardDraft = () => {
    removeDraft(draftBanner?.slug ?? editorSlug);
    setEditorYaml(savedYaml);
    setDraftBanner(null);
  };

  const handleSave = async () => {
    if (!editorSlug.trim()) { toast({ title: "Slug required", variant: "destructive" }); return; }
    setSaving(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml_content: editorYaml }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Save failed"); }
      const data = await res.json();
      toast({ title: "Saved", description: `Config for '${editorSlug}' saved` });
      setSavedYaml(editorYaml);
      setView("editor");
      setDraftBanner(null);
      removeDraft(editorSlug.trim());

      if (data.git_pushed && data.git_message && !data.git_message.includes("up-to-date")) {
        toast({ title: "Saved & synced to GitHub", description: data.git_message });
      }
      await fetchConfigs();
      setSelected(editorSlug.trim());
    } catch (err) {
      toast({ title: "Save failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!selected) return;
    if (!confirm(`Delete config for '${selected}'? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${selected}`, {
        method: "DELETE",
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Delete failed"); }
      toast({ title: "Deleted", description: `Config for '${selected}' removed` });
      removeDraft(selected);
      setSelected(null);
      setEditorYaml("");
      setSavedYaml("");
      setEditorSlug("");
      setView("editor");
      setHistory([]);
      setDraftBanner(null);
      await fetchConfigs();
    } catch (err) {
      toast({ title: "Delete failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setDeleting(false);
    }
  };

  const handleAiFix = async () => {
    if (!aiFixPrompt.trim()) {
      toast({ title: "Prompt required", description: "Describe what you want to change.", variant: "destructive" });
      return;
    }
    if (!editorSlug.trim()) {
      toast({ title: "No config selected", description: "Select or create a config first.", variant: "destructive" });
      return;
    }
    setAiFixing(true);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}/ai-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: aiFixPrompt.trim(), yaml_content: editorYaml }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "AI fix failed"); }
      const data = await res.json();
      setAiFixPrev(editorYaml);
      setEditorYaml(data.yaml ?? editorYaml);
      setAiFixOpen(false);
      setAiFixPrompt("");
      setView("diff");
      toast({ title: "AI fix applied", description: "Review the changes in the diff view, then Save when ready." });
    } catch (err) {
      const msg = (err as Error).message;
      const isBusy = msg.toLowerCase().includes("busy") || msg.toLowerCase().includes("try again");
      toast({ title: isBusy ? "Gemini is busy" : "AI fix failed", description: msg, variant: "destructive" });
    } finally {
      setAiFixing(false);
    }
  };

  const handleDiagnose = async () => {
    if (!editorSlug.trim()) {
      toast({ title: "No config selected", description: "Select a config first.", variant: "destructive" });
      return;
    }
    setDiagnosing(true);
    setDiagnosisOpen(true);
    setDiagnosisResult(null);
    setDiagnosisExpanded({});
    setAiFixOpen(false);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${editorSlug.trim()}/ai-diagnose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ yaml_content: editorYaml, prompt: diagnosisPrompt }),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Diagnosis failed"); }
      const data: DiagnosisResult = await res.json();
      setDiagnosisResult(data);
      const critCount = data.issues.filter(i => i.severity === "critical").length;
      if (data.has_changes) {
        toast({ title: `${critCount > 0 ? critCount + " critical issue(s) found" : "Diagnosis complete"}`, description: "Review the findings below, then apply the fix." });
      } else {
        toast({ title: "All clear", description: "No config changes needed — the config looks correct." });
      }
    } catch (err) {
      const msg = (err as Error).message;
      const isBusy = msg.toLowerCase().includes("busy") || msg.toLowerCase().includes("try again");
      toast({ title: isBusy ? "Gemini is busy" : "Diagnosis failed", description: msg, variant: "destructive" });
      setDiagnosisOpen(false);
    } finally {
      setDiagnosing(false);
    }
  };

  const applyDiagnosis = () => {
    if (!diagnosisResult?.yaml) return;
    setAiFixPrev(editorYaml);
    setEditorYaml(diagnosisResult.yaml);
    setDiagnosisOpen(false);
    setView("diff");
    toast({ title: "Fix applied", description: "Review in the Changes view, then Save to persist." });
  };

  const handleCreateManually = () => {
    const slug = manualSlug.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (!slug) {
      toast({ title: "Slug required", description: "Enter a slug (e.g. 'macquarie' or 'utas').", variant: "destructive" });
      return;
    }
    if (configs.some(c => c.slug === slug)) {
      toast({ title: "Slug already exists", description: `A config for '${slug}' already exists. Select it from the list to edit.`, variant: "destructive" });
      return;
    }
    setEditorYaml(manualYaml);
    setSavedYaml("");
    setEditorSlug(slug);
    setSelected(null);
    setView("editor");
    setHistory([]);
    setDraftBanner(null);
    setShowNewModal(false);
    toast({ title: "Config created", description: "Edit the YAML below, then save when ready." });
    setTimeout(() => textareaRef.current?.focus(), 100);
  };

  const handleGenerate = async () => {
    if (!genForm.university_name.trim() || !genForm.website_url.trim()) {
      toast({ title: "Name and URL required", variant: "destructive" });
      return;
    }
    setGenerating(true);

    // Cycle through descriptive stage messages so the user knows what's happening
    const stages = [
      "Fetching homepage…",
      "Detecting SPA framework…",
      "Probing fee pages…",
      "Probing English requirement pages…",
      "Extracting nav links…",
      "Generating YAML with AI…",
    ];
    let stageIdx = 0;
    setGenStage(stages[0]);
    const stageTimer = window.setInterval(() => {
      stageIdx = Math.min(stageIdx + 1, stages.length - 1);
      setGenStage(stages[stageIdx]);
    }, 6000);

    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(genForm),
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Generation failed"); }
      const data = await res.json();
      setEditorYaml(data.yaml ?? "");
      setSavedYaml("");
      setEditorSlug(data.slug ?? "");
      setSelected(null);
      setView("editor");
      setHistory([]);
      setDraftBanner(null);
      setShowNewModal(false);
      toast({ title: "Generated!", description: "Review and edit the config below, then save." });
      setTimeout(() => textareaRef.current?.focus(), 100);
    } catch (err) {
      toast({ title: "AI generation failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      clearInterval(stageTimer);
      setGenerating(false);
      setGenStage("");
    }
  };

  const handleRestore = async (entry: HistoryEntry) => {
    if (!selected) return;
    if (!confirm(`Restore this version saved ${formatRelativeTime(entry.saved_at)}? The current saved YAML will be replaced.`)) return;
    setRestoringId(entry.id);
    try {
      const res = await fetchWithAuth(`${BASE}/api/settings/scraper-configs/${selected}/restore/${entry.id}`, {
        method: "POST",
      });
      if (!res.ok) { const d = await res.json(); throw new Error(d.detail ?? "Restore failed"); }
      toast({ title: "Restored", description: `Config for '${selected}' reverted to version from ${formatRelativeTime(entry.saved_at)}` });
      setSavedYaml(entry.yaml_content);
      setEditorYaml(entry.yaml_content);
      removeDraft(selected);
      setDraftBanner(null);
      setHistorySelectedEntry(null);
      setHistoryCompareEntry(null);
      await fetchConfigs();
      await fetchHistory(selected);
    } catch (err) {
      toast({ title: "Restore failed", description: (err as Error).message, variant: "destructive" });
    } finally {
      setRestoringId(null);
    }
  };

  const filtered = configs.filter(c =>
    c.slug.includes(filter.toLowerCase()) || c.title.toLowerCase().includes(filter.toLowerCase())
  );

  const selectedConfig = selected ? configs.find(c => c.slug === selected) : null;
  const selectedJob = selected ? triggerJobs[selected] : undefined;
  const selectedJobActive = selectedJob && !TERMINAL_STATUSES.includes(selectedJob.status);
  const isDirty = editorYaml !== savedYaml;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Scraper Configs</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Per-university YAML overrides for the web scraper. Changes take effect on the next scrape job.
        </p>
      </div>

      <SettingsTabs />

      <div className="flex gap-4 h-[calc(100vh-280px)] min-h-[500px]">
        {/* Left sidebar — config list */}
        <div className="w-64 flex-shrink-0 border rounded-lg overflow-hidden flex flex-col bg-background">
          <div className="p-2 border-b flex gap-1">
            <div className="relative flex-1">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
              <Input
                className="pl-7 h-8 text-xs"
                placeholder="Filter…"
                value={filter}
                onChange={e => setFilter(e.target.value)}
              />
            </div>
            <Button
              size="sm"
              variant="outline"
              className="h-8 w-8 p-0"
              title="Refresh"
              onClick={fetchConfigs}
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </Button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="p-3 text-xs text-muted-foreground">Loading…</div>
            ) : filtered.length === 0 ? (
              <div className="p-3 text-xs text-muted-foreground">No configs found</div>
            ) : (
              filtered.map(cfg => {
                const job = triggerJobs[cfg.slug];
                const isRunning = job && !TERMINAL_STATUSES.includes(job.status);
                const isThisTriggering = triggering === cfg.slug;
                const hasUni = cfg.university_id != null;
                const configIsDirty = selected === cfg.slug && isDirty;

                return (
                  <div
                    key={cfg.slug}
                    className={cn(
                      "group flex items-center border-b last:border-b-0 transition-colors",
                      selected === cfg.slug ? "bg-primary/10" : "hover:bg-muted/50",
                    )}
                  >
                    <button
                      onClick={() => handleSelectConfig(cfg.slug)}
                      className="flex-1 text-left px-3 py-2 text-sm min-w-0"
                    >
                      <div className={cn("font-medium truncate flex items-center gap-1.5", selected === cfg.slug && "text-primary")}>
                        {cfg.slug}
                        {configIsDirty && (
                          <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 flex-shrink-0" title="Unsaved changes" />
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground truncate">{cfg.title}</div>
                      {job && (
                        <div className="mt-0.5">
                          <JobStatusBadge state={job} compact />
                        </div>
                      )}
                      {cfg.university_id != null && healthData[String(cfg.university_id)] && (() => {
                        const h = healthData[String(cfg.university_id!)]!;
                        const { barCls, textCls } = healthScoreMeta(h.overall_health);
                        return (
                          <div className="flex items-center gap-1.5 mt-0.5">
                            <div className="flex-1 h-1 rounded-full bg-muted overflow-hidden">
                              <div className={cn("h-full rounded-full transition-all", barCls)} style={{ width: `${h.overall_health}%` }} />
                            </div>
                            <span className={cn("text-[10px] font-medium tabular-nums flex-shrink-0", textCls)}>
                              {h.overall_health}/100
                            </span>
                          </div>
                        );
                      })()}
                      {/* Regression alert indicator */}
                      {cfg.university_id != null && (() => {
                        const alerts = regressionAlerts[String(cfg.university_id!)] ?? [];
                        const open = alerts.filter(a => a.status === "open");
                        if (open.length === 0) return null;
                        const worst = open.find(a => a.severity === "critical") ?? open.find(a => a.severity === "high") ?? open[0];
                        const { dotCls, textCls } = alertSeverityMeta(worst.severity);
                        return (
                          <div className="flex items-center gap-1 mt-0.5">
                            <span className={cn("inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 animate-pulse", dotCls)} />
                            <span className={cn("text-[10px] font-medium", textCls)}>
                              {open.length} regression alert{open.length > 1 ? "s" : ""}
                            </span>
                          </div>
                        );
                      })()}
                    </button>
                    {/* Per-card trigger button */}
                    <button
                      title={hasUni ? `Trigger scrape for ${cfg.university_name ?? cfg.slug}` : "No university linked — add # Hostname: to the YAML"}
                      disabled={isThisTriggering || isRunning || !hasUni}
                      onClick={e => { e.stopPropagation(); void triggerScrape(cfg.slug); }}
                      className={cn(
                        "mr-2 flex-shrink-0 p-1 rounded transition-colors",
                        hasUni
                          ? "text-muted-foreground hover:text-green-600 hover:bg-green-50 group-hover:opacity-100 opacity-0"
                          : "text-muted-foreground/30 cursor-not-allowed opacity-0 group-hover:opacity-100",
                        (isThisTriggering || isRunning) && "opacity-100",
                      )}
                    >
                      {isThisTriggering || isRunning
                        ? <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-500" />
                        : <Play className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                );
              })
            )}
          </div>

          <div className="p-2 border-t flex flex-col gap-1.5">
            <Button
              size="sm"
              className="w-full h-8 text-xs"
              onClick={() => {
                setGenForm({ university_name: "", website_url: "", country: "Australia", notes: "" });
                setNewModalMode("ai");
                setManualSlug("");
                setManualYaml(SAMPLE_YAML);
                setShowNewModal(true);
              }}
            >
              <Plus className="h-3.5 w-3.5 mr-1" />
              New Config
            </Button>
            <div className="flex gap-1.5">
              <Button
                size="sm"
                variant="outline"
                className="flex-1 h-8 text-xs"
                onClick={downloadSampleYaml}
                title="Download sample YAML file"
              >
                <Download className="h-3.5 w-3.5 mr-1" />
                Sample YAML
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="flex-1 h-8 text-xs"
                title="Copy sample YAML to clipboard"
                onClick={() => {
                  navigator.clipboard.writeText(SAMPLE_YAML).then(() => {
                    setCopiedSample(true);
                    setTimeout(() => setCopiedSample(false), 2000);
                  });
                }}
              >
                {copiedSample
                  ? <><Check className="h-3.5 w-3.5 mr-1 text-green-600" /><span className="text-green-600">Copied!</span></>
                  : <><Clipboard className="h-3.5 w-3.5 mr-1" />Copy</>
                }
              </Button>
            </div>
          </div>
        </div>

        {/* Right — editor / diff / history */}
        <div className="flex-1 border rounded-lg overflow-hidden flex flex-col bg-background">
          {!selected && !editorYaml ? (
            <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm">
              Select a config from the list, or click <strong className="mx-1">New Config</strong> to create one.
            </div>
          ) : (
            <>
              <div className="px-4 py-2 border-b flex flex-wrap items-center gap-x-3 gap-y-2">
                <div className="flex items-center gap-2 min-w-0 flex-shrink-0">
                  <Label className="text-xs text-muted-foreground whitespace-nowrap">Slug</Label>
                  <Input
                    className="h-7 text-xs font-mono w-36"
                    value={editorSlug}
                    onChange={e => setEditorSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))}
                    placeholder="e.g. myuniversity"
                  />
                  <span className="text-xs text-muted-foreground hidden md:inline truncate max-w-[180px]" title={`scraper_config/unis/${editorSlug || "…"}.yaml`}>→ scraper_config/unis/{editorSlug || "…"}.yaml</span>
                </div>
                <div className="flex items-center gap-1.5 flex-wrap flex-1 justify-end">
                  {/* Trigger scrape button + status in editor header */}
                  {selected && (
                    <div className="flex items-center gap-2">
                      {selectedJob && (
                        <JobStatusBadge state={selectedJob} />
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        className={cn(
                          "h-7 text-xs",
                          selectedConfig?.university_id == null
                            ? "text-muted-foreground cursor-not-allowed"
                            : "text-green-700 border-green-300 hover:bg-green-50",
                          selectedJobActive && "text-blue-600 border-blue-300",
                        )}
                        disabled={triggering === selected || !!selectedJobActive || selectedConfig?.university_id == null}
                        title={
                          selectedConfig?.university_id == null
                            ? "No university linked — add '# Hostname: your.domain.edu.au' to the YAML comment"
                            : selectedJobActive
                            ? "Scrape already running"
                            : `Trigger scrape for ${selectedConfig?.university_name ?? selected}`
                        }
                        onClick={() => void triggerScrape(selected)}
                      >
                        {triggering === selected || selectedJobActive
                          ? <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />
                          : <Play className="h-3.5 w-3.5 mr-1" />}
                        {triggering === selected
                          ? "Starting…"
                          : selectedJobActive
                          ? "Running…"
                          : selectedConfig?.university_id == null
                          ? "Not linked"
                          : "Trigger scrape"}
                      </Button>
                    </div>
                  )}

                  {/* View toggle buttons */}
                  <div className="flex items-center border rounded-md overflow-hidden">
                    <button
                      onClick={() => setView("editor")}
                      className={cn(
                        "flex items-center gap-1 px-2.5 py-1 text-xs transition-colors",
                        view === "editor" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50"
                      )}
                      title="Edit YAML"
                    >
                      <Code className="h-3.5 w-3.5" />
                      Editor
                      {isDirty && view !== "editor" && (
                        <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-400" />
                      )}
                    </button>
                    <button
                      onClick={() => setView("diff")}
                      className={cn(
                        "flex items-center gap-1 px-2.5 py-1 text-xs border-l transition-colors",
                        view === "diff" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50",
                        isDirty && view !== "diff" && "text-amber-600 dark:text-amber-400"
                      )}
                      title="Preview changes vs saved version"
                    >
                      <GitCompare className="h-3.5 w-3.5" />
                      Changes
                      {isDirty && (
                        <span className={cn(
                          "inline-block w-1.5 h-1.5 rounded-full",
                          view === "diff" ? "bg-amber-200" : "bg-amber-400"
                        )} />
                      )}
                    </button>
                    {selected && (
                      <button
                        onClick={() => setView("history")}
                        className={cn(
                          "flex items-center gap-1 px-2.5 py-1 text-xs border-l transition-colors",
                          view === "history" ? "bg-primary text-primary-foreground" : "hover:bg-muted/50"
                        )}
                        title="View save history"
                      >
                        <History className="h-3.5 w-3.5" />
                        History
                      </button>
                    )}
                    {selected && selectedConfig?.university_id != null && (
                      <button
                        onClick={() => setView("debugger")}
                        className={cn(
                          "flex items-center gap-1 px-2.5 py-1 text-xs border-l transition-colors",
                          view === "debugger" ? "bg-orange-600 text-white" : "hover:bg-muted/50 text-orange-700 dark:text-orange-400"
                        )}
                        title="Debug this university's config — effective settings, admin overrides, rejection log"
                      >
                        <Bug className="h-3.5 w-3.5" />
                        Debugger
                      </button>
                    )}
                  </div>

                  <Button
                    size="sm"
                    variant={diagnosisOpen ? "default" : "outline"}
                    className={cn("h-7 text-xs", diagnosisOpen ? "" : "border-blue-300 text-blue-700 hover:bg-blue-50 dark:border-blue-700 dark:text-blue-400 dark:hover:bg-blue-950/40")}
                    onClick={() => {
                      if (diagnosisOpen && diagnosisResult) { setDiagnosisOpen(false); }
                      else { void handleDiagnose(); }
                    }}
                    disabled={diagnosing}
                    title="Auto-diagnose scrape issues using real data from the last scrape job"
                  >
                    {diagnosing
                      ? <><Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />Diagnosing…</>
                      : <><Bot className="h-3.5 w-3.5 mr-1" />Diagnose &amp; Fix</>
                    }
                  </Button>
                  <Button
                    size="sm"
                    variant={aiFixOpen ? "default" : "outline"}
                    className="h-7 text-xs"
                    onClick={() => { setAiFixOpen(o => !o); setDiagnosisOpen(false); }}
                    title="Fix YAML with AI — describe a change and Gemini applies it"
                  >
                    <Wand2 className="h-3.5 w-3.5 mr-1" />
                    Fix with AI
                  </Button>
                  {aiFixPrev !== null && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs text-amber-700 border-amber-300 hover:bg-amber-50"
                      onClick={() => { setEditorYaml(aiFixPrev!); setAiFixPrev(null); setView("editor"); toast({ title: "Undone", description: "AI fix reverted to previous YAML." }); }}
                      title="Undo the last AI fix"
                    >
                      <Undo2 className="h-3.5 w-3.5 mr-1" />
                      Undo AI
                    </Button>
                  )}
                  <Button size="sm" className="h-7 text-xs" onClick={handleSave} disabled={saving}>
                    <Save className="h-3.5 w-3.5 mr-1" />
                    {saving ? "Saving…" : "Save"}
                  </Button>
                  {selected && (
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs text-destructive hover:text-destructive"
                      onClick={handleDelete}
                      disabled={deleting}
                    >
                      <Trash2 className="h-3.5 w-3.5 mr-1" />
                      {deleting ? "Deleting…" : "Delete"}
                    </Button>
                  )}
                </div>
              </div>

              {/* ── Regression Alert Banners ───────────────────────────────────── */}
              {selectedConfig?.university_id != null && (() => {
                const allAlerts = regressionAlerts[String(selectedConfig.university_id!)] ?? [];
                const visible = allAlerts.filter(a => a.status === "open" || a.status === "acknowledged");
                if (visible.length === 0) return null;

                const refreshAlerts = (uniId: number) => {
                  fetchWithAuth(`${BASE}/api/settings/regression-alerts?university_ids=${uniId}&status=open,acknowledged`)
                    .then(r => r.ok ? r.json() : null)
                    .then(d => {
                      if (d?.alerts) {
                        setRegressionAlerts(prev => ({ ...prev, ...d.alerts }));
                      }
                    })
                    .catch(() => {});
                };

                const doAcknowledge = async (alertId: number, uniId: number) => {
                  const r = await fetchWithAuth(`${BASE}/api/settings/regression-alerts/${alertId}/acknowledge`, { method: "POST" });
                  if (r.ok) { refreshAlerts(uniId); toast({ title: "Alert acknowledged" }); }
                };
                const doResolve = async (alertId: number, uniId: number) => {
                  const r = await fetchWithAuth(`${BASE}/api/settings/regression-alerts/${alertId}/resolve`, { method: "POST" });
                  if (r.ok) { refreshAlerts(uniId); toast({ title: "Alert resolved" }); }
                };

                return (
                  <div className="border-b">
                    {visible.map(alert => {
                      const { bgCls, badgeCls, textCls, iconCls } = alertSeverityMeta(alert.severity);
                      const debTab = alert.alert_type === "discovery_health_drop" ? "discovery" : "extraction";
                      return (
                        <div key={alert.id} className={cn("px-4 py-3 border-b last:border-b-0 flex flex-col gap-2", bgCls)}>
                          {/* Header row */}
                          <div className="flex items-center justify-between gap-2">
                            <div className="flex items-center gap-2 min-w-0">
                              <TriangleAlert className={cn("h-4 w-4 flex-shrink-0", iconCls)} />
                              <span className={cn("text-xs font-bold flex-shrink-0", textCls)}>Regression Detected</span>
                              <span className={cn("text-[10px] font-semibold px-1.5 py-0.5 rounded border flex-shrink-0", badgeCls)}>
                                {alert.severity.toUpperCase()}
                              </span>
                              {alert.status === "acknowledged" && (
                                <span className="text-[10px] text-muted-foreground flex-shrink-0 italic">acknowledged</span>
                              )}
                              <span className="text-[10px] text-muted-foreground truncate">
                                {alert.snapshot_date}
                              </span>
                            </div>
                            {/* Dismiss actions */}
                            <div className="flex items-center gap-1 flex-shrink-0">
                              {alert.status === "open" && (
                                <button
                                  onClick={() => doAcknowledge(alert.id, selectedConfig.university_id!)}
                                  className="text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors"
                                >
                                  Acknowledge
                                </button>
                              )}
                              <button
                                onClick={() => doResolve(alert.id, selectedConfig.university_id!)}
                                className="text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors"
                              >
                                Resolve
                              </button>
                            </div>
                          </div>

                          {/* Change description */}
                          <div className={cn("text-xs font-medium", textCls)}>
                            {alertChangeLine(alert)}
                          </div>

                          {/* Probable causes */}
                          {alert.probable_causes.length > 0 && (
                            <div>
                              <div className="text-[10px] font-semibold text-muted-foreground mb-0.5">Probable causes:</div>
                              <ul className="space-y-0.5">
                                {alert.probable_causes.map((cause, i) => (
                                  <li key={i} className="flex items-start gap-1.5 text-[11px] text-muted-foreground">
                                    <span className="flex-shrink-0 mt-0.5">•</span>
                                    <span>{cause}</span>
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}

                          {/* Action buttons */}
                          <div className="flex items-center gap-2 pt-0.5">
                            <button
                              onClick={() => setDebugTab(debTab as "discovery" | "extraction")}
                              className={cn(
                                "flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1 rounded border transition-colors",
                                "border-border bg-background hover:bg-muted",
                              )}
                            >
                              <Bug className="h-3 w-3" />
                              Open Debugger
                            </button>
                            <button
                              onClick={() => setDebugTab("ai_analysis")}
                              className={cn(
                                "flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1 rounded border transition-colors",
                                "border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30",
                                "text-violet-700 dark:text-violet-300 hover:bg-violet-100 dark:hover:bg-violet-950/50",
                              )}
                            >
                              <Bot className="h-3 w-3" />
                              Run AI Analysis
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                );
              })()}

              {/* ── Auto Repair Panel ──────────────────────────────────────────── */}
              {selectedConfig?.university_id != null && (() => {
                const uidStr = String(selectedConfig.university_id!);
                const suggestions = (repairSuggestions[uidStr] ?? []).filter(
                  s => s.status === "pending" || s.status === "ready" || s.status === "developer_required" || s.status === "failed"
                );
                if (suggestions.length === 0) return null;
                const suggestion = suggestions[0]!;

                const confidenceMeta = (c: string | null) => {
                  if (c === "high")   return { label: "HIGH",   cls: "bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300 border-green-300 dark:border-green-700" };
                  if (c === "medium") return { label: "MED",    cls: "bg-amber-100 dark:bg-amber-900/50 text-amber-700 dark:text-amber-300 border-amber-300 dark:border-amber-700" };
                  return                      { label: "LOW",   cls: "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 border-slate-300 dark:border-slate-600" };
                };

                const refreshRepair = () => {
                  fetchWithAuth(`${BASE}/api/settings/auto-repair?university_ids=${selectedConfig.university_id!}&status=pending,ready,developer_required,failed`)
                    .then(r => r.ok ? r.json() : null)
                    .then(d => { if (d?.suggestions) setRepairSuggestions(prev => ({ ...prev, ...d.suggestions })); })
                    .catch(() => {});
                };

                const doApply = async (sid: number) => {
                  setRepairConfirmSid(null);
                  setRepairApplying(p => ({ ...p, [sid]: true }));
                  try {
                    const r = await fetchWithAuth(`${BASE}/api/settings/auto-repair/${sid}/apply`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ applied_by: "admin" }),
                    });
                    if (r.ok) {
                      toast({ title: "Repair applied", description: "Config updated — trigger a new scrape to verify. Rollback available via old_config in audit log." });
                      refreshRepair();
                      void fetchConfigs();
                    } else {
                      const msg = await r.json().catch(() => ({ detail: "Unknown error" }));
                      toast({ title: "Apply failed", description: msg.detail ?? "Unknown error", variant: "destructive" });
                    }
                  } finally {
                    setRepairApplying(p => ({ ...p, [sid]: false }));
                  }
                };

                const doDismiss = async (sid: number) => {
                  const r = await fetchWithAuth(`${BASE}/api/settings/auto-repair/${sid}/dismiss`, { method: "POST" });
                  if (r.ok) { refreshRepair(); toast({ title: "Suggestion dismissed" }); }
                };

                // ── FAILED ────────────────────────────────────────────────────────
                if (suggestion.status === "failed") {
                  const failLabel =
                    suggestion.fail_reason?.includes("GEMINI_API_KEY") || suggestion.fail_reason?.includes("Gemini")
                      ? "Gemini response failed"
                      : suggestion.fail_reason?.toLowerCase().includes("validation")
                        ? "Validation failed"
                        : "Developer required — code fix needed";
                  return (
                    <div className="border-b bg-red-50 dark:bg-red-950/20">
                      <div className="px-4 py-3 flex flex-col gap-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold text-red-700 dark:text-red-300">⚠ Auto Repair Failed</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded border font-semibold bg-red-100 dark:bg-red-900/50 text-red-600 dark:text-red-400 border-red-300 dark:border-red-700">FAILED</span>
                          </div>
                          <button onClick={() => doDismiss(suggestion.id)} className="text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors flex-shrink-0">Dismiss</button>
                        </div>
                        <div className="text-xs text-red-800 dark:text-red-200">
                          <span className="font-semibold">Reason: </span>{failLabel}
                        </div>
                        {suggestion.fail_reason && (
                          <div className="bg-red-100/70 dark:bg-red-900/30 rounded px-2.5 py-1.5 text-[10px] font-mono text-red-700 dark:text-red-300 border border-red-200 dark:border-red-800 break-all line-clamp-3">
                            {suggestion.fail_reason}
                          </div>
                        )}
                        <button
                          onClick={() => {
                            fetchWithAuth(`${BASE}/api/settings/auto-repair/trigger`, {
                              method: "POST",
                              headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ university_id: selectedConfig.university_id }),
                            }).then(r => r.ok ? r.json() : null).then(d => {
                              if (d?.task_id) {
                                toast({ title: "AI Analysis queued", description: "Check back in ~30 seconds." });
                                setTimeout(refreshRepair, 8000);
                              }
                            }).catch(() => {});
                          }}
                          className="self-start text-[11px] font-medium px-2.5 py-1 rounded border border-red-300 dark:border-red-700 bg-background hover:bg-red-50 dark:hover:bg-red-950/40 text-red-700 dark:text-red-300 transition-colors flex items-center gap-1"
                        >
                          <RotateCcw className="h-3 w-3" /> Run AI Analysis manually
                        </button>
                      </div>
                    </div>
                  );
                }

                // ── PENDING ───────────────────────────────────────────────────────
                if (suggestion.status === "pending") {
                  return (
                    <div className="border-b px-4 py-3 bg-violet-50 dark:bg-violet-950/20 flex items-center gap-2.5">
                      <div className="h-3.5 w-3.5 rounded-full border-2 border-violet-400 border-t-transparent animate-spin flex-shrink-0" />
                      <span className="text-xs text-violet-700 dark:text-violet-300 font-medium">Analysing regression and generating repair suggestion…</span>
                      <button onClick={refreshRepair} className="ml-auto text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors flex-shrink-0">Refresh</button>
                    </div>
                  );
                }

                // ── DEVELOPER REQUIRED ────────────────────────────────────────────
                if (suggestion.status === "developer_required") {
                  return (
                    <div className="border-b bg-slate-50 dark:bg-slate-900/40">
                      <div className="px-4 py-3 flex flex-col gap-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold text-slate-700 dark:text-slate-300">🔧 Developer Intervention Required</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded border font-semibold bg-slate-200 dark:bg-slate-700 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600">DEV ONLY</span>
                          </div>
                          <button onClick={() => doDismiss(suggestion.id)} className="text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors flex-shrink-0">Dismiss</button>
                        </div>
                        {suggestion.issue_summary && (
                          <p className="text-xs text-muted-foreground">{suggestion.issue_summary}</p>
                        )}
                        {suggestion.fix_recommendation && (
                          <p className="text-xs text-slate-600 dark:text-slate-400 font-medium">{suggestion.fix_recommendation}</p>
                        )}
                        {suggestion.developer_note && (
                          <div className="bg-slate-100 dark:bg-slate-800 rounded px-2.5 py-2 text-[11px] text-muted-foreground border border-slate-200 dark:border-slate-700">
                            <span className="font-semibold text-slate-600 dark:text-slate-300">Dev note: </span>{suggestion.developer_note}
                          </div>
                        )}
                        {suggestion.evidence.length > 0 && (
                          <div>
                            <button
                              onClick={() => setRepairEvidenceOpen(p => ({ ...p, [suggestion.id]: !p[suggestion.id] }))}
                              className="text-[10px] text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
                            >
                              {repairEvidenceOpen[suggestion.id] ? "▾" : "▸"} {suggestion.evidence.length} evidence item{suggestion.evidence.length !== 1 ? "s" : ""}
                            </button>
                            {repairEvidenceOpen[suggestion.id] && (
                              <div className="mt-1.5 space-y-1">
                                {suggestion.evidence.map((ev, i) => (
                                  <div key={i} className="flex items-start gap-2 text-[11px]">
                                    <span className="text-muted-foreground flex-shrink-0 mt-0.5">•</span>
                                    <span className="font-medium text-foreground flex-shrink-0">{ev.label}:</span>
                                    <span className="text-muted-foreground break-all">{ev.value}</span>
                                    <span className="text-muted-foreground/60 italic flex-shrink-0 ml-auto">{ev.source}</span>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                }

                // ── READY ─────────────────────────────────────────────────────────
                const vr = suggestion.validation_result;
                const before = vr?.before;
                const after  = vr?.after;
                const { label: confLabel, cls: confCls } = confidenceMeta(suggestion.confidence);
                const safeFix = suggestion.safe_fix;

                return (
                  <div className="border-b bg-emerald-50 dark:bg-emerald-950/20">
                    <div className="px-4 py-3 flex flex-col gap-2.5">
                      {/* Header */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-bold text-emerald-800 dark:text-emerald-200">🔧 Auto Repair Available</span>
                          {suggestion.root_cause_category && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-100 dark:bg-emerald-900/50 text-emerald-700 dark:text-emerald-300 border-emerald-300 dark:border-emerald-700 capitalize">
                              {suggestion.root_cause_category.replace(/_/g, " ")}
                            </span>
                          )}
                          <span className={cn("text-[10px] px-1.5 py-0.5 rounded border font-bold", confCls)}>
                            {confLabel} CONFIDENCE
                          </span>
                        </div>
                        <button onClick={() => doDismiss(suggestion.id)} className="text-[10px] px-2 py-0.5 rounded border border-border bg-background hover:bg-muted transition-colors flex-shrink-0">Dismiss</button>
                      </div>

                      {/* Issue summary */}
                      {suggestion.issue_summary && (
                        <p className="text-xs text-emerald-800 dark:text-emerald-200">{suggestion.issue_summary}</p>
                      )}

                      {/* Before / After metrics table */}
                      {before && after && (
                        <div className="rounded border border-emerald-200 dark:border-emerald-800 overflow-hidden text-[11px]">
                          <div className="grid grid-cols-4 bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 font-semibold">
                            <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800">Metric</div>
                            <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800 text-center">Before</div>
                            <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800 text-center">After (est.)</div>
                            <div className="px-2.5 py-1.5 text-center">Δ</div>
                          </div>
                          {([
                            ["Completeness", before.completeness,    after.completeness,    "%"],
                            ["Fee Coverage", before.fee_coverage,    after.fee_coverage,    "%"],
                            ["English",      before.english_coverage,after.english_coverage,"%"],
                            ["Intake",       before.intake_coverage, after.intake_coverage, "%"],
                          ] as [string, number, number, string][]).map(([metric, bv, av, unit]) => {
                            const delta = av - bv;
                            return (
                              <div key={metric} className="grid grid-cols-4 border-t border-emerald-200 dark:border-emerald-800">
                                <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800 font-medium text-foreground">{metric}</div>
                                <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800 text-center text-muted-foreground">{bv}{unit}</div>
                                <div className="px-2.5 py-1.5 border-r border-emerald-200 dark:border-emerald-800 text-center font-semibold text-emerald-700 dark:text-emerald-300">{av}{unit}</div>
                                <div className={cn("px-2.5 py-1.5 text-center font-bold", delta > 0 ? "text-green-600 dark:text-green-400" : delta < 0 ? "text-red-500 dark:text-red-400" : "text-muted-foreground")}>
                                  {delta > 0 ? "+" : ""}{delta}{unit}
                                </div>
                              </div>
                            );
                          })}
                          {vr?.production_completeness != null && (
                            <div className="grid grid-cols-1 border-t border-emerald-200 dark:border-emerald-800 px-2.5 py-1.5 text-muted-foreground italic">
                              Current production completeness (with AI): {vr.production_completeness}% — est. uses rule-based only (conservative lower bound)
                            </div>
                          )}
                        </div>
                      )}

                      {/* URL-filter simulation panel */}
                      {vr?.url_simulation && (() => {
                        const us = vr.url_simulation!;
                        const hasImprovement = us.improvement > 0;
                        return (
                          <div className={cn(
                            "rounded border text-[11px] overflow-hidden",
                            hasImprovement
                              ? "border-blue-200 dark:border-blue-800"
                              : "border-amber-200 dark:border-amber-800",
                          )}>
                            {/* Header */}
                            <div className={cn(
                              "px-2.5 py-1.5 font-semibold flex items-center gap-1.5",
                              hasImprovement
                                ? "bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200"
                                : "bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200",
                            )}>
                              {hasImprovement ? "✓ Candidate URL Validated" : "⚠ No URL Improvement Detected"}
                              <span className="ml-auto font-normal text-[10px] opacity-70">
                                {us.sample_size} URL sample
                              </span>
                            </div>

                            {/* Before / After counts */}
                            <div className="grid grid-cols-3 border-t border-inherit">
                              {(["Before", "After", "Change"] as const).map(h => (
                                <div key={h} className="px-2.5 py-1 border-r last:border-r-0 border-inherit font-semibold text-center text-muted-foreground bg-muted/30">{h}</div>
                              ))}
                            </div>
                            <div className="grid grid-cols-3 border-t border-inherit">
                              <div className="px-2.5 py-1.5 border-r border-inherit text-center text-muted-foreground">
                                {us.before_pass} pass
                              </div>
                              <div className={cn("px-2.5 py-1.5 border-r border-inherit text-center font-semibold",
                                hasImprovement ? "text-blue-700 dark:text-blue-300" : "text-muted-foreground",
                              )}>
                                {us.after_pass} pass
                              </div>
                              <div className={cn("px-2.5 py-1.5 text-center font-bold",
                                us.improvement > 0 ? "text-green-600 dark:text-green-400"
                                : us.improvement < 0 ? "text-red-500 dark:text-red-400"
                                : "text-muted-foreground",
                              )}>
                                {us.improvement > 0 ? "+" : ""}{us.improvement} URLs
                              </div>
                            </div>

                            {/* Sample rescued URLs */}
                            {hasImprovement && us.sample_rescued.length > 0 && (
                              <div className="border-t border-inherit px-2.5 py-1.5 space-y-0.5">
                                <div className="text-[10px] font-semibold text-blue-700 dark:text-blue-300 mb-1">Sample URLs now passing:</div>
                                {us.sample_rescued.map((u, i) => (
                                  <div key={i} className="text-[10px] text-muted-foreground font-mono break-all truncate" title={u}>• {u}</div>
                                ))}
                              </div>
                            )}

                            {/* Sample dropped URLs */}
                            {us.sample_dropped_before.length > 0 && (
                              <div className="border-t border-inherit px-2.5 py-1.5 space-y-0.5">
                                <div className="text-[10px] font-semibold text-amber-700 dark:text-amber-300 mb-1">
                                  {hasImprovement ? "Sample URLs still dropped by filter:" : "Sample URLs dropped by current filter:"}
                                </div>
                                {us.sample_dropped_before.map((u, i) => (
                                  <div key={i} className="text-[10px] text-muted-foreground font-mono break-all truncate" title={u}>• {u}</div>
                                ))}
                              </div>
                            )}

                            {/* No-improvement warning */}
                            {!hasImprovement && (
                              <div className="border-t border-amber-200 dark:border-amber-800 px-2.5 py-1.5 text-[10px] text-amber-700 dark:text-amber-300">
                                This YAML change does not improve URL pass-through. Apply Fix is disabled.
                              </div>
                            )}
                          </div>
                        );
                      })()}

                      {/* Fix recommendation */}
                      {suggestion.fix_recommendation && (
                        <div className="text-xs text-slate-700 dark:text-slate-300 font-medium">
                          <span className="text-muted-foreground">Fix: </span>{suggestion.fix_recommendation}
                        </div>
                      )}

                      {/* Safe fix description */}
                      {safeFix && (
                        <div className="bg-emerald-100/60 dark:bg-emerald-900/30 rounded px-2.5 py-1.5 text-[11px] text-emerald-800 dark:text-emerald-200 border border-emerald-200 dark:border-emerald-800">
                          <span className="font-semibold">Config change: </span>
                          {safeFix.type === "clear_admin_override"
                            ? `Remove admin override: ${safeFix.key}`
                            : `Set ${safeFix.key} = ${JSON.stringify(safeFix.value)}`
                          }
                        </div>
                      )}
                      {suggestion.fix_yaml_snippet && !safeFix && (
                        <div className="bg-emerald-100/60 dark:bg-emerald-900/30 rounded px-2.5 py-1.5 text-[11px] text-emerald-800 dark:text-emerald-200 border border-emerald-200 dark:border-emerald-800">
                          <span className="font-semibold">YAML change </span>
                          <span className="font-mono text-[10px]">(written to per-uni YAML file)</span>
                        </div>
                      )}

                      {/* Evidence toggle */}
                      {suggestion.evidence.length > 0 && (
                        <div>
                          <button
                            onClick={() => setRepairEvidenceOpen(p => ({ ...p, [suggestion.id]: !p[suggestion.id] }))}
                            className="text-[10px] text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
                          >
                            {repairEvidenceOpen[suggestion.id] ? "▾" : "▸"} View {suggestion.evidence.length} evidence item{suggestion.evidence.length !== 1 ? "s" : ""}
                          </button>
                          {repairEvidenceOpen[suggestion.id] && (
                            <div className="mt-1.5 space-y-1 pl-1">
                              {suggestion.evidence.map((ev, i) => (
                                <div key={i} className="flex items-start gap-2 text-[11px]">
                                  <span className="text-muted-foreground flex-shrink-0 mt-0.5">•</span>
                                  <span className="font-medium text-foreground flex-shrink-0">{ev.label}:</span>
                                  <span className="text-muted-foreground break-all">{ev.value}</span>
                                  <span className="text-muted-foreground/60 italic flex-shrink-0 ml-auto">{ev.source}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}

                      {/* Action buttons */}
                      {(() => {
                        const us = vr?.url_simulation;
                        // Disable Apply Fix when URL simulation ran and shows no improvement,
                        // AND extraction metrics (if available) also show no improvement.
                        const extractionImprovement = before && after
                          ? (after.completeness - before.completeness)
                          : null;
                        const urlBlocked = us != null && us.improvement <= 0;
                        const extractionBlocked = extractionImprovement != null && extractionImprovement <= 0;
                        // Block if URL sim shows no improvement; allow through if extraction
                        // shows improvement (extraction-only fixes like fee keywords).
                        const applyBlocked = urlBlocked && (extractionImprovement == null || extractionBlocked);
                        return (
                        <div className="flex items-center gap-2 pt-0.5 flex-wrap">
                          {(safeFix != null || suggestion.fix_yaml_snippet) && (
                            <button
                              onClick={() => !applyBlocked && setRepairConfirmSid(suggestion.id)}
                              disabled={repairApplying[suggestion.id] || applyBlocked}
                              title={applyBlocked ? "Disabled: URL simulation shows no improvement from this change" : undefined}
                              className={cn(
                                "flex items-center gap-1.5 text-[11px] font-medium px-3 py-1.5 rounded border transition-colors",
                                applyBlocked
                                  ? "bg-muted text-muted-foreground border-border cursor-not-allowed opacity-60"
                                  : "bg-emerald-600 hover:bg-emerald-700 text-white border-emerald-700 disabled:opacity-50",
                              )}
                            >
                              {repairApplying[suggestion.id] ? (
                                <><div className="h-3 w-3 rounded-full border-2 border-white border-t-transparent animate-spin" />Applying…</>
                              ) : applyBlocked ? (
                                <>⊘ Apply Fix (no improvement)</>
                              ) : (
                                <>✓ Preview &amp; Apply</>
                              )}
                            </button>
                          )}
                          <button
                            onClick={() => setDebugTab("ai_analysis")}
                            className={cn(
                              "flex items-center gap-1.5 text-[11px] font-medium px-2.5 py-1.5 rounded border transition-colors",
                              "border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-950/30",
                              "text-violet-700 dark:text-violet-300 hover:bg-violet-100 dark:hover:bg-violet-950/50",
                            )}
                          >
                            <Bot className="h-3 w-3" />
                            Full AI Analysis
                          </button>
                        </div>
                        );
                      })()}
                    </div>

                    {/* ── Confirm Apply Modal ─────────────────────────────────── */}
                    {repairConfirmSid === suggestion.id && (
                      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={e => { if (e.target === e.currentTarget) setRepairConfirmSid(null); }}>
                        <div className="bg-background border rounded-lg shadow-xl w-full max-w-lg max-h-[85vh] overflow-y-auto flex flex-col">
                          <div className="px-5 py-4 border-b flex items-center justify-between">
                            <div>
                              <h3 className="text-sm font-semibold text-foreground">Confirm Repair Application</h3>
                              <p className="text-[11px] text-muted-foreground mt-0.5">Review all changes before applying. This will modify the live university config.</p>
                            </div>
                            <button onClick={() => setRepairConfirmSid(null)} className="text-muted-foreground hover:text-foreground p-1"><X className="h-4 w-4" /></button>
                          </div>

                          <div className="px-5 py-4 space-y-4 flex-1">
                            {/* Confidence + Risk */}
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className={cn("text-[10px] px-1.5 py-0.5 rounded border font-bold", confCls)}>{confLabel} CONFIDENCE</span>
                              {suggestion.risk_label && suggestion.risk_label !== "developer_required" && (
                                <span className={cn("text-[10px] px-1.5 py-0.5 rounded border font-semibold",
                                  suggestion.risk_label === "low"
                                    ? "bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300 border-green-300 dark:border-green-700"
                                    : "bg-amber-100 dark:bg-amber-900/50 text-amber-700 dark:text-amber-300 border-amber-300 dark:border-amber-700"
                                )}>
                                  {suggestion.risk_label.toUpperCase()} RISK
                                </span>
                              )}
                              {suggestion.root_cause_category && (
                                <span className="text-[10px] px-1.5 py-0.5 rounded border bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600 capitalize">
                                  {suggestion.root_cause_category.replace(/_/g, " ")}
                                </span>
                              )}
                            </div>

                            {/* What will change */}
                            <div>
                              <p className="text-[11px] font-semibold text-foreground mb-1.5">What will change</p>
                              {safeFix && (
                                <div className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded px-3 py-2 text-[11px] text-amber-800 dark:text-amber-200">
                                  <span className="font-semibold">DB config change: </span>
                                  {safeFix.type === "clear_admin_override"
                                    ? `Remove admin override key: ${safeFix.key}`
                                    : `Set ${safeFix.key} = ${JSON.stringify(safeFix.value)}`}
                                </div>
                              )}
                              {suggestion.fix_yaml_snippet && (
                                <div className="mt-1.5">
                                  <p className="text-[10px] text-muted-foreground mb-1">YAML file change <span className="italic">(written to per-uni .yaml config)</span>:</p>
                                  <pre className="bg-slate-900 text-green-400 text-[10px] rounded p-2.5 overflow-x-auto font-mono leading-relaxed whitespace-pre-wrap">{suggestion.fix_yaml_snippet}</pre>
                                </div>
                              )}
                            </div>

                            {/* Before / After */}
                            {before && after && (
                              <div>
                                <p className="text-[11px] font-semibold text-foreground mb-1.5">Before → After (rule-based estimate)</p>
                                <div className="rounded border overflow-hidden text-[11px]">
                                  <div className="grid grid-cols-4 bg-muted text-muted-foreground font-semibold">
                                    {["Metric","Before","After (est.)","Δ"].map(h => (
                                      <div key={h} className="px-2.5 py-1.5 border-r last:border-r-0 border-border">{h}</div>
                                    ))}
                                  </div>
                                  {([
                                    ["Completeness", before.completeness,     after.completeness,    ],
                                    ["Fee Coverage", before.fee_coverage,     after.fee_coverage,    ],
                                    ["English",      before.english_coverage, after.english_coverage,],
                                    ["Intake",       before.intake_coverage,  after.intake_coverage, ],
                                  ] as [string, number, number][]).map(([m, bv, av]) => {
                                    const d = av - bv;
                                    return (
                                      <div key={m} className="grid grid-cols-4 border-t border-border">
                                        <div className="px-2.5 py-1.5 border-r border-border font-medium">{m}</div>
                                        <div className="px-2.5 py-1.5 border-r border-border text-center text-muted-foreground">{bv}%</div>
                                        <div className="px-2.5 py-1.5 border-r border-border text-center font-semibold text-emerald-700 dark:text-emerald-300">{av}%</div>
                                        <div className={cn("px-2.5 py-1.5 text-center font-bold", d > 0 ? "text-green-600 dark:text-green-400" : d < 0 ? "text-red-500" : "text-muted-foreground")}>
                                          {d > 0 ? "+" : ""}{d}%
                                        </div>
                                      </div>
                                    );
                                  })}
                                </div>
                              </div>
                            )}

                            {/* Rollback note */}
                            <div className="bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded px-3 py-2 text-[11px] text-blue-800 dark:text-blue-200">
                              <span className="font-semibold">Rollback: </span>
                              The previous config snapshot is saved to <code className="font-mono bg-blue-100 dark:bg-blue-900/40 px-1 rounded">old_config</code> in the audit log.
                              To roll back, paste the <code className="font-mono bg-blue-100 dark:bg-blue-900/40 px-1 rounded">old_config</code> JSON back into the university's <code className="font-mono bg-blue-100 dark:bg-blue-900/40 px-1 rounded">scrape_config</code> field.
                            </div>
                          </div>

                          {/* Footer */}
                          <div className="px-5 py-3 border-t flex items-center justify-end gap-2 bg-muted/30">
                            <button
                              onClick={() => setRepairConfirmSid(null)}
                              className="text-[11px] px-3 py-1.5 rounded border border-border bg-background hover:bg-muted transition-colors"
                            >
                              Cancel
                            </button>
                            <button
                              onClick={() => doApply(suggestion.id)}
                              disabled={repairApplying[suggestion.id]}
                              className="flex items-center gap-1.5 text-[11px] font-medium px-3 py-1.5 rounded border transition-colors bg-emerald-600 hover:bg-emerald-700 text-white border-emerald-700 disabled:opacity-50"
                            >
                              {repairApplying[suggestion.id] ? (
                                <><div className="h-3 w-3 rounded-full border-2 border-white border-t-transparent animate-spin" />Applying…</>
                              ) : (
                                <>✓ Confirm Apply</>
                              )}
                            </button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* ── Scrape Health Card ─────────────────────────────────────────── */}
              {selectedConfig?.university_id != null && healthData[String(selectedConfig.university_id)] && (() => {
                const h = healthData[String(selectedConfig.university_id!)]!;
                const { label, textCls, bgCls } = healthScoreMeta(h.overall_health);
                // Metric → [display label, value, trend value, debug tab, tooltip]
                type DebugTabId = "config" | "overrides" | "rejections" | "extraction" | "discovery" | "ai_analysis";
                const metrics: [string, number, number | null, DebugTabId, string][] = [
                  ["Discovery",  h.discovery_health,  h.trend_discovery,  "discovery",  "How many courses were staged in the last scrape vs. historical median (≥median = 100%)"],
                  ["Extraction", h.extraction_health, h.trend_extraction, "extraction", "Average completeness score across all staged courses for this university"],
                  ["Fee",        h.fee_coverage,      h.trend_fee,        "extraction", "% of courses with an international_fee value filled"],
                  ["English",    h.english_coverage,  h.trend_english,    "extraction", "% of courses with IELTS / PTE / TOEFL score populated"],
                  ["Intake",     h.intake_coverage,   h.trend_intake,     "extraction", "% of courses with at least one intake month recorded"],
                ];
                const trendChip = (delta: number | null) => {
                  if (delta === null) return null;
                  if (delta === 0) return <span className="text-[9px] text-muted-foreground ml-0.5">—</span>;
                  const up = delta > 0;
                  return (
                    <span className={cn("text-[9px] font-medium ml-0.5", up ? "text-green-600 dark:text-green-400" : "text-red-500 dark:text-red-400")}>
                      {up ? "▲" : "▼"}{Math.abs(delta)}
                    </span>
                  );
                };
                return (
                  <div className={cn("px-4 py-2 border-b flex items-center gap-4", bgCls)}>
                    {/* Overall score */}
                    <div className="flex-shrink-0 text-center min-w-[64px]">
                      <div className={cn("text-[10px] uppercase tracking-wide font-semibold", textCls)}>{label}</div>
                      <div className={cn("text-xl font-bold leading-tight tabular-nums flex items-end justify-center gap-0.5", textCls)}>
                        {h.overall_health}
                        <span className="text-xs font-normal opacity-70">/100</span>
                        {trendChip(h.trend_overall)}
                      </div>
                      <div className="text-[9px] text-muted-foreground">{h.total_courses} courses</div>
                      {/* Top issue */}
                      {h.top_issue && (
                        <button
                          className={cn(
                            "mt-1 text-[9px] leading-tight px-1.5 py-0.5 rounded border w-full text-left truncate",
                            "border-orange-300 dark:border-orange-700 text-orange-700 dark:text-orange-400",
                            "bg-orange-50 dark:bg-orange-950/40 hover:bg-orange-100 dark:hover:bg-orange-950/60",
                          )}
                          title={`Top issue: ${h.top_issue.label} ${h.top_issue.score}% — click to open Debugger`}
                          onClick={() => setDebugTab(h.top_issue!.metric === "discovery" ? "discovery" : "extraction")}
                        >
                          ⚠ {h.top_issue.label} {h.top_issue.score}%
                        </button>
                      )}
                    </div>
                    {/* Five metric bars */}
                    <div className="flex-1 grid grid-cols-5 gap-x-4">
                      {metrics.map(([name, val, trend, tab, tip]) => {
                        const { barCls: mb, textCls: mc } = healthScoreMeta(val);
                        return (
                          <button
                            key={name}
                            title={`${tip}\n\nClick to open ${tab} tab in Debugger`}
                            className="text-left group cursor-pointer"
                            onClick={() => setDebugTab(tab)}
                          >
                            <div className="flex justify-between items-center mb-0.5">
                              <span className="text-[10px] text-muted-foreground group-hover:text-foreground transition-colors">{name}</span>
                              <span className={cn("text-[10px] font-semibold tabular-nums flex items-center", mc)}>
                                {val}%{trendChip(trend)}
                              </span>
                            </div>
                            <div className="h-1.5 rounded-full bg-background/60 border border-border/40 overflow-hidden group-hover:border-border transition-colors">
                              <div className={cn("h-full rounded-full transition-all duration-500", mb)} style={{ width: `${val}%` }} />
                            </div>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })()}

              {/* AI Fix panel */}
              {aiFixOpen && (
                <div className="px-4 py-3 border-b bg-violet-50 dark:bg-violet-950/30 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <Wand2 className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400 flex-shrink-0" />
                    <span className="text-xs font-medium text-violet-800 dark:text-violet-300">AI YAML Fix</span>
                    <span className="text-xs text-muted-foreground flex-1">Describe what to change — Gemini updates the YAML for you to review</span>
                    <button onClick={() => setAiFixOpen(false)} className="text-muted-foreground hover:text-foreground">
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <div className="flex gap-2">
                    <input
                      className="flex-1 h-8 rounded-md border border-input bg-background px-3 text-xs focus:outline-none focus:ring-1 focus:ring-violet-400"
                      placeholder={`e.g. "add allow_url_patterns for /courses/ only" or "set bfs_page_budget to 80" or "enable always_sitemap_supplement"`}
                      value={aiFixPrompt}
                      onChange={e => setAiFixPrompt(e.target.value)}
                      onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void handleAiFix(); } }}
                      disabled={aiFixing}
                      autoFocus
                    />
                    <Button
                      size="sm"
                      className="h-8 text-xs bg-violet-600 hover:bg-violet-700 text-white"
                      onClick={handleAiFix}
                      disabled={aiFixing || !aiFixPrompt.trim()}
                    >
                      {aiFixing ? <><Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />Fixing…</> : <><Wand2 className="h-3.5 w-3.5 mr-1" />Fix</>}
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Changes appear in the editor — switch to <strong>Changes</strong> to review the diff, then <strong>Save</strong> to persist.
                  </p>
                </div>
              )}

              {/* AI Diagnose & Fix panel */}
              {diagnosisOpen && (
                <div className="border-b bg-blue-50 dark:bg-blue-950/30 flex flex-col">
                  {/* Header */}
                  <div className="px-4 py-3 flex items-center gap-2 border-b border-blue-100 dark:border-blue-900">
                    <Bot className="h-4 w-4 text-blue-600 dark:text-blue-400 flex-shrink-0" />
                    <span className="text-xs font-semibold text-blue-900 dark:text-blue-200">AI Scrape Diagnosis</span>
                    {diagnosing && <span className="text-xs text-blue-600 dark:text-blue-400 flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" />Analysing last scrape job…</span>}
                    {diagnosisResult && !diagnosing && (
                      <span className="text-xs text-muted-foreground flex-1">
                        {diagnosisResult.university_found
                          ? `${diagnosisResult.university_name} · ${diagnosisResult.issues.length} issue(s) found`
                          : "University not linked — add a # Hostname: comment to the YAML"}
                      </span>
                    )}
                    <div className="flex items-center gap-2 ml-auto">
                      {/* Optional extra note input */}
                      <input
                        className="h-7 w-48 rounded-md border border-blue-200 dark:border-blue-700 bg-white dark:bg-blue-950/60 px-2 text-xs focus:outline-none focus:ring-1 focus:ring-blue-400 placeholder:text-muted-foreground"
                        placeholder="Optional note (e.g. fees are in PDF)"
                        value={diagnosisPrompt}
                        onChange={e => setDiagnosisPrompt(e.target.value)}
                        onKeyDown={e => { if (e.key === "Enter") void handleDiagnose(); }}
                        disabled={diagnosing}
                      />
                      <Button size="sm" variant="outline" className="h-7 text-xs border-blue-300 text-blue-700 hover:bg-blue-100 dark:border-blue-700 dark:text-blue-400" onClick={handleDiagnose} disabled={diagnosing}>
                        {diagnosing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                      </Button>
                      <button onClick={() => setDiagnosisOpen(false)} className="text-muted-foreground hover:text-foreground">
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>

                  {/* Loading skeleton */}
                  {diagnosing && (
                    <div className="px-4 py-5 flex flex-col gap-3">
                      {[1,2,3].map(i => (
                        <div key={i} className="flex gap-3 animate-pulse">
                          <div className="h-5 w-5 rounded-full bg-blue-200 dark:bg-blue-800 flex-shrink-0" />
                          <div className="flex-1 space-y-1.5">
                            <div className="h-3 bg-blue-200 dark:bg-blue-800 rounded w-1/3" />
                            <div className="h-3 bg-blue-100 dark:bg-blue-900 rounded w-2/3" />
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Results */}
                  {diagnosisResult && !diagnosing && (
                    <div className="px-4 py-3 flex flex-col gap-3 max-h-[500px] overflow-y-auto">

                      {/* Last job stats bar */}
                      {diagnosisResult.last_job && (
                        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground bg-white dark:bg-blue-950/20 border border-blue-100 dark:border-blue-800 rounded-md px-3 py-2">
                          <span className="font-medium text-foreground">Last scrape:</span>
                          <span>🔍 {diagnosisResult.last_job.raw_discovered} discovered</span>
                          <span>→ {diagnosisResult.last_job.after_filter} after filter</span>
                          <span>→ <strong>{diagnosisResult.last_job.imported}</strong> staged</span>
                          {diagnosisResult.last_job.errors > 0 && <span className="text-red-600">⚠ {diagnosisResult.last_job.errors} errors</span>}
                          {diagnosisResult.last_job.created_at && <span className="text-muted-foreground">· {diagnosisResult.last_job.created_at}</span>}
                        </div>
                      )}

                      {/* Issues */}
                      {diagnosisResult.issues.length > 0 ? (
                        <div className="flex flex-col gap-2">
                          {diagnosisResult.issues.map((issue, idx) => {
                            const isExpanded = diagnosisExpanded[idx] ?? true;
                            const color = issue.severity === "critical"
                              ? "border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/30"
                              : issue.severity === "warning"
                              ? "border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30"
                              : "border-blue-200 dark:border-blue-800 bg-white dark:bg-blue-950/20";
                            const Icon = issue.severity === "critical" ? ShieldAlert : issue.severity === "warning" ? TriangleAlert : Info;
                            const iconColor = issue.severity === "critical"
                              ? "text-red-600 dark:text-red-400"
                              : issue.severity === "warning"
                              ? "text-amber-600 dark:text-amber-400"
                              : "text-blue-500 dark:text-blue-400";
                            const badge = issue.severity === "critical"
                              ? "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300"
                              : issue.severity === "warning"
                              ? "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300"
                              : "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300";
                            return (
                              <div key={idx} className={cn("border rounded-md overflow-hidden", color)}>
                                <button
                                  className="w-full flex items-center gap-2 px-3 py-2 text-left"
                                  onClick={() => setDiagnosisExpanded(prev => ({ ...prev, [idx]: !isExpanded }))}
                                >
                                  <Icon className={cn("h-4 w-4 flex-shrink-0", iconColor)} />
                                  <span className="flex-1 text-xs font-medium text-foreground">{issue.title}</span>
                                  <span className={cn("text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wide flex-shrink-0", badge)}>
                                    {issue.severity}
                                  </span>
                                  {isExpanded ? <ChevronUp className="h-3 w-3 text-muted-foreground flex-shrink-0" /> : <ChevronDown className="h-3 w-3 text-muted-foreground flex-shrink-0" />}
                                </button>
                                {isExpanded && issue.detail && (
                                  <div className="px-3 pb-2.5 text-xs text-muted-foreground leading-relaxed border-t border-inherit pt-2">
                                    {issue.detail}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="flex items-center gap-2 text-xs text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-3 py-2">
                          <CheckCircle2 className="h-4 w-4 flex-shrink-0" />
                          No issues detected — the config looks correct.
                        </div>
                      )}

                      {/* Summary */}
                      {diagnosisResult.summary && (
                        <div className="text-xs text-muted-foreground italic border-l-2 border-blue-300 dark:border-blue-600 pl-3">
                          {diagnosisResult.summary}
                        </div>
                      )}

                      {/* Changes to be applied */}
                      {diagnosisResult.has_changes && diagnosisResult.changes.length > 0 && (
                        <div className="bg-white dark:bg-blue-950/20 border border-blue-100 dark:border-blue-800 rounded-md px-3 py-2.5">
                          <p className="text-xs font-medium text-foreground mb-1.5">Config changes ready to apply:</p>
                          <ul className="flex flex-col gap-1">
                            {diagnosisResult.changes.map((c, i) => (
                              <li key={i} className="text-xs text-muted-foreground flex gap-1.5">
                                <span className="text-green-600 dark:text-green-400 flex-shrink-0">+</span>
                                {c}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {/* Action buttons */}
                      <div className="flex gap-2 pt-1">
                        {diagnosisResult.has_changes ? (
                          <>
                            <Button size="sm" className="h-8 text-xs bg-blue-600 hover:bg-blue-700 text-white" onClick={applyDiagnosis}>
                              <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
                              Apply Fix &amp; Review Changes
                            </Button>
                            <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setDiagnosisOpen(false)}>
                              Dismiss
                            </Button>
                          </>
                        ) : (
                          <Button size="sm" variant="outline" className="h-8 text-xs" onClick={() => setDiagnosisOpen(false)}>
                            Close
                          </Button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Draft-restored banner */}
              {draftBanner && draftBanner.slug === (selected ?? editorSlug) && (
                <div className="flex items-center gap-3 px-4 py-2 bg-amber-50 dark:bg-amber-950/40 border-b border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300 text-xs">
                  <span className="flex-1">
                    Draft restored — <strong>{draftBanner.lineCount} lines</strong> of unsaved edits recovered from a previous session.
                  </span>
                  <button
                    className="underline underline-offset-2 hover:no-underline font-medium"
                    onClick={() => setDraftBanner(null)}
                  >
                    Keep draft
                  </button>
                  <button
                    className="underline underline-offset-2 hover:no-underline font-medium text-red-600 dark:text-red-400"
                    onClick={discardDraft}
                  >
                    Discard
                  </button>
                </div>
              )}

              {view === "history" ? (
                <HistoryPanel
                  history={history}
                  loading={historyLoading}
                  hasMore={historyHasMore}
                  loadingMore={historyLoadingMore}
                  savedYaml={savedYaml}
                  selectedEntry={historySelectedEntry}
                  compareEntry={historyCompareEntry}
                  onSelectEntry={setHistorySelectedEntry}
                  onSetCompareEntry={setHistoryCompareEntry}
                  onRestore={handleRestore}
                  onLoadMore={() => {
                    if (!selected || historyLoadingMore) return;
                    const minId = history.length > 0 ? Math.min(...history.map(e => e.id)) : undefined;
                    void fetchHistory(selected, { beforeId: minId, append: true });
                  }}
                  restoringId={restoringId}
                />
              ) : view === "diff" ? (
                <DiffViewer oldYaml={savedYaml} newYaml={editorYaml} />
              ) : view === "debugger" ? (
                <DebuggerPanel
                  uniId={selectedConfig?.university_id ?? null}
                  uniName={selectedConfig?.university_name ?? selected ?? ""}
                  effectiveCfg={effectiveCfg}
                  effectiveCfgLoading={effectiveCfgLoading}
                  rejectionLog={rejectionLog}
                  rejectionLogLoading={rejectionLogLoading}
                  rejectionFilter={rejectionFilter}
                  setRejectionFilter={setRejectionFilter}
                  debugTab={debugTab}
                  setDebugTab={setDebugTab}
                  cfgExpandedSections={cfgExpandedSections}
                  setCfgExpandedSections={setCfgExpandedSections}
                  clearingOverrideKey={clearingOverrideKey}
                  clearingAllOverrides={clearingAllOverrides}
                  onClearOverrideKey={(key) => {
                    if (selectedConfig?.university_id) void handleClearOverrideKey(selectedConfig.university_id, key);
                  }}
                  onClearAllOverrides={() => {
                    if (selectedConfig?.university_id) void handleClearAllOverrides(selectedConfig.university_id);
                  }}
                  onRefresh={() => {
                    if (selectedConfig?.university_id) {
                      void fetchEffectiveConfig(selectedConfig.university_id);
                      void fetchRejectionLog(selectedConfig.university_id);
                      void fetchScrapedCourses(selectedConfig.university_id);
                      void fetchDiscoveryStats(selectedConfig.university_id);
                    }
                  }}
                  scrapedCourses={scrapedCourses}
                  scrapedCoursesLoading={scrapedCoursesLoading}
                  extractionTrace={extractionTrace}
                  extractionTraceLoading={extractionTraceLoading}
                  selectedCourseId={selectedCourseId}
                  setSelectedCourseId={setSelectedCourseId}
                  onLoadExtractionTrace={(courseId) => {
                    if (selectedConfig?.university_id) void fetchExtractionTrace(selectedConfig.university_id, courseId);
                  }}
                  discoveryStats={discoveryStats}
                  discoveryStatsLoading={discoveryStatsLoading}
                  testUrl={testUrl}
                  setTestUrl={setTestUrl}
                  urlTestResult={urlTestResult}
                  urlTestLoading={urlTestLoading}
                  onTestUrl={() => {
                    if (selectedConfig?.university_id) void handleTestUrl(selectedConfig.university_id, testUrl);
                  }}
                  aiAnalysis={aiAnalysis}
                  aiAnalysisLoading={aiAnalysisLoading}
                  aiAnalysisApplying={aiAnalysisApplying}
                  fixJustApplied={fixJustApplied}
                  onRunAiAnalysis={() => {
                    setFixJustApplied(false);
                    if (selectedConfig?.university_id) void runAiRootCause(selectedConfig.university_id);
                  }}
                  onApplySafeFix={(fix) => {
                    if (selectedConfig?.university_id) void handleApplySafeFix(selectedConfig.university_id, fix);
                  }}
                  testDiscoveryResult={testDiscoveryResult}
                  testDiscoveryLoading={testDiscoveryLoading}
                  onRunTestDiscovery={() => {
                    if (selectedConfig?.university_id) void runTestDiscovery(selectedConfig.university_id);
                  }}
                  fullValidationResult={fullValidationResult}
                  fullValidationLoading={fullValidationLoading}
                  onRunFullValidation={(urls) => {
                    if (selectedConfig?.university_id) void runFullValidation(selectedConfig.university_id, urls);
                  }}
                  onClearConflict={(conflict, adminRaw) => {
                    if (selectedConfig?.university_id) void clearConfigConflict(selectedConfig.university_id, conflict, adminRaw);
                  }}
                />
              ) : (
                <>
                  {/* ── Quick Settings panel ────────────────────────────────── */}
                  {selected && (
                    <div className="border-b bg-muted/10">
                      <button
                        className="w-full flex items-center justify-between px-4 py-2 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
                        onClick={() => setQuickSettingsOpen(o => !o)}
                        title="Non-developer settings: central page URLs and generic JS expand"
                      >
                        <span className="flex items-center gap-1.5">
                          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                          Quick Settings
                          {quickSettingsLoading && <span className="ml-1 opacity-60">(loading…)</span>}
                          {quickSettings && (quickSettings.central_english_url || quickSettings.central_english_ug_url || quickSettings.central_english_pg_url || quickSettings.central_fees_url || quickSettings.auto_interact_all) && (
                            <span className="ml-1 inline-flex items-center rounded-full bg-blue-100 dark:bg-blue-900/40 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 dark:text-blue-300">active</span>
                          )}
                        </span>
                        <svg className={`h-3.5 w-3.5 transition-transform ${quickSettingsOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                      </button>
                      {quickSettingsOpen && (
                        <div className="px-4 pb-4 pt-2 flex flex-col gap-3">
                          <p className="text-[11px] text-muted-foreground leading-snug">
                            These settings are saved to the university database record and take effect on the next scrape — no YAML editing needed.
                          </p>

                          {/* Central English Requirements URLs (UG / PG / General) */}
                          <div className="flex flex-col gap-2.5">
                            <div>
                              <label className="text-xs font-medium">Central English Requirements</label>
                              <p className="text-[11px] text-muted-foreground leading-snug mt-0.5">
                                Use separate UG / PG URLs when the university publishes requirements on different pages per degree level (e.g. University of Law). The scraper fetches each page once and applies values only to courses at the matching degree level — no cross-contamination. Use the general URL when all courses share the same page.
                              </p>
                            </div>

                            {/* Undergraduate English URL */}
                            <div className="flex flex-col gap-1">
                              <label className="text-[11px] font-medium text-muted-foreground">Undergraduate English URL</label>
                              <div className="flex gap-2 items-center">
                                <input
                                  type="url"
                                  className="flex-1 h-7 rounded border border-border bg-background px-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
                                  placeholder="https://www.example.edu/study/undergraduate/entry-requirements/"
                                  value={quickSettings?.central_english_ug_url ?? ""}
                                  onChange={e => setQuickSettings(q => q ? { ...q, central_english_ug_url: e.target.value } : { central_english_url: "", central_english_ug_url: e.target.value, central_english_pg_url: "", central_fees_url: "", auto_interact_all: false })}
                                />
                                <button
                                  className="shrink-0 h-7 px-2.5 rounded border border-border bg-background text-xs hover:bg-muted/60 disabled:opacity-50 transition-colors"
                                  disabled={quickSettingsSaving || quickSettingsLoading}
                                  onClick={() => selected && void saveQuickSettings(selected, { central_english_ug_url: quickSettings?.central_english_ug_url ?? "" })}
                                >
                                  {quickSettingsSaving ? "Saving…" : "Save"}
                                </button>
                              </div>
                            </div>

                            {/* Postgraduate English URL */}
                            <div className="flex flex-col gap-1">
                              <label className="text-[11px] font-medium text-muted-foreground">Postgraduate English URL</label>
                              <div className="flex gap-2 items-center">
                                <input
                                  type="url"
                                  className="flex-1 h-7 rounded border border-border bg-background px-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
                                  placeholder="https://www.example.edu/study/postgraduate/entry-requirements/"
                                  value={quickSettings?.central_english_pg_url ?? ""}
                                  onChange={e => setQuickSettings(q => q ? { ...q, central_english_pg_url: e.target.value } : { central_english_url: "", central_english_ug_url: "", central_english_pg_url: e.target.value, central_fees_url: "", auto_interact_all: false })}
                                />
                                <button
                                  className="shrink-0 h-7 px-2.5 rounded border border-border bg-background text-xs hover:bg-muted/60 disabled:opacity-50 transition-colors"
                                  disabled={quickSettingsSaving || quickSettingsLoading}
                                  onClick={() => selected && void saveQuickSettings(selected, { central_english_pg_url: quickSettings?.central_english_pg_url ?? "" })}
                                >
                                  {quickSettingsSaving ? "Saving…" : "Save"}
                                </button>
                              </div>
                            </div>

                            {/* General English URL (all-levels fallback) */}
                            <div className="flex flex-col gap-1">
                              <label className="text-[11px] font-medium text-muted-foreground">General English URL <span className="font-normal opacity-70">(all levels, used when no UG/PG URL is set)</span></label>
                              <div className="flex gap-2 items-center">
                                <input
                                  type="url"
                                  className="flex-1 h-7 rounded border border-border bg-background px-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
                                  placeholder="https://www.example.edu/entry-requirements/"
                                  value={quickSettings?.central_english_url ?? ""}
                                  onChange={e => setQuickSettings(q => q ? { ...q, central_english_url: e.target.value } : { central_english_url: e.target.value, central_english_ug_url: "", central_english_pg_url: "", central_fees_url: "", auto_interact_all: false })}
                                />
                                <button
                                  className="shrink-0 h-7 px-2.5 rounded border border-border bg-background text-xs hover:bg-muted/60 disabled:opacity-50 transition-colors"
                                  disabled={quickSettingsSaving || quickSettingsLoading}
                                  onClick={() => selected && void saveQuickSettings(selected, { central_english_url: quickSettings?.central_english_url ?? "" })}
                                >
                                  {quickSettingsSaving ? "Saving…" : "Save"}
                                </button>
                              </div>
                            </div>
                          </div>

                          {/* Central Fees page URL */}
                          <div className="flex flex-col gap-1">
                            <label className="text-xs font-medium">Central Fees URL</label>
                            <p className="text-[11px] text-muted-foreground leading-snug">
                              If international fees are listed on a shared page (e.g. a tuition fees PDF or table), paste that URL here.
                            </p>
                            <div className="flex gap-2 items-center">
                              <input
                                type="url"
                                className="flex-1 h-7 rounded border border-border bg-background px-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
                                placeholder="https://www.example.edu/fees/"
                                value={quickSettings?.central_fees_url ?? ""}
                                onChange={e => setQuickSettings(q => q ? { ...q, central_fees_url: e.target.value } : { central_english_url: "", central_english_ug_url: "", central_english_pg_url: "", central_fees_url: e.target.value, auto_interact_all: false })}
                              />
                              <button
                                className="shrink-0 h-7 px-2.5 rounded border border-border bg-background text-xs hover:bg-muted/60 disabled:opacity-50 transition-colors"
                                disabled={quickSettingsSaving || quickSettingsLoading}
                                onClick={() => selected && void saveQuickSettings(selected, { central_fees_url: quickSettings?.central_fees_url ?? "" })}
                              >
                                {quickSettingsSaving ? "Saving…" : "Save"}
                              </button>
                            </div>
                          </div>

                          {/* Auto-interact-all toggle */}
                          <div className="flex items-start gap-3 pt-1">
                            <button
                              role="switch"
                              aria-checked={quickSettings?.auto_interact_all ?? false}
                              className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                                (quickSettings?.auto_interact_all ?? false) ? "bg-blue-600" : "bg-input"
                              }`}
                              disabled={quickSettingsSaving || quickSettingsLoading}
                              onClick={() => {
                                const next = !(quickSettings?.auto_interact_all ?? false);
                                setQuickSettings(q => q ? { ...q, auto_interact_all: next } : { central_english_url: "", central_english_ug_url: "", central_english_pg_url: "", central_fees_url: "", auto_interact_all: next });
                                if (selected) void saveQuickSettings(selected, { auto_interact_all: next });
                              }}
                            >
                              <span className={`pointer-events-none block h-4 w-4 rounded-full bg-background shadow-lg ring-0 transition-transform ${(quickSettings?.auto_interact_all ?? false) ? "translate-x-4" : "translate-x-0"}`} />
                            </button>
                            <div className="flex flex-col gap-0.5">
                              <span className="text-xs font-medium">Auto-expand all collapsed sections</span>
                              <span className="text-[11px] text-muted-foreground leading-snug">
                                When enabled, after the browser loads each course page the scraper automatically clicks every collapsed accordion and <code className="font-mono">&lt;details&gt;</code> element before reading the page. Use when IELTS or fee information is hidden behind expandable sections.
                              </span>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                  <textarea
                    ref={textareaRef}
                    className="flex-1 resize-none font-mono text-xs p-4 bg-muted/20 focus:outline-none focus:bg-background transition-colors"
                    value={editorYaml}
                    onChange={e => setEditorYaml(e.target.value)}
                    spellCheck={false}
                    placeholder={`# University Name\n# Hostname: www.example.edu.au\n\ndiscovery: {}\nextraction:\n  fees:\n    default_currency: "AUD"\n`}
                  />
                </>
              )}

              <div className="px-4 py-1.5 border-t bg-muted/30 text-xs text-muted-foreground flex items-center gap-4">
                {view === "history" ? (
                  <span>{history.length} saved version{history.length !== 1 ? "s" : ""}</span>
                ) : view === "debugger" ? (
                  <span className="text-orange-600 dark:text-orange-400 flex items-center gap-1">
                    <Bug className="h-3 w-3" />
                    Debugger — read-only view of runtime config &amp; rejection log
                  </span>
                ) : view === "diff" ? (
                  <>
                    <span>Diff: saved → current edit</span>
                    {isDirty ? (
                      <span className="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                    ) : (
                      <span>No changes</span>
                    )}
                  </>
                ) : (
                  <>
                    <span>{editorYaml.split("\n").length} lines</span>
                    {isDirty ? (
                      <span className="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                    ) : (
                      <span>Changes take effect on next scrape job</span>
                    )}
                  </>
                )}
                {selectedConfig?.university_name && (
                  <span className="text-green-700">
                    ✓ Linked to {selectedConfig.university_name}
                  </span>
                )}
                {selected && selectedConfig?.university_id == null && (
                  <span className="text-amber-600">
                    ⚠ No university linked — add <code className="font-mono">{'# Hostname: www.example.edu'}</code> to the YAML
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Generate modal */}
      {showNewModal && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className={cn("bg-background rounded-xl shadow-xl w-full p-6 space-y-4", newModalMode === "manual" ? "max-w-2xl" : "max-w-md")}>
            <div className="flex items-center justify-between">
              <h2 className="font-semibold text-lg">New University Config</h2>
              <button onClick={() => setShowNewModal(false)} className="p-1 rounded hover:bg-muted">
                <X className="h-4 w-4" />
              </button>
            </div>

            {newModalMode === "ai" ? (
              <>
                <p className="text-sm text-muted-foreground">
                  Enter the university details and let AI generate a starter YAML config based on the website structure.
                </p>

                <div className="space-y-3">
                  <div>
                    <Label className="text-xs">University Name *</Label>
                    <Input
                      className="mt-1"
                      placeholder="e.g. Macquarie University"
                      value={genForm.university_name}
                      onChange={e => setGenForm(f => ({ ...f, university_name: e.target.value }))}
                    />
                  </div>
                  <div>
                    <Label className="text-xs">Website URL *</Label>
                    <Input
                      className="mt-1"
                      placeholder="e.g. https://www.mq.edu.au"
                      value={genForm.website_url}
                      onChange={e => setGenForm(f => ({ ...f, website_url: e.target.value }))}
                    />
                  </div>
                  <div>
                    <Label className="text-xs">Country</Label>
                    <div className="mt-1">
                      <CountrySelect
                        value={genForm.country}
                        onChange={v => setGenForm(f => ({ ...f, country: v }))}
                        className="h-9"
                      />
                    </div>
                  </div>
                  <div>
                    <Label className="text-xs">Notes for AI (optional)</Label>
                    <Input
                      className="mt-1"
                      placeholder="e.g. React SPA, NZ dollars, filters domestic courses"
                      value={genForm.notes}
                      onChange={e => setGenForm(f => ({ ...f, notes: e.target.value }))}
                    />
                  </div>
                </div>

                <div className="flex gap-2 pt-1">
                  <Button variant="outline" className="flex-1" onClick={() => setShowNewModal(false)} disabled={generating}>
                    Cancel
                  </Button>
                  <Button
                    className="flex-1"
                    onClick={handleGenerate}
                    disabled={generating || !genForm.university_name.trim() || !genForm.website_url.trim()}
                  >
                    {generating
                      ? <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      : <Sparkles className="h-4 w-4 mr-2" />}
                    {generating ? "Working…" : "Generate with AI"}
                  </Button>
                </div>

                {generating && (
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin shrink-0" />
                      <span className="truncate">{genStage}</span>
                    </div>
                    <div className="w-full h-1 bg-muted rounded-full overflow-hidden relative">
                      <style>{`@keyframes indbar{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}`}</style>
                      <div className="absolute h-full w-1/3 bg-primary rounded-full" style={{ animation: "indbar 1.4s ease-in-out infinite" }} />
                    </div>
                  </div>
                )}

                {!generating && (
                  <div className="flex flex-col items-center gap-1">
                    <p className="text-xs text-muted-foreground">
                      Uses Gemini AI · crawls the site first · review before saving
                    </p>
                    <button
                      className="text-xs text-muted-foreground hover:text-foreground underline underline-offset-2"
                      onClick={() => setNewModalMode("manual")}
                    >
                      <Code className="inline h-3 w-3 mr-1 -mt-0.5" />
                      Write manually instead
                    </button>
                  </div>
                )}
              </>
            ) : (
              <>
                <p className="text-sm text-muted-foreground">
                  Enter a slug and write or paste the YAML config directly. The sample template is pre-loaded — edit as needed.
                </p>

                <div className="space-y-3">
                  <div>
                    <Label className="text-xs">Config Slug *</Label>
                    <Input
                      className="mt-1 font-mono"
                      placeholder="e.g. macquarie or utas"
                      value={manualSlug}
                      onChange={e => setManualSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                    />
                    <p className="text-[11px] text-muted-foreground mt-1">
                      Lowercase letters, numbers, hyphens. Usually the university's short name.
                    </p>
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <Label className="text-xs">YAML Config</Label>
                      <button
                        className="text-[11px] text-muted-foreground hover:text-foreground underline"
                        onClick={() => setManualYaml(SAMPLE_YAML)}
                      >
                        Reset to template
                      </button>
                    </div>
                    <textarea
                      className="w-full h-64 font-mono text-xs border rounded-md p-2 bg-muted/30 resize-y focus:outline-none focus:ring-2 focus:ring-ring"
                      value={manualYaml}
                      onChange={e => setManualYaml(e.target.value)}
                      spellCheck={false}
                    />
                  </div>
                </div>

                <div className="flex gap-2 pt-1">
                  <Button variant="outline" className="flex-1" onClick={() => setShowNewModal(false)}>
                    Cancel
                  </Button>
                  <Button
                    className="flex-1"
                    onClick={handleCreateManually}
                    disabled={!manualSlug.trim()}
                  >
                    <Code className="h-4 w-4 mr-2" />
                    Open in Editor
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Unsaved changes confirmation dialog */}
      {pendingSlug !== null && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="bg-background rounded-xl shadow-xl w-full max-w-sm p-6 space-y-4">
            <div>
              <h2 className="font-semibold text-base">Unsaved changes</h2>
              <p className="text-sm text-muted-foreground mt-1">
                You have unsaved edits to <span className="font-mono font-medium">{selected ?? "current draft"}</span>.
                Switching to <span className="font-mono font-medium">{pendingSlug}</span> will discard them.
              </p>
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                className="flex-1"
                onClick={() => setPendingSlug(null)}
              >
                Keep editing
              </Button>
              <Button
                variant="destructive"
                className="flex-1"
                onClick={confirmDiscard}
              >
                Discard & switch
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

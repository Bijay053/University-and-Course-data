# University YAML — Guide for AI Assistants (ChatGPT / Claude)

> **Purpose:** This document is written specifically so that ChatGPT, Claude, or any
> AI assistant can produce a correct per-university YAML config **without access to the
> codebase**, given only the information below plus answers to a short set of questions
> about the target university.
>
> For the operator troubleshooting guide see `YAML_OPERATIONS_GUIDE.md`.
> For the complete annotated schema see `scraper_config/_template.yaml`.

---

## Quick answer: yes, an AI can create this YAML

The scraper is driven by a `UniConfig` Pydantic model.  Every university has a file at:

```
backend-py/scraper_config/unis/<slug>.yaml
```

That file is **deep-merged on top of `defaults.yaml`**, so you only need to write the
fields that differ from the global defaults.  A minimal working file is often 5–10 lines.

---

## Step 0 — Derive the slug (filename)

```
hostname  →  strip "www."  →  take the first domain label
```

| Hostname | Slug | File |
|---|---|---|
| `www.aut.ac.nz` | `aut` | `unis/aut.yaml` |
| `www.uow.edu.au` | `uow` | `unis/uow.yaml` |
| `bond.edu.au` | `bond` | `unis/bond.yaml` |
| `www.hud.ac.uk` | `hud` | `unis/hud.yaml` |

> **Warning — generic subdomain trap:** if the university's courses live on a subdomain
> like `study.csu.edu.au`, the slug is still `csu` (derived from the apex domain
> `csu.edu.au`), not `study`.  Only use the subdomain label when the entire university is
> hosted there with no apex (very rare).

---

## Step 1 — Questions to ask before writing the YAML

Ask the user (or look up the university website yourself) and note the answers:

| # | Question | Why it matters |
|---|---|---|
| 1 | What is the university's main website URL? | Derives the slug and seed URLs |
| 2 | What country is it in? | Sets `default_currency` (AUD/NZD/GBP/USD) |
| 3 | Is the course listing page rendered by JavaScript (React/Angular/Vue SPA), or is it plain HTML? | Determines discovery strategy |
| 4 | Does browsing to a course-listing page with JavaScript disabled (or via `curl`) return course links? | Confirms static BFS will work |
| 5 | Is the site protected by Cloudflare? (Look for "Checking your browser" or "Cloudflare" in the page source) | Sets `use_stealth_browser` |
| 6 | What URL pattern do individual course pages follow? e.g. `/courses/bachelor-of-arts` | Sets `allow_url_patterns` / `must_contain` |
| 7 | Does the university publish a single fee schedule page for international students? If yes, what is the URL? | Sets `fees.central_page` |
| 8 | Does the university publish a fee schedule PDF? If yes, what is the URL? | Sets `fees.fees_pdf_url` |
| 9 | Does the individual course page show an international fee, or does it only show a domestic fee? | Sets `prefer_international`, `reject_keywords` |
| 10 | Does the university publish a central English requirements page? If yes, what is the URL? | Sets `english.central_page` |
| 11 | Does the site require an `?international=true` (or similar) query parameter to show international fees / intakes? | Sets `url_rewrites` |
| 12 | Does the H1 on each course page include the university name as a suffix (e.g. "Bachelor of Arts \| My University")? | Sets `course_name.strip_title_suffixes` |
| 13 | Is there a JSON/REST API for the course catalogue? (Check DevTools → Network → XHR on the course search page) | Potentially sets `generic_search_api` block |
| 14 | Approximately how many courses does the university offer internationally? | Validates `expected_min_courses` |
| 15 | Does the university accept online-only enrolment for international students? (Distance education) | Sets `filters.online_only.enabled` |

---

## Step 2 — Choose a base pattern

Read the table below, pick the **first pattern that matches**, and start from that
template.  Each template is a complete, copy-pasteable YAML.

---

### Pattern 1 — Standard server-rendered HTML (most common)

**Matches when:**
- `curl https://www.uni.edu.au/courses/` returns visible course links in the HTML
- No Cloudflare challenge page
- Individual course pages at a consistent URL like `/courses/<slug>` or `/study/<slug>`

```yaml
# <University Full Name>
# Hostname: www.uni.edu.au
# Country: Australia  |  Currency: AUD

discovery:
  always_sitemap_supplement: true        # keeps sitemap as safety net
  allow_url_patterns:
    - '/courses/'                        # ONLY keep URLs containing this
  block_url_patterns:
    - '/news'
    - '/events'
    - '/about'
    - '/staff'
    - '/research'

extraction:
  fees:
    default_currency: "AUD"
    central_page: "https://www.uni.edu.au/international/fees/"   # if one exists; remove line otherwise

  english:
    central_page: "https://www.uni.edu.au/international/english-requirements/"  # if one exists
    default_ielts: 6.5          # only if the uni states a single entry standard

  course_name:
    strip_title_suffixes:
      - " | My University"      # only if H1 includes the uni name

  filters:
    domestic_only:
      enabled: false
    online_only:
      enabled: true
```

**Trim ruthlessly** — remove any line that doesn't apply.  A 4-line file is fine.

---

### Pattern 2 — JavaScript SPA with sitemap

**Matches when:**
- `curl` on the listing page returns an empty shell (`<div id="app"></div>` with no links)
- But the university has a sitemap at `/sitemap.xml` or `/sitemap_index.xml`

```yaml
# <University Full Name>
# Hostname: www.uni.edu.au
# CMS: React/Next.js SPA with XML sitemap

discovery:
  always_sitemap_supplement: true
  skip_browser_discovery: false           # let browser BFS discover what sitemap misses
  allow_url_patterns:
    - '/courses/'
  block_url_patterns:
    - '/news'
    - '/events'
    - '/about'

extraction:
  fees:
    default_currency: "AUD"
    central_page: ""                      # fill in or remove

  filters:
    domestic_only:
      enabled: false
    online_only:
      enabled: true
```

---

### Pattern 3 — Cloudflare-protected SPA

**Matches when:**
- Browsing the site shows a "Checking your browser…" or "Cloudflare" page briefly
- `curl` returns `403 Forbidden` or a Cloudflare challenge HTML
- Regular headless Playwright also gets blocked (empty page body)

```yaml
# <University Full Name>
# Hostname: www.uni.edu.au
# CMS: React SPA behind Cloudflare Bot Management

discovery:
  use_stealth_browser: true               # patchright + Xvfb bypasses CF challenge
  always_sitemap_supplement: true
  seed_urls:
    - "https://www.uni.edu.au/courses/undergraduate"
    - "https://www.uni.edu.au/courses/postgraduate"
  allow_url_patterns:
    - '/courses/undergraduate/'
    - '/courses/postgraduate/'
  block_url_patterns:
    - '/news'
    - '/events'
    - '/about'
    - '/research'
  bfs_page_budget: 20                     # stealth is slow; keep budget low
  max_candidates: 800
  expected_min_courses: 200

extraction:
  max_parallel_fetch: 6                   # stealth sessions are slow; 4 is fine too
  fees:
    default_currency: "AUD"

  staging:
    reject_if_missing:
      - course_name
    skip_degree_qualifier_check: true     # SPA pages often lack degree prefix in H1
    require_international_fee: false      # fees may be on a JS tab; stage for review
```

> **Note:** `use_stealth_browser` adds ~3–5 s per page.  Only use it for Cloudflare-
> protected sites.  Do NOT set it for standard sites.

---

### Pattern 4 — Central fee page only (no per-course fee figures)

**Matches when:**
- Individual course pages don't show a dollar amount
- The university publishes a single fee schedule page or PDF listing all courses
- Or the fee is calculated via a credit-point calculator

```yaml
# <University Full Name>
# Fees: central schedule page only (no per-course fee on individual pages)

extraction:
  fees:
    default_currency: "AUD"
    central_page: "https://www.uni.edu.au/international/fees/fee-schedule/"
    force_central_fee_stage: true         # pass staging gate even when fee=None
```

If fees are in a **PDF** instead of a web page:

```yaml
extraction:
  fees:
    default_currency: "AUD"
    fees_pdf_url: "https://www.uni.edu.au/fees/2026-international-fee-schedule.pdf"
    prefer_annual_over_total: true        # store annual fee, not full-course total
```

---

### Pattern 5 — New Zealand university

**Matches when:** country = New Zealand

```yaml
# <University Full Name>
# Hostname: www.uni.ac.nz
# Country: New Zealand  |  Currency: NZD

discovery:
  always_sitemap_supplement: true
  allow_url_patterns:
    - '/study/qualifications/'            # adjust to match NZ uni URL structure

extraction:
  fees:
    default_currency: "NZD"
    prefer_annual_over_total: true        # NZ sites often show both annual + total

  english:
    degree_level_defaults:
      undergraduate:
        ielts: 6.0
        pte: 50
        toefl: 80
      postgraduate:
        ielts: 6.5
        pte: 58
        toefl: 90
    central_page: "https://www.uni.ac.nz/international/english-requirements/"
```

---

### Pattern 6 — UK university

**Matches when:** country = United Kingdom

```yaml
# <University Full Name>
# Hostname: www.uni.ac.uk
# Country: United Kingdom  |  Currency: GBP

discovery:
  always_sitemap_supplement: true
  allow_url_patterns:
    - '/study/undergraduate/'
    - '/study/postgraduate/'
  block_url_patterns:
    - '/research/'
    - '/about'
    - '/news'
    - '/events'
    - '/staff/'

extraction:
  fees:
    default_currency: "GBP"
    prefer_international: true
    prefer_annual_over_total: true
    reject_keywords:                      # drop any row that mentions domestic fees
      - "Home student"
      - "Home fee"
      - "UK student"
      - "UK fee"
      - "Domestic"

  english:
    trust_vision_ocr: false               # UK sites often have decorative IELTS images
    default_ielts: 6.0
    default_pte: 60
    default_toefl: 80

  intake:
    start_dates_only: true               # ignore application deadlines

  course_name:
    strip_title_suffixes:
      - " | My University"
      - " - My University"

  filters:
    online_only:
      enabled: false                     # UK universities often blend online + campus
```

---

### Pattern 7 — Requires international query parameter

**Matches when:**
- Adding `?international=true` (or similar) to the course URL shows international fees,
  intakes, and IELTS requirements that are absent from the base URL

```yaml
# International-student data behind a query parameter

extraction:
  url_rewrites:
    - host: "www.uni.edu.au"              # matches both uni.edu.au and www.uni.edu.au
      append_query: "international=true"
```

Common variants seen in production:
- UNE: `append_query: "international=true"`
- UOW: `append_query: "students=international&year=2026"`
- CQU: `append_query: "audience=INTERNATIONAL"`

---

### Pattern 8 — JSON / REST API course catalogue

**Matches when:**
- DevTools → Network → XHR on the course-search page shows a JSON API request
- The API returns a list of course URLs (not HTML)

```yaml
# Course catalogue served by a JSON/REST API (no BFS needed)

discovery:
  generic_search_api:
    enabled: true
    method: GET                          # or POST
    url: "https://api.uni.edu.au/courses/search"
    params:
      q: "*"
      rows: "250"
    root_path: "response.docs"           # dot-path to results array; null if response IS the array
    url_fields: [url, page_url, link]    # first non-empty field wins
    title_fields: [title, name, course_name]
    allow_url_patterns:
      - '^https://www\.uni\.edu\.au/courses/[a-z0-9-]+/?$'
    normalize_relative_urls: true
    base_url: "https://www.uni.edu.au"
    page_size: 100
    max_pages: 20
```

> **POST body APIs:** add a `body:` block (YAML dict) and set `method: POST`.
> For body-based pagination also add `body_pagination:` with `current_path`,
> `size_path`, `total_pages_path`.  See `scraper_config/unis/waikato.yaml` for
> a real working example.

---

### Pattern 9 — Distance-education / online-only university

**Matches when:**
- The university explicitly markets courses to international students as online/distance
- Example: Charles Sturt University (CSU), Open Universities Australia

```yaml
# Distance-education university — online-only courses ARE valid for international students

extraction:
  filters:
    online_only:
      enabled: false     # default is true; turn off for distance-ed unis
```

---

## Step 3 — Fill in common fields

After picking a pattern, add the following fields **only if they apply**:

### Course name has university suffix

Check: look at the `<h1>` on any course detail page.  If it says
`"Bachelor of Arts | My University"`, add:

```yaml
extraction:
  course_name:
    strip_title_suffixes:
      - " | My University"
      - " - My University"
      - " — My University"
      - "| My University"             # without leading space, just in case
```

List every realistic separator variant (pipe, dash, em-dash).

---

### Central English requirements page

If the university has a single page listing IELTS/PTE/TOEFL requirements for all courses:

```yaml
extraction:
  english:
    central_page: "https://www.uni.edu.au/international/english-requirements/"
```

If individual courses **vary** in their requirements and the course page is authoritative:

```yaml
extraction:
  english:
    course_english_priority: true
    central_page: "..."    # still useful as a last-resort fallback
```

---

### Band-name English requirements

Some universities publish named bands ("Band 1", "English Band B") rather than raw scores.
Map each band to its actual scores:

```yaml
extraction:
  english:
    band_mapping:
      "Band 1":
        ielts_overall: 6.0
        ielts_each: 5.5
        pte_overall: 50
        toefl_overall: 60
      "Band 2":
        ielts_overall: 6.5
        ielts_each: 6.0
        pte_overall: 58
        toefl_overall: 79
```

Valid `BandSpec` keys: `ielts_overall`, `ielts_each`, `pte_overall`, `toefl_overall`,
`cambridge_overall`, `duolingo_overall`.  Note: it is `cambridge_overall` (not `cae_overall`).

---

### Duration shows "maximum completion time" instead of program length

Some course pages show "up to 10 years" which is the max candidature, not the actual program.
Reject those sentences:

```yaml
extraction:
  text_cleaning:
    duration:
      reject_sentence_patterns:
        - 'up to \d+(?:\.\d+)? (?:years?|months?)'
        - 'maximum of \d+ (?:years?|months?)'
        - 'candidature.*\d+ years'
```

Use single-quoted strings for all regex patterns in YAML (avoids backslash escaping).

---

### BFS crawls navigation pages instead of courses

Symptom: scraper stages 200+ URLs but they are "About", "News", "Staff" pages.
Fix: add a whitelist (`allow_url_patterns`) and a blocklist (`block_url_patterns`):

```yaml
discovery:
  allow_url_patterns:
    - '/courses/'          # keep ONLY URLs with /courses/ in the path
  block_url_patterns:
    - '/news'
    - '/events'
    - '/staff'
    - '/about'
    - '/research'
    - '\.pdf$'
```

---

### Rolling/continuous intake (research degrees)

If PhD / MPhil programs have no fixed intake months but instead enrol year-round:

```yaml
extraction:
  intake:
    rolling_enrollment_label: "Rolling"
    rolling_enrollment_markers:
      - "rolling intake"
      - "enrol at any time"
      - "research degree"
```

---

## Step 4 — What NEVER to do

These are the most common mistakes that silently break the config:

| ❌ Wrong | ✅ Correct | Why |
|---|---|---|
| Inventing a new key name | Only use keys from `_template.yaml` | Unknown keys are silently ignored by Pydantic — no error, no effect |
| Nesting `extraction.fees` directly under the root | Always nest under `extraction:` | `fees:` at root level is ignored |
| Putting regex patterns in double quotes | Use single quotes: `'\d+'` | Double-quoted YAML requires `\\d+` which is a literal two-char sequence |
| Setting `use_stealth_browser` for non-Cloudflare sites | Only use when Cloudflare confirmed | Adds 3–5 s per page to every fetch |
| Changing `defaults.yaml` | Only edit `unis/<slug>.yaml` | `defaults.yaml` changes affect ALL universities |
| Setting `default_ielts` when scores vary by course | Only set when the uni publicly states ONE entry standard for ALL courses | Will overwrite correct per-course values |
| Using `cae_overall` in `band_mapping` | Use `cambridge_overall` | `cae_overall` is not a valid BandSpec field — silently ignored |

---

## Step 5 — Minimal starter template

If none of the patterns above fit, start here and add only what you need:

```yaml
# <University Full Name>
# Hostname: <www.uni.edu.au>
# Country: <Australia / NZ / UK / other>  |  Currency: <AUD / NZD / GBP>
# Discovery: <describe how courses are found>
# Notes: <any site quirks found during initial browser inspection>

discovery:
  always_sitemap_supplement: true

extraction:
  fees:
    default_currency: "AUD"    # change to NZD / GBP as appropriate

  filters:
    domestic_only:
      enabled: false
    online_only:
      enabled: true
```

---

## Step 6 — Complete annotated example (ARU — UK Cloudflare SPA)

This is a real production YAML.  Read the comments to understand each decision:

```yaml
# Anglia Ruskin University (ARU)
# Hostname: www.aru.ac.uk
# Country: UK  |  Currency: GBP
# CMS: React SPA behind Cloudflare — use_stealth_browser required

discovery:
  use_stealth_browser: true          # Cloudflare blocks regular Playwright
  seed_urls:
    - https://www.aru.ac.uk/study/course-search?levelofstudy=Undergraduate
    - https://www.aru.ac.uk/study/course-search?levelofstudy=Postgraduate
  always_sitemap_supplement: true
  sitemap_url: https://www.aru.ac.uk/sitemap.xml
  bfs_page_budget: 20                # stealth is slow; keep budget small
  max_candidates: 800
  expected_min_courses: 200

  allow_url_patterns:
    - '/study/undergraduate/'
    - '/study/postgraduate/'

  block_url_patterns:
    - '/study/course-search/?$'      # bare search widget (no JS = no results)
    - 'course-search\?.*levelofstudy=.*&'   # multi-filter combos burn budget
    - '/study/postgraduate/[^/]+-research$' # research pages have no intl fee
    - 'mphil'
    - '/news'
    - '/events'
    - '/about'

extraction:
  max_parallel_fetch: 12
  browser_wait_strategy: networkidle  # wait for JS API calls to finish
  browser_dcl_settle_ms: 1500

  fees:
    default_currency: GBP
    prefer_international: true
    prefer_annual_over_total: true
    prefer_year_one_over_total: true
    reject_keywords:
      - Home student
      - UK student
      - UK fee
      - Domestic

  english:
    trust_vision_ocr: false          # decorative images cause hallucinations
    default_ielts: 6.0
    default_pte: 60
    default_toefl: 80

  intake:
    start_dates_only: true

  course_name:
    strip_title_suffixes:
      - " | ARU"
      - " | Anglia Ruskin University"

  filters:
    online_only:
      enabled: false

  staging:
    reject_if_missing:
      - course_name
    skip_degree_qualifier_check: true   # SPA H1 lacks degree prefix
    require_international_fee: false    # fees on JS tabs; stage for human review
```

---

## Step 7 — Valid key quick-reference

All valid top-level keys and their nesting.  **Only these keys are valid.**
Any key not in this list is silently ignored.

```
discovery:
  generic_search_api:        (block — see Pattern 8)
  searchstax:                (block — only for SearchStax Solr providers like HUD)
  bfs_page_budget:           int
  max_candidates:            int
  always_sitemap_supplement: bool
  sitemap_url:               string
  fallback_subdomains:       list of strings (use {domain} placeholder)
  expected_min_courses:      int
  seed_urls:                 list of strings
  extra_course_urls:         list of strings
  allow_url_patterns:        list of regex strings
  block_url_patterns:        list of regex strings
  block_nav_patterns:        list of regex strings
  must_contain:              list of substrings
  skip_browser_discovery:    bool
  always_browser_discover:   bool
  use_stealth_browser:       bool
  browser_time_budget_s:     int
  browser_early_stop_courses: int
  scrape_do_fallback:        bool
  use_wayback:               bool | null
  auto_api_discovery:        bool

extraction:
  max_parallel_fetch:        int
  skip_browser_rescue:       bool
  prefer_blended_over_on_campus: bool
  default_course_location:   string
  campus_allowlist:          list of strings
  browser_wait_strategy:     string ("domcontentloaded" | "networkidle")
  browser_dcl_settle_ms:     int
  actions:                   list of action dicts (click_text, click_css, etc.)
  url_rewrites:              list of {host, path_contains?, append_query}

  fees:
    default_currency:        string ("AUD" | "NZD" | "GBP" | "USD")
    prefer_international:    bool
    prefer_annual_over_total: bool
    prefer_year_one_over_total: bool
    fee_url_suffix:          string
    force_central_fee_stage: bool
    max_annual_fee:          number
    central_page:            string (URL)
    fees_pdf_url:            string (URL)
    pdf_row_pattern:         regex string
    pdf_fee_term:            string ("Annual" | "Semester" | "Full Course")
    credit_points_per_unit:  int
    course_pdf_aliases:      dict {pdf_name: db_name}
    reject_keywords:         list of strings
    follow_links:            list of strings

  english:
    central_page:            string (URL)
    requirements_pdf_url:    string (URL)
    band_reference_url:      string (URL)
    course_english_priority: bool
    default_ielts:           float
    default_pte:             int
    default_toefl:           int
    degree_level_defaults:   dict (undergraduate/postgraduate/doctorate)
    trust_vision_ocr:        bool
    trust_tier1_vision_ocr_english: bool
    test_blocklist:          list ("ielts"|"pte"|"toefl"|"cambridge"|"duolingo"|"kite")
    follow_links:            list of strings
    band_mapping:            dict {band_label: BandSpec}

  filters:
    domestic_only:
      enabled:               bool
      text_must_appear_in:   string
    online_only:
      enabled:               bool

  intake:
    start_dates_only:        bool
    start_dates_window_chars: int
    rolling_enrollment_markers: list of strings
    rolling_enrollment_label: string

  course_name:
    strip_title_suffixes:    list of strings
    university_aliases:      list of strings

  text_cleaning:
    location:
      strip_patterns:        list of regex strings
    duration:
      split_on_slash:        bool
      reject_sentence_patterns: list of regex strings
    global_substring_blocklist: list of strings
    field_overrides:         list of {field, value, url_pattern}

  staging:
    reject_if_missing:       list of field names
    skip_degree_qualifier_check: bool
    require_international_fee:   bool
```

---

## Prompt template for ChatGPT / Claude

Copy this block and fill in the bracketed sections, then paste it to the AI:

```
I need you to create a per-university YAML scraper config for [University Full Name].

Here is all the context you need to produce the YAML:

University details:
- Full name: [e.g. "University of Auckland"]
- Hostname: [e.g. "www.auckland.ac.nz"]
- Country: [e.g. "New Zealand"]
- Currency: [e.g. "NZD"]
- Course listing URL: [e.g. "https://www.auckland.ac.nz/en/study/study-options/find-a-study-option.html"]
- Individual course URL pattern: [e.g. "/en/study/study-options/find-a-study-option/bachelor-of-arts"]
- Is the site Cloudflare-protected? [Yes / No / Unsure]
- Is the site a JavaScript SPA? [Yes / No / Unsure]
- Central fee schedule page: [URL or "None"]
- Fee schedule PDF: [URL or "None"]
- Central English requirements page: [URL or "None"]
- Does the H1 include the university name as suffix? [Yes — " | Auckland" / No]
- Does a course URL need ?international=true or similar to show international data? [Yes / No / Query param if yes]
- Are online-only international courses permitted? [Yes (distance-ed) / No]
- Approximate course count: [e.g. "300"]

Your task:
1. Derive the slug from the hostname.
2. Pick the matching pattern from the guide you have.
3. Write the minimal YAML (only keys that differ from the defaults).
4. Add a header comment block summarising: hostname, country, currency, discovery strategy, and any known quirks.
5. Use single-quoted strings for all regex patterns.
6. Only use keys from the valid key list — no invented keys.
7. Output the YAML as a single code block, ready to save as `scraper_config/unis/<slug>.yaml`.
```

---

## Where to save and how to apply

1. Save the file to `backend-py/scraper_config/unis/<slug>.yaml`.
2. Ensure the university row exists in the `universities` table with the correct hostname in `scrape_url`.
3. Trigger a scrape job from the UI (Scraper → Run Scrape → select the university).
4. Check the staging results:
   - If < 5 courses staged: discovery failed → revisit `discovery.*` fields.
   - If many courses missing fees: revisit `extraction.fees.*` fields.
   - If many courses staging as `review` (not `pending`): check completeness fields.
5. Iterate: add/adjust YAML fields using `YAML_OPERATIONS_GUIDE.md` Section recipes.

---

## See also

- `scraper_config/_template.yaml` — complete annotated schema with every possible key
- `scraper_config/defaults.yaml` — global defaults (shows what you inherit for free)
- `docs/YAML_OPERATIONS_GUIDE.md` — symptom → recipe troubleshooting guide
- Real examples: `unis/uwa.yaml` (complex AU), `unis/aru.yaml` (UK Cloudflare SPA),
  `unis/waikato.yaml` (NZ API discovery), `unis/bond.yaml` (minimal — 6 lines)

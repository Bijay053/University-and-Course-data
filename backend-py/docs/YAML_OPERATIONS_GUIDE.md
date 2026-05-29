# YAML Operations Guide

> **Audience:** Operators adding or tuning per-university configurations.
> Engineers: see `docs/architecture.md` for the system design.

---

## How per-uni YAML works

Every scrape job loads configuration in this order (later entries win):

```
scraper_config/defaults.yaml          ← global defaults
  └── DB scrape_config table          ← legacy DB overrides (feePage, entryPage)
       └── scraper_config/unis/<slug>.yaml   ← per-uni overrides  ← YOU EDIT THIS
```

The merged result is a `UniConfig` object available to all extractors for
the duration of that job.  **Changes to a per-uni YAML affect only that
university** and do not require a regression sweep.  Changes to
`defaults.yaml` affect every university and require human approval +
full regression sweep.

---

## Flowchart: I see a bug → what do I do?

```
Scrape result has a problem
         │
         ├─ Discovery found wrong/too few pages? ──────────► SECTION A (Discovery)
         │
         ├─ Fee is wrong/missing? ────────────────────────► SECTION B (Fees)
         │
         ├─ IELTS/PTE/TOEFL is wrong/missing/hallucinated? ► SECTION C (English)
         │
         ├─ Intake months are wrong? ─────────────────────► (intake fields below)
         │
         ├─ Duration shows max-time instead of program len? ► SECTION D (Duration)
         │
         ├─ Location is blank or wrong? ──────────────────► SECTION E (Location)
         │
         ├─ Course name has junk suffix? ─────────────────► SECTION F (Course name)
         │
         ├─ Domestic-only courses are being staged? ──────► filters.domestic_only
         │
         └─ Nothing above fits → escalate to engineering   ► SECTION G (Not YAML-fixable)
```

---

## SECTION A — Discovery fields

Discovery settings control which URLs the crawler finds before any
extraction happens.  They are safe to replay against unknown universities.

| Field | Default | When to use |
|-------|---------|-------------|
| `always_sitemap_supplement` | `false` | JS-rendered SPAs (Torrens, CDU) where BFS burns its budget on marketing pages |
| `fallback_subdomains` | `[]` | BFS finds < 5 candidates; probe `handbook.{domain}`, `study.{domain}`, etc. |
| `block_url_patterns` | `[]` | Drop noisy non-course URLs (news, events, staff, old handbook years) |
| `allow_url_patterns` | `[]` | Whitelist — keep ONLY URLs matching these regex patterns. Cuts Gemini cost significantly on large sites |
| `must_contain` | `[]` | Simpler whitelist — drop any URL that doesn't contain one of these substrings. No regex needed |
| `sitemap_url` | `null` | Sitemap is not at `/sitemap.xml` or `/sitemap_index.xml` |
| `bfs_page_budget` | `null` (12 fast / 25 full) | Site has more listing pages than default budget (e.g. UOW ~62) |
| `max_candidates` | `null` (20 fast / 200 full) | Sitemap publishes more courses than cap; late-alphabet courses get dropped |
| `always_browser_discover` | `false` | Cloudflare-protected sites where plain HTTP BFS misses entire faculties |
| `use_stealth_browser` | `false` | Regular headless Playwright fails Cloudflare challenge (e.g. Macquarie) |
| `use_wayback` | `false` | All live-site discovery fails; last resort |
| `extra_course_urls` | `[]` | Inject specific URLs known to all tiers consistently miss (surgical fallback) |

### Recipe A1 — "BFS crawls nav pages instead of courses"

```yaml
discovery:
  must_contain:
    - /courses/
    - /programs/
  block_url_patterns:
    - /news/
    - /events/
    - /staff/
    - /about/
```

### Recipe A2 — "Sitemap not auto-discovered"

```yaml
discovery:
  sitemap_url: https://www.example.edu.au/custom-sitemap.xml
```

### Recipe A3 — "BFS finds < 5 courses, real ones are on a subdomain"

```yaml
discovery:
  fallback_subdomains:
    - handbook.{domain}
    - study.{domain}
```

### Recipe A4 — "Cloudflare blocks the crawler silently"

Try in order (escalating cost):
```yaml
# Step 1 — supplement BFS with browser discovery:
discovery:
  always_browser_discover: true

# Step 2 — if 403s persist, use stealth browser stack:
discovery:
  use_stealth_browser: true
```
> **Note:** `use_stealth_browser` adds ~2-4s per page.  Do NOT enable fleet-wide.

---

## SECTION B — Fee fields

| Field | Default | When to use |
|-------|---------|-------------|
| `fees.central_page` | `null` | University publishes a single fee schedule page (not per-course) |
| `fees.fees_pdf_url` | `null` | Fee schedule is a PDF |
| `fees.force_central_fee_stage` | `false` | Courses lack per-course fees but a central schedule exists; prevents staging gate rejection |
| `fees.default_currency` | `"AUD"` | Change to `"NZD"` for New Zealand universities |
| `fees.credit_points_per_unit` | `null` | Site publishes per-unit fees; multiply by this to get full-course fee |
| `fees.prefer_annual_over_total` | `false` | PDF row has both annual and total; store annual (e.g. Torrens) |
| `fees.prefer_year_one_over_total` | `false` | Course page has Year-1 and Total; store Year-1 (e.g. Curtin) |
| `fees.pdf_overrides_page_regex` | `false` | PDF schedule is authoritative; overwrite values already set by page-regex or Gemini |
| `fees.pdf_parser` | `null` | Switch to `"columnar"` for PDFs where course titles wrap across lines (poppler pdftotext) |
| `fees.pdf_row_pattern` | `null` | Per-uni regex for fee PDF rows; must define `(?P<cricos>...)` group |
| `fees.pdf_fee_term` | `null` | Override fee term emitted by PDF rows (`"Annual"` or `"Full Course"`) |
| `fees.course_pdf_aliases` | `{}` | Map DB course name → PDF row name when they differ enough to break fuzzy matching |

### Recipe B1 — "University publishes fees on a calculator, not a page"

```yaml
extraction:
  fees:
    force_central_fee_stage: true
    # This marks every staged course as "has central fee page" so the
    # no_international_fee staging gate doesn't block them.
    # Set central_page so operators know where to find fees manually:
    central_page: https://www.example.edu.au/fees/calculator
```

### Recipe B2 — "Fee extracted from PDF but PDF has multi-line course titles"

```yaml
extraction:
  fees:
    fees_pdf_url: https://www.example.edu.au/fees-2026.pdf
    pdf_parser: "columnar"
    pdf_overrides_page_regex: true
```

### Recipe B3 — "Per-course page shows total fee but we want annual"

```yaml
extraction:
  fees:
    prefer_year_one_over_total: true   # from page regex (e.g. Curtin)
    # OR:
    prefer_annual_over_total: true     # from PDF row (e.g. Torrens)
```

---

## SECTION C — English requirement fields

| Field | Default | When to use |
|-------|---------|-------------|
| `english.central_page` | `null` | University-wide English requirements on a single page |
| `english.requirements_pdf_url` | `null` | Requirements published as a PDF |
| `english.trust_vision_ocr` | `true` | Set `false` when Gemini hallucinates scores from decorative images (e.g. ACAP) |
| `english.trust_tier1_vision_ocr_english` | `false` | Set `true` ONLY when requirements live exclusively inside images the DOM detector misses (e.g. ASAHE) |
| `english.default_ielts` | `null` | University-wide IELTS floor (last resort; only when uni publicly states a single standard) |
| `english.default_pte` | `null` | University-wide PTE floor |
| `english.default_toefl` | `null` | University-wide TOEFL iBT floor |
| `english.test_blocklist` | `[]` | Drop specific test names from results (e.g. site mentions a test the uni doesn't accept) |

### Recipe C1 — "Gemini is hallucinating IELTS scores from banner images"

```yaml
extraction:
  english:
    trust_vision_ocr: false
```

### Recipe C2 — "PTE scores appear on pages that don't actually list them"

```yaml
extraction:
  english:
    test_blocklist:
      - pte
```

---

## SECTION D — Duration fields

| Field | Default | When to use |
|-------|---------|-------------|
| `text_cleaning.duration.split_on_slash` | `false` | Compound duration strings like `"X years / Y subjects / Z trimesters"` (KBS, Torrens) |
| `text_cleaning.duration.reject_sentence_patterns` | `[]` | Skip sentences that match these patterns from the duration tournament; use when the page shows max-completion-time instead of program length |

### Recipe D1 — "Duration shows maximum candidature time instead of program length"

Use YAML single-quoted strings for regex patterns:

```yaml
extraction:
  text_cleaning:
    duration:
      reject_sentence_patterns:
        - 'up to \d+ years to complete'
        - 'up to \d+ months'
        - 'candidature.*\d+ years'
```

---

## SECTION E — Location fields

| Field | Default | When to use |
|-------|---------|-------------|
| `text_cleaning.location.strip_patterns` | `[]` | Strip CMS noise from raw location strings before parsing |
| `default_course_location` | `null` | Fallback when all extractors return blank; prevents online-only staging gate from rejecting on-campus courses whose location HTML was missing |

### Recipe E1 — "Location includes a trailing label like 'Delivery method'"

Use YAML single-quoted strings for all regex patterns — backslashes need no extra escaping:

```yaml
extraction:
  text_cleaning:
    location:
      strip_patterns:
        - '\bDelivery\s*method\b.*'
        - '^\s*\^.*$'          # ACAP "^ ^Available in Perth" cruft
```

### Recipe E2 — "Cloudflare occasionally delivers partial HTML that omits the Location panel"

```yaml
extraction:
  default_course_location: "Hobart"   # primary campus city
  max_parallel_fetch: 2               # reduce concurrency to avoid 429s
```

---

## SECTION F — Course name fields

| Field | Default | When to use |
|-------|---------|-------------|
| `course_name.strip_title_suffixes` | `[]` | CMS appends a fixed provider string to every H1; strip it before standard suffix detection |

### Recipe F1 — "Course name ends with ' : the University of Western Australia'"

```yaml
extraction:
  course_name:
    strip_title_suffixes:
      - " : the University of Western Australia"
```

---

## Intake fields

| Field | Default | When to use |
|-------|---------|-------------|
| `intake.rolling_enrollment_label` | `null` | Research degrees (PhD/MPhil) with continuous enrolment rather than fixed intakes |
| `intake.rolling_enrollment_markers` | `[]` | Substrings that trigger the rolling label (case-insensitive) |

### Recipe — "PhD programs show no intake months"

```yaml
extraction:
  intake:
    rolling_enrollment_label: "Rolling"
    rolling_enrollment_markers:
      - "enrolment shall be continuous"
      - "rolling intake"
      - "research degree"
```

---

## URL rewrites

Append query parameters to every course URL before fetching, so the
international-student view (fees, IELTS, intakes, campus list) is visible.

```yaml
extraction:
  url_rewrites:
    - host: www.example.edu.au
      append_query: "international=true"
    - host: www.example.edu.au
      path_contains: /courses/
      append_query: "audience=INTERNATIONAL&year=2026"
```

Real examples shipped to production:
- **UNE:** `append_query: "international=true"`
- **UOW:** `append_query: "students=international&year=2026"`
- **CQU:** `append_query: "audience=INTERNATIONAL"`

---

## Filter fields

| Field | Default | When to use |
|-------|---------|-------------|
| `filters.domestic_only.enabled` | `false` | Enable when the site lists non-international courses without marking them (e.g. ACAP) |
| `filters.online_only.enabled` | `true` | Set `false` for distance-education universities (CSU, OUA) |
| `filters.broken_cms_retry_strip_query` | `false` | Most pages need a query flag but a few return a branded error template with that flag; retry bare URL |

---

## Other fields

| Field | Default | When to use |
|-------|---------|-------------|
| `max_parallel_fetch` | `null` (4) | Lower to `2` or `1` for Cloudflare-heavy sites with aggressive rate-limiting (e.g. UTAS) |
| `text_cleaning.global_substring_blocklist` | `[]` | Strip boilerplate from EVERY string field (e.g. "Apply Now", "Find out more") |
| `text_cleaning.field_overrides` | `[]` | Hard-set a specific field for URLs matching a regex (surgical last resort) |
| `staging.reject_if_missing` | `["course_name"]` | Add fields that must be non-empty to pass the staging gate |

---

## SECTION G — What is NOT YAML-fixable

If the problem is one of the following, **do not spend time iterating on YAML** —
escalate to engineering.

| Problem | Why YAML can't fix it | Engineering fix |
|---------|----------------------|-----------------|
| Course page renders as 0 bytes — `always_browser_discover` and `use_stealth_browser` both tried | SPA hydrates content via XHR after page load; browser may be timing out before render | Add host to `_NETWORKIDLE_HOSTS` in `per_course_browser.py` so browser waits for `networkidle` + settle time instead of `domcontentloaded` |
| Scrape times out on every page for a specific host | Heavy third-party trackers prevent `networkidle` from ever settling; browser eats full 60s per course | Add host to `_SKIP_BROWSER_HOSTS` in `per_course_browser.py` to skip browser pass entirely (static HTML must be sufficient) |
| Cloudflare 403 even with `use_stealth_browser: true` | WAF fingerprinting beyond what patchright bypasses | Engineering investigation; may need a new XHR/API extraction route |
| Fees only behind a JS fee calculator (AJAX endpoint) | No static HTML to parse | Custom API extractor targeting the calculator's XHR endpoint |
| English requirements behind a login wall | Cannot be crawled without authentication | Out of scope unless the university provides a public API or credential |
| Course data inside an embedded third-party iframe | Cross-origin restriction blocks the iframe content | Custom fetcher targeting the iframe `src` URL directly |
| CRICOS 0% coverage even though page contains "CRICOS" | Regex pattern doesn't match the site's CRICOS formatting | Run `scripts/cricos_coverage_diagnostic.py --uni-id N`, then fix regex in `extractors/cricos_code.py` |
| Test failures in `test_location.py`, `test_universities_bulk_import.py` | Pre-existing failures unrelated to YAML | See `KNOWN_ISSUES.md` |

---

## Fully-tuned YAML example (UWA)

`scraper_config/unis/uwa.yaml` is the most complete real-world example,
incorporating: `must_contain`, `prefer_year_one_over_total`,
`force_central_fee_stage` (for research degrees), `reject_sentence_patterns`,
`course_name.strip_title_suffixes`, and `max_parallel_fetch`.  Use it as a
reference when building out a new university YAML.

---

## Adding a new university YAML

1. Find the slug: `hostname → strip www. → take the domain label`.
   `www.acu.edu.au` → `acu`.  File: `scraper_config/unis/acu.yaml`.
2. Start from the sample YAML in the Scraper Configs UI (Settings →
   Scraper Configs → Download Sample or Copy Sample).
3. Run a fast scrape, check the spot-check results.
4. For each problem, find the recipe above and add the relevant field.
5. Repeat until the scrape is clean (no unexpected empty fields).
6. Document any site-specific quirks in the YAML header comment.

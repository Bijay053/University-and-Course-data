# University Portal — Scraper Architecture Document

*Last updated: April 2026. Redacted: credentials, production IP, internal PDF URLs.*

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Tech Stack](#2-tech-stack)
3. [Data Flow — Every Stage](#3-data-flow--every-stage)
4. [Database Schema](#4-database-schema)
5. [Per-University Configuration](#5-per-university-configuration)
6. [Field Extraction Strategy](#6-field-extraction-strategy)
7. [Error Handling and Logging](#7-error-handling-and-logging)
8. [Data Quality Controls](#8-data-quality-controls)
9. [Monitoring and Dashboards](#9-monitoring-and-dashboards)
10. [Known Limitations and Pain Points](#10-known-limitations-and-pain-points)
11. [Repository Structure](#11-repository-structure)
12. [Recent Changes](#12-recent-changes)

---

## 1. System Overview

### Hosting

| Component | Where |
|-----------|-------|
| Development / IDE | Replit (pnpm monorepo) |
| Production API + Celery | Ubuntu VPS (systemd services `uni-api-py`, `uni-celery`) |
| Database | PostgreSQL (same VPS) |
| Queue / Cache | Redis (same VPS, localhost-only) |
| Frontend CDN | Served by the same VPS via Vite build |

### Trigger Modes

| Mode | How |
|------|-----|
| On-demand | Admin clicks "Scrape" in the UI → `POST /api/scrape/start` |
| Bulk | Admin selects multiple universities → `POST /api/scrape/bulk` |
| Scheduled (planned) | Celery Beat is configured; periodic task not yet activated for all universities |

### High-Level Flow

```
Admin UI (React)
      │
      │  POST /api/scrape/start
      ▼
FastAPI Router ──────────────────────────────────┐
      │  creates scrape_runtime_jobs row          │
      │  dispatches Celery task                   │ advisory lock prevents
      ▼                                           │ duplicate jobs per uni
Redis (Celery broker) ◄──────────────────────────┘
      │
      ▼
Celery Worker  (--concurrency=4)
      │
      ▼
Orchestrator
  ├─ Discovery phase (BFS + sitemap)
  │       ↓ list of course URLs
  ├─ Central prefetch (fee page / requirements page / PDFs)
  │       ↓ uni-wide fallback data
  ├─ Extraction phase (up to 4 in parallel)
  │   for each URL:
  │       Static HTTP fetch (httpx)
  │           │  if JS required
  │           └─► Playwright browser render
  │       Pre-seed extractors (CSU, Bond, ECU, VIT — static)
  │       Gemini primary call (LLM)
  │       Regex/rule extractors
  │       Browser fallback (IELTS/fee still missing)
  │       Vision OCR fallback (Gemini + screenshot)
  │       AI fallback (final LLM sweep)
  │       PDF backfills (fee PDF, requirements PDF)
  │       PG-SKIP guard
  │       enforce_source_evidence gate
  │       ↓ enriched payload
  └─ Staging phase (serial)
          should_stage_course guards
          completeness + confidence scoring
          deduplication check
          write to scraped_courses + scraped_field_evidence
      ▼
scrape_runtime_jobs → status = completed
      ▼
Admin review queue (UI) → approve / reject → courses table (production)
```

---

## 2. Tech Stack

### Backend

| Item | Detail |
|------|--------|
| Language | Python 3.12 |
| Web framework | FastAPI (async, uvicorn) |
| ORM | SQLAlchemy 2 (async) |
| Database | PostgreSQL 15 |
| Task queue | Celery 5 with Redis as broker and result backend |
| HTTP client | httpx (async, connection pooling) |
| Browser automation | Playwright (async, Chromium) |
| PDF parsing | pdfplumber |
| AI / LLM | Google Gemini (gemini-2.0-flash for text; gemini-2.0-flash for vision/OCR) via `google-generativeai` |
| Structured logging | Python `logging` + custom `get_logger` helper |

### Frontend

| Item | Detail |
|------|--------|
| Language | TypeScript 5 |
| Framework | React 19 + Vite |
| UI components | Tailwind CSS + Shadcn/ui |
| State / data fetching | TanStack Query (React Query) |
| Real-time logs | Server-Sent Events (SSE) streamed from FastAPI |

### External Services

| Service | Purpose | Quota risk |
|---------|---------|-----------|
| Google Gemini API | Text extraction, vision OCR, AI fallback | Yes — exhausted on production during heavy scrape runs; vision calls are the heaviest consumer |

---

## 3. Data Flow — Every Stage

### Stage 1 — Job Initiation

**Input:** University ID from UI  
**Output:** `scrape_runtime_jobs` row (status = `queued`), Celery task enqueued  
**Code:** `backend-py/app/routers/scrape.py`

A PostgreSQL advisory lock (`pg_advisory_xact_lock`) ensures that two concurrent button clicks never create two active jobs for the same university.

---

### Stage 2 — Discovery

**Input:** `university.scrape_url` (start URL)  
**Output:** Deduplicated list of course-detail page URLs  
**Code:** `backend-py/app/services/scraper/discovery.py`

```
start_url
   │
   ├─ BFS crawl (depth 1 from nav links)
   │      filter: URL must contain /courses/, /degrees/, etc.
   │      filter: anchor text must look like a course name
   │      filter: strip junk patterns (news, events, contact, blog)
   │
   ├─ Sitemap probe (if BFS finds < 5 courses)
   │      tries /sitemap.xml, /sitemap_index.xml, robots.txt
   │
   ├─ _ALWAYS_SITEMAP_SUPPLEMENT_HOSTS
   │      Torrens: always supplement BFS with sitemap
   │      (BFS alone missed 130+ courses on this SPA)
   │
   └─► deduplicated URL list → orchestrator
```

Host-specific overrides exist for SPAs:
- `torrens.edu.au` — sitemap always supplemented; SPA needs 6s browser settle
- VIT — full browser render required (International fee toggle)
- CSU — custom browser discovery (`csu_browser_discover.py`)

---

### Stage 3 — Central Prefetch

**Input:** `scrape_config['uniPages']` keys  
**Output:** `CentralData` object cached for the whole run  
**Code:** `backend-py/app/services/scraper/central_pages.py`

The orchestrator runs this once before touching individual courses.

```
uniPages keys:
  feePage          → fee schedule page (HTML)
  feesPdf          → fee schedule PDF (pdfplumber)
  entryPage        → English requirements page (HTML)
  requirementsPage → secondary English requirements URL
  requirementsPdf  → requirements PDF (pdfplumber)

Outputs stored in CentralData:
  central_fee_rows   → list of {course_name, ielts, pte, ...}
  central_english    → {ielts_overall, pte_overall, ...}
  uni_pdf_data       → {fee: [...], requirements: [...]}
```

The fee PDF parser extracts a table of `(course_name, CRICOS_code, total_fee)` rows. Matching to individual courses uses a token-overlap fuzzy match (≥ 50% token overlap → match). This is the primary fee source for ASAHE and Bond.

---

### Stage 4 — Per-Course Extraction

**Input:** One course URL + `CentralData`  
**Output:** Raw payload dict with all extracted fields  
**Code:** `backend-py/app/services/scraper/pipelines/single_course.py`  
(delegates to extractors in `backend-py/app/services/scraper/extractors/`)

Each course goes through this ordered pipeline (later steps only fill slots still `null`):

```
1. Static HTTP fetch (httpx)
      If blocked/SPA → Playwright render

2. Pre-seed extractors (per-host authoritative overrides)
      CSU   → csu_static_extract.py   (extracts from 1.3MB JS-embedded JSON)
      Bond  → bond_static_extract.py
      ECU   → ecu_static_extract.py
      VIT   → vit_static_extract.py

3. Gemini primary (gemini_primary.py)
      One LLM call: fee + duration + IELTS/PTE/TOEFL + intake + mode
      Only for slots not filled by pre-seeds

4. Regex/rule extractors (_EXTRACTORS list)
      course_name.py   degree_level.py   duration.py
      intake.py        fee.py            english_test.py
      study_mode.py    location.py

5. Browser fallback (per_course_browser.py)
      Triggered if IELTS/fee still null AND host is in:
        _FORCE_BROWSER_HOSTS: federation.edu.au, une.edu.au,
                               uow.edu.au, unisq.edu.au, vit.edu.au
        _SLOW_HOSTS:          asa/kbs/CSU (networkidle, 60s timeout)
        _NETWORKIDLE_HOSTS:   vit.edu.au (networkidle + 3s settle)
      After render, full extractor suite reruns on the new DOM.
      _EXTENDED_EXTRACT_HOSTS (UOW, UniSQ): specialised campus
        pivot-table parser for intake + availability.

6. Vision OCR fallback (per_course_vision.py)
      If IELTS still null, screenshots the page.
      Calls Gemini Vision to read requirement tables in images.
      Results stored as method="per_course_vision" in evidence.

7. AI fallback (ai_fallback.py)
      Final sweep: remaining null slots sent to Gemini with
      the full page text.  Method="ai_fallback" in evidence.

8. PDF backfills (university_pdfs.py)
      fee backfill:  match_course_in_pdf_table(fee PDF rows)
      english backfill: requirements PDF rows matched by course name

9. Central page backfill
      Any slot still null filled from CentralData.
      Method="central_page" in evidence.

10. PG-SKIP guard (central_english_pg_skip flag)
      For universities where the central page only shows UG
      requirements, any english slot that came from the generic
      central page is wiped for Masters/Graduate courses.
      DOES NOT wipe slots sourced from:
        per_course_vision, per_course_vision_cached,
        ai_fallback, uni_pdf:requirements
      (These are per-course authoritative sources.)

11. enforce_source_evidence
      Drops any field that has no evidence record with a
      non-empty snippet AND a non-empty source_url.
      Prevents ghost values with no proof.
```

---

### Stage 5 — Staging

**Input:** Enriched payload dict  
**Output:** `scraped_courses` row + `scraped_field_evidence` rows  
**Code:** `backend-py/app/services/scraper/stage_course.py`

```
should_stage_course guards:
  ✗ Category landing pages (is_generic_course_category_name)
  ✗ Domestic-only courses (no international fee, no CRICOS)
  ✗ Confidence < 60/100

Deduplication:
  Key = (university_id, course_website_url)    ← URL-based
  (Changed from course_name key in April 2026 to fix VIT
   specialization collapse)

Preservation rule:
  If a previously approved/published row exists and the new
  scrape finds a slot null, the old value is kept (not wiped).

Completeness scoring (0–100):
  13 canonical fields, each weighted.
  English slots (IELTS / PTE / TOEFL / Cambridge / Duolingo)
  collapsed into one "english_ok" slot.

Eligibility:
  ready   → completeness ≥ threshold, no hard blockers
  review  → some fields missing or low confidence
  blocked → missing course name, level, OR all english tests

Auto-publish:
  Only "ready" courses with high confidence skip manual review.
```

---

### Stage 6 — Review and Approval

**Input:** `scraped_courses` rows (status = pending)  
**Output:** `courses` rows (production table)  
**Code:** `artifacts/university-portal/src/pages/scraping.tsx`, `review-scraped-courses-table.tsx`  
**API:** `backend-py/app/routers/review.py`, `approve_course.py`

Admins see a live log stream (SSE) during the scrape and then a review table afterward showing each staged course with its evidence (snippets, source URLs). They can approve, reject, or edit individual fields before approving.

---

## 4. Database Schema

### Scraping-Related Tables

```
universities
  id              serial PK
  name            text
  country         text
  scrape_url      text
  scrape_config   jsonb          ← all per-uni settings (see §5)
  fee_page_url    text
  requirements_page_url text
  created_at      timestamptz

scraping_jobs
  id              serial PK
  university_id   int FK → universities.id
  frequency       text           (manual / daily / weekly)
  enabled         boolean

scrape_runtime_jobs
  id              text PK        (e.g. "job_2dc0ba6bf4c9")
  university_id   int FK → universities.id
  status          text           (queued | running | completed | failed | stopped)
  total_found     int            courses discovered
  imported        int            courses staged
  skipped         int
  errors          int
  heartbeat_at    timestamptz    updated every 30s by worker
  stop_requested  boolean        set by UI to gracefully halt
  created_at      timestamptz
  completed_at    timestamptz

scrape_runtime_logs
  id              serial PK
  job_id          text FK → scrape_runtime_jobs.id
  level           text           (info | warn | error | success | debug)
  message         text
  created_at      timestamptz

scraped_courses                  ← STAGING table
  id              serial PK
  scrape_job_id   text FK → scrape_runtime_jobs.id
  university_id   int FK → universities.id
  course_id       int FK → courses.id   (null until approved)
  course_name     text
  degree_level    text
  course_website  text           (the canonical unique URL key)
  international_fee  real
  fee_term        text           (per year | full course | per semester)
  fee_year        int
  currency        text
  ielts_overall   real
  ielts_listening real
  ielts_reading   real
  ielts_writing   real
  ielts_speaking  real
  pte_overall     real
  toefl_overall   real
  cambridge_overall real
  duolingo_overall real
  duration        real           (years, e.g. 1.5)
  intakes         text[]         (["February", "July"])
  study_mode      text
  location        text
  cricos_code     text
  completeness    int            (0–100)
  confidence      int            (0–100)
  auto_publish_status text       (ready | review | blocked)
  status          text           (pending | approved | rejected | published)
  created_at      timestamptz

scraped_field_evidence           ← PROOF for every value
  id              serial PK
  scraped_course_id int FK → scraped_courses.id
  field_key       text           ("ielts_overall", "international_fee", ...)
  candidate_value text           raw extracted string
  normalized_value text          parsed/canonical value
  snippet         text           exact text found on page
  source_url      text           page URL or PDF URL
  method          text           how it was found (see §6)
  selected        boolean        was this the value used?
  confidence      int

field_conflicts
  id              serial PK
  scraped_course_id int FK → scraped_courses.id
  field_key       text
  values          jsonb          all conflicting candidates

course_field_approvals           ← links production value to evidence
  id              serial PK
  course_id       int FK → courses.id
  field_key       text
  evidence_id     int FK → scraped_field_evidence.id
```

### Production Tables

```
courses                          ← production "source of truth"
  id              serial PK
  university_id   int FK → universities.id
  name            text
  degree_level    text
  status          text
  approval_status text           (approved | pending)
  ...

fees
  id              serial PK
  course_id       int FK → courses.id
  international_fee real
  currency        text
  fee_term        text

english_requirements
  id              serial PK
  course_id       int FK → courses.id
  test_type       text           (IELTS | PTE | TOEFL | Cambridge | Duolingo)
  overall         real
  listening       real
  reading         real
  writing         real
  speaking        real
```

**Key design note:** Scraped and manual values are always separated by table (`scraped_courses` vs `courses`). A scrape result never overwrites `courses` directly; it must go through the review queue. Inside `scraped_courses`, field-level evidence records (`scraped_field_evidence.method`) tell you exactly how each value was obtained.

---

## 5. Per-University Configuration

All settings live in `universities.scrape_config` (a `jsonb` column). There is no separate config file in production — the DB is the source of truth. `backend-py/seed/prod_uni_scrape_config.sql` and `dev_universities.json` are used to seed or reset environments.

### Key Fields

```jsonc
{
  "scrape_url": "https://www.example.edu.au/courses/",   // discovery start URL

  "uniPages": {
    "feePage":          "https://...",   // HTML fee schedule
    "feesPdf":          "https://...",   // PDF fee schedule (overrides feePage)
    "entryPage":        "https://...",   // English requirements (HTML)
    "requirementsPage": "https://...",   // secondary English source
    "requirementsPdf":  "https://..."    // English requirements PDF
  },

  "central_english_pg_skip": true,   // wipe central-page english for PG courses
                                     // (only safe per-course sources survive;
                                     //  see §3 Stage 4, step 10)

  "use_ai_fallback": false,          // disable AI fallback (e.g. CSU)

  "has_central_fee_page": true       // don't fail course for missing per-page fee
}
```

### Adding a New University

1. Insert a row into `universities` with `name`, `country`, `scrape_url`.
2. Populate `scrape_config` with at minimum `uniPages.feePage` and `uniPages.entryPage` if they exist.
3. Run a test scrape and inspect logs for:
   - Too many "category page" skips → add the host's junk-URL patterns to `discovery.py:_NON_COURSE_URL_PATTERNS`.
   - Fee/IELTS missing → decide which `uniPages` keys to configure, or whether a custom extractor is needed.
   - SPA content not rendering → add to `_NETWORKIDLE_HOSTS`, `_SLOW_HOSTS`, or `_FORCE_BROWSER_HOSTS` in `per_course_browser.py`.
4. If the site has a deeply non-standard structure (e.g. 1.3MB JS blob like CSU), write a static extractor in `backend-py/app/services/scraper/` and wire it into the pre-seed step in `single_course.py`.

### Config Deployment

Config is stored in PostgreSQL. Changing it in the admin UI or via SQL takes effect on the next scrape — no code deployment needed. Code-level overrides (browser host lists, discovery filters) require `git pull + systemctl restart` on the production server.

---

## 6. Field Extraction Strategy

### Evidence Method Labels

These labels appear in `scraped_field_evidence.method` and control the PG-SKIP preserve logic:

| Method label | What it means |
|---|---|
| `pre_seed` | Hard-coded static extractor for known site structure |
| `gemini_primary` | First Gemini LLM call |
| `rule:fee`, `rule:english`, etc. | Regex extractor |
| `per_course_browser` | Value from Playwright-rendered DOM |
| `per_course_vision` | Gemini Vision OCR of course page screenshot |
| `per_course_vision_cached` | Cached vision result reused |
| `ai_fallback` | Last-resort Gemini text call |
| `uni_pdf:fee` | Value from the university fee PDF |
| `uni_pdf:requirements` | Value from the university requirements PDF |
| `central_page` | Value from the central fee/requirements page |

### Per-Field Strategy

#### course_name
- **Primary:** `<h1>` text, then `<title>`
- **Cleaning:** strips provider suffix (e.g. " — ECU"), normalises acronyms (MBA, ICT)
- **Augmentation:** for VIT/specialisation URLs, `_augment_specialization_name()` derives a readable name from the URL slug (e.g. `/bits/bits-ai` → "Bachelor of IT and Systems (Artificial Intelligence Analytics)")

#### degree_level
- **Primary:** regex against course name string (most reliable signal)
- **Fallback:** page-text regex for "Award", "AQF Level N" labels; maps AQF 1–10 to canonical names (Bachelor, Master, Graduate Certificate, etc.)

#### duration
- **Primary:** structural DOM pass — `<strong>Duration</strong>`, `<dt>/<dd>`, `<th>/<td>` pairs
- **Fallback:** regex weighting tournament in plain text; labelled matches ("Duration: 2 years") score higher than bare matches
- **Normalisation:** weeks → years (e.g. 104 weeks → 2.0); float precision capped at 1 decimal place for display

#### intake
- **Primary:** campus pivot-table parser (UNE/ECU style checkmarks showing month availability)
- **Secondary:** structural DOM pass for "Intake", "Start date" labels
- **Fallback:** regex for month names, semester labels
- **Special:** UOW session codes → month mapping (Autumn → March, Spring → July)

#### international_fee
- **Primary:** structural DOM pass looking for "International tuition", "International students" labels near currency figures
- **Secondary:** Gemini primary LLM call
- **Fallback:** scoring regex; penalises "First year fee" (+label bias for "Full course fee"); includes per-unit → full-course multiplier where unit count is available
- **PDF override:** `match_course_in_pdf_table()` fuzzy-matches the course name against the university fee PDF table (token overlap ≥ 50%); PDF fees always overwrite regex fees where a match is found
- **Sanity check:** `data_quality.py` flags if ≥ 75% of a batch share identical fee (CSS selector scope bug detection)

#### ielts_overall (and pte_overall, toefl_overall, cambridge_overall, duolingo_overall)
- **Primary:** Gemini primary LLM call
- **Secondary:** regex patterns; "no band below X" patterns scored first; bare "IELTS X.X" second
- **Browser fallback:** Playwright render of the course page if still null
- **Vision fallback:** Gemini Vision OCR of screenshots from the course page
- **PDF fallback:** requirements PDF table, matched by course/level
- **PG-SKIP:** for `central_english_pg_skip` universities, any value sourced from `central_page` is wiped for Masters/PG courses; values from `per_course_vision`, `ai_fallback`, `uni_pdf:requirements` survive
- **Vision sanity check:** if vision-OCR value differs from central-page value by more than threshold (IELTS ±1.0, PTE ±10, TOEFL ±10) the vision value is reverted and logged as `[VISION SANITY ✗]`

#### study_mode
- **Highest confidence:** `<span id="delivery">` or `data-delivery` attributes
- **Primary:** structural DOM pass for "Mode of study", "Delivery" labels
- **Fallback:** keyword scan (Blended > Online > On Campus priority)
- **Derived correction:** if location is a physical campus, mode is overridden to On Campus

#### location
- **Primary:** structural DOM pass for "Campus", "Location" labels
- **Secondary:** campus availability check in pivot tables
- **Fallback:** regex window around known city names
- **Normalisation:** expands short codes (SYD → Sydney, MEL → Melbourne); appends country where ambiguous

---

## 7. Error Handling and Logging

### Fetch Failures
- `httpx` retries with exponential back-off (configured in `http_fetcher.py`)
- If static fetch fails completely, `per_course_browser.py` is called
- If Playwright fails, the course is flagged with `error` level log and skipped; the rest of the run continues

### Extraction Failures
- Each course runs in a `try/except` inside `_extract_only()` in `orchestrator.py`
- An exception produces an error-level log entry and an error payload; the course is counted in `scrape_runtime_jobs.errors` but does not abort the batch

### Celery Time Limits
- Soft limit: 7200 s (2 hours) — raises `SoftTimeLimitExceeded`, which marks the job `failed` cleanly
- Hard limit: 7800 s (2 hrs 10 min) — SIGKILL

### Stale Job Recovery
- A `_heartbeat_pulser` background coroutine updates `heartbeat_at` every 30 s
- A Celery Beat periodic task re-dispatches jobs stuck in `queued` for too long
- Jobs where `heartbeat_at` is > 5 minutes old are declared stale and re-queued

### Log Storage
- **UI-facing:** `scrape_runtime_logs` table, one row per message, linked to the job. Streamed live to the admin via SSE. Retained indefinitely (no automated purge currently).
- **System logs:** stdout/stderr from uvicorn and Celery, captured by systemd journal on the production VPS. Access via `journalctl -u uni-api-py` or `journalctl -u uni-celery`.

---

## 8. Data Quality Controls

### Pre-staging guards (`guards.py`)

| Guard | What it checks |
|---|---|
| `is_generic_course_category_name()` | Rejects pages that are subject hubs, not specific degrees (>100 blocked patterns) |
| `should_stage_course()` | Rejects domestic-only courses, non-course pages |
| `enforce_source_evidence()` | Drops any field with no snippet + source URL proof |
| Confidence threshold | Courses below 60/100 are discarded entirely |

### Staging safety nets (`stage_course.py`)

| Mechanism | Behaviour |
|---|---|
| URL-based dedup | Same `(university_id, course_website)` never staged twice in one run |
| Value preservation | If a previously approved row exists and the new scrape is null for a field, the old value is kept |
| Rejection block | Courses rejected by an admin are not re-staged for a configurable number of days |

### Post-batch quality checks (`data_quality.py`)

| Check | Threshold |
|---|---|
| Implausible fee | Fee < $1,000 or > $200,000 → flagged |
| Suspicious duration | < 0.25 years or > 10 years → flagged |
| Duplicate detection | Same name+level in same batch → flagged |
| CSS selector scope bug | ≥ 75% of courses in batch share identical fee → flagged |

### Manual data protection

Manual `courses` table entries have `approval_status = 'approved'` and are never touched by the scraper pipeline. The scraper writes only to `scraped_courses`. Manual values enter `courses` only via an admin action, never automatically.

### Review queue

Courses with `auto_publish_status = 'review'` are held in the review queue and require admin sign-off before they reach the production `courses` table. "Ready" courses may auto-publish if configured.

---

## 9. Monitoring and Dashboards

### What exists today

- **Live log stream:** The scraping page in the admin UI tails `scrape_runtime_logs` in real time. Color-coded by level (info / warn / error / success).
- **Job stats panel:** Shows `total_found`, `imported`, `skipped`, `errors` updating as the run progresses.
- **Review table:** After a run, the admin reviews staged courses with full evidence (source snippets, URLs) before approving.
- **Data quality report:** `data_quality.py` runs after each batch and emits warnings into the log stream for implausible fees, scope bugs, duplicates.

### What is NOT automated

- No alerting if a job fails (requires checking the UI manually or the systemd journal)
- No coverage dashboard (e.g. "ASAHE had 12 courses last week, now 3 — alert!")
- No automated per-field fill-rate tracking across runs
- No Slack/email notifications

---

## 10. Known Limitations and Pain Points

### Universities — current status

| University | Status | Notes |
|---|---|---|
| CSU | Working | Complex: 1.3MB JS-embedded HTML; custom static extractor |
| Bond | Working | Fees from PDF; pre-seed extractor wired |
| ECU | Working | Pre-seed extractor; central fee page |
| UOW | Working | Always-browser; extended campus pivot-table parser |
| UniSQ | Working | Always-browser; extended pivot-table parser |
| VIT | Fixed (April 2026) | Specialisation dedup fixed (URL-based key); international fee toggle requires Playwright |
| Torrens | Fixed (April 2026) | SPA: sitemap supplement + 6s browser settle added |
| ASAHE | Partially fixed | Fee PDF parsing works on production. IELTS from requirements PDF was being wiped by PG-SKIP — fixed April 2026 (uni_pdf:requirements added to preserve list). Needs re-scrape to confirm |
| UNE | Working | Campus pivot-table parser |
| Federation | Working | Always-browser |

### Known pain points

1. **Gemini API quota exhaustion.** Vision OCR and AI fallback calls are expensive. During a bulk re-scrape of all universities, the Gemini API key can be exhausted mid-run, causing all subsequent vision and AI fallback calls to return empty. There is no automatic quota-aware throttling or fallback API key.

2. **PG-SKIP over-clearing (root-cause found).** The `central_english_pg_skip` mechanism was correctly designed but missed one evidence method: `uni_pdf:requirements`. Fixed April 2026. Any university relying on a requirements PDF for PG-level english scores needs a re-scrape to benefit.

3. **Vision sanity check reverting correct values.** If a course has a different IELTS threshold from the university-wide default (e.g. a pathway program at 5.5 while the default is 6.5), the sanity check incorrectly reverts the correct course-specific value to the central default. Threshold is ±1.0 band.

4. **Token-overlap fuzzy matching for PDFs.** Works well for straightforward course names. Fails when the PDF and the website use significantly different names for the same course (e.g. "Software Application Design" vs "Software Application Development"). No CRICOS-code cross-reference is used.

5. **No proxy / IP rotation.** httpx fetches from a single VPS IP. Sites with aggressive Cloudflare or rate limiting can block the scraper silently (returns a 403 or challenge page instead of course HTML).

6. **Intake extraction is the weakest field.** Intakes are represented inconsistently across universities (some use month names, some use semester labels, some use checkmark tables, some are in a JS modal). Fill rate is lower than for fees or english requirements.

7. **No change-detection.** Every scrape is a full re-scrape. If a university updates one course's fee, the entire university is re-scraped. There is no incremental/diff mode.

8. **Review queue backlog.** If auto-publish thresholds are conservative, the review queue fills up faster than admins can clear it.

---

## 11. Repository Structure

```
University-and-Course-data/          (GitHub: Bijay053/University-and-Course-data)
│
├── backend-py/                      Python FastAPI application
│   ├── app/
│   │   ├── main.py                  FastAPI app factory
│   │   ├── models/                  SQLAlchemy ORM models
│   │   │   ├── university.py        scrape_config jsonb definition
│   │   │   ├── course.py
│   │   │   └── scraped_course.py
│   │   ├── routers/                 FastAPI route handlers
│   │   │   ├── scrape.py            trigger + status endpoints
│   │   │   └── review.py            approve / reject endpoints
│   │   ├── services/
│   │   │   └── scraper/             ← ALL scraper logic lives here
│   │   │       ├── orchestrator.py  job lifecycle, parallelism  ★
│   │   │       ├── discovery.py     URL harvesting               ★
│   │   │       ├── central_pages.py uni-wide fee/english fetch   ★
│   │   │       ├── stage_course.py  dedup, scoring, DB write     ★
│   │   │       ├── guards.py        pre-staging filters          ★
│   │   │       ├── completeness.py  scoring + eligibility
│   │   │       ├── confidence.py    confidence scoring
│   │   │       ├── data_quality.py  post-batch checks
│   │   │       ├── per_course_browser.py  Playwright renders     ★
│   │   │       ├── per_course_vision.py   Gemini Vision OCR
│   │   │       ├── http_fetcher.py        httpx wrapper
│   │   │       ├── pdf_fetcher.py         PDF download
│   │   │       ├── pdf_vision.py          PDF → image → OCR
│   │   │       ├── sitemap.py             sitemap parser
│   │   │       ├── pipelines/
│   │   │       │   ├── single_course.py   full extraction pipeline ★
│   │   │       │   └── university_pdfs.py fee/req PDF parsing     ★
│   │   │       ├── extractors/            rule-based extractors
│   │   │       │   ├── fee.py
│   │   │       │   ├── english_test.py
│   │   │       │   ├── duration.py
│   │   │       │   ├── intake.py
│   │   │       │   ├── course_name.py
│   │   │       │   ├── degree_level.py
│   │   │       │   ├── study_mode.py
│   │   │       │   ├── location.py
│   │   │       │   ├── gemini_primary.py  first LLM call
│   │   │       │   └── ai_fallback.py     last LLM call
│   │   │       ├── bond_static_extract.py  ┐
│   │   │       ├── csu_static_extract.py   │ university-specific
│   │   │       ├── ecu_static_extract.py   │ pre-seed extractors
│   │   │       └── vit_static_extract.py   ┘
│   │   └── tasks/
│   │       ├── celery_app.py        Celery config + Beat schedule
│   │       └── scrape_tasks.py      Celery task definitions
│   └── seed/
│       ├── prod_uni_scrape_config.sql
│       └── dev_universities.json
│
├── artifacts/
│   └── university-portal/           React + Vite admin frontend
│       └── src/
│           ├── pages/
│           │   ├── scraping.tsx     live scrape control + log view
│           │   ├── search.tsx       course search / browse
│           │   └── review.tsx       review queue
│           └── components/
│               └── review-scraped-courses-table.tsx
│
└── lib/
    └── db/
        └── src/schema/              Drizzle schema (TypeScript side)
```

★ = highest-impact files for debugging fee or IELTS extraction problems

---

## 12. Recent Changes

### April 2026

| Change | File | Effect |
|---|---|---|
| Duration float display fix | `review-scraped-courses-table.tsx`, `search.tsx` | "1.7000000476837158 Year" → "1.7 Year" |
| Torrens SPA discovery | `discovery.py` (`_ALWAYS_SITEMAP_SUPPLEMENT_HOSTS`) | Torrens courses no longer missed from BFS-only discovery |
| Torrens browser settle | `central_pages.py` (`_SLOW_SPA_HOSTS`, 6s wait) | JS-rendered Torrens pages now fully hydrated before extraction |
| VIT specialisation dedup | `stage_course.py` | Dedup key changed from `course_name` to `course_website` URL; `_augment_specialization_name()` generates readable names from URL slugs |
| ASAHE PG-SKIP bug fix | `single_course.py` (`_PER_COURSE_VISION_METHODS`) | `"uni_pdf:requirements"` added to the preserve list — requirements PDF english scores now survive PG-SKIP for Masters courses (ASAHE IELTS 6.5) |

### Known unfixed issues as of April 2026

- ASAHE needs a re-scrape to confirm the PG-SKIP fix delivers IELTS to Masters courses
- Gemini API quota exhaustion has no automatic mitigation — bulk re-scrapes should be staggered
- No alerting infrastructure — failed jobs are only visible by checking the admin UI or systemd journal

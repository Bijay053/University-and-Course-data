# University Course, Fee, Intake & Requirement Management System

## Overview

A centralized admin portal for managing university course data including courses, fees, intakes, scholarships, and admission requirements. Supports web scraping, bulk upload/download, and change detection.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **Frontend**: React + Vite + Tailwind CSS + shadcn/ui + TanStack React Query + wouter
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Artifacts

- **`artifacts/university-portal`** — React + Vite frontend at `/`
- **`artifacts/api-server`** — Express 5 API server at `/api`

## Pages

- `/` — Dashboard with stats, courses by degree level, upcoming intakes, recent changes
- `/universities` — Searchable university list with add/view actions
- `/universities/:id` — University detail with courses
- `/courses` — Searchable/filterable course list
- `/courses/:id` — Course detail with tabs: Overview, Intakes, Fees, English Requirements, Academic Requirements, Scholarships
- `/courses/new` — Create new course
- `/scraping` — AI-powered web scraper + university coverage + import history
- `/bulk` — Bulk Excel upload for importing course data

## Database Schema

Tables: `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`, `scraped_courses` (staging table for scraped data review), `import_jobs`

## Production Deployment

- **Server**: DigitalOcean droplet at `159.65.152.72` (Ubuntu 24.04)
- **Repo path on production**: `/root/University-and-Course-data` (NOT `/opt/app` — that is a Replit container path and must never be used)
- **Process manager**: pm2 with `ecosystem.config.cjs`
- **Env file**: `/root/.env.backup` (real `DATABASE_URL`, `GEMINI_API_KEY`, etc.)
- **Database**: Local PostgreSQL — db `university_portal`, user `uniportal`
- **SSH credentials are NOT available in the Replit env** — the user runs deploys themselves. Always provide commands in the format below.

### Standard deploy command (full)
```bash
cd /root/University-and-Course-data && \
git pull && \
pnpm install --frozen-lockfile && \
pnpm --filter @workspace/api-server run build && \
pnpm --filter @workspace/university-portal run build && \
source /root/.env.backup && \
pm2 delete uni-api && \
pm2 start ecosystem.config.cjs && \
pm2 save
```

### When schema changes are needed
Add this step BEFORE the builds:
```bash
pnpm --filter @workspace/db push --force
```

### Frontend-only changes
Skip the api-server build, skip pm2 delete/start (Nginx serves the new bundle automatically):
```bash
cd /root/University-and-Course-data && git pull && pnpm install --frozen-lockfile && \
pnpm --filter @workspace/university-portal run build
```

### Verification commands user runs after deploy
```bash
git log -1 --oneline                                              # confirm commit deployed
curl -s http://localhost/ | grep -oE 'assets/index-[A-Za-z0-9-]+\.js'   # confirm new bundle served
pm2 env 0 | grep -E "DATABASE_URL|GEMINI|UV_THREADPOOL"            # confirm pm2 has correct env
```

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `pnpm --filter @workspace/university-portal run dev` — run frontend locally

## API Routes

All routes served at `/api/...`:
- `GET/POST /universities` — list/create universities
- `GET/PATCH/DELETE /universities/:id` — university CRUD
- `GET/POST /courses` — list/create courses
- `GET/PATCH/DELETE /courses/:id` — course CRUD
- `GET/POST /courses/:courseId/intakes` — intake management
- `PATCH/DELETE /intakes/:id` — intake update/delete
- `GET/POST /courses/:courseId/fees` — fee management
- `PATCH/DELETE /fees/:id` — fee update/delete
- `GET/POST /courses/:courseId/english-requirements` — English requirement management
- `PATCH/DELETE /english-requirements/:id`
- `GET/POST /courses/:courseId/academic-requirements` — academic requirement management
- `PATCH/DELETE /academic-requirements/:id`
- `GET/POST /courses/:courseId/scholarships` — scholarship management
- `PATCH/DELETE /scholarships/:id`
- `GET/POST /scraping/jobs` — scraping job management
- `POST /scraping/jobs/:id/run` — trigger scraping job
- `GET /scraping/changes` — list detected changes
- `POST /scraping/changes/:id/approve|reject` — review changes
- `GET /dashboard/stats|recent-changes|courses-by-level|upcoming-intakes` — dashboard data
- `GET /bulk/courses/download` — CSV download
- `POST /bulk/courses/upload` — CSV upload
- `POST /import/excel` — Excel file import with auto-mapping
- `GET /import/history` — import job history
- `POST /scrape/start` — AI-powered web scraper (background job, returns jobId)
- `GET /scrape/status/:jobId` — poll scrape job status + logs
- `GET /scrape/jobs` — list recent scrape jobs
- `GET /scrape/staged/:jobId` — get staged courses for review
- `GET /scrape/staged` — get all pending staged courses
- `PUT /scrape/staged/:id` — edit a staged course (whitelist-validated fields)
- `DELETE /scrape/staged/:id` — reject/delete a staged course
- `POST /scrape/staged/:id/approve` — approve single course (transactional)
- `POST /scrape/staged/approve-all` — approve all pending for a job
- `POST /scrape/staged/reject-all` — reject all pending for a job
- `POST /scrape/preview` — preview page analysis before scraping
- `POST /scrape/rescrape` — re-scrape using saved config (no AI, zero cost)
- `POST /scrape/stop/:jobId` — stop a running scrape job

## AI Integration

- **Gemini API** via `GEMINI_API_KEY` secret
- Model chain: `gemini-2.5-flash` -> `gemini-2.0-flash-001` -> `gemini-2.0-flash-lite-001` (auto-fallback on 429/503/404)
- Used by AI web scraper: cheerio extracts data first (zero AI cost), AI used as fallback
- Scraper saves to `scraped_courses` staging table for review before approval to live `courses` table

## Scraper Capabilities

- **Link discovery**: AI analysis + HTML/cheerio fallback merged together (finds all courses even if AI misses some)
- **Tab content preservation**: Does not remove hidden tab panes (Webflow w-tab-pane etc.), captures all course page tabs
- **PDF fee extraction**: Detects fee schedule PDF links and uses Gemini multimodal to extract international fees from PDFs
- **Image analysis**: Detects images with IELTS/fee data in filenames, downloads and sends to Gemini multimodal for extraction
- **Graceful degradation**: If AI analysis fails (rate limit), falls back to cheerio-only HTML link scanning
- **Related page enrichment**: Follows fee, requirements, and entry links to gather missing data
- **International fees only**: All fee extraction (cheerio, AI, PDF) enforces international-student-only rule
- **Re-scrape (No AI)**: After initial AI scrape, saved `scrapeConfig` (course links + uni pages) enables zero-cost re-scraping using only HTML/regex extraction
- **Scrape config persistence**: `scrapeConfig` JSONB saved on universities table with courseLinks, uniPages, resolvedUrl, lastScrapedAt
- **Auto-fill URL**: Frontend auto-fills scrape URL when a university is selected from dropdown (uses saved `scrapeUrl`)

## Python Scraper Parity (T201–T211)

`backend-py/` is a FastAPI + SQLAlchemy async + Celery + Playwright rewrite of the
Node scraper. As of commit `203226a`, the Python pipeline is at data-parity with
the Node implementation across these features:

- **T201** course-name slug detection + title-casing (`extractors/course_name.py`)
- **T202** duration term suffix (Year/Month) + Masters credit-points fix (`extractors/duration.py`)
- **T203** Per-Unit → Full-Course fee multiplier (`extractors/fee.py`)
- **T204** category keyword pre-map before AI classification (`category.py:map_course_to_category` + `[CATEGORY det]` log)
- **T205** eligibility reason in Node format: `"Publish blocked: ... | Validation: ... | Missing: ... | Warnings: ..."` (`completeness.py:decide_eligibility`)
- **T206** sibling-cache english-test back-fill across degree bucket (`sibling_cache.py:backfill_english_from_siblings`)
- **T207** per-course browser fetch fallback (`per_course_browser.py`)
- **T208** per-course Gemini Vision OCR for image-only english tables (`per_course_vision.py`)
- **T209** orchestrator emits `[INFO ] [TIMING]` line + typed `done` event (`══ DONE ══`) consumed by the React log viewer at `scraping.tsx:1630`
- **T210** UI log rows colour-coded by `level` field (`scraping.tsx` `levelColor` map ~L1595) with phase/event fallbacks for legacy rows

**Regression tests**: `backend-py/tests/test_scraper_pipeline_parity.py` (19 tests) pins T201–T206 + T209.
T207/T208 are network-bound — covered by manual smoke runs, not pytest.
Full suite: 207 passed, 1 skipped.

## Python Route Parity (Bugs L–Q close-out)

After T201–T211, a follow-on parity wave was triggered to stop "whack-a-mole"
bug-fixing on the Node→Python rewrite. The Node API server has many endpoints
the React UI relies on that were never ported; each missing one surfaced
in production as a "Save failed" toast.

Closed in commit `bfe50d7`:
- **Bug L** acronyms POST + DELETE (`acronyms.py`).
- **Bug M** `/api/import/excel` route registered (already worked; covered by parity test).
- **Bug N** `POST /api/universities/:id/bulk-english` (`per_course_resources.py`).
- **Bug O** `POST /api/universities/:id/bulk-academic` (`per_course_resources.py`); 409 conflict body matches Node top-level `{error, conflicts}`, not FastAPI's `{detail:{...}}`.
- **Bug P** `POST /api/universities/:id/bulk-scholarships` (`per_course_resources.py`).
- **Bug Q** `PUT /api/scrape/staged/:id` with field whitelist + completeness recompute (`scrape.py`).

Plus full per-course CRUD (intakes, fees, english/academic reqs, scholarships),
PATCH /universities/:id/featured, /healthz alias, and a complete port of
Node's `backup_mapping.ts` (apply-backup + bulk-apply-backup + backup-match).

**Regression test**: `backend-py/tests/test_route_parity.py` seeds a throwaway
university + course and drives every UI fetch via httpx ASGITransport. Three
checks: (1) route-table membership (no framework 404), (2) live smoke (no 5xx,
no unrouted 404), (3) JSON-shape contracts the UI actually destructures
(`{courses: [...]}`, `{results, summary}`, `appliedFields[]`,
top-level 409 `{error, conflicts}`). 3/3 green.

## Repair Scrape (PR-1.5 B18)

`POST /api/scrape/repair/start` runs a "back-fill only" pass for courses
already in the DB whose key fields are blank. Frontend (`university-detail.tsx`
`startRepairScrape`) sends `{universityId}` only — backend re-runs the same
SQL as `/repair/missing` to discover targets so UI and worker stay aligned.

- Service: `backend-py/app/services/scraper/repair.py:run_repair` reuses
  the orchestrator's `_emit`, `_extract_only`, and `infer_log_level`
  helpers. Skips discovery entirely; for each `(course_id, url)` target
  it extracts and **only fills blank scalar Course fields** (never
  overwrites existing values). Inserts `EnglishRequirement` rows only
  when the course has zero existing rows.
- Celery task: `app/tasks/scrape_tasks.py:repair_university` (name
  `scrape.repair`), mirrors `scrape_university`'s engine.dispose dance.
- Job row: `ScrapeRuntimeJob` written with `job_type='repair'` and
  `request_payload.repair_targets = [{course_id, url}, ...]`.
- **Worker routing**: Node `scrape-worker.mjs` claim query in
  `artifacts/api-server/src/services/scrape-runtime-jobs.ts:478` filters
  `AND (job_type IS NULL OR job_type IN ('single','bulk'))` so repair
  jobs are exclusively handled by the Python Celery worker (Node would
  otherwise treat them as a fresh discovery scrape).

**Regression test**: `backend-py/tests/test_repair_scrape.py` (7 tests):
endpoint validation (missing/unknown/non-int id), URL-less course
rejection, no-targets short-circuit, blank-only back-fill, and
existing-english skip. Full suite: 282 passed, 1 skipped.

## Mode/Duration Extraction Hardening (PR-1.5 B20)

User report: VIT staging showed every course with Mode="Blended" and a
duration with no unit (e.g. Bachelor "3" rather than "3 Years"). Two
distinct latent bugs:

- **Mode**: VIT pages label the field "Learning Mode" — a synonym not
  in `_LABEL_RE` (`backend-py/app/services/scraper/extractors/study_mode.py`).
  The label-first path missed, falling through to the bare-keyword
  fallback, which on VIT pages can pick up the literal word "Blended"
  from the embedded enquiry-form `<select>` ("Online Studies / On Campus
  / Blended"). Added `learning mode`, `learning method`, `mode of
  learning`, and `delivery method` to the labelled-field regex so VIT's
  `<dt>Learning Mode</dt><dd>On Campus</dd>` layout is now first-class.
- **Duration**: AI fallback returns the model's answer under
  `duration_value` + `duration_unit` (matching the prompt the model is
  shown), but the staged-course schema reads `duration` + `duration_term`.
  The merge in `single_course.py` (`payload.setdefault(k, v)`) copied
  the AI keys verbatim — so when the rule extractor failed to match a
  page's duration the AI's answer was silently dropped on the floor and
  the row landed with no unit. New helper
  `_apply_ai_duration_mapping(payload, ai_filled)` translates
  `duration_value`→`duration` (float coercion) and `duration_unit`→
  `duration_term` (via the existing `_normalise_unit` from the duration
  extractor, which knows `years`/`months`/`weeks`/`semesters`/`trimesters`).
  Mapping only fires when the canonical key is *missing* from the
  payload, so a confident regex hit always beats an AI guess.

**Regression tests**: `tests/test_single_course_ai_mapping.py` (7 tests:
years/months/weeks translation, rule-extractor priority, missing-fields
no-op, junk-unit rejection, non-numeric value safety) +
`tests/test_study_mode.py::test_learning_mode_label_recognised`. Full
suite: 290 passed, 1 skipped.

## PR-1.5 Prod Regression Hot-Fix (Apr 2026)

After PR-1.5 B19/B20 shipped, prod job_01cec454ebd2 (VIT, 24 courses) and
job_440a0e26c6df (CSU, 9 courses) surfaced four new defects that B20 did
NOT cover. All six fixes below land together.

### 1. Browser fallback returned empty extracts on SPA pages
- **Symptom**: VIT pages staged with IELTS/PTE/TOEFL/CAE empty on 23/24
  URLs even after the per-course browser pass; vision fallback also empty.
- **Root cause**: `browser_pool.fetch_html` defaulted to
  `wait_until="domcontentloaded"` + a fixed 1.5s settle. VIT's
  english-requirements `<table>` is hydrated by a post-DCL XHR, so we
  grabbed the skeleton HTML and english_test.extract returned nothing.
- **Fix**: `browser_pool.fetch_html` now accepts a `settle_ms`
  parameter; `per_course_browser` uses
  `wait_until="networkidle"` + `settle_ms=3000` and bumps the
  hard ceiling 45→60s (`_BROWSER_FETCH_TIMEOUT_SEC`).

### 2. AI fallback timing out on heavy pages
- **Symptom**: `AI fallback exceeded 60s on https://vit.edu.au/mba —
  moving on without AI fill` on multiple courses.
- **Fix**: `_AI_FALLBACK_TIMEOUT_SEC` 60→120s (matches Node-era
  budget; vision-capable Gemini calls take 60–90s on heavy pages
  during model-side queueing events).

### 3. study_mode defaulted to "Blended" on every VIT row
- **Symptom**: 100% of VIT's 24 staged rows had `study_mode='Blended'`,
  even MBA which is on-campus only.
- **Root cause**: B20 added `<select>`/`<form>`/`<nav>` noise stripping,
  but bare `\b(blended|hybrid|mixed[-]mode)\b` still matched marketing
  copy outside those blocks ("blended learning environment", "blended
  teaching approach"). One match anywhere on the page wins.
- **Fix**: pattern 1 of `_MODE_PATTERNS` now requires the keyword to be
  immediately followed by an explicit delivery noun
  (`delivery|mode|format|program(me)?`). Multi-mode combos
  ("On Campus and Online") still fire on their own.
- **Coverage**: `tests/test_study_mode.py` adds
  `test_bare_blended_marketing_copy_does_not_default_to_blended` (6
  cases) and `test_blended_with_delivery_noun_still_classifies_as_blended`
  (5 cases).

### 4. duration=10 Year on VIT MBA / Master rows
- **Symptom**: every VIT postgrad row staged with duration=10 Year.
- **Root cause**: pattern 3 (loose `\b<num>\s*<unit>\b` fallback) matched
  "over 10 years of industry partnerships" in the page footer; that hit
  beat the legitimate 2-year MBA duration in the weight-by-weeks
  tournament because 10*52 > 2*52.
- **Fix**: `extractors/duration.py` adds `_DURATION_CONTEXT` (positive
  filter — sentence must contain a duration-related word) and
  `_DURATION_ANTI_CONTEXT` (negative filter — `experience`,
  `established`, `celebrating`, `partnership`, etc.). Pattern 3 now
  ONLY fires inside a sentence that passes both gates. Patterns 1 and 2
  are already context-bound and unaffected.
- **Coverage**: `tests/test_extractors.py` adds 5 new cases (rejects
  staff tenure / anniversaries / institutional history; preserves
  loose-fallback success when duration context is present; legitimate
  signal still wins over noise in multi-sentence text).

### 5. Counter mismatch: imported=9 vs DB COUNT(*)=0
- **Symptom**: job_440a0e26c6df reported `imported=9` but
  `SELECT COUNT(*) FROM scraped_courses WHERE scrape_job_id=...` returned
  0 — operator chasing phantom rows.
- **Root cause**: `_clear_stale_dedup` deleted EVERY pending row older
  than 10 min for a university, including rows from a previous
  *successfully completed* run. Scrape #2 (>10 min later) wiped scrape
  #1's 9 reviewer rows during its own dedup pass before its own staging
  started, and scrape #1's in-memory `imported` counter was already
  reported.
- **Fix (twofold)**:
  1. `_clear_stale_dedup` now adds `NOT EXISTS (SELECT 1 FROM
     scrape_runtime_jobs WHERE status IN ('completed','running'))` to
     the DELETE — only failed/stopped/orphaned-job rows are cleaned up.
  2. Post-staging in `run_scrape` re-reads
     `COUNT(*) FROM scraped_courses WHERE scrape_job_id=:rid` and
     uses that as the authoritative `staged` count; warns loudly in
     both server log and live job log on any drift so future
     regressions surface immediately.
- **Coverage**: `tests/test_stale_dedup_cleanup.py` adds
  `test_clear_stale_dedup_preserves_rows_from_completed_jobs` and
  `test_clear_stale_dedup_preserves_rows_from_running_jobs`.

### 6. Sibling cache cross-contamination (DOCUMENTED, NOT FIXED)
- **Symptom**: every postgraduate course at VIT got the same English
  requirements (IELTS=6.5/PTE=58/TOEFL=87/CAE=176) because /mba was
  the only URL that successfully extracted them and the sibling cache
  filled the rest.
- **Decision**: this is **by design**. The sibling cache buckets by
  degree level (`undergraduate|postgraduate|unknown`) so MBA-spec rows
  share their parent MBA's requirements (which is correct — they are
  the same university policy). The fix to defects 1+2 above (real
  extracts succeed for more URLs) is the proper remediation; the
  sibling cache only ever fills missing slots, never overwrites real
  extractions. Bucket can be narrowed to per-category later if a
  university surfaces with genuinely divergent postgrad requirements.

**Test summary**: 301 passed, 1 skipped (was 292+1; +9 new regression tests).

### 7. IELTS/PTE/TOEFL "Overall score X" prose blocked all patterns (commit fd14a6a)
- **Symptom**: After PR-1.5 hot-fix #1 (commit 36f30ee) deployed, prod
  job_24323bbb8715 (VIT, 12 courses) STILL showed `browser_empty=12/12`
  and `vision_with_data=12/20` (60%). Page prose plainly states
  `IELTS Academic: Overall score 6.5, with no band below 6.0`.
- **Root cause**: every `_ielts` / `_pte` / `_toefl` pattern in
  `extractors/english_test.py` required `overall\s*<digit>` — they did
  NOT allow the literal word "score" (or "band"/"of") between
  "Overall" and the number. VIT's phrasing put "score" there, so all
  five IELTS patterns and the rich PTE/TOEFL twins failed. The same
  bug also throttled `per_course_vision`, which feeds Gemini output
  back through `english_test.extract`.
- **Fix**: insert optional
  `(?:score\s+|band\s+|score\s+of\s+|of\s+)?` bridge after
  `overall\s*` in IELTS pattern 1, PTE pattern 1, TOEFL pattern 1.
  Tail constraint (`with no band/skill/section below`) is unchanged,
  so no false-positive regressions.
- **Coverage**: 3 new regression tests in `tests/test_extractors.py`
  using actual VIT prose for IELTS/PTE/TOEFL. End-to-end verified
  against `/tmp/vit.html`: extractor now returns
  `ielts_overall=6.5, ielts_listening=6.0`.
- **Test summary**: 304 passed, 1 skipped (was 301+1; +3 new).
- **Out of scope (deferred)**: PTE/TOEFL/CAE on VIT only appear in the
  equivalence-table image, not prose. Vision still owns those (60%
  hit-rate). Lifting to ~100% needs a real table parser.
- **Adjacent variants noted but not fixed**: phrasings like
  `overall band score 6.5` or `overall IELTS score of 6.5` are still
  unsupported by pattern 1; they have not been observed in the wild.

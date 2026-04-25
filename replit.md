# University Course, Fee, Intake & Requirement Management System

## Overview

A centralized admin portal for managing university course data including courses, fees, intakes, scholarships, and admission requirements. Supports web scraping, bulk upload/download, and change detection.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **Frontend**: React + Vite + Tailwind CSS + shadcn/ui + TanStack React Query + wouter
- **API framework**: FastAPI in Replit dev (Python, port 8080); Express 5 in production (Node, PM2 + Nginx). See "Local Dev API" and "Production Deployment".
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Artifacts

- **`artifacts/university-portal`** — React + Vite frontend at `/`
- **`artifacts/api-server`** — Express 5 API server at `/api`. **Production-only.** Built and run by PM2 on the DigitalOcean droplet via `deploy.sh`. The local dev workflow is intentionally inert (it prints a status line and exits cleanly, so the workflow lands in the `finished` state — never `failed`); FastAPI owns `/api` in the Replit dev container — see "Local Dev API" below.
- **`backend-py/`** — FastAPI + SQLAlchemy async + Celery + Playwright rewrite. **Authoritative in local dev only** (workflows `backend-py: FastAPI` on `:8080` and `backend-py: Celery worker`). Not yet on production. See "Python Backend (Parallel Deployment)" further down.

### Local Dev API

In the Replit container, **only the Python FastAPI service binds `:8080`** and serves `/api/*`. The Node `artifacts/api-server` workflow is still registered (because its `[[services]] paths = ["/api"], localPort = 8080` block is what tells the Replit preview proxy to route `/api/*` requests to `localhost:8080` — where FastAPI is listening), but its dev `run` command is a no-op that exits cleanly. The workflow therefore reads as `finished` in the workflow list rather than `failed`, and never races FastAPI for the port. The Node source is still built and shipped to production unchanged. To actually run the Node API locally for debugging, run `pnpm --filter @workspace/api-server run dev` in a shell after stopping `backend-py: FastAPI`. To edit the artifact's `run` command, use the `verifyAndReplaceArtifactToml` callback — do not edit `artifact.toml` in place.

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
- `pnpm --filter @workspace/api-server run dev` — run the legacy Node API server locally (only needed for debugging the production Node code; FastAPI serves `/api` in dev by default)
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

**Required workflows for scraping to actually run** (all three must be up,
otherwise jobs sit in `queued` and the UI hangs polling status forever —
the symptom that surfaced as "scraping is freezed" on 2026-04-25):

1. `Redis` — `redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --loglevel notice`
   (Celery broker + result backend; `redis` system pkg from Nix; no
   persistence, no waitForPort because 6379 isn't in Replit's allow-list).
2. `backend-py: Celery worker` —
   `cd backend-py && PYTHONPATH=. celery -A app.tasks.celery_app worker --concurrency=2 --loglevel=info -Q scrape`
   (consumes the `scrape` queue; jobs are Celery-dispatched from
   `routers/scrape.py` via `scrape_university.delay(job_id)`, the call
   is wrapped in try/except so the API still returns 202 even with no
   broker — that's why a missing worker presents as silent freeze, not
   a 5xx).
3. `backend-py: FastAPI` —
   `cd backend-py && PYTHONPATH=. python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload`.

The `scrape_runtime_jobs.status='queued'` rows are auto-reaped inline by
the `GET /active` endpoint (`routers/scrape.py:list_active`, lines
~457–575) if they sit unclaimed for >10 min — the row moves to
`stopped` with `error_message='Auto-reaped (never claimed by a
worker)'` (lines 528 and 544 cover the heartbeat-lost and
never-claimed cases). The `/status/{job_id}` endpoint does NOT reap;
it just reports current state. The reaper is a self-heal for
dead-broker scenarios, not a substitute for actually running the
worker — and it only fires on `/active` polls, so a UI screen that
only polls `/status` won't trigger reaping.


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

## VIT Parity Port (T001–T005, commit `a20b728`)

Five VIT-specific scraper features ported Node→Python so the Python pipeline
matches Node on `vit.edu.au` (24 → 30 discovered courses; per-course
IELTS/PTE/TOEFL/CAE values match across BBus/MBA/Diploma rows; duration +
intake + location recovered after the international toggle strips them).

- **T001** Home-page → `/course-list` redirect (`scraper/home_page_redirect.py::detect_course_listing_page`). HEAD-probes `/course-list`, `/courses`, `/study/degrees-and-courses` etc.; falls back to weighted link-scan, then a broad HEAD probe. Wired into `discovery.py::discover_course_links` so `https://vit.edu.au/` is auto-redirected to `https://vit.edu.au/course-list` before BFS.
- **T002** Per-course Bootstrap-modal English-test extractor (`scraper/per_course_modal.py::extract_modal_english`). Finds `.modal/[role=dialog]` containers with IELTS+PTE+TOEFL, parses tables, classifies numbers into ielts/pte/toefl/cae buckets, picks the row whose IELTS is closest to the degree-level target (5.5/6.0/6.5). Also extracts IELTS sub-bands via three patterns ("no individual band below X.X" / explicit L/R/W/S / short-form `L X.X R X.X`). Wired into `pipelines/single_course.py::extract_course` BEFORE the per-course browser pass.
- **T003** VIT static fallback for duration / intake / location (`scraper/vit_static_extract.py::apply_vit_summary_extraction`). Label-walks `<strong>/<b>` for `intake|location|duration`, harvests values from sibling `<ul>/<ol>` lists, normalises via existing extractors. Wired AFTER the per-course browser pass when the toggle stripped the static narrative paragraph.
- **T004** Category-filtered listing expansion (`home_page_redirect.py::expand_course_list_with_categories`). HEAD-probes `?course_categories[0]=bbus`, `?category=master`, `?type=diploma`, `/{slug}` variants on the listing path; merges new course links found on each variant page. Raises VIT discovery from 24 → 30.
- **T005** Browser "International" toggle action (`scraper/browser_pool.py::fetch_html(click_international=True)`). JS-evaluates the page, finds radios/checkboxes/elements whose value/text matches "international", clicks the first that fires a renderedSinceBaseline change. `per_course_browser.maybe_browser_refetch` passes `click_international=True` for hosts in `_INTERNATIONAL_TOGGLE_HOSTS = ("vit.edu.au",)`.

**Regression tests** (`backend-py/tests/`): `test_home_page_redirect.py` (9), `test_per_course_modal.py` (9), `test_vit_static_extract.py` (7), `test_category_expand.py` (10), `test_browser_international_toggle.py` (6), plus `test_data_parity_priorities.py` (9) — 50/50 green.

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

### 8. PTE/TOEFL/CAE buried in equivalence table (commit f54ebf8)
- **Symptom**: After hot-fix #2 deployed, prod job_24323bbb8715
  reported `has_pte=11/24, has_toefl=11/24, has_cae=11/24,
  avg_completeness=67.5%` (baseline 99.6%). VIT MBA pages stated
  IELTS=6.5 in prose but PTE/TOEFL/CAE only in a multi-row HTML
  equivalence table. Vision OCR delivered IELTS reliably but only
  ~45% of PTE/TOEFL/CAE because picking the right cell from a wide
  table image is fragile.
- **Root cause**: the Python port had **no HTML table parser** for
  equivalence layouts. Prose extractors couldn't see `<th>IELTS`/
  `<th>PTE` columns at all because \`html_to_text\` flattens the table
  into a wall of unlabelled numbers.
- **Fix**: \`extractors/english_test.py\` adds three new helpers:
  - \`_is_equivalence_table\` — header-text heuristic; matches when a
    \`<table>\` has IELTS plus at least one of PTE/TOEFL/CAE/Duolingo.
  - \`_parse_equivalence_table\` — handles two-row headers,
    rowspan/colspan cells, and the common 'TOEFL ... as per IELTS
    website' flavour text. Returns
    \`{ielts_overall: {pte, toefl, cambridge, ...}, ...}\`. Per-test
    sanity bounds applied (PTE 10-90, TOEFL 0-120, CAE 140-230,
    Duolingo 50-160).
  - \`_equivalence_fallback\` — runs after the prose extractors; only
    fills missing slots when (a) IELTS overall was extracted from
    prose and (b) the table has a row matching that IELTS value.
    Never overwrites prose results.
- **Confidence ladder**: prose=0.85 > equivalence_table=0.8 >
  AI fallback=0.5 — guarantees AI cannot clobber real table data
  and prose still wins on conflict.
- **End-to-end on /tmp/vit.html**: now extracts IELTS=6.5 (regex),
  PTE=55, TOEFL=81, CAE=176 (all equivalence_table). Matches the
  page's own published values exactly.
- **Coverage**: 4 new tests in \`tests/test_extractors.py\` —
  fills missing slots from the real VIT table layout, does NOT
  overwrite prose extractions, skipped when no IELTS anchor is
  present, and ignores non-equivalence tables (e.g. fees tables).
- **Test summary**: 308 passed, 1 skipped (was 304+1; +4 new).
- **Why not just improve vision**: vision still has a role for
  image-only equivalence tables (rare on AU/UK uni sites). For
  pages that render the table as HTML (the common case), structured
  parsing is both cheaper and 100% accurate.

### 9. CSU browser-fallback never escalated on the AI path
- **Symptom**: study.csu.edu.au course pages with no static campus list
  (offerings JSON hydrated client-side) still returned with empty
  `courseLocation` even when a browser fetch would have recovered it.
- **Root cause**: `needsBrowserFallback` in
  `artifacts/api-server/src/routes/scrape.ts` has a CSU-specific rule
  (`if (url && isCsuCoursePage(url) && !hasLocation && studyMode !==
  "Online") return true;`) that only fires when the URL is passed in.
  The no-AI call site already passed `link.url`; the AI-path call site
  was calling it as `needsBrowserFallback(quickData)` without the URL,
  so the CSU branch silently no-op'd on the hot path.
- **Fix**:
  1. AI-path call site now passes `link.url`.
  2. Function extracted from `routes/scrape.ts` into a dedicated module
     `artifacts/api-server/src/lib/needs-browser-fallback.ts` (with a
     small structural input type `BrowserFallbackInput`) so it can be
     unit-tested without dragging in the full scrape.ts dependency
     graph (gemini, drizzle, browser-helper).
  3. Both call sites import the helper from the new module.
- **Coverage**: new test file
  `artifacts/api-server/src/routes/needs-browser-fallback.test.ts`
  with 6 cases — missing `courseLocation` on CSU, whitespace-only
  `courseLocation`, online-only CSU exemption, populated
  `courseLocation` no-op, non-CSU host not falsely triggered, and
  URL-omitted legacy-caller safety.
- **Result**: Node tests 89/93 pass (same 4 unrelated pre-existing
  failures as before this change). Python tests unchanged at
  308 passed / 1 skipped. API server build + restart clean.

### 10. Live progress bar / elapsed timer / ETA missing on the scrape page (Apr 2026)
- **Symptom**: While a scrape was running on a 24-course university (e.g.
  VIT), the UI only showed the per-course-vision counter `[per-course
  vision img 0/6]` next to the URL — no overall `1/24` bar, no elapsed
  time, no estimated time remaining. The "Scraping in Background…"
  badge stayed green but the user had no sense of progress.
- **Root cause**: The React UI in
  `artifacts/university-portal/src/pages/scraping.tsx` renders the
  progress bar (with `current/total`, ETA, and elapsed labels) only when
  it finds a log row with `event === "progress"` carrying `current` and
  `total` fields. The legacy Node backend emitted such rows
  (`artifacts/api-server/src/routes/scrape.ts:8642 / 10950`) but the
  Python rewrite (`backend-py/app/services/scraper/orchestrator.py` and
  `repair.py`) only emitted `event="status"` rows like
  `[EXTRACT] N/total: <name>`. The frontend filter at
  `scraping.tsx:1140` (`scrapeLogs.findLast(l => l.event === "progress")`)
  therefore never matched, and the entire bar / timer / ETA block was
  skipped.
- **Fix**: alongside each existing `[EXTRACT] N/total` status emit in the
  per-course loops of both `orchestrator.py` and `repair.py`, also emit
  a structured `event="progress"` row with `current=idx, total=total,
  courseName=name, url=...`. The status row is preserved for the
  textual log; the new progress row drives the bar.
- **Result**: as soon as the first parallel extract grabs a slot (idx=1),
  the bar renders with `1/24`, the elapsed counter starts ticking
  every second from `scrapeStartTime`, and ETA appears once at least
  one course has finished (so a per-item pace is computable). Bar and
  timer remain visible during long per-course-vision / per-course-browser
  fallbacks because they sit inside the same extract loop.
- **Tests**: 308 passed, 1 skipped (unchanged baseline — change is purely
  additive log emission).


## VIT Parity Port (Apr 2026, Node→Python)

**Goal:** close the per-course completeness gap between the legacy
Node `artifacts/api-server` scraper and the Python rewrite under
`backend-py`. On VIT pages, prod (DigitalOcean, Python) was returning
~24 candidates with empty IELTS sub-bands and missing duration / intake
/ location, while preview (Replit, Node) returned ~30 candidates with
full data. Five missing features were ported:

1. **Home-page → course-listing redirect**
   `app/services/scraper/home_page_redirect.py::detect_course_listing_page`
   — HEAD-probes 12 high-priority paths (`/course-list`,
   `/study/degrees-and-courses`, `/courses`, …), falls back to a link-
   scan with weighted scoring (URL-pattern + link-text), then a broad
   HEAD-probe over 17 common catalogue paths. Wired into
   `discovery.py::discover_course_links`; only fires when `start_url`'s
   path is `/`. Strong URL patterns are anchored to end-of-path so a
   leaf URL like `/courses/bachelor-of-business` never wins.
2. **Per-course Bootstrap-modal English-test extractor**
   `app/services/scraper/per_course_modal.py::extract_modal_english`
   — locates `.modal/[role=dialog]` containers, parses concordance
   tables, picks the row whose IELTS is closest to the degree-level
   target (5.5 cert/diploma · 6.0 bachelor · 6.5 master/MBA), and
   recovers IELTS sub-bands (L/R/W/S) via three regex patterns (A/A2/B/C).
   Wired into `single_course.py` BEFORE per-course-browser; gated on
   any english slot being empty so it's a no-op when primary extraction
   already filled IELTS/PTE/TOEFL/CAE.
3. **VIT static fallback for duration / intake / location**
   `app/services/scraper/vit_static_extract.py::apply_vit_summary_extraction`
   — re-parses the static (server-rendered) HTML to recover the
   `<p><strong>Duration:</strong>` narrative paragraph and intake-list
   `<ul>`s that the per-course-browser pass strips when it clicks the
   "International students" toggle. Host-gated on `vit.edu.au`; merge
   uses `setdefault`-style first-write-wins.
4. **Category-filtered listing expansion**
   `app/services/scraper/home_page_redirect.py::expand_course_list_with_categories`
   — for known category slugs, HEAD-probes 4 URL variants per slug
   (`?course_categories[0]=`, `?category=`, `?type=`, `/{slug}`),
   fetches the first that 200s, harvests new course links via the
   existing `_looks_like_course` filter. Slugs are split into
   `_GENERIC_CATEGORY_SLUGS` (degree-level names: `bachelor`, `master`,
   `diploma`, `certificate`, …, tried on every host) and
   `_HOST_CATEGORY_SLUGS` (host-keyed dict; `vit.edu.au` maps to brand
   slugs `bits`, `mits`, `mba`, `bbus`, `vocational`, `elicos`, only
   probed on that host). Path-gated to VIT-shaped listing roots
   (`/course-list`, `/course-finder`, `/course-guide`); 3-empty-slug
   early-exit caps the worst-case cost at ~12 HEAD probes.
5. **Browser "International" toggle action**
   `browser_pool.py::fetch_html(click_international=True)` runs an
   in-page JS evaluator that finds a radio / checkbox / link / button
   matching `/international|overseas|offshore/`, clicks it, then waits
   for network-idle + 1.2 s. Wired into `per_course_browser.py` via
   `_needs_international_toggle(url)` (host whitelist:
   `vit.edu.au`).

**Live verification:** the Bachelor of Business worker emitted
`G-db-insert-payload :: Bachelor of Business :: {"ieltsOverall":6,
"ieltsListening":5.5,"ieltsReading":5.5,"ieltsWriting":5.5,
"ieltsSpeaking":5.5}` immediately after the FastAPI restart — exactly
the values the Node scraper produces.

**Tests:** 37 new unit tests across
`tests/test_home_page_redirect.py`,
`tests/test_per_course_modal.py`,
`tests/test_vit_static_extract.py`,
`tests/test_category_expand.py` (host-config split, slug-leak
invariant), and
`tests/test_browser_international_toggle.py` (mocked Playwright page,
verifies toggle JS routing + post-click waits + silent-error
behaviour). Existing 38 regression tests still pass (data-parity,
discovery-regression, browser-fallback-timeout, scraper-pipeline-
parity, completeness). Total: 75 passing.

**Hardening (architect-driven follow-ups, commit `12d15f7`):**
- Toggle JS now captures `location.href` before click and returns
  false if the click navigated away — prevents a nav-menu
  "International" link from being treated as a successful fee-toggle
  click. Strategy 2 (text-based) skips elements wrapped in
  `<nav>`/`<header>`/`<footer>` and no longer considers `<a>` tags.
- Category expansion path-regex tightened from
  `/(course-list|course-finder|courses?|programs?)/?$` to only the
  VIT-shaped paths (`/course-list`, `/course-finder`, `/course-guide`).
  Generic `/courses` and `/programs` paths excluded.

**Outstanding gaps vs Node** (not yet ported, tracked for PR-5):
- `[fix3]` short-circuit (skip requirements-page vision-AI when modal
  returned complete IELTS+PTE+TOEFL+CAE — `scrape.ts:8276`). Would
  cut per-course time to ~0.1s on VIT.
- `[browser ✓ intl]` log line with `international_toggle_scripted[N]`
  strategy attribution.
- `[SMART]` discovery prefix + sitemap-first candidate discovery
  (`scrape.ts:10367`). Python uses `[DISCOVER]` BFS path.
- `detect_course_listing_page` Step-3 GET-content fallback
  (`scrape.ts:7071-7077`) for hosts that reject HEAD but serve GET.

**PR-5 ASA/Torrens regression sweep (status after owner review):**

Owner verdict on the five-bug PR-5: ship Bug 3 alone as PR-5. Bugs 1
and 2 fixes were rejected as "papering over" the real issue. Bugs 4
and 5 partially address Torrens but need additional work (real
catalogue discovery + cross-page nav dedup) before they can land.

- Bug 1 (postgrad IELTS bump) — **REVERTED.** Owner correctly
  identified the bump as a heuristic that masks the real problem.
  Diagnosis: `sibling_cache.py:148-152` and `single_course.py` uni-PDF
  backfill both already enforce course-page-wins (skip-if-set). The
  ASA Bachelor of Business page publishes its english requirements as
  a screenshot PNG (`Screenshot%202026-01-19%20104316.png`), so the
  per-course extractor fills nothing → uni-PDF backfill fires
  correctly → bachelor-tier value gets stamped on every course
  including masters. Right fix lives upstream: OCR the screenshot,
  per-degree-level PDF parsing, or surface the gap as needs-review.
  `_postgrad_english_bump`, `_is_postgraduate`, `_POSTGRAD_TOKENS`
  removed from `pipelines/single_course.py`; backfill loop restored to
  store PDF value verbatim. `test_postgrad_english_bump.py` deleted.
- Bug 2 (study_mode bare-online confidence 0.5) — **DIAGNOSED, FIX
  PENDING.** Owner correctly noted lowered confidence is meaningless
  without a downstream consumer. ASA HTML diagnosis: course page has
  literal `<strong>Delivery</strong> Face to Face on campus` and
  `<strong>Location</strong> Sydney, Online` in adjacent
  `course-header-text` divs. Extractor reads "Online" from the
  Location field and ignores the explicit Delivery label. Right fix:
  parse the sibling-div label/value pattern so an explicit
  `Delivery: Face to Face on campus` beats any keyword match in other
  fields. The 3-tuple `ExtractionResult` plumbing stays (it's neutral
  scaffolding); the bare-online confidence change can be removed once
  the proper Delivery-label extractor lands.
- Bug 3 (per-host browser config) — **APPROVED, READY TO SHIP.**
  `_browser_config_for` returns `(wait_until, settle_ms,
  outer_timeout_sec, goto_timeout_ms)`. Default 20s/15s (was 60s);
  VIT keeps 30s/25s for SPA hydration. `browser_pool.fetch_html`
  catches Playwright `TimeoutError` ONLY and falls back to
  `page.content()` with 1024-byte floor + Chromium error-page sniff
  (`neterror`, `chrome-error://`, `ERR_*`). 9 + 4 + 2 unit tests.
  This is what gets pushed as the standalone PR-5.
- Bug 4 (nav/news URL filter) — **PARTIAL.** Implemented:
  `discovery._is_known_non_course_url` + `_JUNK_LAST_SEG_RE` reject
  `/stories/`, `/news/`, `/blog/`, `/studying-with-us/`, `/about/`,
  `/research/`, plus last-segment suffixes (`-events`,
  `-scholarships`, `-jobs`, …). Wired into `_looks_like_course`. 16
  tests. Still missing per owner: cross-page nav dedup ("if same 11
  links appear on 25 crawled pages, those are nav, not courses").
- Bug 5 (category-landing drill-in) — **PARTIAL.** Implemented:
  `discovery._is_category_landing` detects
  `/courses/{single-segment-without-degree-qualifier}` shapes on
  `courses/programs/programmes/degrees/study` bases. BFS legacy
  sweep enqueues these at `depth<2`. Still missing per owner: locate
  the actual Torrens course directory (152 real courses) — current
  drill-in alone won't surface them if they live behind a different
  URL pattern.
- Evidence Review API fix (LANDED in `routers/scrape.py`):
  `_attach_evidence_bulk` loads evidence with a single
  `WHERE scraped_course_id = ANY(:ids)` query and attaches camelCase
  aliases (`fieldKey`, `candidateValue`, `normalizedValue`,
  `sourceUrl`, `pageType`, `extractionMethod`, `decisionScore`,
  `validationStatus`, `decisionStatus`, `selected`). Wired into
  `/staged` and `/staged/{job_id}`. 4 tests in
  `test_staged_list_evidence.py`.

## PR-6 Bug 1 — uni-PDF / sibling-cache precedence audit (Apr 2026)

User report: ASA postgrad courses staged with `ielts_overall=6.0` —
the bachelor-tier number from the requirements PDF — even though the
two cohorts have different policies. Investigation conclusion: the
**precedence rule is correct everywhere**; the leak is a missing-data
problem, not a merge bug.

### Audit of every per-course payload merge site (`pipelines/single_course.py`)

Order of operations in `extract_course`, with the conflict-resolution
rule at each step:

1. **Per-course extractors** (course_name, location, fee, english_test,
   intake, duration, degree_level, study_mode) — `if v is None: continue`
   then `payload.setdefault(k, v)`. Nones filtered → setdefault pins
   highest-confidence first-write. ✓
2. **Per-course modal (T002)** — explicit empty-aware:
   `if k in payload and payload.get(k) not in (None, "", 0): continue`. ✓
3. **Per-course browser (T207)** — `payload.setdefault(k, v)`. ✓ in
   practice (`per_course_browser` strips empties before returning).
4. **Per-course vision (T208)** — `payload.setdefault(k, v)`. ✓ in
   practice (vision returns no key when empty).
5. **VIT static fallback (T003)** — explicit empty-aware:
   `if v in (None, "", 0): continue; if payload.get(k) not in (None, "", 0):
   continue`. ✓
6. **AI fallback** — `payload.setdefault(k, v)`. ✓ (`ai_fallback.fill_missing`
   only returns coerced-non-None values, line 163).
7. **uni-PDF backfill (fee + english blocks)** — was `if v is None or
   k in payload: continue` (key-exists). Hardened in this PR to
   empty-aware `if payload.get(k) not in (None, "", 0): continue` so
   it matches sibling-cache / VIT static / per-course modal. Defensive
   change — eliminated a latent fragility where any future upstream
   merge site emitting a `None` placeholder would have silently
   blocked the PDF fill.
8. **Sibling cache (orchestrator, after `extract_course` returns)** —
   `existing = payload.get(k); if existing not in (None, "", 0): continue`. ✓

Course-page wins at every site. The PR-1.5 hot-fix #6 design decision
("sibling cache only ever fills missing slots, never overwrites real
extractions") is now uniformly enforced by the same `(None, "", 0)`
predicate everywhere.

### ASA bachelor-vs-master diagnosis (live evidence)

Per-course extractor run directly against the live HTML of one ASA
bachelor (`/courses/bachelor-of-business`) and one ASA master
(`/courses/master-of-information-technology-software-application-development`):

- Both pages: `english_test.extract` returns **0 results**.
- Both pages' raw HTML contains **zero matches** for
  `ielts|toefl|pte|cambridge|english|language` (the policy lives only
  inside a PNG screenshot the rule extractors cannot read).

So the bleed is NOT a precedence bug. It is two compounding facts:

1. The per-course extractor is genuinely empty for both bachelor and
   master ASA URLs (image-only requirements section).
2. The university-level requirements PDF only publishes one tier of
   English requirements, so uni-PDF backfill applies the same
   `ielts_overall=6.0` to both bachelor courses (correct) and master
   courses (incorrect — masters need 6.5 per ASA's own policy).

### What this means for the fix

- **No code-level precedence fix is warranted** — every merge site
  already honours course-page-wins.
- **A heuristic postgrad-IELTS bump remains out of scope** (was
  reverted last session and stays reverted — see `replit.md` PR-1.5
  hot-fix #6).
- **The real fix is upstream** and falls into one of two future tasks:
  (a) discover and parse a master-tier requirements PDF if ASA
  publishes one (research required — may not exist as a separate
  document), or (b) add a per-course PNG OCR pass to read the
  embedded English-requirements screenshot. Either is a separate task,
  not part of PR-6 Bug 1.

### Latent precedence-policy gap (DOCUMENTED, NOT FIXED)

Code-review (architect) flagged a real but ASA-unrelated ordering
issue: `extract_course` runs uni-PDF backfill BEFORE returning, and
`backfill_english_from_siblings` runs AFTER in the orchestrator. So
when one sibling in a bucket has a real per-page extraction (e.g.
postgrad sibling X extracted IELTS=6.5 from its own page) and another
sibling (Y) only has the generic uni-PDF value (6.0), sibling-cache
sees Y as "already filled" and does NOT supply the better per-cohort
peer value to Y. Y stays at 6.0.

Why not fix here:

- The actual ASA bug is unaffected — both ASA tiers are image-only,
  so NO sibling has a real extraction to promote.
- The project's "fill gaps only" principle in `replit.md` puts uni-PDF
  and sibling-cache in the same low-confidence tier. Reordering them
  is a deliberate product choice, not an obvious correctness fix.
- A safe restructure (move uni-PDF backfill out of `extract_course`
  into the orchestrator, run after sibling-cache) needs care because
  `extract_course` is also called directly by `repair_scrape` and
  several unit tests.

The current ordering is now pinned by
`tests/test_university_pdfs.py::test_uni_pdf_backfill_is_applied_before_sibling_cache`
so any future change is intentional. Tracked as a follow-up: "decide
whether sibling-cache should beat uni-PDF when same-bucket peer
extractions exist".

### Tests

- `tests/test_university_pdfs.py::test_extract_course_backfills_from_uni_pdf`
  (already existed) — empty page → uni-PDF fills.
- `tests/test_university_pdfs.py::test_extract_course_pdf_does_not_overwrite_existing`
  (already existed) — page has fee+IELTS → uni-PDF dropped, page wins.
- `tests/test_university_pdfs.py::test_extract_course_pdf_fills_through_empty_string_placeholder`
  **(new)** — pins down the empty-aware merge after the harden:
  monkeypatches extractors to write `""`/`0` placeholders into payload
  via step-1 setdefault, then asserts uni-PDF overwrites both. Under
  the OLD `k in payload` gate the placeholders would have survived
  (test would FAIL); under the NEW empty-aware gate the PDF fills
  them. Verified mathematically and by execution.
- `tests/test_university_pdfs.py::test_uni_pdf_backfill_is_applied_before_sibling_cache`
  **(new)** — pins the current ordering so the sibling-vs-uni-PDF
  policy is explicit, not accidental.

Full suite: 387 passed, 1 skipped (was 385+1 → +2 new tests).

## PR-6 Bug 2 — ASA `<strong>Delivery</strong>` mis-classified as Online (Task #19)

### Symptom
Every ASA on-campus course (e.g. `/courses/bachelor-of-business`)
staged with `study_mode='Online'` despite the page literally saying:

```html
<div class="course-header-text"><strong>Delivery</strong></div>
<div class="course-header-text">Face to Face on campus</div>
```

### Diagnosis (proved with `/tmp/diagnose_study_mode.py`)
The previous extractor relied entirely on a tag-stripped flattened
text run, then walked `_MODE_PATTERNS` in priority order. ASA's
header is a sequence of sibling-div label/value pairs:

```
<strong>Location</strong>  Sydney, Online
<strong>Delivery</strong>  Face to Face on campus
<strong>Course Duration</strong>  3 years Full Time
```

Tag-stripping flattens that into:
`"… Location Sydney, Online Delivery Face to Face on campus Course Duration …"`

`_MODE_PATTERNS[1]` (`online\s+(?:study|delivery|course|mode)`) then
matches the substring **"Online Delivery"** at the boundary between
the previous Location *value* ("Sydney, Online") and the next *label*
("Delivery") — and returns `("Online", …, 0.7)` before
`_MODE_PATTERNS[2]` (which would have matched "on campus" / "face to
face") gets a chance.

Diagnostic dump:
```
[1] -> Online      match='Online Delivery'  pattern='\\b(fully\\s+online|100%\\s+online|online\\s+(?:study|delivery|c'
        context: …'AQF Level 7 Location Sydney, Online Delivery Face to Face on campus Course Duration '…
classify_study_mode -> ('Online', 'AQF Level 7 Location Sydney, Online Delivery Face to Face on campus Course', 0.7)
```

### Fix design
Structural pre-pass `_extract_strong_label_value(html)` runs BEFORE
the flattened-text fallback. It uses BeautifulSoup to find every
`<strong>` (or `<b>`) whose text matches a delivery-label whitelist
(Delivery, Study mode, Mode of study/attendance/delivery/learning,
Delivery method, Learning mode/method, Attendance mode), then walks
forward in document order via `next_elements` collecting text until
it hits the next `<strong>`/`<b>`/`<h1-6>`/`<dt>` (cap 300 chars).
The collected value text is fed to the existing
`_classify_label_value(value)` and the first canonical hit returns
`(label, snippet, 0.7)`.

This bypasses tag-stripping entirely for the high-signal case, so
the boundary-collision class of bugs cannot fire.

### Generality
Same `<strong>Label</strong>` idiom is used by **VIT** for course
metadata (`<p><strong>Locations:</strong> Melbourne, Sydney</p>`,
parsed by `vit_static_extract.py`). The new pre-pass handles BOTH
shapes (sibling-div for ASA, inline-after-strong for VIT) so the fix
generalises across AU university templates that emit `<strong>` as a
field-label tag — not ASA-specific.

### Tests
- `tests/test_study_mode.py::test_strong_delivery_sibling_div_classifies_as_on_campus`
  — exact ASA structure (sibling div), pre-fix returned `'Online'`.
- `tests/test_study_mode.py::test_strong_label_inline_value_with_colon_classifies`
  — VIT-style `<p><strong>Delivery:</strong> Face to face</p>`.
- `tests/test_study_mode.py::test_strong_label_does_not_misfire_on_unrelated_strong_tags`
  — `<strong>Apply</strong>` / `<strong>Contact Us</strong>` skipped;
  unrelated `<strong>fully online</strong>` still classifies via
  the keyword path.
- `tests/test_study_mode.py::test_strong_label_value_blended_when_value_lists_both_modes`
  — multi-mode value `"On Campus and Online"` → Blended.
- `tests/test_study_mode.py::test_asa_full_bachelor_fixture_classifies_as_on_campus`
  — end-to-end against the saved `tests/fixtures/asa_bachelor_of_business.html`
  (26566 bytes), confirms the actual production page now classifies
  correctly.

Full suite: 392 passed, 1 skipped (was 387+1 → +5 new tests).

## PR-6 Bug C investigation — debunked at the browser layer

### Hypothesis under test
User-reported symptom: "the per-course browser fetch returns empty
for ASA". Three priors offered: JS bundle 404 / wrong wait-condition
/ http vs https URL.

### Method
Reproduced the EXACT scraper code path locally:
* `pool.fetch_html(url, wait_until="domcontentloaded", settle_ms=1500,
  timeout=15000, click_international=False)` — same args
  `per_course_browser._browser_config_for("asahe.edu.au")` returns
  for ASA (apex `asahe.edu.au` is NOT in `_NETWORKIDLE_HOSTS`).
* Tested all three URL forms the user suggested as suspect:
  `http://asahe.edu.au/...`, `https://asahe.edu.au/...`,
  `https://www.asahe.edu.au/...`.
* For each, captured: `resp.status`, redirect chain, `page.url`
  after goto, content length, presence of `<strong>Delivery</strong>`
  marker, `<img>` tags with english/language/requirement in src,
  presence of `IELTS` substring.
* Then ran `english_test.extract(html, url)` against the rendered
  HTML to see what the downstream extractor finds.
* Then ran the PUBLIC entry-point `maybe_browser_refetch(url, payload={})`
  with empty payload to force the same code path the scraper uses
  when the static fetcher returns no english values.

Diagnostic script: `/tmp/diagnose_asa_browser_fetch.py`.

### Findings — the browser fetch is NOT empty
* All three URL forms resolve to a 200 OK at
  `https://www.asahe.edu.au/courses/bachelor-of-business`
  (apex 301→ www, http 301→ https).
* Browser fetch returns **30329 bytes** of fully-rendered HTML
  (vs 26523 from the static `httpx` fetcher — the 3806-byte delta
  is just GA / GTM / WebFlow scripts injected at runtime, no new
  course data).
* Rendered HTML contains every course-page marker we care about:
  `<strong>Delivery</strong>` ✓, `<strong>Location</strong>` ✓,
  `Face to Face on campus` ✓, `Bachelor of Business` title ✓,
  `data-wf-page` (Webflow-served, server-rendered) ✓.
* `maybe_browser_refetch(url, payload={})` returned
  `(filled={}, evidence=[], rendered=30329-byte HTML)` — the browser
  did its job; the *extractor* just had nothing english-shaped to
  pull out.
* `english_test.extract` against the rendered HTML returned **0
  results**. Same against the static HTML — also **0 results**.
  Browser pass and static pass produce IDENTICAL extraction output.

### Why the page has no english requirements text
* Page has 10 `<img>` tags. None have `english`, `language`, or
  `requirement` in the `src`.
* `IELTS` and `Test of English Language` substrings are absent
  from the rendered HTML entirely.
* ASA renders the full English-requirements panel as a baked-in
  PNG / JPEG asset somewhere in the design system (a marketing-design
  decision, not a SPA hydration delay). Neither static nor browser
  fetch can read those numbers — both see zero text to regex.

### Verdict
**Bug C as originally framed is debunked.** The browser fetch
returns the full server-rendered HTML for ASA on every URL form
tested, with no JS bundle 404, no wait-condition issue, no http /
https mismatch, and no Akamai gate. The actual production
"empty response" symptom (when seen) is the **`per_course_browser_empty`
status emit** firing because:
  1. The static fetcher correctly extracts `study_mode`, `location`,
     `duration`, `intake` from the server-rendered HTML — so for
     those fields the page IS the source of truth.
  2. The english slots come back empty (image-only requirements →
     0 hits from the regex extractor).
  3. `_all_english_empty(payload)` fires → browser refetch is
     attempted.
  4. Browser refetch returns the SAME server-rendered HTML the
     static fetcher already had → still 0 english hits → the
     `[per-course browser ✓]` log line shows `IELTS=— PTE=— TOEFL=— CAE=—`
     i.e. visually "empty" output despite a successful 30329-byte
     HTML response.

The fix is image-content-aware extraction (OCR / image-URL pattern
recognition / sibling-cache lookup), not browser configuration.
That work is already scoped as project task #22
("Read English requirements from ASA's image-only course pages") —
no new PR-6 Bug 3 task needed.

### Recommended task scope refinement
Project task #20 ("Make the live course-page browser fetch work
for ASA so course data isn't pulled from PDFs") is misframed —
the live browser fetch DOES work; the issue is that the
**english-requirements field falls back to the PDF because the
course page has no extractable english text**. Recommend
either renaming #20 or merging it into #22.

### CORRECTION (2026-04-25) — partially wrong verdict

The above "debunked" verdict was issued from a diagnostic shell that
HAD the chromium runtime libs installed. The Celery worker process
that actually runs scrape jobs DID NOT have those libs and was
silently returning empty responses for every per-course browser
fetch — symptom logs show
`[per-course browser ✗] {url}: empty response` for all 9 ASA URLs
on a real sweep.

What's still correct from the analysis above:
* When the browser layer DOES work, the rendered HTML IS 30329 bytes,
  and that HTML genuinely has no `IELTS` / `PTE` / `TOEFL` / `CAE`
  text strings (image-only English requirements). Task #22 is still
  the right home for the OCR fix.

What's wrong:
* The verdict that the browser fetch always succeeds. It only succeeds
  if chromium can actually launch. If the worker's environment is
  missing `libnspr4.so` (or any other chromium runtime lib), the
  browser layer returns empty for EVERY URL — including non-ASA ones
  — and the symptom looks identical to a real "no extractable english
  text" empty.
* The recommendation to merge / rename task #20. Task #20 is the
  legitimate home for "make sure the browser layer can actually run
  on the deployed worker" — independent of the OCR work in #22.

### Required environment for the per-course browser fetch
Both the local Celery worker and the production worker must have
the chromium runtime libs installed BEFORE the worker process
starts. If chromium libs are installed AFTER the worker starts, the
worker will keep failing until restarted (subprocess env is
inherited at fork time). Symptoms of a misconfigured worker:
* `[per-course browser ✗] {url}: empty response` on every URL
* Worker stderr: `error while loading shared libraries: libnspr4.so:
  cannot open shared object file`
* All courses get identical fees (uni-PDF stamping the gap left by
  the empty per-course response)
* AI fallback fires for every course missing
  `international_fee, ielts_overall, duration_text, intake_text,
  location_text` — i.e. all the fields that should have come from
  the per-course HTML.

Smoke test: `cd backend-py && PYTHONPATH=. python -c "import asyncio;
from playwright.async_api import async_playwright; ..."` — should
return 30329 bytes for a known-good ASA URL.

### Reproducer fixtures
* `backend-py/tests/fixtures/asa_bachelor_of_business.html`
  (26523 bytes — what the static `httpx` fetcher sees)
* `backend-py/tests/fixtures/asa_bachelor_of_business_browser_rendered.html`
  (30329 bytes — what `browser_pool.fetch_html` returns; identical
  course data, +GA/GTM script injection)
* `/tmp/diagnose_asa_browser_fetch.py` — re-runnable diagnostic.

### Local-env note (does not affect production)
This dev container did not have Playwright installed at investigation
time. Resolved by `pip install playwright>=1.44.0`, then
`python -m playwright install chromium`, then installing the Nix
system libs Chromium needs at runtime: `nspr`, `nss`, `atk`,
`at-spi2-atk`, `at-spi2-core`, `cups`, `dbus`, `expat`,
`gdk-pixbuf`, `glib`, `gtk3`, `libdrm`, `libxkbcommon`, `mesa`,
`alsa-lib`, `pango`, `fontconfig`, `freetype`, `cairo`,
`xorg.libX11`, `xorg.libXcomposite`, `xorg.libXdamage`,
`xorg.libXfixes`, `xorg.libXrandr`, `xorg.libXext`, `xorg.libxcb`,
`libgbm`, `xorg.libXtst`, `xorg.libXi`, `xorg.libXcursor`. Future
fresh containers may need the same setup if the chromium browser
fallback is to be exercised locally.

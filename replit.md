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

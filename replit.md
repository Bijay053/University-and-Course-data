# University Course, Fee, Intake & Requirement Management System

## Overview

This project provides a centralized administrative portal for universities to manage their course-related data. It enables comprehensive management of courses, fees, intakes, scholarships, and admission requirements. Key capabilities include AI-powered web scraping for data acquisition, bulk upload/download functionalities, and change detection mechanisms. The system aims to streamline data management for educational institutions, offering a robust solution for maintaining up-to-date and accurate course information.

## User Preferences

- Provide commands in the format `cd /root/University-and-Course-data && <command>`.
- Always provide commands in the specified format, especially for production deployment and verification.
- When schema changes are needed, explicitly provide the `pnpm --filter @workspace/db push --force` command before builds.
- The Node.js API server has been deleted. Python FastAPI is now the sole API server in both dev and production.
- Provide verification commands to confirm commit deployment, new bundle serving, and correct PM2 environment variables.
- **GitHub push target**: Always push to `Studyinfocentre/University-and-course-managment` using `STUDYINFO_GITHUB_PAT`. Command: `git push "https://Studyinfocentre:${STUDYINFO_GITHUB_PAT}@github.com/Studyinfocentre/University-and-course-managment.git" HEAD:main`

## System Architecture

The system is built as a monorepo utilizing `pnpm workspaces`.

### Technology Stack

- **Frontend**: React with Vite, styled using Tailwind CSS and `shadcn/ui`. Data fetching is managed by TanStack React Query, and routing by `wouter`.
- **Backend**: FastAPI (Python / Uvicorn) serving on port 8080 — both dev and production. Node.js API server has been deleted.
- **Database**: PostgreSQL with Drizzle ORM for type-safe data access.
- **Type Safety & Validation**: TypeScript 5.9, Zod (`zod/v4`), and `drizzle-zod`.
- **API Code Generation**: Orval, generating client code from an OpenAPI specification.
- **Build System**: esbuild for CommonJS bundles.

### Authentication

The admin portal now requires login. The auth flow:
- `GET /api/auth/me` is called on startup; redirects to `/login` if no valid session.
- `POST /api/auth/login` with `{ email, password }` sets an `httponly` JWT cookie named `session` (7-day expiry).
- `POST /api/auth/logout` clears the cookie and returns to `/login`.
- Default credentials: email `admin@university-portal.local`, password `Bijay@12345` (overridden by `ADMIN_EMAIL`/`ADMIN_PASSWORD` env vars on the production server).
- Auth state managed by `src/context/auth.tsx` (`AuthProvider` + `useAuth` hook).
- All protected routes wrapped in `AuthGuard` in `App.tsx`.
- Logout button visible at the bottom of the sidebar when logged in.

### Core Features

- **Dashboard**: Provides an overview with statistics, courses by degree level, upcoming intakes, and recent changes.
- **University Management**: CRUD operations for universities, including viewing associated courses.
- **Course Management**: Comprehensive CRUD for courses, with detailed views covering intakes, fees, English requirements, academic requirements, and scholarships.
- **AI-Powered Web Scraper**: Extracts course data from university websites, utilizing AI for advanced data extraction and fallback mechanisms. Scraped data is staged for review.
- **Bulk Data Operations**: Supports bulk Excel uploads for importing course data and CSV downloads.
- **Data Import History**: Tracks all import jobs for auditing and review.
- **Scraping Job Management**: Includes functionalities to trigger, monitor status, and review/approve/reject scraped changes.
- **Repair Scrape**: A "back-fill only" pass for existing courses with blank key fields, ensuring data completeness without overwriting existing values.
- **Mode/Duration Extraction**: Robust extraction of study modes and course durations with AI fallback and rule-based parsing.
- **PDF Data Extraction**: Advanced parsing of PDF documents for fees and English requirements, including per-course matching in multi-row tables.
- **Gemini Cost Optimisation (Priority 6)**: Six-component cost-reduction system:
  - *Skip gate* (`gemini_gate.py`): skips Gemini or downgrades to a cheap 100-token classification-only prompt when other extractors already populated ≥90% of high-value fields at ≥0.70 confidence. Expected 30-50% cost reduction on static-HTML-rich universities.
  - *Circuit breaker* (`gemini_client.py`): `GeminiQuotaTracker` singleton trips after 5 quota errors (HTTP 429/503/keywords) within 60 s; stays open 5 min to prevent cascading quota failures.
  - *Cost ceiling* (`cost_ceiling.py`): `JobCostMonitor` per scrape job caps Gemini spend per university; per-university budgets configurable via `LARGE_UNI_BUDGETS` dict.
  - *Call log table* (`gemini_call_log`): every Gemini API call logged with `call_type`, model, tokens, cost, duration, success, scrape_run_id FK. Written by orchestrator after each gather() batch.
  - *Per-job cost columns*: `scrape_runtime_jobs.total_gemini_cost_usd` and `cost_ceiling_hit` written at job completion.
  - *SQL reporting views*: `v_gemini_cost_by_university`, `v_gemini_cost_by_call_type`, `v_gemini_top_spenders_30d`, `v_gemini_skip_efficiency` for cost dashboards.
  - *Model*: `gemini-2.5-flash-lite` confirmed cost-optimal (Component 5 check script at `backend-py/scripts/check_gemini_model.py`).
- **Per-host URL rewriting**: UNE appends `?international=true`; UOW appends `?students=international&year=<year>` before fetching each course page so the international-student fee, IELTS, intake, and campus data is visible.
- **UOW discovery**: BFS page budget raised to 80 (non-fast mode) and all 70 pagination pages pre-seeded so the full ~300 course catalogue is discovered.
- **University of Huddersfield — SearchStax Solr provider (uni_id=1166 dev, 2026-05-29)**: The live site (`www.hud.ac.uk` / `courses.hud.ac.uk`) is a Cloudflare-protected React SPA; the old `courses.hud.ac.uk/json/...` endpoint now returns an SPA shell, and browser discovery is unreliable. The SPA itself queries a SearchStax-hosted Solr core client-side, so we query that core directly and bypass HTML/browser discovery AND per-course extraction.
  - **Provider**: `backend-py/app/services/scraper/searchstax_hud.py` — `fetch_searchstax_links(cfg, emit)` paginates Solr (`fq=sectionType_s:course`, ~790 docs) and maps each doc → a prebuilt link dict `{name, url, searchstax_result:{name, url, payload, evidence}}`. Each `content` field (~15KB page text) supplies IELTS, entry requirements, duration, mode, intake. Fees are band-derived (UG/PG subject→band lookup, most-specific-first); name reformatter leads with the degree phrase. Emits evidence rows for the 3 evidence-guarded critical fields (`international_fee`, `ielts_overall`, `study_mode`).
  - **Orchestrator wiring** (`orchestrator.py`): when `discovery.searchstax` is set, `run_scrape()` calls the provider for links and disables browser; `_extract_only()` short-circuits and returns `link["searchstax_result"]` verbatim (no `extract_course()` call).
  - **Schema**: `SearchStaxConfig` Pydantic model on `DiscoveryConfig` (`config/schema.py`).
  - **Config**: `scraper_config/unis/hud.yaml` — `discovery.searchstax` block. Token is read ONLY from the `HUD_SEARCHSTAX_TOKEN` secret (`token_env`); NO literal token is committed. Set this secret in every environment and rotate it periodically in the SearchStax console.
  - **Guards**: `guards.py` `_DEGREE_QUALIFIER_RE` extended (additively) with `postgraduate (certificate|diploma)` and `foundation degree`.
  - **Completeness improvements (2026-05-30)**: Two new extractor functions added to `searchstax_hud.py`:
    - `_academic_level(degree_level)` — derives Undergraduate/Postgraduate/Doctorate from `degree_level` string; sets `academic_level` on every staged course (100% fill).
    - `_extract_entry_requirement(content)` — regex over Solr `content` field; tries `_DEGREE_REQ_RE` (explicit degree-classification phrases) first, then falls back to `_ENTRY_REQ_RE` anchor with a `_LANG_REQ_RE` guard that filters out IELTS/English-language sentences that share the same pattern.
  - **Verified end-to-end (dev, 2026-05-30)**: `total_found=574` (Solr filtered to 2025+ year, ~787 raw deduped to ~574 distinct links), `imported=294`, `errors=280` (280 = year-duplicate dedup rejections — same course in 2026-27 AND 2027-28 — expected, not crashes). Avg completeness **85.1%** (up from 66%); **224/294 courses at ≥85%** auto-publish threshold. All 294 have `academic_level` and `other_requirement` set. Gemini cost $0.00.
  - **Test suite**: `backend-py/tests/test_searchstax_hud.py` — 50 unit tests covering `_academic_level`, `_extract_entry_requirement` (including IELTS-filter cases), `_parse_duration`, `_parse_intakes`, `_fee_for`, `_reformat_name`. All 50 pass.
- **Session → intake mapping (Pass 4)**: "Autumn Session" → March, "Spring Session" → July, "Summer Session" → November fallback for Australian universities (UOW-style).
- **PTE host blocklist**: UOW course pages don't publish PTE scores — a per-host blocklist suppresses false positives from Pattern-3 broad regex.
- **Location "Delivery method" fix**: Added `delivery\s*method` to `_TRAILING_KEYS` so that label is stripped from extracted location values.
- **Per-university YAML config system (Week 1 — infrastructure only)**:
  - `backend-py/scraper_config/defaults.yaml` — conservative global defaults (change requires full regression sweep + human approval).
  - `backend-py/scraper_config/unis/<slug>.yaml` — per-university overrides. 20 stubs created for bug-reported unis (acap, acu, ait, asa, aut, bmihms, bond, cdu, csu, ecu, jcu, kaplan, kbs, latrobe, saibt, torrens, uel, uow, vit, acpe).
  - `backend-py/app/services/scraper/config/` Python package: `schema.py` (Pydantic `UniConfig` split into `discovery` + `extraction` sections), `loader.py` (deep-merge: defaults → DB `scrape_config` translation → per-uni YAML), `context.py` (`ContextVar[UniConfig]` for scrape-job scope).
  - Config is loaded and set as a contextvar at the start of every `run_scrape()` and `run_repair()` call. No extractor reads it yet (pure infrastructure). Week-2 migrates hardcoded hostname if-blocks.
  - Contextvar audit complete: only two entry points — `orchestrator.run_scrape()` and `repair.run_repair()`. Both now call `set_uni_config()`. No FastAPI routers or scripts call extractors directly.
  - `require_uni_config()` guard at the top of `extract_course()`: logs a WARNING + returns bare defaults if contextvar is unset (soft-fail in prod, visible as "extractor called without uni context" log lines).
  - `UniConfig.for_tier3_replay()`: returns config with only `discovery:` section. `extraction:` (including `filters:`) is stripped. Must be used by any Tier-3 playbook-matching code to prevent per-uni filter assumptions from contaminating unknown-uni scrapes.
  - `backend-py/scripts/capture_baseline.py` — snapshot staged courses with per-field `extraction_method` provenance + last-job stats (discovered, staged, skipped, Gemini cost, elapsed). Dev baseline: `backend-py/baselines/20260430_021811_*`.
  - **Prod baseline command**: `cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/capture_baseline.py --out-dir backend-py/baselines/`
  - Slug derived from hostname: `www.acu.edu.au` → `acu`, `www.aut.ac.nz` → `aut`, `bond.edu.au` → `bond`. Files named `{timestamp}_{slug}_{uni_id}.json`.

### Feature set (session 2026-05-01)

- **Tier-7 operator alert** (`discovery_failure_alerts` table): When all discovery tiers (BFS, sitemap, alt-paths, subdomain probes, browser, Wayback) complete with fewer than 3 course-link candidates, the orchestrator persists a `DiscoveryFailureAlert` row (with JSONB diagnostic) and fires a Slack/email push via `alert_delivery.deliver_discovery_failure_alert()`. Threshold = 3 (catches zero AND near-zero silently-failed runs). Migration: `013_discovery_failure_alerts.py` (apply manually on prod — see migration file header for SQL).
- **Nightly sweep Celery beat task** (`scrape.nightly_sweep`, 02:00 UTC): Runs `capture_baseline.py` to snapshot all universities into `baselines/nightly/<YYYYMMDD>/`, then runs `regression_sweep.py` to compare against the previous night's snapshot. If unexpected field-value diffs are found (exit code 1), calls `alert_delivery.deliver_drift_alert()` with a summary. First-run produces `sweep=skipped_no_baseline` (no comparison, no alert). Added to `celery_app.beat_schedule` at 02:00 UTC (before 03:00 snapshot, before 04:00 baseline-refresh).
- **Tier-2 per-uni subdomain probes** (`discover_course_links`): New `discovery_config` kwarg accepted by `discover_course_links`. When BFS + sitemap + alt-listing-path probes all yield < 5 candidates and `DiscoveryConfig.fallback_subdomains` is non-empty for the university, probes each configured subdomain (e.g. `handbook.{domain}` → `handbook.myuni.edu.au`). The `{domain}` placeholder is expanded using the apex domain (www-stripped). The orchestrator passes `_uni_cfg.discovery` to wire per-uni YAML config. 13 tests covering all three features in `tests/test_new_features_v2.py`.

### Shadow-mode operation

Enable per-uni shadow mode via env vars. **Never set these in prod `.env` permanently — set them on the Celery worker process only for the migration window.**

```bash
# Enable shadow mode for ACAP (uni_id=41)
export SHADOW_MODE_UNI_IDS=41

# After 5-run clean streak, flip to cutover
export SHADOW_CUTOVER_UNI_IDS=41
unset SHADOW_MODE_UNI_IDS
```

Reports written to `backend-py/shadow_reports/{timestamp}_{slug}_{uni_id}_run{N}.json`. Ignored by git (only `.gitkeep` is tracked). The JSON includes `is_clean`, `summary`, `clean_streak`, `cutover_ready` fields.

**Cutover criterion**: `cutover_ready: true` = clean_streak ≥ 5. Each run must be a fresh scrape, ≥1 hour apart against the live site. Streak resets on any unexpected diff.

### Kingston discovery-timeout fix (2026-07-06)

Kingston (like earlier Cardiff/QMUL cases) was failing with "[DISCOVER] Discovery phase exceeded 300s deadline". Unlike Cardiff (CF-Enterprise IP block, fixed via `scrape_do_render`), Kingston's root cause was different and shared-code:

- **Root cause**: `http_fetcher.py`'s `fetch_html()` classified 403/429/503 with `cf-ray`/cloudflare headers all as "Cloudflare block" and immediately escalated through the full tiered ladder (cffi retry → Wayback → Scrape.do static → Scrape.do render). For Kingston, pages past ~11 in the BFS trip Cloudflare's plain rate limiter (429, not a challenge) — cffi normally works fine here — but every 429 still paid the latency of Wayback + (if `SCRAPE_DO_TOKEN` set) Scrape.do round-trips, which cumulatively blew the 300s discovery budget across the ~35-page crawl.
- **Also found**: the per-university `discovery.use_wayback: false` flag (which Kingston sets, documenting "archive.org has nothing useful here") was only wired into the orchestrator's separate discovery-wide Wayback CDX sweep — the per-request Wayback tier inside `fetch_html()`'s CF-block ladder ignored it completely.
- **Fix** (`backend-py/app/services/scraper/http_fetcher.py`): (1) a 429 now gets 2 short same-tier backoff retries (3s/8s) via plain httpx before falling through to the cffi/Wayback/Scrape.do ladder — since only waiting resolves a rate limit, not switching transport; (2) the per-request Wayback tier is now skipped when the active `UniConfig.discovery.use_wayback` is explicitly `False`. Tier-4/5 Scrape.do escalation was deliberately left untouched (Cardiff/Westminster/QMUL rely on it; broader gating would need a full regression sweep).
- **Tests**: `backend-py/tests/test_kingston_rate_limit_retry.py` (4 tests) — 429 backoff-then-cffi-fallback, and Wayback-tier skip/no-skip. Full `http_fetcher`-adjacent regression suite (Cardiff, QMUL, discovery, sitemap, browser toggle) re-verified green.

### QMUL "missing courses" review-visibility fix (2026-07-06)

Reported as "122 staged, 59 rejected, rest missing out of 409" — not a data-loss bug. `total_found=409`, `imported=350` (staged), `skipped=59` on the actual completed job all summed correctly; the missing-looking 228 courses were legitimately staged `scraped_courses` rows sitting under **two earlier job_ids** from runs that were interrupted (by Celery worker restarts during the Kingston fix deploy) and then resumed under a new job_id per the task229 resume checkpoint (`_clear_stale_dedup` in `orchestrator.py` preserves a recently-failed/stopped job's pending rows so the next run skips already-processed URLs instead of re-scraping them).

- **Root cause**: `GET /api/scrape/staged/{job_id}` (`backend-py/app/routers/scrape.py`) filtered strictly by `scrape_job_id == job_id`, so the review UI only ever showed the *latest* run's slice of pending courses. Rows staged under a stale, resumed-from job_id were still `status='pending'` in the DB (fully reviewable/approvable) but invisible in the normal review screen.
- **Fix**: the endpoint now resolves the job's `university_id` and returns all `status='pending'` `scraped_courses` for that university (falls back to the old job_id-only filter if the job_id doesn't resolve to a row). This makes the review queue show every currently-pending course for a university regardless of which run in a resume chain staged it.
- **Test**: `backend-py/tests/test_staged_resume_chain_visibility.py` — two `scrape_runtime_jobs` rows (one `failed`, one `completed`) for the same university, each with its own pending `scraped_courses` row; asserts `/staged/{new_job_id}` returns both.
- **Not changed**: the runs-history list's per-job `stagedCount` (`GET /api/scrape/runs`) still reports per-job_id counts — that's a historical/audit view and is correct as-is; only the reviewer-facing staged-courses list needed to aggregate across the resume chain.

### UK fee-table "no international row" false-fee fix (2026-07-06)

Canvas-reported bug: "HNC Building Studies should be rejected because it is part time only" plus a general requirement that UK fee tables select the International + Full-time row only. Concrete example course belongs to University of Wolverhampton (uni_id=1761), not QMUL as first assumed — verified by live scrape.do fetch of the course page, which has only Home/Part-time fee rows, no International row at all.

- **Root cause**: `fee.py`'s row-selection logic already correctly detected "no International + Full-time row exists" but returned a bare `[]` on that path, so the signal was lost. `single_course.py`'s `degree_level_defaults` fee fallback (a legitimate mechanism — see `wlv.yaml`, most WLV courses have no fee data in Solr) then filled in the university's flat-rate default fee whenever fee was null, unable to distinguish "unknown fee" from "confirmed no international offering."
- **Fix**: (1) `fee.py` `extract()` now returns an explicit `fee_table_confirmed_no_international=True` sentinel instead of `[]`; (2) `single_course.py` skips the `degree_level_defaults` fallback when this flag is set; (3) `guards.py` `should_stage_course` checks this flag first, before all other escape hatches (central-fee-page, degree_level_defaults, skip_per_course_browser), and force-rejects with reason `no_international_fee`. The `degree_level_defaults` flat-rate fallback itself is unchanged and still fires for genuinely-unknown fees.
- **Verified**: live scrape.do fetch of the HNC Building Studies page confirms the sentinel now fires correctly, and `should_stage_course` rejects the course end-to-end. QMUL has no `fees.degree_level_defaults` configured, so this specific fallback-masking bug did not apply there — the shared row-selection fix still benefits all UK universities with structured fee tables.
- **Tests**: `tests/test_fee.py` (rewritten for the new sentinel) and a new `TestFeeTableConfirmedNoInternational` class in `tests/test_guards.py` (3 tests — reject despite `degree_level_defaults`, reject despite `has_central_fee_page`, sanity check that normal missing-fee courses still use escape hatches). All pass; one pre-existing unrelated failure (`test_missing_ielts_is_warning` in `test_data_quality.py`) confirmed out of scope.

### Fetch-layer brief (2026-07-09, dev)

- **A3 fetch-error registry**: `http_fetcher.py` records the final failure (status/tier/detail) per URL in a bounded dict; discovery log lines append `format_fetch_error(url)` so "fetch failed" is never opaque.
- **B account-wide Scrape.do semaphore** (`scrape_do_semaphore.py`): Redis zset caps fleet-wide in-flight Scrape.do calls across all 8 prefork workers. Opt-in via `SCRAPEDO_MAX_CONCURRENCY` (default 0 = disabled); fail-open on any Redis error; stale slots reaped after 180 s; gauge log `[SCRAPEDO GAUGE] in-flight N/cap`. Local semaphore is acquired first, account slot nested inside.
- **C1 7-day discovery URL cache** (`discovery_url_cache` table, migration 046 — applied in dev, run `scripts/apply_migration_046.py` on prod): re-scrapes within 7 days skip the whole discovery phase. Bypass with `forceDiscovery: true` in the start-scrape body. Only healthy runs (≥5 course links, fetch-fail <30%) write the cache; SearchStax unis excluded; BFS-blocked fee URLs persisted alongside (marked `fee_page: true`).
- **C4 per-phase DONE timing**: DONE line now ends with `Discovery:Xs | Extraction:Xs | Sweep:Xs | Staging:Xs`.
- Tests: `tests/test_fetch_layer_brief.py` (30 tests). C2/C3 of the brief deferred.

### Data Model

The database schema includes tables for `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`, `scraped_courses` (staging), and `import_jobs`.

### Deployment Architecture

- **Production Server**: AWS EC2 instance `i-03547b132b6aa4ffb` (`university-portal-production`), managed through AWS Systems Manager Session Manager / Run Command. Direct SSH from Replit may be blocked; use SSM instead of the retired DigitalOcean host.
- **Process Management**: systemd. Services: `uni-api-py.service` (Gunicorn/FastAPI on port 8000) and `uni-celery.service` (Celery scrape worker). Nginx proxies `/api` → `127.0.0.1:8000`.
- **Git repo on server**: `/opt/university-portal`, owned by `ubuntu`. Deploy as the repo owner with `git -c safe.directory=/opt/university-portal pull --ff-only origin main`, then run `systemctl restart uni-api-py.service uni-celery.service`.
- **IMPORTANT — university_id values differ between prod and dev**: Torrens = `id=3` on prod, `id=5` in dev. Always check `SELECT id, name FROM universities WHERE name ILIKE '%torrens%'` on prod before running university-specific SQL. Do NOT assume dev IDs match prod.
- **Database and environment**: Production connection settings are supplied by `/opt/university-portal/backend-py/.release.env`. Source that file inside the remote shell before running application scripts, never print its values, and use `app.database.AsyncSessionLocal` for production queries. The AWS host does not have the retired server's local `postgres` OS account.
- **Logs**: Use `journalctl -u uni-api-py.service` and `journalctl -u uni-celery.service` through SSM, plus database job state for scrape progress.

## Live Course Data State

The 2026-05-01 verified-counts snapshot (per-university live-course table) has been archived to `backend-py/docs/history/replit_md_archive_2026-07.md` since it is a stale point-in-time count, not durable reference. The mechanisms below (auto-publish gate, promotion scripts) remain current.

### Auto-publish gate

Hard floor: **85% completeness** (`_PHASE_A_MIN_COMPLETENESS` in `backend-py/app/services/auto_publish.py`).
13 review fields: course_name, degree_level, category, study_mode, course_location, duration,
intake_months, international_fee, description, academic_level, academic_score, english_test, other_requirement.
Force-promote path: `UPDATE scraped_courses SET auto_publish_status='ready' WHERE university_id=N AND status='pending' AND auto_publish_status IN ('review','pending_review');`
then run `bulk_approve.py`.

### bulk_approve.py (prod-side script)

Location on prod: `~/University-and-Course-data/backend-py/scripts/bulk_approve.py`
Usage: `PYTHONPATH=. venv/bin/python3 scripts/bulk_approve.py --university-id <N> [--status pending] [--ap-status ready] [--dry-run]`
Idempotent: deduplicates on `lower(course_name)` per university. Updates existing row if name matches.

### GitHub push reminder

GitHub repo: https://github.com/Studyinfocentre/University-and-course-managment (always push here).
Push command: `git push "https://${STUDYINFO_GITHUB_PAT}@github.com/Studyinfocentre/University-and-course-managment.git" main`
The Replit `github` remote URL may be stale — always use the push command above directly. Prod remote is `origin`. Always confirm prod is running the latest commit (`git log -1`) before assuming a Replit-side fix is live.

### Historical per-university fixes archived

Old dated per-university bug-fix write-ups (Week 3–5 engineering sessions, the 2026-05-01 "Live Course Data State" snapshot — CSU review rows, bond_enrich.py notes, the historical `/api/courses` 500 fix, promotion targets, next-session housekeeping — plus the Macquarie/QUT/UniSQ/UOW/sibling-cache dated fix log entries and the 2026-05-25 Macquarie coursehandbook pivot) have been moved to `backend-py/docs/history/replit_md_archive_2026-07.md`. The code is unchanged and still in force — only the documentation moved.

- **2026-05-12 / 2026-05-13 fixes**: Curtin year-1-vs-total fee, ECU "not currently offered" domestic-only, La Trobe space-thousands-separator fee, CQU broken-CMS retry diagnostic logging, location extractor country-name stripping, Federation "Federation University Online" location, La Trobe per-course browser fee extraction, and Curtin rolling-enrolment intake fallback. The code is unchanged and still in force. (Note: the `backend-py/docs/history/replit_md_archive_2026-05.md` file this entry originally pointed to was never actually created, so that write-up's original text is not recoverable — only this summary line remains.)

## External Dependencies

- **AI/ML**: Gemini API (`GEMINI_API_KEY`) for AI-powered web scraping and data extraction. Uses `gemini-2.5-flash`, `gemini-2.0-flash-001`, and `gemini-2.0-flash-lite-001` with auto-fallback.
- **Web Scraping**: Playwright for browser automation in the Python backend.
- **Message Queue**: Redis for Celery as a broker and result backend.
- **Web Server**: Nginx for serving the frontend and proxying API requests in production.
- **Database**: PostgreSQL.
- **Cloud Provider**: DigitalOcean for production hosting.

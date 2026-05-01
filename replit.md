# University Course, Fee, Intake & Requirement Management System

## Overview

This project provides a centralized administrative portal for universities to manage their course-related data. It enables comprehensive management of courses, fees, intakes, scholarships, and admission requirements. Key capabilities include AI-powered web scraping for data acquisition, bulk upload/download functionalities, and change detection mechanisms. The system aims to streamline data management for educational institutions, offering a robust solution for maintaining up-to-date and accurate course information.

## User Preferences

- Provide commands in the format `cd /root/University-and-Course-data && <command>`.
- Always provide commands in the specified format, especially for production deployment and verification.
- When schema changes are needed, explicitly provide the `pnpm --filter @workspace/db push --force` command before builds.
- The Node.js API server has been deleted. Python FastAPI is now the sole API server in both dev and production.
- Provide verification commands to confirm commit deployment, new bundle serving, and correct PM2 environment variables.

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

### Week 2 ACAP migration — correct order (reviewer-mandated)

Do NOT fix the NameError first. Order matters because step 4 is a shared-code change:

1. **Shadow-mode scaffolding** — run old + new code paths in parallel for ACAP. Both should produce the same broken result (`Errors:14`). This validates the diff machinery itself.
2. **Move `domestic_only` to YAML** — migrate `domestic_only.text_must_appear_in: main_content` from shared if-block into `acap_41.yaml`. Shadow mode for 5 runs → byte-identical → cut over. Now the filter is per-uni-configurable.
3. **Fix the `re` NameError last** — it's a shared-code change that affects every uni. Run the full regression sweep (all 23 baselined unis). Diff against `20260430_024437_*` baseline. Zero regressions → merge.

Rationale: if the NameError fix sweep finds regressions on unexpected unis, that means the `re.*` call was doing something other unis depend on — far better to discover that through the sweep than through bug reports.

### Data Model

The database schema includes tables for `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`, `scraped_courses` (staging), and `import_jobs`.

### Deployment Architecture

- **Production Server**: DigitalOcean droplet at `159.65.152.72`, Ubuntu 24.04.
- **Process Management**: systemd. Services: `uni-api-py.service` (FastAPI/uvicorn, port 8000) and `uni-celery.service` (Celery worker). Nginx proxies `/api` → `127.0.0.1:8000`.
- **Git repo on server**: `/root/University-and-Course-data`. Deploy = `git pull origin main` + `systemctl restart uni-api-py uni-celery`.
- **Database**: Local PostgreSQL. Database: `university_portal`, owner: `uniportal`. Access via `sudo -u postgres psql -d university_portal`. Schema changes via direct psql (alembic cannot be used on production — asyncpg fails to connect via TCP to `localhost` due to SSL hostname DNS issue).
- **CRITICAL — DB URL**: Must use `127.0.0.1` not `localhost` in the asyncpg connection string. asyncpg attempts SSL hostname verification using `getaddrinfo("localhost")` which fails on this server (`[Errno -3] Temporary failure in name resolution`). Using the IP literal bypasses the DNS lookup.  Hardcoded default in `backend-py/app/config.py` is already set to `127.0.0.1`.
- **alembic**: Do NOT run `alembic upgrade head` on production — it will fail with the same DNS error. Apply all schema changes via `sudo -u postgres psql -d university_portal -c "ALTER TABLE ..."` directly.
- **alembic_version table**: Contains fake version IDs (`001_initial` … `006_add_scrape_warnings`) inserted manually. The actual migration filenames are `001_add_rejection_reason`, `002_add_extraction_method`, etc. — these do NOT match. Ignore alembic version tracking on production entirely.
- **Environment Management**: DB credentials hardcoded in `app/config.py` default. No `.env` file needed on production.
- **journalctl**: The service does NOT log uvicorn application output to journalctl — only systemd lifecycle events appear. To see application errors, check `/tmp/dashboard_stats_error.log` (written by the try/except in dashboard.py) or run uvicorn in the foreground temporarily.

## Live Course Data State (verified 2026-05-01)

### Verified live counts — 623 total (as of 2026-05-01 session end)

| University | id | Live courses | Notes |
|---|---|---|---|
| USQ | 1 | 67 | stable |
| ASA | 2 | 8 | stable |
| CSU | 4 | 184 | 8 dups deleted; 11 review rows pending (see below) |
| UTas | 5 | 1 | |
| VIT | 6 | 38 | fresh scrape promoted; 6 old prefix-code dups deleted |
| KBS | 8 | 29 | |
| Flinders | 12 | 99 | promoted 2026-05-01; 6 campus-dups correctly merged |
| SCU | 17 | 1 | |
| AUT | 20 | 70 | |
| Bond | 22 | 126 | promoted 2026-05-01; enriched via bond_enrich.py |
| **Total** | | **623** | up from 267 at session start (2.3×) |

### Outstanding CSU review rows (11)

11 `pending/review` CSU scraped_courses rows blocked by Phase A completeness floor (< 85%).
- 4 also missing `international_fee`
- All missing `category`, `academic_level`, `academic_score`, `other_requirement`
- Leave for next CSU targeted re-scrape — do NOT force-promote (genuinely incomplete)
- Courses include: Associate Degree in Policing Practice, Bachelor of Education (Primary),
  Bachelor of Education (Secondary) - Pdhpe, Graduate Certificate/Diploma of Theological Studies,
  Bachelor of Theology, Bachelor of Oral Health, Bachelor of Paramedicine,
  Graduate Diploma of Ageing and Pastoral Studies, Graduate Certificate in Fish Conservation

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

**Known git anomaly (2026-05-01):** The file was present in the git index but missing from the
prod working tree — caused by an earlier `git reset --hard` + cherry-pick sequence that left the
`scripts/` directory partially emptied. Recovery: `git checkout HEAD -- backend-py/scripts/`
If the file is missing again mid-session, run this immediately before retrying `bulk_approve.py`.

### bond_enrich.py (Bond-specific enrichment)

Location: `backend-py/scripts/bond_enrich.py`
Usage: `PYTHONPATH=. venv/bin/python3 scripts/bond_enrich.py [--workers 8] [--dry-run]`
- Calls Bond JSON APIs (`/api/program-details/{id}`, `/api/program-fees/{id}/{code}`) and
  parses `/entry_requirements` HTML for IELTS — no Playwright, no Gemini.
- Runs in ~23 seconds (8 workers). Enriched 117/126 rows; fee=72, ielts=113.
- Does NOT set `extraction_method` — NULL source in spot-checks is expected and correct.
- 2 known non-enrichable URLs: `bachelor-of-communication` and
  `master-occupational-therapy/master-occupational-therapy-prerequisites` (no data-program-detail-url).

### Pipeline code gap (IMPORTANT)

Pipeline fixes made in Replit (stage_course.py VIT specialization fix, single_course.py
CENTRAL_ENGLISH_OVERRIDABLE guard, central_pages.py diploma fix) are NOT on GitHub/prod.
Prod runs the old pipeline code. Any future scrape on prod will use unfixed extractors.
Resolution: push Replit commits to GitHub via Replit's GitHub integration, then `git pull` on prod.
The Replit remote is named `github` (not `origin`). Prod remote is `origin`.

### Next promotion targets (priority order)

- **ACU (uni_id=51)**: ~99 courses in snapshot — check `SELECT status, auto_publish_status, COUNT(*) FROM scraped_courses WHERE university_id=51 GROUP BY status, auto_publish_status;` to determine if bulk_approve ready or needs rescrape
- **apicollege**: ~30 staged
- **ait**: ~37 staged
- **Study (weird)**: 694 staged but 0 live — investigate before touching

### Next session housekeeping (do first, ~15 min)

1. **bulk_approve.py audit**: `git log --oneline --diff-filter=D --all -- backend-py/scripts/bulk_approve.py` — check if ever deleted in git history
2. **Untracked file cleanup**: `git clean -nfd` (preview only), then `git clean -fd` if all are scratch files (check `backend-py/baselines/` first — may have baseline JSONs to keep)
3. **pg_dump backup**: still pending from this session
4. **Bond IELTS spot-check**: open 5 random `course_website` URLs from Bond rows to verify IELTS against bond.edu.au (expected: IELTS 6.5 default for undergrad, 7.0 for Law/health)

## External Dependencies

- **AI/ML**: Gemini API (`GEMINI_API_KEY`) for AI-powered web scraping and data extraction. Uses `gemini-2.5-flash`, `gemini-2.0-flash-001`, and `gemini-2.0-flash-lite-001` with auto-fallback.
- **Web Scraping**: Playwright for browser automation in the Python backend.
- **Message Queue**: Redis for Celery as a broker and result backend.
- **Web Server**: Nginx for serving the frontend and proxying API requests in production.
- **Database**: PostgreSQL.
- **Cloud Provider**: DigitalOcean for production hosting.
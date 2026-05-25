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
- **Week 3 — Cost optimisation + CRICOS matching (closeout 2026-05-09)**:
  - **Track A — Gemini cost** (P1A–P5A): all five prompts already shipped via the Priority-6 system documented above (`gemini_call_log` table, `gemini_gate.py` skip rule, `gemini_client.py` circuit breaker, `cost_ceiling.py`, four SQL views, `check_gemini_model.py` audit). No new code required for Track A — it pre-dates the spec.
  - **Track B — CRICOS matching** (P1B–P5B): extractor (`extractors/cricos_code.py`), PDF pipeline integration (`pipelines/university_pdfs.py` returns `"cricos_match"` suffix), authority tier `2.5` for `uni_pdf:cricos_match:fees` / `uni_pdf:cricos_match:requirements`, and existing tests are all in place (`test_cricos_extraction.py`, `test_university_pdfs.py`, `test_gemini_gate.py` — 75 pass). The spec calls for a separate `pdf_course_extracts` staging table; the existing implementation writes provenance directly into `scraped_field_evidence` rows with the `cricos_match` method, which is functionally equivalent and avoids data duplication.
  - **P5B verification**: migration 018 adds `v_cricos_coverage_au` view (per-AU-uni: total_staged, has_cricos, cricos_coverage_pct, enriched_via_pdf). Apply on prod: `cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_018.py`.
  - **Live coverage gap (open issue, not a code gap)**: as of 2026-05-09, dev `v_cricos_coverage_au` shows 0% for CSU / UOW / VIT (3 AU unis with recently-staged courses). The extractor is wired and tested; the issue is that those universities' course pages don't expose CRICOS in the patterns the regex matches (or the courses scraped lack CRICOS at the source). Diagnostic script: `backend-py/scripts/cricos_coverage_diagnostic.py [--uni-id N]` — counts pages where the literal token "CRICOS" appears vs pages where the extractor matches it. Backlog item for Week 5 production scale-up.
- **Week 4 — Production scale-up prep (top 10 AU unis, prep shipped 2026-05-09)**: Week 4 is operational, not engineering — the actual scrapes / spot-checks / approvals run on the prod droplet. This commit ships the prep pack so the on-prod work has zero engineering friction:
  - **Pre-flight gate runner**: `backend-py/scripts/week4_preflight.sh` — runs the 4 gates from Prompt 1 with column names corrected to match this codebase (`scrape_run_alerts.rule_id`/`created_at`, not `rule_type`/`fired_at`; `scrape_runtime_jobs.imported`/`total_found`/`total_gemini_cost_usd`, not `staged`/`discovered`/per-row `avg_cost_per_course`). Also lists which top-10 YAMLs exist. Run on prod: `cd /root/University-and-Course-data && bash backend-py/scripts/week4_preflight.sh`.
  - **Per-uni protocol queries**: `backend-py/scripts/week4_per_uni_protocol.sql` — Step-2 (alerts), Step-3 (job stats), Step-4 (random spot-check picker). Bind via `psql -v run_id="'<id>'" -v uni_id=42 -f ...`.
  - **Cost projection**: `backend-py/scripts/week4_cost_projection.sql` — Prompt 6 cost queries (per-uni cost, 80-uni projection, outliers, suspiciously low unis). Run after each scrape day.
  - **Scale-up log**: `backend-py/docs/week4_scale_up_log.md` — top-10 status table + per-uni run template (alerts / spot-checks / decision / YAML changes).
  - **Patterns doc**: `backend-py/docs/uni_onboarding_patterns.md` — empty skeleton with sections for site platforms, common gotchas, reusable YAML templates, and per-uni status. Update after every 2 unis processed (Prompt 5).
  - **Stub per-uni YAMLs**: `monash.yaml`, `unimelb.yaml`, `usyd.yaml`, `unsw.yaml`, `uq.yaml`, `rmit.yaml`, `deakin.yaml`, `uts.yaml`, `anu.yaml`, `uwa.yaml` — minimal `discovery: {} / extraction.filters.domestic_only.enabled: false` with hostname comments and a small number of educated initial overrides (`always_sitemap_supplement: true` for UNSW/RMIT/Deakin which are JS-heavy, `fallback_subdomains: ['handbook.{domain}']` for UWA, `fallback_subdomains: ['programsandcourses.{domain}']` for ANU). All overrides are conservative and intended to be tightened after the first scrape's spot-check results.
  - **Suggested order**: Group A (low risk, server-rendered HTML) — ANU, UWA, UQ, USyd. Group B (medium, custom CMS / Cloudflare) — Monash, Melbourne, UNSW, UTS. Group C (heavy JS) — RMIT, Deakin. Final order is set after one-page browser spot-check per uni at start of Week 4.
  - **What this prep does NOT do**: trigger any scrape, change any extractor, or push anything to prod. Triggering scrapes, manual browser spot-checks, and approval-to-prod are operator tasks per spec.
- **Week 5 — Scale-up engineering pack (next-20 prep + promotion-gap fix, shipped 2026-05-09)**: Week 5 has two parts in spec — operational scale-up (Prompts 3-5, run on prod) and engineering deliverables (Prompts 1, 5-fix, 6, 7). This commit ships the engineering portion; the operational scale-up runs on the prod droplet.
  - **Promotion-gap root-cause fix (Prompt 5)**: investigated the spec's "Charles Sturt 92-course promotion gap". Two contributing bugs found and fixed:
    - `app/services/scraper/approve_course.py`: `func.lower(Course.name) == sc.course_name.lower()` crashed with `AttributeError: 'NoneType' object has no attribute 'lower'` when course_name was NULL — *after* opening the SQLAlchemy transaction but *before* commit, leaving the session poisoned. Now raises a clear `ValueError` before opening the transaction.
    - `scripts/bulk_approve.py`: per-row exception handler did NOT call `db.rollback()`. So one bad row poisoned the session and made every subsequent row in the batch fail with "transaction has been rolled back" — exactly matching the "92 missing rows" pattern. Now rolls back per-row.
  - **CSU promotion diagnostic** (`scripts/csu_promotion_diagnostic.py`): identifies `scraped_courses` rows with `status='approved'` but `course_id IS NULL`. Run on prod: `cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/csu_promotion_diagnostic.py [--university-id 4]`.
  - **Pre-flight gate runner** (`scripts/week5_preflight.sh`): 4 gates from Prompt 1 with corrected schema (alert table is `scrape_run_alerts` not `scrape_alerts`; uses `acknowledged` not `resolved_at`; YAMLs live under `scraper_config/unis/` not `scraper/unis/`; universities has no `slug` column).
  - **Fleet diagnostics SQL** (`scripts/week5_fleet_diagnostics.sql`): Prompt 6 four diagnostics — fill-rate distribution, method distribution shift, AI-fallback overuse red-flag (>20% of fleet), cost outliers (>5x median), sibling-cache health. Spec assumed a denormalised wide-row `scrape_run_metrics` (`fill_rate_international_fee` etc); rewritten against the actual per-(uni, field, method) tall ledger.
  - **Architecture doc** (`docs/architecture.md`): captures Sprint 1 deliverables grouped by layer (data correctness, observability, cost optimisation, CRICOS, per-uni YAML, promotion safety) plus 7 architecture invariants.
  - **Sprint 2 backlog** (`docs/sprint2_backlog.md`): 9 candidate items with effort + impact + decision criteria, plus retrospective process changes (mandatory verification SQL in PR, mandatory regression sweep on shared-code changes, mandatory architecture-doc updates per feature).
  - **Stub per-uni YAMLs (next 20)**: 11 new stubs added — macquarie, curtin (with `study.{domain}` fallback), griffith, qut, westernsydney, adelaide, newcastle, murdoch, unisq, federation, scu. The other 9 (latrobe, flinders, jcu, ecu, cdu, acu, csu, bond, uow) already existed.
  - **What this commit does NOT do**: trigger any prod scrape, change any extractor, or cherry-pick rows for re-promotion. The bug fixes in approve_course.py and bulk_approve.py are defensive but the actual prod re-promotion (re-running bulk_approve.py for CSU after pulling the fix) is operator-driven.
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
- **Git repo on server**: `/root/University-and-Course-data`. Deploy = `git pull origin main` + `systemctl restart uni-api-py.service uni-celery.service`.
- **IMPORTANT — university_id values differ between prod and dev**: Torrens = `id=3` on prod, `id=5` in dev. Always check `SELECT id, name FROM universities WHERE name ILIKE '%torrens%'` on prod before running university-specific SQL. Do NOT assume dev IDs match prod.
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

### /api/courses 500 fix (COMPLETED)

Root cause: the prod `courses.py` was an older version missing two things:
1. `from decimal import Decimal` import
2. `def _f(v) -> float` helper function

asyncpg returns PostgreSQL `NUMERIC` columns as Python `Decimal` objects, which are
not JSON-serializable. The fix (applied directly to prod via Python patch scripts):
- Added `from decimal import Decimal` import
- Added `def _f(v): return float(v) if isinstance(v, Decimal) else v`
- Wrapped all english_requirements band scores with `_f()` (listening/speaking/writing/reading/overall for ielts/pte/toefl/other)
- Added safety final-pass that re-checks all dict values for stray Decimals before `out.append(d)`

**These changes are in the Replit dev repo (courses.py) but NOT yet on GitHub/prod via git.**
The prod file was patched manually. Next git push will include the correct version.

### Next session housekeeping (do first, ~15 min)

1. **bulk_approve.py audit**: `git log --oneline --diff-filter=D --all -- backend-py/scripts/bulk_approve.py` — check if ever deleted in git history
2. **Untracked file cleanup**: `git clean -nfd` (preview only), then `git clean -fd` if all are scratch files (check `backend-py/baselines/` first — may have baseline JSONs to keep)
3. **pg_dump backup**: still pending from this session
4. **Bond IELTS spot-check**: open 5 random `course_website` URLs from Bond rows to verify IELTS against bond.edu.au (expected: IELTS 6.5 default for undergrad, 7.0 for Law/health)

- **2026-05-18 — Macquarie (uni 277) discovery + junk-page filter**: User-reported scrape log showed two compounding bugs. (1) WRONG YAML: DB row `id=277, name="mq"` → slug `(uni_name or "").lower().replace(" ","")` resolves to `"mq"`, so the loader read `unis/mq.yaml` — which had been auto-generated on 2026-05-18 04:41 UTC as a clone of `acap.yaml` (ACAP's domestic_only filter, ACAP's `trust_vision_ocr: false`, ACAP's `^^Available in Perth` strip patterns) — none of which applies to Macquarie. The parallel `unis/macquarie.yaml` file was never loaded because the slug never resolves to "macquarie". (2) CLOUDFLARE-BLOCKED DISCOVERY: `https://www.mq.edu.au/` returns `HTTP 403` with `cf-mitigated: challenge` to plain HTTP (verified 2026-05-18 via curl), so BFS yielded 0 candidates, the browser fallback wandered 14 nav links from the homepage and harvested only 6 nav-pages-as-courses, four of which were category landings ("Undergraduate", "Browse all degrees", "View degrees", "Combined Bachelor Master Degrees") that then staged as junk courses. Fixes: (a) Rewrote `backend-py/scraper_config/unis/mq.yaml` as the canonical MQ config with `discovery.always_browser_discover: true` (bypasses dead HTTP path) and `discovery.block_url_patterns` for `/find-a-course/courses/major/`, `/find-a-course/courses/specialisation/`, and bare listing roots (`/study/find-a-course/?$`, `/study/find-a-course/undergraduate/?$`, etc.). (b) Deleted the orphan `unis/macquarie.yaml` (never loaded). (c) Added `mq.edu.au` / `www.mq.edu.au` entries to `_HOST_EXTRA_SEEDS` in `backend-py/app/services/scraper/browser_discover_generic.py` pointing at `/study/find-a-course`, `/study/find-a-course/undergraduate`, `/study/find-a-course/postgraduate` so the browser pass enumerates the full UG+PG A-Z indexes (~300 courses). (d) Extended `_BLOCK_URL_LAST_SEGMENTS` in `backend-py/app/services/scraper/guards.py` with bare `"undergraduate"` and `"postgraduate"` (safe globally — real course URLs always have the degree-name slug as the last segment, never the bare word), plus `"combined-bachelor-master-degrees"`, `"double-degree-builder"`, `"browse-all-degrees"`, `"view-degrees"`, `"view-all-degrees"`, `"all-degrees"`, `"all-courses"`, `"find-a-course"`. (e) Extended `_BLOCK_TITLE_EXACT` with `"browse all degrees"`, `"view degrees"`, `"view all degrees"`, `"view all courses"`, `"all degrees"`, `"all courses"`, `"combined bachelor master degrees"`, `"combined bachelor/master degrees"`, `"double degree builder"`, `"find a course"` (full-string equals only, so real award titles like "Undergraduate Certificate of Psychology Fundamentals" and "Postgraduate Diploma of Counselling" are NEVER blocked). 19 new tests in `tests/test_macquarie_discovery_and_junk_filter.py` (all pass) including 6 false-positive fences asserting real MQ undergraduate course pages, combined-degree slugs, and award titles starting with "Undergraduate"/"Postgraduate" must NOT block. 191 tests pass across macquarie + curtin + guards + qut + not-accepting + uon regression suites. Both FastAPI and Celery worker restarted (deploy invariant for extractor/guard/seed changes).

- **2026-05-25 — Macquarie coursehandbook → admissions URL pivot (empty-extraction fix)**: After the 2026-05-18 coursehandbook sitemap discovery shipped (200-354 URLs harvested cleanly), the user's next scrape log showed every course staging with fee/IELTS/duration/intake BLANK and `[GP-DEBUG] static=216714B rendered=0B using=static text_len=77` for every coursehandbook.mq.edu.au URL — the sparse-static rescue fired, the per-course browser pass ran (`[per-course browser ↻]`), but the result was `[per-course browser ✓] … filled=[]`. Root cause (verified live 2026-05-25 via stealth probe): **coursehandbook.mq.edu.au is the ACADEMIC catalogue, not the admissions site** — pages contain course name, description, learning outcomes, credit points, but ZERO fee / IELTS / session / campus / study-mode data. The admissions data lives at `https://www.mq.edu.au/study/find-a-course/courses/<slug>` (9/10 sample courses returned 200 with "Estimated annual fee AUD $15,500", "Session 1 (23 February 2026)", "North Ryde" campus, "International student" toggle). Fix: (a) added `_resolve_to_study_urls()` in `backend-py/app/services/scraper/mq_browser_discover.py` — after the coursehandbook sitemap pass returns N C-code URLs, opens a single `stealth_context()` and renders each URL just long enough to read its `<title>` tag (present in the SPA shell static HTML, no body-wait needed), then slugifies the name via `_slugify_course_name()` and constructs the equivalent `www.mq.edu.au/study/find-a-course/courses/<slug>` admissions URL. Runs 6-parallel via an `asyncio.Queue` of workers sharing one context (keeps the patchright + xvfb session alive across all gotos). Skips titles that are empty / "Handbook" site-nav fallback / unparseable. Dedupes on admissions URL (multi-year handbook entries collapse to ONE admissions URL). 354-URL resolve takes ~2-3 min wall-time. (b) Added `mq.edu.au` to `_FORCE_BROWSER_HOSTS` and `mq.edu.au` + `www.mq.edu.au` to `_EXTENDED_EXTRACT_HOSTS` in `backend-py/app/services/scraper/per_course_browser.py` so the per-course browser pass ALWAYS launches against MQ admissions URLs and runs the FULL extractor suite (fee + IELTS + intake + duration + location + study_mode) against the rendered DOM — not just `english_test` as on default hosts. mq.yaml already had `discovery.use_stealth_browser: true` so `browser_pool.fetch_html` routes through patchright + Xvfb (CF-protected hosts). 28 new tests in `tests/test_mq_coursehandbook_to_admissions.py` (all pass) covering: 9 canonical slug examples from real admissions URLs (`bachelor-of-arts`, `master-of-business-administration`, etc.), suffix-strip variants (`| Macquarie University`, `- Macquarie University`, em-dash), special chars (parens / ampersand / apostrophe / comma → hyphen), no-double-hyphens structural pin, end-to-end resolver mapping 3 handbook URLs → 3 admissions URLs, `"Handbook"` site-nav title skip (regression for would-emit `/courses/handbook` 404), missing-title skip, multi-year dedup, host-gating pins (`mq.edu.au in _FORCE_BROWSER_HOSTS`, `mq.edu.au + www.mq.edu.au in _EXTENDED_EXTRACT_HOSTS`, `_force_browser_for_url` suffix-matches `www.mq.edu.au`). 17 prior coursehandbook sitemap tests still pass. Both FastAPI and Celery worker restarted (deploy invariant).

- **2026-05-18 — QUT domestic-only filter wired (YAML gate flipped)**: User reported `graduate-certificate-in-business-public-sector-management` staging with international fee row despite the page banner "This course is only available for Australian and New Zealand students." (and a single Australia-flag icon, vs the two-icon audience toggle on international-eligible pages). Root cause: `_DOMESTIC_ONLY_RE` already covers the QUT phrasing — the alternative `this\s+(?:course|program|degree)\s+is\s+only\s+available\s+for\s+(?:australian|domestic)` was added in commit 1892941 — but `backend-py/scraper_config/unis/qut.yaml` carried `extraction.filters.domestic_only.enabled: false` from the original commit 309c2a4, so the gate `_domestic_only_filter_enabled()` short-circuited and `_is_domestic_only_page()` was never called on QUT HTML. Fix: flipped the YAML to `enabled: true` (no code change required). International-eligible QUT pages render a different banner on the default tab ("You are viewing Australian and New Zealand students' course information.") which deliberately does NOT match the regex (requires "this course is only available for …" with the course as the explicit subject) — verified against Bachelor of Architectural Design / Bachelor of Built Environment (Honours) (Landscape Architecture). 7 tests in `tests/test_qut_domestic_only_filter.py` pin both halves: YAML enabled, regex matches the domestic-only banner, regex does NOT match the audience-toggle banners (domestic-tab or international-tab variants). Symptom-of-the-bug receipt from the user's scrape log: `[FIELD SUMMARY] Graduate Certificate in Business (Public Sector Ma Fee: ✅ '2027'` — "2027" is the year picked off page chrome, not a dollar amount. After the fix the domestic-only early-exit at `single_course.py:1304` fires and the row is rejected with reason `"domestic_only"` before the regex/Gemini cascade runs.

- **2026-05-22 — UniSQ (uni 562) Gemini PRIMARY chrome-text leak into `course_location`**: User reported ~40% of UniSQ rows staging with `course_location="Accommodation UniSQ Events Contributing to our communities"` (the homepage-footer quick-links column) — verified live against Master of Laws, Bachelor of Laws (Hons), Diploma of Multidisciplinary Studies, Master of Nursing, Graduate Diploma of Counselling, etc. (~25 of 57 staged rows in the user-attached snapshot). Real campuses (Toowoomba/Ipswich/Springfield) staged correctly on the other ~60% of rows, proving the structural `_from_unisq_quickfacts` reader works when the per-degree page renders the quick-facts panel — but on off-shape pages it returns None and Gemini falls through to the page-wide text where it reads the footer column. Existing `_LOCATION_CHROME_RE` in `backend-py/app/services/scraper/pipelines/single_course.py` (lines 56-73) already covers all three UniSQ footer phrases (`accommodation`, `unisq events`, `contributing to our communit(?:y|ies)`) and `_is_location_chrome()` (line 113) requires ≥2 matches. The filter was wired in TWO places — Gemini PRIMARY's `location_text → course_location` mapping at line 2434 AND the AI FALLBACK loop at line 3696 (covering both `location_text` and `course_location`). **Bug**: when Gemini PRIMARY returned the field DIRECTLY as `course_location` (not via `location_text`), the per-key dispatch at line 2564 guarded only against (a) study-mode keywords and (b) structural-extractor ownership — the chrome check was missing. Fix: added Guard 3 at line ~2584 calling `_is_location_chrome(_gp_v)` and `continue`-ing when matched, mirroring the existing PRIMARY `location_text` guard at line 2434 and the FALLBACK course_location guard at line 3696. Surgical 3-line addition; no other code paths touched. 5 new tests in `tests/test_unisq_location_chrome_primary_guard.py` (verbatim user-reported string, lowercase variant, comma-separated Gemini-normalised variant, real-campus false-positive fences for Toowoomba/Ipswich/Springfield/Online/External, single-phrase-not-enough pin). 42/42 pass across all 3 UniSQ test files (primary_guard + chrome_filter + quickfacts). Both FastAPI and Celery worker restarted per deploy invariant. Per-uni YAML untouched — fix is the cross-uni chrome guard already proven to work for UTAS panel-headings.

- **2026-05-18 — Macquarie (uni 277) discovery round 3: faculty seeds + interactive filter fallback**: Round-1 (mq.yaml rewrite + browser_discover_generic seeds) and round-2 (dedicated `mq_browser_discover.py` Playwright module with 3 catalogue seeds + hydration wait on `a[href*='/undergraduate/'], a[href*='/postgraduate/']`) both shipped 0 staged courses on prod. Operator log showed the MQ module DID pass Cloudflare (Playwright reached the catalogue pages) but every seed yielded 0 course anchors after the passive scroll loop, while the generic-browser fallback wandering nav links serendipitously found 1 real course URL (`/study/find-a-course/undergraduate/employability-initiatives/cooperative-education-program-in-actuarial-studies`) — proving the site loads and real course URL shape is correct. Root cause: the 3 catalogue landing pages (`/study/find-a-course`, `/undergraduate`, `/postgraduate`) are **pure SPA search shells** — they render filter widgets and faculty cards on first paint, NOT course links. Course anchors only appear after the user clicks a filter chip (or navigates to a faculty subpage which IS plain HTML). Fix (3 changes in `backend-py/app/services/scraper/mq_browser_discover.py`): **(1)** Split `_SEED_URLS` into `_FACULTY_SEED_URLS` (Arts, Business, Medicine and Health Sciences, Science and Engineering at `/study/find-a-course/<faculty>`) + `_CATALOGUE_SEED_URLS` (the original 3 landing pages); faculty seeds are visited FIRST because they render plain HTML course links with no UI interaction. **(2)** Relaxed `_HYDRATE_WAIT_SELECTOR` from the narrow `a[href*='/undergraduate/'], a[href*='/postgraduate/']` form to the broad `a[href*='/study/find-a-course/']` so faculty pages don't time out waiting for a UG/PG-anchored selector that only appears on populated SPA result grids. **(3)** Added `_interactive_filter_harvest()` helper + 8-selector `_FILTER_CLICK_SELECTORS` tuple (Playwright `:has-text()` form covering Undergraduate / Postgraduate filter chips, "All courses" / "View all" links, "Search" / "Apply filters" buttons). Helper is invoked from the main loop ONLY when a catalogue seed yields 0 anchors after the passive scroll loop; per-selector clicks are wrapped in try/except so a missing button never aborts the sweep, and an early-stop heuristic exits as soon as any single click adds ≥5 new course URLs. Also added 0-anchor diagnostics (page title + raw `<a>` tag count emitted to scrape log) so the next iteration knows whether the page is a Cloudflare shell, a pre-hydration SPA, or genuinely empty without guessing. 8 new tests in `tests/test_mq_browser_discover.py`: faculty-seeds-before-catalogue-shells ordering pin, hydration selector breadth pin, filter-selector coverage pin, two `_interactive_filter_harvest` unit tests (no-button no-op + click-yields-5-links). 31 pass / 1 skipped (live smoke gated on `MQ_LIVE_TEST=1`). The interactive selectors are SPA-pattern guesses (real MQ filter labels not yet inspected from a non-Cloudflare-blocked network); if all 8 fail to match, the diagnostic logging will reveal the actual button shape so a 4th iteration can lock the right selector. Both FastAPI and Celery worker restarted (deploy invariant for discovery/extractor changes).

- **2026-05-18 — UOW Master courses missing (round 2: cap raised to 1000)**: User reported "all the master courses are missing" on UOW. Live verification of UOW pagination on 2026-05-18 confirmed the catalogue is fully alphabetical — Bachelor pages 1-29, Certificate III 30-31, Diploma 32-34, Doctor 35, Graduate Certificate 38-44, then Master 44-60+, then loops back to Bachelor of Arts at page 65. The 500-candidate cap shipped on 2026-05-17 was exhausted at page 40 (Graduate Certificate range), BEFORE reaching the M-prefix pages — verified against `scrape_runtime_logs` for `job_9fc23c976b81` which shows `[DISCOVER] Page 40/80: +19 candidates (total 500)` and no `master-of-*` URL ever entering the candidate stream. DB confirmation: `SELECT degree_level, COUNT(*) FROM scraped_courses WHERE university_id=13` returned only `Bachelor's=103, Graduate Certificate=3` — zero Masters, zero Doctors, zero Diplomas. Fix: raised `discovery.max_candidates` from 500 → 1000 in `backend-py/scraper_config/unis/uow.yaml`. The orchestrator (`backend-py/app/services/scraper/orchestrator.py:694-696`) honours the YAML override when the value exceeds the in-flight `max_courses`. Downstream dedup collapses major-filter duplicates so the staged-row count remains ~300. Test `tests/test_uow_max_candidates_cap.py::test_uow_max_candidates_covers_full_catalogue` updated to require `>= 800` (was `>= 350`), with module-level docstring tracking both 2026-05-17 (500) and 2026-05-18 (1000) waves so the next regression has the receipt.

- **2026-05-18 — UOW Bachelor of Social Science duration "12.0" → above-sanity-max AI rescue**: All BoSocSci variants (Sociology, Public Health, Human Services, Environment & Society, Criminology, base, double-Laws combinations) staged with `duration=NULL` despite the page showing "3 years full-time". Root cause: static-HTML duration regex extracts `12.0 Year` (likely a "12 subjects/sessions" panel artefact from the JS-stripped DOM), the sanity check at `single_course.py:~L4775` then nullifies it because 12 > 7.0 (`_SUSPICIOUS_MAX` bachelor ceiling), and Gemini's correct `duration_value=3, duration_unit="years"` is dropped on the floor by `_apply_ai_duration_mapping` (existing rescue branches only handle the LOW-side bachelor-floor breach). Fix: extended `_apply_ai_duration_mapping` in `backend-py/app/services/scraper/pipelines/single_course.py` with a symmetric `_ai_above_max_rescue` branch — when the regex value normalised to years exceeds the per-degree-level ceiling (7.0 bachelor/master/honours, 4.0 grad cert/dip, 12.0 else, same rules as the downstream sanity check) AND the AI returns a Year-shape value strictly inside `[0.25, ceiling]` AND inside the existing `_ai_plausible` band (1.0–10.0), the AI value overwrites the regex value. Host-agnostic by design (mirrors the existing B30 Torrens bachelor-floor rescue); never fires unless the regex value would be nullified anyway. 7 new tests in `tests/test_single_course_ai_mapping.py` (160 total tests pass across the AI-mapping / UOW / duration / guards suites). Code review approved.

- **2026-05-15 — sibling-cache English backfill globally disabled**: `_SIBLING_BACKFILL_SLOTS` in `backend-py/app/services/scraper/sibling_cache.py` is now `()`. IELTS — like PTE/TOEFL/CAE/Duolingo before it — is no longer propagated from one course to another, even within a same-bucket consensus. Trigger: Flinders Master of Science (Biology / Environmental Science) staged with `ielts_overall=6.0` inherited from a postgrad-bucket vote of 8 sibling courses, including the combined `bachelor-engineering-biomedical-honours-master-engineering-biomedical` page; user's stance: "if there is no IELTS leave blank, do not add from a sibling". `backfill_english_from_siblings` and `_build_bucket_cache` are unchanged structurally — they early-return on the empty slot tuple. Test pinning: `test_p4_only_high_precision_methods_seed_cache` now monkeypatches the slot list to keep gate-coverage; `test_p6_two_source_consensus_does_not_propagate_after_global_disable`, the T206 backfill assertion in `test_scraper_pipeline_parity.py`, and the postgrad assertion in `test_data_parity_priorities.py` were flipped to expect `fills == 0`. 130 sibling/PDF/metrics/parity/v2 tests pass.

- **2026-05-12 / 2026-05-13 fixes archived**: Curtin year-1-vs-total fee, ECU "not currently offered" domestic-only, La Trobe space-thousands-separator fee, CQU broken-CMS retry diagnostic logging, location extractor country-name stripping, Federation "Federation University Online" location, La Trobe per-course browser fee extraction, and Curtin rolling-enrolment intake fallback have all been moved to `backend-py/docs/history/replit_md_archive_2026-05.md` (anchors: `#curtin-year-1-vs-total`, `#ecu-not-currently-offered`, `#latrobe-space-separator`, `#cqu-broken-cms-retry-logging`, `#location-country-strip`, `#federation-online-brand`, `#latrobe-browser-fees`, `#curtin-rolling-intake`). The code is unchanged and still in force — only the documentation moved.
## External Dependencies

- **AI/ML**: Gemini API (`GEMINI_API_KEY`) for AI-powered web scraping and data extraction. Uses `gemini-2.5-flash`, `gemini-2.0-flash-001`, and `gemini-2.0-flash-lite-001` with auto-fallback.
- **Web Scraping**: Playwright for browser automation in the Python backend.
- **Message Queue**: Redis for Celery as a broker and result backend.
- **Web Server**: Nginx for serving the frontend and proxying API requests in production.
- **Database**: PostgreSQL.
<<<<<<< HEAD
- **Cloud Provider**: DigitalOcean for production hosting.
=======
- **Cloud Provider**: DigitalOcean for production hosting.

- **2026-05-25 — Macquarie (uni 277) coursehandbook sitemap discovery (real catalogue host)**: User-reported scrape log showed MQ harvesting 0-6 nav junk pages instead of real courses, despite the stealth (patchright+xvfb) wiring landing successfully. Root cause investigation found that the `www.mq.edu.au/study/find-a-course` Svelte SPA does NOT expose a course list — the search modal renders 12 category bubbles only, no degree-detail anchors, and no data XHR fires when typed into. The real catalogue is at a separate host: `coursehandbook.mq.edu.au` (Squiz-fronted handbook). Its `/sitemap.xml` index returns 14 child sitemaps with ~28K URLs across years 2020-2027 in three shapes: `/YYYY/courses/CXXXXXX` (real course detail pages — the target), `/YYYY/units/<CODE>` (subjects), `/YYYY/aos/NXXXXXX` (areas-of-study), `/YYYY/doubledegree/DXXXXXX` (combined degrees). Probed live: `https://coursehandbook.mq.edu.au/2026/courses/C000001` returns 200 with `<title>Bachelor of Biodiversity and Conservation</title>` + full overview/structure HTML. Fix: added `_discover_from_coursehandbook_sitemap()` in `backend-py/app/services/scraper/mq_browser_discover.py` as Tier-1 in `browser_discover_mq()` (runs BEFORE the existing widget sweep). Uses `stealth_context()` to bypass Cloudflare, fetches the sitemap index, walks child sitemaps, filters with `_COURSEHANDBOOK_COURSE_RE = ^https://coursehandbook\.mq\.edu\.au/(\d{4})/courses/C\d+/?$` and a rolling year window `{this_year, this_year+1}` so prior-year stale offerings don't leak through. Returns early when ≥20 URLs harvested (widget sweep skipped); otherwise falls through to the existing widget logic. Live smoke from Replit sandbox: **354 real course URLs harvested** (well above the existing `_DISCOVERY_FLOOR=150`). 17 new tests in `tests/test_mq_coursehandbook_sitemap.py` (16 unit + 1 live behind `MQ_LIVE_TEST=1`) pinning: regex contract (must match real /YYYY/courses/CXXXXXX, must reject /units/, /aos/, /doubledegree/, wrong host, http scheme, extra path segments, 2-digit year), year-filter contract (exactly current+next, 2 entries), sitemap index URL, early-return floor of 20, fall-through-to-widget when sitemap returns <20. 52 tests pass across coursehandbook + stealth + discovery-junk-filter suites. Both FastAPI and Celery worker restarted (deploy invariant). NOTE: the existing widget sweep (`_FACULTY_SEED_URLS`, `_CATALOGUE_SEED_URLS`, `_interactive_filter_harvest`) is preserved as the fallback path — it was empirically ineffective for MQ but remains as defense-in-depth in case coursehandbook ever goes down.

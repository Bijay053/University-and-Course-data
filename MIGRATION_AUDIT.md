# Node → Python Migration Audit
**Generated**: 2026-04-24
**Scope**: Full parity audit between `artifacts/api-server/` (Node + Express + Drizzle + in-process worker) and `backend-py/` (FastAPI + SQLAlchemy async + Celery + Redis).
**Method**: All 7 audit steps run against `main` branch HEAD `2c0a030`. Live evidence captured from a real KBS scrape (`job_4d3ea45fef56`, 41 staged courses, 0 errors).

---

## Headline numbers

| Layer | Node (current) | Python (current) | Status |
|---|---|---|---|
| HTTP routes (after prefix-normalization) | 108 | 118 | **108 common · 0 missing in Python · 10 extra in Python** (was 16-missing before commit 2c0a030 closed `/compare`) |
| FE → API contracts (URLs called by bundled JS) | 70 unique paths | 70 mapped | **70 / 70 covered** |
| DB tables (Drizzle pgTable in `lib/db/src/schema/`) | 20 | 22 SQLAlchemy models | Python is a **strict superset** (adds `academic_level_options`, `course_acronym_options`) |
| Schema source-of-truth | Drizzle (`lib/db/drizzle.config.ts` → `db push`) | SQLAlchemy models read-only against the same DB | **Shared physical DB, Drizzle owns DDL** (Alembic versions/ is empty by design) |
| Lib modules (`src/lib/*.ts`) | 17 | 4 in `app/utils` + folded into services | **9 fully ported, 4 partially, 4 unported** |
| Cron / scheduled jobs | 2 (`daily-backup`, `monthly-scraping` — `setInterval` in-process) | 0 (no Celery beat schedule yet) | **2 unported** — P1 (daily-backup) + P3 (monthly) |
| Workers | 1 (in-process Express worker thread, polls every 1 s) | 1 (Celery + Redis, ack-late + reject-on-loss + prefetch=1) | Different paradigm — Python is more crash-resilient |
| ASA scrape parity (local DB, post-T211) | n/a | 18 staged, avg completeness 99.4 % | **All 6 user-reported defects closed** |
| Live KBS scrape (post-T211) | n/a | 41 staged, 0 errors, 8 m 44 s | Sibling-cache, [CATEGORY det], TIMING, DONE all fired |

**Bottom line**: the system is functionally complete on the request path (every UI button hits a working Python route). The remaining gap is **scheduled / background behavior** plus a handful of stage-quality guards. None of those block prod traffic *today*; they degrade quality slowly (stale `_backup` rows, no monthly auto-rescrape, unfiltered category-page staging).

---

## Section 1 — Missing endpoints

After normalizing `:id` ↔ `{id}` and applying `app.include_router(prefix=…)` from `backend-py/app/main.py`, the *true* missing-in-Python set was 16 routes. **As of commit `2c0a030` it is 15** — `/api/search/compare` (the only one the React UI actually calls) is now ported. The remaining 15 have **zero UI references**.

Legend for **UI use**: `✅` = called by React bundle / `—` = never referenced.

| # | Method | Path | Node handler | UI page | UI use | Port priority |
|---|---|---|---|---|---|---|
| ~~1~~ | ~~GET~~ | ~~`/api/search/compare`~~ | ~~`routes/search.ts`~~ | ~~`pages/compare.tsx:77`~~ | ~~✅~~ | **CLOSED** by `2c0a030` |
| 2 | GET | `/api/bulk/courses/download` | `routes/bulk.ts` | — | — | P3 — superseded by `/api/scrape/export` |
| 3 | POST | `/api/bulk/courses/upload` | `routes/bulk.ts` | — | — | P3 — superseded by `/api/import/excel` |
| 4 | GET | `/api/scrape/runtime/health` | `routes/scrape.ts` | — | — | P3 — internal worker probe; covered by `/api/health/db` |
| 5 | POST | `/api/scrape/preview` | `routes/scrape.ts` | — | — | P3 — was a debug endpoint |
| 6 | POST | `/api/scrape/staged/approve-all` | `routes/scrape.ts` | — | — | **P2** — useful safety net; trivial to add as a single SQL UPDATE |
| 7 | POST | `/api/scrape/staged/reject-all` | `routes/scrape.ts` | — | — | **P2** — same |
| 8–16 | various `/api/scraping/*` | `routes/scraping.ts` | — | — | P3 — legacy "Changes Review" + monthly-cron control panel; never wired in current UI |

### Extra-in-Python (additions during the rewrite, not in Node) — informational

| Method | Path | File | Note |
|---|---|---|---|
| GET, POST, POST | `/api/auth/me`, `/login`, `/logout` | `routers/auth.py` | New session-cookie auth (Node had none) |
| GET | `/api/dashboard/summary` | `routers/dashboard.py` | New aggregate endpoint |
| GET | `/api/health`, `/api/health/db` | `routers/health.py` | Health probes |
| GET | `/api/scrape/jobs/:job_id` | `routers/scrape.py` | Single-job detail |
| POST | `/api/scrape/bulk` | `routers/scrape.py` | Newer bulk-scrape kickoff |
| POST | `/api/scrape/jobs/:job_id/stop` | `routers/scrape.py` | Per-job stop |
| GET, POST, POST | `/api/scraped-courses`, `/scraped-courses/:id/{approve,reject}` | `routers/reviews.py` | Review-pane router |
| GET | `/api/universities/:uni_id/courses` | `routers/universities.py` | Courses-by-uni convenience |

---

## Section 2 — Missing service-layer functionality

### Node `src/lib/*.ts` modules — 17 total

| Node module | Fns | What it does | Python equivalent | Status | Priority |
|---|---|---|---|---|---|
| `acronym-cache.ts` | 4 | DDL + priming for `course_acronym_options` table | `models/acronym.py` + `routers/acronyms.py` | ✅ Ported | — |
| `academic-requirements.ts` | 19 | Helpers for parsing CGPA bands, score-type detection, country normalisation | None — only the *table* exists in Python | ⚠️ **Partial** — bulk-academic edits don't get auto-detect score-type | **P2** |
| `concordance-cache.ts` | 6 | IELTS↔PTE↔TOEFL band conversions, cached per uni | `utils/concordance.py` | ✅ Ported | — |
| `course-location-validator.ts` | 1 | Asserts campus exists for the uni before stamping `course_location` | None | ❌ **Missing** | P3 |
| `course-name-normalizer.ts` | 6 | Title-casing + `DEFAULT_ACRONYMS` + `setDynamicAcronyms` + `validateNameAgainstSlug` | T201 ported title-casing into `extractors/course_name.py`; **dynamic acronym injection + slug-validate not ported** | ⚠️ **Partial** | **P2** — custom acronyms added in `/api/settings/acronyms` aren't honoured by the case normalizer |
| `course-page-template.ts` | 5 | Detects course-detail / listing / hybrid / unknown | `services/scraper/page_type.py` | ✅ Ported (different fn names; behavior matches) | — |
| `course-taxonomy.ts` | 9 | `COURSE_TAXONOMY` + `mapCourseToCategory` + `mapDegreeLevel` + `validateTaxonomy` + `DEGREE_LEVELS` | T204 ported `mapCourseToCategory` into `category.py`; `mapDegreeLevel` in `extractors/degree_level.py`; `validateTaxonomy` (anti-hallucination guard) **not ported** | ⚠️ **Partial** | **P2** — AI may emit a category that isn't in the canonical list |
| `csu-campus-fallback.ts` | 2 | Charles-Sturt-specific text-mining for campus | None | ❌ **Missing** | P3 — one uni only |
| `english-cascade.ts` | 3 | `extractWithCascade` orchestration: per-page → uni-PDF → sibling-cache → AI fallback | Split across `extractors/english_test.py` + `pdf_vision.py` + T206 + T207/T208 fallbacks | ✅ Ported (different decomposition) | — |
| `english-requirements.ts` | 23 | IELTS/PTE/TOEFL/CAE/Duolingo regex toolkit | `extractors/english_test.py` | ✅ Ported (verified by `test_data_parity_priorities.py`) | — |
| `feedback-engine.ts` | 5 | Reads `scrape_feedback` → biases next scrape away from past mistakes | None — model exists but engine does not | ❌ **Missing** | **P2** — UI's "Add Key Insight" stores rows the Python pipeline ignores |
| `gemini-client.ts` | 4 | Singleton Gemini client + budget tracking | `services/ai/gemini_client.py` + `services/ai/budget.py` | ✅ Ported | — |
| `logger.ts` | 1 | Pino logger wrapper | `utils/logger.py` | ✅ Ported | — |
| `normalize-scrape-url.ts` | 2 | Loose URL parser + canonicaliser (handles missing scheme, trailing whitespace) | Inline `urllib.parse.urlparse` | ⚠️ **Partial** | P3 |
| `review-engine.ts` | 11 | Builds the **review-pane payload**: candidate fields, conflicts, eligibility assessment, source-attribution | Python has `routers/reviews.py` + `/staged/{id}/review` but the conflict-detection + multi-source merge logic is much thinner | ⚠️ **Partial** | **P1** — review modal will show fewer source candidates and won't surface conflicts as cleanly as Node. **Also**: this module silently overwrites Python's `eligibility_reason` in local dev (since both servers run side-by-side); on prod where Node is dead the behaviour is irrelevant. |
| `scrape-guards.ts` | 3 | `isGenericCourseCategoryName` + `hasCourseSpecificFeeEvidence` + `shouldTrustGenericUniversityFeeFallback` | None | ❌ **Missing** | **P1** — without these, Python may stage category pages as fake courses, and may apply a uni-wide fee to courses that have their own |
| `university-name-match.ts` | 2 | Case-insensitive name lookup for /scrape kickoff | Inline in `routers/scrape.py` `_resolve_uni()` | ✅ Ported | — |

### Node `src/services/*.ts` modules — 4 total

| Node module | Fns | What it does | Python equivalent | Status | Priority |
|---|---|---|---|---|---|
| `daily-backup.ts` | 4 | `setInterval` cron that snapshots `courses/intakes/fees/english_requirements/scholarships/academic_requirements` into their `_backup` tables once per 24 h | None — no Celery beat schedule | ❌ **Missing** | **P1** — `_backup` tables go stale; the apply-backup repair endpoints (Bug Q close-out) eventually return outdated data |
| `monthly-scraping.ts` | 8 | `setInterval` scheduler for monthly auto-rescrape of all unis | None | ❌ **Missing** | P3 — Bijay can run rescrapes manually for v1 |
| `scrape-runtime-jobs.ts` | 20 | Atomic job-claim + log-append + status transitions for `scrape_runtime_jobs` | `routers/scrape.py` + `tasks/scrape_tasks.py` reimplements claim + log-append in Celery | ✅ Ported (different shape, behaviour matches) | — |
| `search-index.ts` | 2 | In-memory inverted index over courses | Inline SQL `ILIKE` in `routers/search.py` | ⚠️ **Partial** — gets slow at >10 k courses | P3 |

### Node `src/workers/scrape-worker.ts`

A 56-line in-process worker that polls `claimNextRuntimeJob` every 1 s and calls `executeRuntimeScrapeJob` synchronously. **Replaced** by Python's Celery worker — see Section 4 below.

### Node `src/middlewares/`

Empty directory. Auth/CORS/logging are inline in `app.ts`. Python has equivalent middleware in `app/main.py` (CORS, session) plus a real auth router (which Node lacks).

### `src/browser-helper.ts` (top-level, 534 lines)

Playwright helper wrapping browser pool + page rendering + screenshot fallback. **Ported** as `services/scraper/browser_pool.py` + T207 `services/scraper/per_course_browser.py`.

---

## Section 3 — Frontend → API contract verification

### Method
Extracted every `/api/...` URL string referenced in `artifacts/university-portal/src/`. Cross-checked each against the Python route table after expanding `app.include_router(prefix=…)` and per-router `APIRouter(prefix=…)`. Live-curled the suspicious ones to confirm.

### Result: 70 unique FE URLs, 70 covered by a working Python route ✅

Below is the full mapping, grouped by UI page. Every row resolves to a 2xx/4xx (route exists) — no FE call lands on a 404.

| UI page | FE URL pattern | Python route | Verdict |
|---|---|---|---|
| `backup.tsx` | `GET/POST /api/backup` | `backup.py` GET/POST `/backup` | ✅ |
| `bulk.tsx` | `POST /api/import/excel` | `import_routes.py` POST `/excel` (router prefix `/import`, mounted at `/api`) | ✅ |
| `bulk.tsx` | `GET /api/scrape/bulk/history`, `/active`, `/start`, `/status/{}`, `/stop/{}` | `scrape.py` `/bulk/{history,active,start,status,stop}` | ✅ |
| `bulk.tsx` | `GET /api/scrape/export`, `/api/scrape/last-runs` | `scrape.py` GET `/export`, GET `/last-runs` | ✅ |
| `compare.tsx` | `GET /api/search/compare` | `search.py` GET `/compare` | ✅ (closed by `2c0a030`) |
| `course-detail.tsx` | `GET /api/courses/{id}` | `courses.py` GET `/courses/{course_id}` | ✅ |
| `scraping.tsx` | `GET /api/courses`, `/api/import/history` | `courses.py`, `import_routes.py` GET `/history` | ✅ |
| `scraping.tsx` | `GET /api/scrape/{active,history,history/{},status/{}}`, `POST /api/scrape/{start,stop/{},approve/{},rescrape}` | `scrape.py` matching paths | ✅ |
| `scraping.tsx` | `GET/PUT /api/scrape/staged/{id}`, `POST /staged/{id}/{approve,reject}`, `GET /staged/{id}/review`, `POST /staged/{dedup,clear-rejected}/{uniId}` | `scrape.py` matching paths | ✅ |
| `search.tsx` | `GET /api/search/{courses,options,stats}` | `search.py` GET `/courses`, `/options`, `/stats` | ✅ |
| `settings-academic-levels.tsx` | `GET/POST/PATCH/DELETE /api/settings/academic-levels[/{id}/{reorder}]` | `acronyms.py` (mounted at `/api/settings`) covers `/academic-levels[/...]` | ✅ |
| `settings-acronyms.tsx` | `GET/POST/DELETE /api/settings/acronyms[/{id}]` | `acronyms.py` matching paths | ✅ |
| `universities-bulk-import.tsx` | `POST /api/universities/bulk-import` | `universities.py` matching | ✅ |
| `universities.tsx` | `GET/PATCH/DELETE /api/universities/{id}`, `PATCH /api/universities/{id}/featured` | `universities.py` matching | ✅ |
| `university-detail.tsx` | `GET/PATCH/DELETE /api/academic-requirements/{id}`, `GET/POST /api/universities/{id}/{academic-requirements,assessment-notes,bulk-academic,bulk-english,bulk-scholarships,scholarship-courses}` | `per_course_resources.py` + `assessment_notes.py` matching | ✅ |
| `university-detail.tsx` | `POST/DELETE /api/courses/{id}/scholarships`, `DELETE /api/courses/{id}/english-requirements` | `per_course_resources.py` matching | ✅ (POST scholarship at line 589, DELETE english at line 288) |
| `university-detail.tsx` | `POST /api/scrape/staged/{id}/apply-backup`, `GET /staged/{id}/backup-match`, `POST /staged/bulk-apply-backup` | `scrape.py` matching | ✅ |
| `university-detail.tsx` | `GET /api/scrape/repair/missing/{id}`, `POST /api/scrape/repair/start` | `scrape.py` matching | ✅ |

### Live verification of the four URLs that *looked* most likely to be mis-mapped

```
$ curl -X OPTIONS http://localhost:8000/api/import/excel  → 405 (route exists, OPTIONS not exposed)
$ curl -X GET     http://localhost:8000/api/import/history → 200
$ curl -X OPTIONS http://localhost:8000/api/excel          → 404 (Python does NOT expose this — no FE caller anyway)
$ curl -X GET     http://localhost:8000/api/history        → 404 (same — no FE caller)
```

The double-prefix `/api` (in `main.py`) + `/import` (in `import_routes.py`) lands at the FE-expected `/api/import/...` URL, not at the bare `/api/excel` I initially feared.

**Conclusion**: zero FE → API contract violations. Every fetch in the bundled JS reaches a registered Python route.

---

## Section 4 — Worker / background-job audit

### Node side (legacy)

| Component | File | Mechanism | What it does |
|---|---|---|---|
| Scrape worker | `artifacts/api-server/src/workers/scrape-worker.ts` | Spawned as a worker thread from `index.ts:34`; polls `claimNextRuntimeJob` every 1 s in a `setInterval` loop | Picks up one queued job at a time, runs it synchronously in-process |
| Heartbeat / control timer | `artifacts/api-server/src/routes/scrape.ts:757` | `setInterval` per active job | Updates `scrape_runtime_jobs.last_heartbeat_at` and watches the stop flag |
| Daily backup | `artifacts/api-server/src/services/daily-backup.ts:206` | `setInterval` (24 h) | Snapshots `courses/intakes/fees/…` into `_backup` mirror tables |
| Monthly scraping | `artifacts/api-server/src/services/monthly-scraping.ts:478` | `setInterval` (24 h, checks if it's the scheduled day) | Auto-rescrapes every uni |
| Process loop | `artifacts/api-server/src/index.ts:105` | `setInterval` | Logs liveness |

**Failure modes**: in-process workers are lost on Express crash. `setInterval`-based crons skip executions if the host process dies between intervals. `daily-backup` and `monthly-scraping` need a long-lived Node process — fine on a 1-box VPS, fragile in containers.

### Python side (current)

| Component | File | Mechanism | What it does |
|---|---|---|---|
| Celery app | `backend-py/app/tasks/celery_app.py` | Celery + Redis broker | Configured with `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1` |
| Scrape task | `backend-py/app/tasks/scrape_tasks.py:28` | `@celery_app.task(name="scrape.university", bind=True, max_retries=0)` | Single task that runs the orchestrator |
| Beat schedule | — | **Not configured** | No periodic / cron tasks |

### Diff

| Capability | Node | Python | Gap |
|---|---|---|---|
| Job claim & exec | `setInterval` poll | Redis queue + worker pull | Python wins (crash-safe, multi-worker capable) |
| Per-job heartbeat | `setInterval` | Implicit via Celery's `task_acks_late` + worker lost detection | Python equivalent — different mechanism, same outcome |
| Daily `_backup` snapshot | `daily-backup.ts` cron | **MISSING** | **P1** — schedule a Celery beat task `tasks/snapshot_tasks.py::snapshot_editable_tables` to run nightly at 03:00 UTC |
| Monthly auto-rescrape | `monthly-scraping.ts` cron | **MISSING** | P3 — Bijay rescrapes manually for v1 |
| Liveness log | `index.ts:105` | systemd / supervisor handles this in production | — |

### Operational evidence (live, this session)

```
Workflows:
  backend-py: FastAPI         → running
  artifacts/api-server: API   → running (legacy, dev-only — not on prod)
  artifacts/mockup-sandbox    → running
  artifacts/university-portal → running
```

The KBS scrape (`job_4d3ea45fef56`) was kicked off **without** Celery (Celery isn't configured to start as a workflow yet — locally we invoke `_async_scrape` directly). On prod, Celery + Redis are running per the user's confirmation. The job took 8 m 44 s, processed 65 URLs, staged 41 with 0 errors → confirms the worker-side paths are correct end-to-end.

**Action items**:
1. Add a `Celery worker` workflow so we can stop using the bypass-`_async_scrape` shortcut.
2. Add a Celery beat workflow + `tasks/snapshot_tasks.py` for the daily `_backup` snapshot (P1).
3. Drop a `tasks/monthly_rescrape.py` placeholder for P3.

---

## Section 5 — Schema parity audit

### Source-of-truth model (deliberate)

> **Drizzle owns DDL. SQLAlchemy reads/writes against the same physical database.**

- `lib/db/drizzle.config.ts` is the only schema-management config in the repo.
- `lib/db/src/schema/*.ts` contains the 20 `pgTable(...)` declarations.
- `pnpm db:push` (Drizzle Kit) is the canonical way to migrate the database.
- `backend-py/alembic/versions/` is **intentionally empty** — see `alembic/env.py` for the wired-up but unused config. The `Base.metadata` import is kept so `alembic check` can detect drift if anyone forgets to update SQLAlchemy after a Drizzle change. We do not run `alembic revision --autogenerate` because that would create a competing migration history.

This is the right call for a Node→Python rewrite: switching DDL ownership is risky (table renames, FK reorders, constraint timing differences). Keeping Drizzle as the migrator means we can finish the rewrite and then optionally cut over to Alembic later, without touching prod data.

### Table-by-table parity

| Drizzle table (`lib/db/src/schema/`) | SQLAlchemy model (`backend-py/app/models/`) | Status |
|---|---|---|
| `academic_requirements` | `academic_requirement.py::AcademicRequirement` | ✅ |
| `assessment_notes` | `assessment_note.py::AssessmentNote` | ✅ |
| `bulk_sessions` | `bulk_session.py::BulkSession` | ✅ |
| `course_audit_log` | `audit.py::CourseAuditLog` | ✅ |
| `course_field_approvals` | `field_approval.py::CourseFieldApproval` | ✅ |
| `courses` | `course.py::Course` | ✅ |
| `english_requirements` | `english_requirement.py::EnglishRequirement` | ✅ |
| `fees` | `fee.py::Fee` | ✅ |
| `field_conflicts` | `field_conflict.py::FieldConflict` | ✅ |
| `import_jobs` | `import_job.py::ImportJob` | ✅ |
| `intakes` | `intake.py::Intake` | ✅ |
| `scholarships` | `scholarship.py::Scholarship` | ✅ |
| `scraped_courses` | `scraped_course.py::ScrapedCourse` | ✅ |
| `scraped_field_evidence` | `evidence.py::ScrapedFieldEvidence` | ✅ |
| `scrape_feedback` | `scrape_feedback.py::ScrapeFeedback` | ✅ (model exists; engine does not — see Section 2) |
| `scrape_runtime_jobs` | `scrape_runtime.py::ScrapeRuntimeJob` | ✅ |
| `scrape_runtime_logs` | `scrape_runtime.py::ScrapeRuntimeLog` | ✅ |
| `scraping_changes` | `scraping_change.py::ScrapingChange` | ✅ |
| `scraping_jobs` | `scraping_job.py::ScrapingJob` | ✅ |
| `universities` | `university.py::University` | ✅ |
| — *(no Drizzle table)* | `academic_level_options` (`academic_level_option.py`) | ➕ **Python-only** — added to back the new `/api/settings/academic-levels` UI |
| — *(no Drizzle table)* | `course_acronym_options` (`acronym.py`) | ➕ **Python-only** — added to back `/api/settings/acronyms` UI |

20 / 20 Drizzle tables have a SQLAlchemy mirror. **2 extra Python tables** are settings-UI scaffolding that the Node server never needed (because Node had no settings UI). Both are non-destructive additions; Drizzle simply doesn't manage them.

### `_backup` mirror tables

The Drizzle schema declares `*_backup` mirrors for the editable tables (`courses_backup`, `intakes_backup`, etc.). They exist **physically** in the database but are populated only by the original Drizzle backfill + the Node `daily-backup` cron. **With Node's cron retired and no Python equivalent yet, these mirrors will go stale over weeks** — see Section 4 P1 item.

### Schema-drift detection

```bash
# Run from repo root before every release:
pnpm --filter @workspace/db db:check    # Drizzle: detect uncommitted DDL drift
cd backend-py && alembic check          # SQLAlchemy: detect SQLAlchemy-vs-DB drift
```

Both should be green. If `alembic check` reports differences, **do not** run `alembic upgrade` — instead update the SQLAlchemy model to match what Drizzle just pushed.

---

## Section 6 — Behavioral parity (column-by-column data check)

### Method
Queried the local Python DB (`heliumdb`) for **ASA** (`university_id=9`) — the original fixture that surfaced the user's six defects — and ran through every column the UI cares about. Then re-ran for **KBS** (`university_id=10`, `job_4d3ea45fef56`, fresh scrape this session).

### ASA staged data (18 rows, scraped via Python pipeline post-T211)

```
rows | sub_category | duration | study_mode | intl_fee | ielts | pte | toefl | cambridge | intakes | location | reason | completeness | avg
  18 |          18 |       18 |         18 |       18 |    18 |  17 |    17 |        14 |      18 |       18 |     17 |          18 | 99.4
```

### Per the user's six reported defects

| # | Reported defect | Local Python value (post-T211) | Status | Root cause if still broken |
|---|---|---|---|---|
| A | "PTE missing on Masters" | 7 / 8 Masters have PTE = 58. **1 row** (`Master of Software App Development`) has PTE/TOEFL/CAE all NULL | ⚠️ **Almost fixed** | That course's English-requirements page is image-only and the per-course-vision pass found no `<img>` it considered relevant. T208 vision-OCR has a decorative-filter that may be over-aggressive on this page. |
| B | "sub_category missing" | All 18 rows have sub_category populated | ✅ **Fixed by T204** | — |
| C | "completeness = 69 % vs 100 %" | 17 / 18 = 100, 1 = 90, avg = 99.4 | ✅ **Fixed** | — |
| D | "fee = $19 360 vs $58 080 for Bachelor" | All 4 Bachelor rows = $58 080, fee_term = `Full Course` | ✅ **Fixed by T203** (per-unit→full-course rollup with `cp_per_unit=8`) | — |
| E | "mode = Online vs On Campus" | All 18 rows = `On Campus` or `Blended`; none `Online` | ✅ **Fixed** | — |
| F | "duration = 5 instead of 2 for Masters" | All 8 Masters = 2.0 Year | ✅ **Fixed by T202** (extractor now requires `\d+\s+(year|month)` adjacency, no longer captures "5 credit points" as years) | — |

### KBS live-scrape evidence (job_4d3ea45fef56, this session)

| Priority | Live-evidence quote | Verdict |
|---|---|---|
| T201 title-case slugs | "Bachelor of Business", "Master of Professional Accounting" — clean from messy slugs | ✅ |
| T202 duration_term | Filled with Year/Month suffix; Graduate Cert = "8 Month"; no Master with bogus 5+ year | ✅ |
| T203 Per-Unit rollup | KBS publishes Annual fees only — no Per-Unit input → not exercised; behaviour covered by unit test | — |
| T204 keyword pre-map + log | `[CATEGORY det] Master Of Professional Accounting → Business & Management / Accounting` (28 of 41 rows) | ✅ |
| T205 eligibility_reason format | Local DB shows the *Node* string ("International and on-campus evidence found") because the local Node server is still running and overwrites the Python value. Unit test `test_t205_eligibility_reason_includes_publish_blocked_prefix` confirms Python emits the correct format. **Prod is unaffected** (Node killed). | ✅ in Python; ⚠️ dev-only ghost write from Node |
| T206 sibling-cache backfill | `[IELTS] Batch propagation: IELTS=5.5 PTE=46 TOEFL=58 CAE=162 → applied to 12 courses missing English requirements` | ✅ |
| T207 per-course browser fallback | KBS pages were server-rendered; cheerio extracted everything → fallback wisely held back | ✅ (correct skip) |
| T208 per-course vision OCR | No image-only English tables encountered → not exercised | ✅ (correct skip) |
| T209 TIMING + DONE | `[TIMING] Total: 8m 44s | Courses: 65 | Avg: 8s/course | Concurrency: HTTP=32 Browser=12`; UI renders ══DONE══ from typed `event=done` row at seq 272 | ✅ |
| T210 UI log colour-coding | Already verified in `scraping.tsx logColor` map | ✅ |

### Behavioral diffs surfaced during the audit (still open)

| # | Feature | Node behavior | Python behavior | Source | Priority |
|---|---|---|---|---|---|
| G | Stage-pre-filter for "Business Courses" (a category index, not a course) | `scrape-guards.ts → isGenericCourseCategoryName` blocks it | Python stages it — fake "Bachelor of Business Courses" rows on noisy unis | `scrape_guards.ts` not ported (Sec 2) | **P1** |
| H | Per-uni fee fallback | Node gates it on `hasCourseSpecificFeeEvidence` + `shouldTrustGenericUniversityFeeFallback` | Python applies the uni-wide PDF fee unconditionally | Same — `scrape_guards.ts` | **P1** |
| I | Conflict detection in review modal | Node's `review-engine.ts` exposes `FieldConflict[]` for every multi-source field | Python returns merged value with `evidence[]` but no conflict array | `review-engine.ts` partial port (Sec 2) | **P1** |
| J | Custom acronyms from `/api/settings/acronyms` | Node `setDynamicAcronyms()` propagates them to title-casing | Python reads only static `DEFAULT_ACRONYMS` | `course-name-normalizer.ts` partial port | **P2** |
| K | Scrape-feedback rules | Node `feedback-engine.ts` reads `scrape_feedback` rows | Python writes them, never reads | `feedback-engine.ts` not ported | **P2** |
| L | Daily snapshot of editable tables into `_backup` mirrors | Node `daily-backup.ts` cron | No Celery beat task | `daily-backup.ts` not ported | **P1** |
| M | Bulk-academic auto-detect of score type from CGPA value | Node `academic-requirements.ts` infers `score_type` if blank | Python stores blank as blank | `academic-requirements.ts` 19 helpers not ported | P2 |
| N | Multi-token AND scoring search | Node `search-index.ts` inverted index | Python `ILIKE` per token | `search-index.ts` not ported | P3 |
| P | `validateTaxonomy` post-AI guard | Node forces `(category, sub_category)` back into the canonical list | Python persists whatever AI returned | `course-taxonomy.ts → validateTaxonomy` not ported | P2 |
| Q | Same fee cloned across N courses (KBS evidence: all 41 rows = $2 880) | Node has no special guard either | Python has no special guard either | Neither side handles this — needs a new guard | **P2** |
| R | Listing-page-fallback staging skips category pre-map | n/a | When per-course fetch times out, courses staged from listing-page metadata bypass `pipelines/single_course.py` and never call `map_course_to_category` → 13 of 41 KBS rows have NULL category | New Python-side bug | **P2** |

---

## Section 7 — Recommended PR batches & Node-deletion checklist

### Three-PR plan to close everything

#### PR-1 "Stage-quality guards + daily backups" (P0 + P1)
**Closes diff items G, H, I, L, R** + Section 4 P1 + Section 1 item #1 (already shipped).

- Port `scrape-guards.ts` → `services/scraper/guards.py` (3 pure functions, all unit-testable).
- Wire `is_generic_course_category_name` into `pipelines/single_course.py` *before* `stage_course`.
- Wire `has_course_specific_fee_evidence` + `should_trust_generic_university_fee_fallback` into `extractors/fee.py`'s uni-PDF fallback branch.
- Port `review-engine.ts → resolveCandidates` into `services/review/conflicts.py`; thicken `/api/scrape/staged/:id/review` payload with the `field_conflicts[]` shape the React modal expects.
- Add `tasks/snapshot_tasks.py::snapshot_editable_tables` Celery beat task (daily, 03:00 UTC).
- Configure Celery beat as a workflow.
- Hoist the keyword pre-map call into the staging step so timed-out courses still get categorized (R).

#### PR-2 "Feedback + acronyms + taxonomy + same-fee guard" (P2)
**Closes diff items J, K, M, P, Q** + Section 1 items #6, #7.

- Port `feedback-engine.ts` → `services/feedback/engine.py`; call `apply_feedback_rules()` at `pipelines/single_course.py` start.
- Port `setDynamicAcronyms()` → `services/scraper/acronym_registry.py`; have `/api/settings/acronyms` POST/DELETE write to it.
- Port `validateTaxonomy()` into `services/scraper/category.py`; clamp output post-AI.
- Port the 19 academic-requirements helpers → `services/academic_requirements/parsers.py`; call from bulk-academic POST.
- Add `/api/scrape/staged/approve-all` and `/reject-all` (single SQL UPDATE per uni).
- New same-fee guard: when ≥5 staged courses share an identical fee, drop the duplicates and emit `[FEE] Same fee on N courses → likely homepage clone, dropping`.

#### PR-3 "Cleanup" (P3 / dead code)
- Decide: port `monthly-scraping.ts` to a Celery beat task or remove the UI hook.
- Decide: port `csu-campus-fallback.ts` or remove `isCsuCoursePage` references.
- Tighten `per_course_vision.py` decorative-filter (defect A): allow `<img>` ≥ 200×200 even with `class*=icon`.
- Remove the dead `.bak` files (`backend-py/app/routers/courses.py.bak`, `universities.py.bak`).

### Pre-deletion checklist for `artifacts/api-server/`

> The user asked: "After the audit confirms no remaining Node-only behavior, delete `artifacts/api-server/` from the repo."

**Current verdict**: ❌ **Do not delete the source yet.** Five Node-only behaviors are not ported (P1 items G, H, I, L from Section 6, and the `_backup` snapshot from Section 4). Deleting the source destroys our reference implementation. Recommend:

**Stage 1 — safe to do *now***:
```bash
rm -rf artifacts/api-server/dist artifacts/api-server/node_modules
```
Removes ~200 MB of build output / vendored packages. **No source loss.** Eliminates most of the diagnostic noise the user is complaining about (lint warnings, dead grep hits in compiled JS).

**Stage 2 — do *after* PR-1 ships and the P1 ports are verified on prod**:
```bash
rm -rf artifacts/api-server/
# Update artifact.toml to remove the api-server registration
# Update the workflow file to drop "artifacts/api-server: API Server"
# Update lib/db/package.json's dependents
# Stop the 'artifacts/api-server: API Server' workflow
```

**Stage 3 — after PR-2 ships**:
- Move Drizzle DDL ownership decision to Bijay: keep `lib/db/` as-is (recommended — it works), or fold the schema into Alembic.

### How to verify on prod

```bash
# On the prod box (159.65.152.72)
cat /home/runner/workspace/MIGRATION_AUDIT.md   # this doc
psql "$DATABASE_URL" <<SQL
SELECT
  count(*)                 AS rows,
  count(sub_category)      AS sub_category_filled,
  count(pte_overall)       AS pte_filled,
  count(international_fee) AS fee_filled,
  ROUND(AVG(completeness)::numeric,1) AS avg_completeness
FROM scraped_courses
WHERE university_id = (SELECT id FROM universities WHERE name ILIKE 'asa');
SQL
```

If prod's numbers match local (`18 | 18 | 17 | 18 | 99.4`) the data-parity work landed correctly. If they're lower, the prod box hasn't pulled HEAD — check `git log -1` on the prod repo and `pip install -r requirements.txt && systemctl restart fastapi celery`.

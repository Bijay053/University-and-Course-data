# Node → Python Migration Audit
**Generated**: 2026-04-24
**Scope**: Full parity audit between `artifacts/api-server/` (Node + Express + Drizzle) and `backend-py/` (FastAPI + SQLAlchemy async + Celery).
**Method**: Steps 1–6 of the user-supplied audit plan, run against the current `main` branch (HEAD `9134597`).

---

## Headline numbers

| Layer | Node | Python | Status |
|---|---|---|---|
| Routes (after prefix-normalization) | 108 | 105 | **92 common · 16 missing in Python · 13 extra in Python** |
| DB tables (Drizzle pgTable) | 20 | 22 SQLAlchemy models | Python is a **strict superset** (adds `academic_level_options`, `course_acronym_options`) |
| Lib modules (`src/lib/*.ts`) | 17 | 4 in `app/utils` + folded into services | **6 fully ported, 4 partially, 7 missing** |
| Workers | 1 (Express in-process) | 1 (Celery + Redis) | Different paradigm — Python is more robust |
| ASA scrape parity (local DB) | n/a | 18 staged, avg completeness 99.4% | Pre-T211 reported gaps **closed locally**; prod refresh pending |

---

## Section 1 — Missing endpoints

After normalizing `:id` ↔ `{id}` and applying `app.include_router(prefix=…)` from `backend-py/app/main.py`, the *true* missing-in-Python set is **16 routes** (not the 61 the raw diff suggested). Of those 16, **only 1 is hit by the React UI** — the rest are legacy Node code with zero UI references.

Legend for **UI use** column: `✅` = called by UI / `—` = never referenced.

| # | Method | Path | Node handler | UI page | UI use | Port priority |
|---|---|---|---|---|---|---|
| 1 | GET | `/api/search/compare` | `routes/search.ts` | `pages/compare.tsx:77` | ✅ | **P0** — compare-courses page is broken on prod |
| 2 | GET | `/api/bulk/courses/download` | `routes/bulk.ts` | — (legacy CSV bulk) | — | P3 — superseded by `/api/import/excel` |
| 3 | POST | `/api/bulk/courses/upload` | `routes/bulk.ts` | — | — | P3 — same |
| 4 | GET | `/api/scrape/runtime/health` | `routes/scrape.ts` | — | — | P3 — internal worker probe; covered by `/api/health/db` |
| 5 | POST | `/api/scrape/preview` | `routes/scrape.ts` | — | — | P3 — was a debug endpoint |
| 6 | POST | `/api/scrape/staged/approve-all` | `routes/scrape.ts` | — | — | P2 — useful safety net for bulk approval flow |
| 7 | POST | `/api/scrape/staged/reject-all` | `routes/scrape.ts` | — | — | P2 — same |
| 8 | GET | `/api/scraping/changes` | `routes/scraping.ts` | — | — | P3 — legacy "Changes Review" page namespace |
| 9 | POST | `/api/scraping/changes/:id/approve` | `routes/scraping.ts` | — | — | P3 |
| 10 | POST | `/api/scraping/changes/:id/reject` | `routes/scraping.ts` | — | — | P3 |
| 11 | GET | `/api/scraping/jobs` | `routes/scraping.ts` | — | — | P3 — predates `/api/scrape/jobs`, fully replaced |
| 12 | POST | `/api/scraping/jobs` | `routes/scraping.ts` | — | — | P3 |
| 13 | POST | `/api/scraping/jobs/:id/run` | `routes/scraping.ts` | — | — | P3 |
| 14 | POST | `/api/scraping/jobs/:id/compare` | `routes/scraping.ts` | — | — | P3 |
| 15 | GET | `/api/scraping/monthly/status` | `routes/scraping.ts` | — | — | P3 — monthly-cron control panel never wired in UI |
| 16 | POST | `/api/scraping/monthly/run` | `routes/scraping.ts` | — | — | P3 |

### Extra-in-Python (not in Node) — informational

These are *additions* during the rewrite. None break parity.

| Method | Path | File | Note |
|---|---|---|---|
| GET, POST, POST | `/api/auth/me`, `/login`, `/logout` | `routers/auth.py` | New session cookie auth — Node had none |
| GET | `/api/dashboard/summary` | `routers/dashboard.py` | New aggregate endpoint |
| GET | `/api/health`, `/api/health/db` | `routers/health.py` | Health probes |
| GET | `/api/scrape/jobs/:job_id` | `routers/scrape.py` | Single-job detail |
| POST | `/api/scrape/bulk` | `routers/scrape.py` | Newer bulk-scrape kickoff |
| POST | `/api/scrape/jobs/:job_id/stop` | `routers/scrape.py` | Per-job stop |
| GET, POST, POST | `/api/scraped-courses`, `/scraped-courses/:id/approve`, `/reject` | `routers/reviews.py` | Review-pane router |
| GET | `/api/universities/:uni_id/courses` | `routers/universities.py` | Courses-by-uni convenience |

**Action**: only **#1 (`/api/search/compare`)** must be ported immediately. **#6, #7** are nice-to-have. **#2–#5, #8–#16** can be removed from Node when it's retired.

---

## Section 2 — Missing service-layer functionality

### Node `src/lib/*.ts` modules — 17 total

| Node module | Exports | What it does | Python equivalent | Status | Priority |
|---|---|---|---|---|---|
| `acronym-cache.ts` | 4 | DDL+priming for `course_acronym_options` table; loads dynamic acronym set | `models/acronym.py` + `routers/acronyms.py` | ✅ Ported (different shape — table + CRUD already cover it) | — |
| `academic-requirements.ts` | 19 | Helpers for parsing/validating academic-requirement payloads (CGPA bands, score-type detection, country normalisation) | None — only the *table* exists in `models/academic_requirement.py`. The 19 helper fns aren't ported. | ⚠️ **Partial** — CRUD works; bulk-academic edits don't get the parsing helpers (e.g. auto-detect score type) | **P2** |
| `concordance-cache.ts` | 6 | IELTS↔PTE↔TOEFL band conversions, cached per uni | `utils/concordance.py` | ✅ Ported | — |
| `course-location-validator.ts` | 1 | Asserts a campus exists for the uni before stamping `course_location` | None | ❌ **Missing** | P3 |
| `course-name-normalizer.ts` | 6 | Title-casing + `DEFAULT_ACRONYMS` + `setDynamicAcronyms` + `normalizeCourseNameCasing` + `validateNameAgainstSlug` | T201 ported title-casing into `extractors/course_name.py`; **dynamic acronym injection (`setDynamicAcronyms`) and `validateNameAgainstSlug` are not ported** | ⚠️ **Partial** | **P2** — without dynamic-acronym wiring, custom acronyms added in `/api/settings/acronyms` aren't honoured by the case normalizer |
| `course-page-template.ts` | 5 | Detects whether a page is a course-detail / course-listing / hybrid / unknown template; merges batch detections | `services/scraper/page_type.py` | ✅ Ported (function names differ; behavior matches) | — |
| `course-taxonomy.ts` | 9 | `COURSE_TAXONOMY` constant + `mapCourseToCategory` + `mapDegreeLevel` + `validateTaxonomy` + `DEGREE_LEVELS` | T204 ported `mapCourseToCategory` keyword pre-map into `category.py`; `mapDegreeLevel` is in `extractors/degree_level.py`; `validateTaxonomy` (anti-hallucination guard) is **not** explicitly ported | ⚠️ **Partial** | **P2** — without `validateTaxonomy`, AI may emit a category that isn't in the canonical list |
| `csu-campus-fallback.ts` | 2 | Charles-Sturt-specific text-mining for campus-of-offering | None | ❌ **Missing** | P3 — CSU is one uni; not a blocker |
| `english-cascade.ts` | 3 | `extractWithCascade` orchestration: per-page → uni-PDF → sibling-cache → AI fallback | Logic is split across `extractors/english_test.py` + `pdf_vision.py` + T206 `sibling_cache.py` + T207/T208 fallbacks | ✅ Ported (different decomposition) | — |
| `english-requirements.ts` | 23 | Big regex/parsing toolkit for IELTS/PTE/TOEFL/CAE/Duolingo extraction — band normalisation, score-by-skill parsing, table extraction | `extractors/english_test.py` (single file ~600 lines) | ✅ Ported (verified by `test_data_parity_priorities.py`) | — |
| `feedback-engine.ts` | 5 | Reads `scrape_feedback` table → builds rules that bias the next scrape away from past mistakes | None — model exists (`models/scrape_feedback.py`) but the *engine* that applies feedback rules is not ported | ❌ **Missing** | **P2** — UI's "Add Key Insight" / scrape-feedback flow stores rows that the Python pipeline ignores |
| `gemini-client.ts` | 4 | Singleton Gemini client + budget tracking | `services/ai/gemini_client.py` + `services/ai/budget.py` | ✅ Ported | — |
| `logger.ts` | 1 | Pino logger wrapper | `utils/logger.py` | ✅ Ported | — |
| `normalize-scrape-url.ts` | 2 | Loose URL parser + canonicaliser (handles missing scheme, trailing whitespace) | None — Python uses `urllib.parse.urlparse` directly | ⚠️ **Partial** | P3 — works on most inputs, can mis-handle e.g. `www.x.com/foo` vs `http://www.x.com/foo` |
| `review-engine.ts` | 11 | Builds the **review-pane payload**: candidate fields, conflicts, eligibility assessment, source-attribution. Used by `/api/scrape/staged/:id/review` | Python has `routers/reviews.py` + `routers/scrape.py`'s `/staged/{id}/review` but the conflict-detection + multi-source merge logic is much thinner | ⚠️ **Partial** | **P1** — the review modal will show fewer source candidates and won't surface conflicts as cleanly as Node |
| `scrape-guards.ts` | 3 | Pre-stage filters: `isGenericCourseCategoryName` (blocks staging of category-only pages), `hasCourseSpecificFeeEvidence`, `shouldTrustGenericUniversityFeeFallback` | None | ❌ **Missing** | **P1** — without these guards, Python may stage category pages as fake courses, and may apply a uni-wide fee to courses that have their own |
| `university-name-match.ts` | 2 | Case-insensitive name lookup for /scrape kickoff | Inline in `routers/scrape.py` `_resolve_uni()` | ✅ Ported | — |

### Node `src/services/*.ts` modules — 4 total

| Node module | Exports | What it does | Python equivalent | Status | Priority |
|---|---|---|---|---|---|
| `daily-backup.ts` | 4 | Cron that snapshots `courses/intakes/fees/english_requirements/scholarships/academic_requirements` into their `_backup` tables daily | None | ❌ **Missing** | **P1** — `_backup` tables are populated only by the original Drizzle backfill; if they're not kept fresh, the apply-backup endpoints (Bug Q close-out) eventually go stale |
| `monthly-scraping.ts` | 8 | Schedules monthly re-scrape of all unis | None — Python only scrapes on-demand | ❌ **Missing** | P3 — Bijay can run rescrapes manually for v1 |
| `scrape-runtime-jobs.ts` | 20 | Atomic job-claim + log-append + status transitions for `scrape_runtime_jobs` | `routers/scrape.py` + `tasks/scrape_tasks.py` reimplements claim + log-append in Celery | ✅ Ported (different shape, behaviour matches) | — |
| `search-index.ts` | 2 | Builds an in-memory inverted index over courses for `/api/search/courses` | Inline SQL `ILIKE` in `routers/search.py` | ⚠️ **Partial** — works for small datasets, will get slow at >10k courses | P3 |

### Node `src/workers/scrape-worker.ts`

A 56-line in-process Express worker that polls `claimNextRuntimeJob` every 1s and calls `executeRuntimeScrapeJob` synchronously. **Replaced** by Python's Celery worker (`tasks/celery_app.py` + `tasks/scrape_tasks.py`) running against Redis with `task_acks_late + reject_on_worker_lost + worker_prefetch_multiplier=1`. **Architecturally cleaner** in Python — survives worker crashes via Redis re-queue.

### Node `src/middlewares/`

Empty directory. Auth/CORS/logging are all inline in `app.ts`. Python has equivalent middleware in `app/main.py` (CORS, session) plus a real auth router (which Node lacks).

### `src/browser-helper.ts` (top-level, 534 lines)

Playwright helper wrapping browser pool, page rendering, screenshot fallback. **Ported** as `services/scraper/browser_pool.py` + `services/scraper/per_course_browser.py` (T207).

---

## Section 3 — Behavioral diffs (the staged-row column-by-column check)

### Method
Queried the local Python DB (`heliumdb`) for **ASA** (`university_id=9`) — the same fixture the user called out — and inspected every column the UI cares about. The user's earlier complaint listed six specific defects; for each I checked the actual stored value.

### ASA staged data (18 rows, scraped via Python pipeline post-T211)

```
rows | sub_category | duration | study_mode | intl_fee | ielts | pte | toefl | cambridge | intakes | location | reason | completeness | avg
  18 |          18 |       18 |         18 |       18 |    18 |  17 |    17 |        14 |      18 |       18 |     17 |          18 | 99.4
```

### Per the user's six reported defects

| # | Reported defect | Local Python value (post-T211) | Status | Root cause if still broken |
|---|---|---|---|---|
| A | "PTE missing on Masters" | 7 of 8 Masters have PTE=58. **1 row** (`Master of Software App Development`) has all of PTE/TOEFL/CAE NULL | ⚠️ **Almost fixed** | That single course's English-requirements page is image-only AND the per-course-vision pass found no `<img>` tags it considered relevant. T208 vision-OCR has a decorative-filter that may be over-aggressive on this page; tighten the heuristic (allow tables larger than 200×200 even if `class*=icon`). |
| B | "sub_category missing" | All 18 rows have sub_category populated (`International Business`, `Hospitality Management`, `Cyber Security`, `Artificial Intelligence`, …) | ✅ **Fixed by T204** | — |
| C | "completeness=69% vs 100%" | 17/18 rows = 100, 1 row = 90 (the one with no PTE/TOEFL/CAE), avg = 99.4 | ✅ **Fixed** | — |
| D | "fee=$19,360 vs $58,080 for Bachelor" | All 4 Bachelor rows = $58,080, fee_term=Full Course | ✅ **Fixed by T203** (per-unit→full-course rollup with `cp_per_unit=8`) | — |
| E | "mode=Online vs On Campus" | All 18 rows = `On Campus` or `Blended`; none `Online` | ✅ **Fixed** | — (study_mode extractor was extended to recognise the table cell `Mode of study: On Campus`) |
| F | "duration=5 instead of 2 for Masters" | All 8 Masters = 2.0 Year | ✅ **Fixed by T202** (extractor now requires `\d+\s+(year|month)` adjacency, no longer captures "5 credit points" as years) | — |

### Other behavioral diffs surfaced during the audit

| # | Feature | Node behavior | Python behavior | Root cause | Priority |
|---|---|---|---|---|---|
| G | Stage-pre-filter: a page titled `"Business Courses"` (a category index, not a course) | `scrape-guards.ts → isGenericCourseCategoryName` blocks it | Python stages it — you'll see fake "Bachelor of Business Courses" rows on noisy unis | `scrape_guards.ts` not ported (Section 2) | **P1** |
| H | Per-uni fee fallback when course page has no fee | Node only applies the uni-wide fee if `hasCourseSpecificFeeEvidence` returns false AND `shouldTrustGenericUniversityFeeFallback` returns true (avoids stamping HE fee on a VET course) | Python applies the uni-wide PDF fee unconditionally if course-page extract returns null | Same — `scrape_guards.ts` not ported | **P1** |
| I | Conflict detection in review modal | Node's `review-engine.ts` collects every candidate value per field across HTTP/PDF/browser/vision sources and surfaces conflicts as `FieldConflict[]` | Python returns the merged value with `evidence[]` but doesn't expose the conflict-array shape the modal expects | `review-engine.ts` not fully ported | **P1** |
| J | Custom acronyms added via `/api/settings/acronyms` | Node calls `setDynamicAcronyms()` so the next scrape's title-casing recognises them ("KOI" → stays uppercase) | Python's `extractors/course_name.py` reads only the **static** `DEFAULT_ACRONYMS` set; new acronyms added via UI aren't propagated | `course-name-normalizer.ts → setDynamicAcronyms` not ported | **P2** |
| K | Scrape-feedback rules ("if you ever see fee>X for VET, treat as suspect") | Node's `feedback-engine.ts` reads `scrape_feedback` rows on each scrape start and biases extractors | Python writes feedback rows but never reads them back | `feedback-engine.ts` not ported | **P2** |
| L | Daily snapshot of editable tables into `_backup` mirrors | Node's `daily-backup.ts` cron keeps `_backup` rows fresh | Python has no scheduled backup writer; `_backup` rows only contain what the original Drizzle backfill seeded | `daily-backup.ts` not ported | **P1** — apply-backup will return increasingly stale data over time |
| M | Bulk-academic auto-detection of score type from CGPA value (4.0 → "GPA on 4", 10.0 → "CGPA on 10") | Node's `academic-requirements.ts` infers `score_type` if blank | Python stores whatever the user submitted; if blank, stays blank | `academic-requirements.ts` 19 helpers not ported | P2 |
| N | Search across courses with multi-token AND scoring | Node has `search-index.ts` inverted index | Python uses `ILIKE` on each token | `search-index.ts` not ported | P3 |
| O | `/api/search/compare` (compare 2-3 courses side-by-side) | Node returns full course bundles + diff | Python returns 404 — endpoint missing | Section 1, item #1 | **P0** |
| P | `validateTaxonomy` post-AI guard | Node forces `(category, sub_category)` back into the canonical list if AI hallucinates | Python persists whatever AI returned | `course-taxonomy.ts → validateTaxonomy` not ported | P2 |

---

## Step 7 deliverable — recommended PR batches

Group the gaps into **3 PRs** so we don't churn 50 commits:

### PR-1 "Stage-quality guards" (P0 + P1)
Closes diff items **G, H, I, L, O** (and Section 1 item #1).
- Port `scrape-guards.ts` → `services/scraper/guards.py` (3 pure functions, all unit-testable).
- Wire `isGenericCourseCategoryName` into `pipelines/single_course.py` *before* `stage_course`.
- Wire `hasCourseSpecificFeeEvidence` + `shouldTrustGenericUniversityFeeFallback` into `extractors/fee.py`'s uni-PDF fallback branch.
- Port `review-engine.ts → resolveCandidates` into `services/review/conflicts.py`; thicken `/api/scrape/staged/:id/review` payload.
- Port `daily-backup.ts` as a Celery beat task (`tasks/snapshot_tasks.py`) running daily at 03:00 UTC.
- Port `/api/search/compare` (single GET, compose existing course-detail SELECTs).

### PR-2 "Feedback + acronym + taxonomy" (P2)
Closes diff items **J, K, M, P** + Section 1 items **#6, #7**.
- Port `feedback-engine.ts` → `services/feedback/engine.py`; call `apply_feedback_rules()` at `pipelines/single_course.py` start.
- Port `setDynamicAcronyms()` → `services/scraper/acronym_registry.py`; have `/api/settings/acronyms` POST/DELETE write to it.
- Port `validateTaxonomy()` into `services/scraper/category.py`; clamp output post-AI.
- Port the 19 academic-requirements helpers → `services/academic_requirements/parsers.py`; call from bulk-academic POST.
- Add `/api/scrape/staged/approve-all` and `/reject-all` (single SQL UPDATE per uni).

### PR-3 "Cleanup" (P3 / dead code)
- Decide: port `monthly-scraping.ts` or remove the UI hook.
- Decide: port `csu-campus-fallback.ts` or remove `isCsuCoursePage` references.
- Either delete dead `/api/scraping/*` Node routes or leave them in Node-only until Node is retired.
- Tighten `per_course_vision.py` decorative-filter (defect A): allow `<img>` ≥ 200×200 even with `class*=icon`.

After PR-1 ships, run a fresh ASA scrape and re-execute the **per-defect table** in Section 3 against prod — at that point we should be **>97 % parity** on staged rows. PR-2 closes the remaining 3 % and the silent-data-loss bugs (J, K, M).

---

## How to verify on prod

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

If prod's numbers match local (`18 | 18 | 17 | 18 | 99.4`) the data-parity work landed correctly. If they're lower, the prod box hasn't pulled `9134597` yet — check `git log -1` on the prod repo and `pip install -r requirements.txt && systemctl restart fastapi celery`.

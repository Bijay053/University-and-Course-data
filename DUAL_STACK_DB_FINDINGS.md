# Dual-Stack DB Findings (Drizzle + Python ORM)

Investigation result for task #26 ("Drop unused Node-era backup tables so the
database can sync without warnings"). Task was **cancelled** — the premise
(that these tables are unused leftovers) is wrong.

## Tables Drizzle wants to drop

Every `pnpm --filter db push` (run by `scripts/post-merge.sh`) prompts to
drop the following because they are not declared in `lib/db/src/schema/`:

| Object                            | Kind             | Rows (at investigation time) |
| --------------------------------- | ---------------- | ---------------------------- |
| `courses_backup`                  | table            | 4056 |
| `fees_backup`                     | table            | 1668 |
| `intakes_backup`                  | table            | 5660 |
| `english_requirements_backup`     | table            | 5656 |
| `academic_requirements_backup`    | table            | 1246 |
| `scholarships_backup`             | table            | 92 |
| `academic_level_options`          | table            | 9 |
| `course_search_view`              | materialized view | 1014 |

## Who actually owns them in the Python backend

All eight objects are actively read or written by `backend-py/`. Dropping
them would break the listed surfaces:

### `*_backup` tables (six tables)
- **Writer:** `backend-py/app/tasks/snapshot_tasks.py` — Celery beat task
  `snapshot-editable-tables-daily`, scheduled at 03:00 UTC in
  `backend-py/app/tasks/celery_app.py`. Inserts a fresh snapshot row per
  source table on every run.
- **Reader (status UI):** `backend-py/app/routers/backup.py` — mounted at
  `/api/backup`. Returns per-table row counts, last-snapshot time, and a
  30-day snapshot history for the React backup page
  (`artifacts/university-portal/src/pages/backup.tsx`).
- **Reader (restore flow):** `backend-py/app/routers/scrape.py` — the
  `/api/scrape/staged/{sc_id}/backup-match` and `apply-backup` endpoints
  (functions `_backup_table_exists`, `staged_backup_match`,
  `_apply_backup_one`, ~lines 1600–1790). Powers the "restore from latest
  backup" action on the staged-courses review UI.

### `course_search_view` (materialized view)
- **Reader:** `backend-py/app/routers/search.py` — the entire `/api/search`
  endpoint. Every list/count/by-id query selects `FROM course_search_view`
  (e.g. lines 209, 214, 361). Without this MV the main course-search page
  is broken.
- The MV is **created** in `artifacts/api-server/src/services/search-index.ts`
  (Node-side bootstrap), and refreshed by Drizzle migrations
  `lib/db/migrations/0001_*.sql` and `0003_*.sql`. The Python side only
  reads it; it does not own its DDL.

### `academic_level_options` (table)
- **Model:** `backend-py/app/models/academic_level_option.py`
  (`AcademicLevelOption`), re-exported from `backend-py/app/models/__init__.py`.
- **Reader/Writer:** `backend-py/app/routers/acronyms.py` — full CRUD under
  `/api/settings` (list, insert with `ON CONFLICT`, update, delete, reorder).
  Also seeds defaults if the table is empty.
- Touched by `backend-py/tests/test_route_parity.py`.

## Why this happens — the dual-stack confusion

The DB has **two ORM-shaped views of itself** and they disagree about which
tables exist:

1. **Drizzle (`lib/db/src/schema/`)** is the historical source of truth from
   the Node era. `drizzle-kit push` diffs the live DB against this schema
   and treats anything not declared as an "extra" to drop. The `*_backup`
   tables, `course_search_view`, and `academic_level_options` were all
   added later by the Python side and were never back-ported into the
   Drizzle schema.

2. **Python SQLAlchemy (`backend-py/app/models/`)** is the live runtime.
   It declares whatever it needs as needed (`AcademicLevelOption` is a
   declared model; the `*_backup` tables and `course_search_view` are
   touched via raw `text()` SQL). It never runs DDL through Alembic for
   these objects — `snapshot_tasks.py` self-heals with
   `CREATE TABLE IF NOT EXISTS`, the MV is created by the Node bootstrap
   service, and Drizzle migrations refresh it.

Because Drizzle's schema is incomplete relative to what Python actually uses,
every `drizzle-kit push` after a task merge sees these objects as orphans
and prompts to drop them. Saying "yes" would silently delete data and
break five Python surfaces (backup status, daily snapshot, scrape
restore-match, search, settings). Saying "no" leaves `.git/index.lock`
behind, which has bitten at least one prior task (#25).

## What is **not** the right fix

- **Drop the tables** (literal task #26): destroys actively-used data and
  breaks five routers + the daily snapshot.
- **`drizzle-kit push --force`**: silently drops on every merge — same
  outcome as the above, just non-interactive.
- **Remove `drizzle-kit push` from `scripts/post-merge.sh`**: hides the
  drift instead of resolving it; the two schemas stay out of sync forever.

## What the right fix looks like (future PR, not now)

Pick one and commit to it as an architectural decision:

- **Option A — declare the Python-owned objects in the Drizzle schema** as
  read-only definitions (six `*_backup` tables, `academic_level_options`,
  and a `course_search_view` view declaration). Lowest churn; keeps both
  ORMs but makes Drizzle stop complaining.
- **Option B — retire Drizzle entirely** now that Python is authoritative.
  Move all DDL into Alembic (or accept the existing
  `CREATE-IF-NOT-EXISTS` self-healing pattern), delete `lib/db/`, and
  drop `drizzle-kit push` from `scripts/post-merge.sh`. Highest churn,
  cleanest end state.
- **Option C — generate the Drizzle schema from the SQLAlchemy models**
  (or vice-versa) so they cannot drift. Most complex; only worth it if
  both stacks are staying long-term.

This is a planning conversation, not a same-session refactor. Park until
after PR-5 / T007 land.

## Files of interest

- `scripts/post-merge.sh`
- `lib/db/drizzle.config.ts`
- `lib/db/src/schema/index.ts` (and siblings)
- `lib/db/migrations/0001_clean_course_names_and_locations.sql`,
  `0003_university_metadata_backfill.sql` (refresh `course_search_view`)
- `backend-py/app/models/academic_level_option.py`
- `backend-py/app/routers/backup.py`
- `backend-py/app/routers/scrape.py` (lines ~1600–1790)
- `backend-py/app/routers/search.py`
- `backend-py/app/routers/acronyms.py`
- `backend-py/app/tasks/snapshot_tasks.py`
- `backend-py/app/tasks/celery_app.py`
- `artifacts/api-server/src/services/search-index.ts` (MV bootstrap, Node-side)
- `artifacts/api-server/src/services/daily-backup.ts` (Node-era backup writer
  — Python's `snapshot_tasks.py` is the cutover replacement)

/**
 * Loads operator-managed acronyms from the `course_acronym_options` table
 * into the in-memory cache used by `normalizeCourseNameCasing`.
 *
 * The normalizer itself is sync (it's called from many hot paths inside
 * the scrape pipeline) so we don't want to await a DB call on every word.
 * Instead we prime an in-memory cache:
 *   - on demand from `primeAcronymCache()` (called at the start of each
 *     scrape job; cache is reused for `CACHE_TTL_MS` before re-querying)
 *   - immediately after operators add/remove an acronym via the admin UI
 *     (the route calls `primeAcronymCache(true)` to force-reload).
 *
 * If the DB isn't reachable we leave whatever was last loaded in place
 * (or fall through to DEFAULT_ACRONYMS on a cold cache).  The point of
 * this feature is to be additive — a flaky DB must never break course
 * name capitalization.
 */
import { pool } from "@workspace/db";
import { setDynamicAcronyms } from "./course-name-normalizer.js";

const CACHE_TTL_MS = 60_000;
let lastLoadedAt = 0;
let tableEnsured = false;
let inflight: Promise<void> | null = null;

export const ACRONYMS_TABLE_DDL = `
  CREATE TABLE IF NOT EXISTS course_acronym_options (
    id SERIAL PRIMARY KEY,
    acronym TEXT NOT NULL UNIQUE,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  )
`;

export async function ensureAcronymsTable(): Promise<void> {
  if (tableEnsured) return;
  await pool.query(ACRONYMS_TABLE_DDL);
  await pool.query(
    `CREATE INDEX IF NOT EXISTS course_acronym_options_acronym_idx ON course_acronym_options (acronym)`,
  );
  tableEnsured = true;
}

async function loadFromDb(): Promise<void> {
  try {
    await ensureAcronymsTable();
    const { rows } = await pool.query<{ acronym: string }>(
      `SELECT acronym FROM course_acronym_options`,
    );
    setDynamicAcronyms(rows.map((r) => r.acronym));
    lastLoadedAt = Date.now();
  } catch (err) {
    // Best-effort: keep whatever's already in the cache.  Log but don't throw.
    console.warn("[acronym-cache] failed to load custom acronyms", (err as Error).message);
  }
}

/**
 * Make sure the dynamic acronym list is loaded.  Cheap to call on every
 * request — only hits the DB once per `CACHE_TTL_MS` unless `force` is set.
 */
export async function primeAcronymCache(force = false): Promise<void> {
  if (!force && Date.now() - lastLoadedAt < CACHE_TTL_MS && lastLoadedAt > 0) return;
  if (inflight) {
    await inflight;
    return;
  }
  inflight = loadFromDb().finally(() => { inflight = null; });
  await inflight;
}

/** For tests: reset the TTL gate so the next call re-queries the DB. */
export function resetAcronymCacheForTests(): void {
  lastLoadedAt = 0;
  tableEnsured = false;
  inflight = null;
}

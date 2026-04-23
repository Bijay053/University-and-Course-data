import { pool } from "@workspace/db";
import { logger } from "../lib/logger";

let ensured = false;
let ensuringPromise: Promise<void> | null = null;

/**
 * Self-healing search infrastructure for the public Course Search API.
 *
 * Creates (idempotently):
 *   - pg_trgm extension (for fast ILIKE-style fuzzy search)
 *   - indexes on every filterable column
 *   - course_search_view materialized view (denormalized join of
 *     courses + universities + fees + english_requirements + intakes)
 *   - unique index on the MV (required for REFRESH CONCURRENTLY)
 *   - GIN/BTREE indexes on the MV for fast filter + search
 *
 * Safe to call repeatedly — uses IF NOT EXISTS everywhere.
 */
export async function ensureSearchInfra(): Promise<void> {
  if (ensured) return;
  if (ensuringPromise) return ensuringPromise;

  ensuringPromise = (async () => {
    const t0 = Date.now();
    const client = await pool.connect();
    try {
      // Trigram extension is needed for the gin_trgm_ops indexes used by ILIKE
      // and similarity()/word_similarity(). fuzzystrmatch provides
      // levenshtein() used for the "did you mean" suggestion. Wrap in
      // try/catch — on managed Postgres without superuser the extension may
      // be pre-installed, and CREATE EXTENSION can fail silently.
      try { await client.query("CREATE EXTENSION IF NOT EXISTS pg_trgm"); }
      catch (err) { logger.warn({ err: (err as Error).message }, "search-index: pg_trgm extension creation skipped"); }
      try { await client.query("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch"); }
      catch (err) { logger.warn({ err: (err as Error).message }, "search-index: fuzzystrmatch extension creation skipped"); }

      // Indexes on base tables.
      const indexStatements = [
        // Trigram index on course name for fast ILIKE.
        `CREATE INDEX IF NOT EXISTS idx_courses_name_trgm ON courses USING gin (name gin_trgm_ops)`,
        // Filterable columns.
        `CREATE INDEX IF NOT EXISTS idx_courses_university_id ON courses(university_id)`,
        `CREATE INDEX IF NOT EXISTS idx_courses_degree_level ON courses(degree_level)`,
        `CREATE INDEX IF NOT EXISTS idx_courses_category ON courses(category)`,
        `CREATE INDEX IF NOT EXISTS idx_courses_sub_category ON courses(sub_category)`,
        `CREATE INDEX IF NOT EXISTS idx_courses_duration ON courses(duration)`,
        `CREATE INDEX IF NOT EXISTS idx_courses_status ON courses(status)`,
        // Join-friendly indexes on related tables.
        `CREATE INDEX IF NOT EXISTS idx_fees_course_id ON fees(course_id)`,
        `CREATE INDEX IF NOT EXISTS idx_fees_intl_amount ON fees(international_fee)`,
        `CREATE INDEX IF NOT EXISTS idx_intakes_course_id ON intakes(course_id)`,
        `CREATE INDEX IF NOT EXISTS idx_english_req_course_id ON english_requirements(course_id)`,
        `CREATE INDEX IF NOT EXISTS idx_english_req_test_type ON english_requirements(test_type)`,
        // Trigram on university name for the joint q-search.
        `CREATE INDEX IF NOT EXISTS idx_universities_name_trgm ON universities USING gin (name gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_universities_city ON universities(city)`,
        `CREATE INDEX IF NOT EXISTS idx_universities_country ON universities(country)`,
        // Compound index for common filters.
        `CREATE INDEX IF NOT EXISTS idx_courses_university_level ON courses(university_id, degree_level)`,
      ];
      for (const stmt of indexStatements) {
        try { await client.query(stmt); }
        catch (err) { logger.warn({ stmt, err: (err as Error).message }, "search-index: index creation skipped"); }
      }

      // Denormalized materialized view. Designed around the actual schema:
      //   - english_requirements is per-test-type rows -> pivot via correlated subqueries
      //   - intakes is one row per month -> array_agg
      //   - fees may have multiple rows per course -> take latest international_fee
      //
      // The "approved + active" gate ensures only published courses appear in
      // public search. We coalesce status/approval_status because legacy rows
      // may have NULLs.
      // Schema version. Bump whenever the MV definition changes — the block
      // below drops the old view and rebuilds it. Using a marker column
      // (search_tsv) keeps the upgrade path automatic on every deploy.
      const mvHasSearchTsv = await client.query(`
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'course_search_view'
           AND column_name = 'search_tsv'
        LIMIT 1
      `);
      if (mvHasSearchTsv.rowCount === 0) {
        logger.info("search-index: dropping legacy MV (missing search_tsv)");
        try { await client.query("DROP MATERIALIZED VIEW IF EXISTS course_search_view CASCADE"); }
        catch (err) { logger.warn({ err: (err as Error).message }, "search-index: drop MV failed"); }
      }

      const mvExists = await client.query(
        "SELECT 1 FROM pg_matviews WHERE matviewname = 'course_search_view'",
      );
      if (mvExists.rowCount === 0) {
        logger.info("search-index: creating course_search_view materialized view");
        await client.query(`
          CREATE MATERIALIZED VIEW course_search_view AS
          SELECT
            c.id,
            c.name AS course_name,
            c.category,
            c.sub_category,
            c.degree_level,
            c.duration,
            c.duration_term,
            c.study_mode,
            c.course_website,
            c.course_location,
            c.university_id,
            -- Legacy simple-dictionary tsvector kept for any older code paths.
            setweight(to_tsvector('simple', coalesce(c.name, '')), 'A') ||
              setweight(to_tsvector('simple', coalesce(u.name, '')), 'B') AS name_tsv,
            -- Rich English-stemmed tsvector for fuzzy / typo-tolerant search.
            -- Weights:
            --   A: course name           (highest signal)
            --   B: university name       (still strong)
            --   C: category + location   (medium signal)
            --   D: sub-category          (weakest, fallback)
            -- The English dictionary stems "nursing" / "nurses" / "nurse" to
            -- the same lexeme, so word variations match without code-side
            -- normalisation. Stopwords ("of", "in", "the"...) are dropped by
            -- the dictionary too, so "Bachelor of Nursing" and
            -- "Bachelor in Nursing" produce identical lexeme sets.
            (
              setweight(to_tsvector('english', coalesce(c.name, '')), 'A') ||
              setweight(to_tsvector('english', coalesce(u.name, '')), 'B') ||
              setweight(to_tsvector('english', coalesce(c.category, '')), 'C') ||
              setweight(to_tsvector('english', coalesce(c.course_location, '')), 'C') ||
              setweight(to_tsvector('english', coalesce(u.city, '')), 'C') ||
              setweight(to_tsvector('english', coalesce(u.country, '')), 'C') ||
              setweight(to_tsvector('english', coalesce(c.sub_category, '')), 'D')
            ) AS search_tsv,
            u.name AS university_name,
            u.logo_url,
            u.city AS university_city,
            u.country AS university_country,
            u.website AS university_website,
            -- Latest fee row per course (best-effort: highest id wins).
            (SELECT international_fee FROM fees WHERE course_id = c.id ORDER BY id DESC LIMIT 1) AS international_fee,
            (SELECT currency FROM fees WHERE course_id = c.id ORDER BY id DESC LIMIT 1) AS currency,
            (SELECT fee_term FROM fees WHERE course_id = c.id ORDER BY id DESC LIMIT 1) AS fee_term,
            NULL::real AS application_fee, -- not yet in schema
            -- All intake months for the course.
            (SELECT array_agg(DISTINCT intake_month ORDER BY intake_month)
               FROM intakes WHERE course_id = c.id AND intake_month IS NOT NULL) AS intakes,
            -- Pivoted english requirements (overall scores).
            (SELECT overall FROM english_requirements WHERE course_id = c.id AND test_type = 'IELTS' LIMIT 1) AS ielts_overall,
            (SELECT overall FROM english_requirements WHERE course_id = c.id AND test_type = 'PTE'   LIMIT 1) AS pte_overall,
            (SELECT overall FROM english_requirements WHERE course_id = c.id AND test_type = 'TOEFL' LIMIT 1) AS toefl_overall,
            (SELECT overall FROM english_requirements WHERE course_id = c.id AND test_type IN ('CAE','Cambridge') LIMIT 1) AS cae_overall,
            (SELECT overall FROM english_requirements WHERE course_id = c.id AND test_type IN ('Duolingo','DET') LIMIT 1) AS duolingo_overall,
            -- Convert duration to "years" so we can range-filter regardless of unit.
            CASE
              WHEN c.duration IS NULL THEN NULL
              WHEN c.duration_term ILIKE 'month%' THEN c.duration / 12.0
              WHEN c.duration_term ILIKE 'week%'  THEN c.duration / 52.0
              ELSE c.duration -- assume years (Year/Years/Yr/null)
            END AS duration_years
          FROM courses c
          JOIN universities u ON u.id = c.university_id
          WHERE coalesce(c.status, 'active') = 'active'
            AND coalesce(c.approval_status, 'approved') = 'approved'
        `);
        logger.info("search-index: materialized view created");
      }

      // Indexes on the materialized view. UNIQUE index on id is REQUIRED for
      // REFRESH MATERIALIZED VIEW CONCURRENTLY (which avoids locking readers).
      const mvIndexStatements = [
        `CREATE UNIQUE INDEX IF NOT EXISTS idx_csv_id ON course_search_view (id)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_name_trgm ON course_search_view USING gin (course_name gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_uni_name_trgm ON course_search_view USING gin (university_name gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_name_tsv ON course_search_view USING gin (name_tsv)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_search_tsv ON course_search_view USING gin (search_tsv)`,
        // Trigram indexes for typo-tolerant city / location matching.
        `CREATE INDEX IF NOT EXISTS idx_csv_city_trgm ON course_search_view USING gin (lower(university_city) gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_country_trgm ON course_search_view USING gin (lower(university_country) gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_loc_trgm ON course_search_view USING gin (lower(coalesce(course_location, '')) gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_course_name_lower_trgm ON course_search_view USING gin (lower(course_name) gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_uni_name_lower_trgm ON course_search_view USING gin (lower(university_name) gin_trgm_ops)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_university_id ON course_search_view (university_id)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_degree_level ON course_search_view (degree_level)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_category ON course_search_view (category)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_intl_fee ON course_search_view (international_fee)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_duration_years ON course_search_view (duration_years)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_country ON course_search_view (university_country)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_city ON course_search_view (university_city)`,
        `CREATE INDEX IF NOT EXISTS idx_csv_intakes ON course_search_view USING gin (intakes)`,
      ];
      for (const stmt of mvIndexStatements) {
        try { await client.query(stmt); }
        catch (err) { logger.warn({ stmt, err: (err as Error).message }, "search-index: MV index creation skipped"); }
      }

      ensured = true;
      logger.info({ ms: Date.now() - t0 }, "search-index: ready");
    } finally {
      client.release();
      ensuringPromise = null;
    }
  })();

  return ensuringPromise;
}

/**
 * Refresh the materialized view. Called after course approval and any other
 * write that affects published course data. Uses CONCURRENTLY so search
 * traffic is not blocked. Coalesces concurrent calls so a burst of approvals
 * results in a single (or at most a few) refreshes instead of N.
 */
let refreshInflight: Promise<void> | null = null;
let refreshPending = false;

export function refreshCourseSearchView(): Promise<void> {
  if (refreshInflight) {
    refreshPending = true;
    return refreshInflight;
  }
  refreshInflight = (async () => {
    try {
      do {
        refreshPending = false;
        const t0 = Date.now();
        try {
          await pool.query("REFRESH MATERIALIZED VIEW CONCURRENTLY course_search_view");
          logger.info({ ms: Date.now() - t0 }, "search-index: refreshed (concurrently)");
        } catch (err) {
          // CONCURRENTLY can fail if MV is empty / not yet populated. Fallback.
          logger.warn({ err: (err as Error).message }, "search-index: concurrent refresh failed, falling back");
          try {
            await pool.query("REFRESH MATERIALIZED VIEW course_search_view");
            logger.info({ ms: Date.now() - t0 }, "search-index: refreshed (blocking)");
          } catch (err2) {
            logger.error({ err: (err2 as Error).message }, "search-index: refresh failed");
          }
        }
      } while (refreshPending);
    } finally {
      refreshInflight = null;
    }
  })();
  return refreshInflight;
}

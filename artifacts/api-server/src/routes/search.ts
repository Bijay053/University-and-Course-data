import { Router, type IRouter, type Request, type Response } from "express";
import { pool } from "@workspace/db";
import { ensureSearchInfra } from "../services/search-index";
import { logger } from "../lib/logger";

const router: IRouter = Router();

// Ensure the search infrastructure (indexes + materialized view) is built
// before the first search hits. We kick this off lazily on the first request
// so a fresh deploy doesn't need to wait for a 30s startup migration.
let warmupStarted = false;
function warmup(): Promise<void> {
  if (!warmupStarted) {
    warmupStarted = true;
    void ensureSearchInfra().catch((err) => {
      warmupStarted = false; // allow retry
      logger.error({ err: (err as Error).message }, "search-index warmup failed");
    });
  }
  return ensureSearchInfra();
}

/* ─────────────────── Helpers ─────────────────── */

type SqlBuilder = {
  parts: string[];
  values: unknown[];
  add: (clause: string, ...vals: unknown[]) => void;
};

/** Tiny WHERE-clause builder that auto-numbers $N placeholders. */
function sqlWhere(): SqlBuilder {
  const b: SqlBuilder = {
    parts: [],
    values: [],
    add(clause, ...vals) {
      // Replace each "?" in clause with the correct $N.
      let i = 0;
      const replaced = clause.replace(/\?/g, () => {
        b.values.push(vals[i++]);
        return `$${b.values.length}`;
      });
      b.parts.push(replaced);
    },
  };
  return b;
}

function whereSql(b: SqlBuilder): string {
  return b.parts.length ? `WHERE ${b.parts.join(" AND ")}` : "";
}

/**
 * Wraps the materialized view in a subquery that adds an
 * `international_fee_yearly` column normalised to AUD-per-year. This lets
 * the Tuition Fee filter and the fee_asc/fee_desc sorts behave consistently
 * regardless of how the source data expresses the term:
 *   - "Year" / "Annual" / NULL  → as-is
 *   - "Trimester"               → ×3 (3 trimesters per academic year)
 *   - "Full Course" / "Total"   → ÷ duration_years (when duration is known)
 *
 * The alias is kept as `course_search_view` so all existing column references
 * in the calling SQL continue to compile without changes.
 */
const FEE_YEARLY_SQL = `
  CASE
    WHEN international_fee IS NULL THEN NULL
    WHEN fee_term ILIKE 'full%' OR fee_term ILIKE 'total%'
      THEN CASE
        WHEN duration_years IS NOT NULL AND duration_years > 0
          THEN international_fee / duration_years
        ELSE international_fee
      END
    WHEN fee_term ILIKE 'trimester%' THEN international_fee * 3
    ELSE international_fee
  END
`;
const COURSE_SEARCH_VIEW = `(
  SELECT csv.*, ${FEE_YEARLY_SQL} AS international_fee_yearly
  FROM course_search_view csv
) course_search_view`;

/** Parse a comma-separated query param into a trimmed string array. */
function csvList(v: unknown): string[] | null {
  if (typeof v !== "string" || !v.trim()) return null;
  const arr = v.split(",").map((s) => s.trim()).filter(Boolean);
  return arr.length ? arr : null;
}

function num(v: unknown): number | null {
  if (v === undefined || v === null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function int(v: unknown, def: number, min: number, max: number): number {
  const n = Number(v);
  if (!Number.isFinite(n)) return def;
  return Math.max(min, Math.min(max, Math.trunc(n)));
}

/** Map known english_exam values to MV column names (overall pivot). */
const ENGLISH_EXAM_COLS: Record<string, string> = {
  IELTS: "ielts_overall",
  PTE: "pte_overall",
  TOEFL: "toefl_overall",
  CAE: "cae_overall",
  CAMBRIDGE: "cae_overall",
  DET: "duolingo_overall",
  DUOLINGO: "duolingo_overall",
};

/** Map UI exam name to actual english_requirements.test_type values. */
const ENGLISH_EXAM_TEST_TYPES: Record<string, string[]> = {
  IELTS: ["IELTS"],
  PTE: ["PTE"],
  TOEFL: ["TOEFL"],
  CAE: ["CAE", "Cambridge", "Cambridge CAE"],
  CAMBRIDGE: ["CAE", "Cambridge", "Cambridge CAE"],
  DET: ["Duolingo", "DET"],
  DUOLINGO: ["Duolingo", "DET"],
};

/**
 * City / country aliases for the location filter. Lets users type "syd" or
 * "nsw" and still match Sydney rows, etc. Keys must be lower-case.
 */
const LOCATION_ALIASES: Record<string, string[]> = {
  sydney: ["sydney", "syd", "nsw", "new south wales"],
  melbourne: ["melbourne", "melb", "vic", "victoria"],
  brisbane: ["brisbane", "bris", "qld", "queensland"],
  perth: ["perth", "wa", "western australia"],
  adelaide: ["adelaide", "sa", "south australia"],
  canberra: ["canberra", "act"],
  hobart: ["hobart", "tas", "tasmania"],
  darwin: ["darwin", "nt", "northern territory"],
  syd: ["sydney"],
  melb: ["melbourne"],
  bris: ["brisbane"],
  nsw: ["sydney", "new south wales"],
  vic: ["melbourne", "victoria"],
  qld: ["brisbane", "queensland"],
  wa: ["perth", "western australia"],
  sa: ["adelaide", "south australia"],
  act: ["canberra"],
  tas: ["hobart", "tasmania"],
  nt: ["darwin", "northern territory"],
  aussie: ["australia"],
  oz: ["australia"],
  uk: ["united kingdom", "england"],
  us: ["united states", "usa"],
  usa: ["united states"],
};

/** Expand a user-typed location into the set of strings to fuzzy-match. */
function expandLocation(loc: string): string[] {
  const key = loc.toLowerCase().trim();
  const aliases = LOCATION_ALIASES[key];
  return aliases ? Array.from(new Set([key, ...aliases])) : [key];
}

/** Build the WHERE for /search/courses from request query params. */
function buildSearchWhere(q: Request["query"]): SqlBuilder {
  const b = sqlWhere();

  const term = typeof q.q === "string" ? q.q.trim() : "";
  if (term) {
    // Combined fuzzy match: full-text (handles word variations and stopwords
    // via the English dictionary) OR trigram similarity (handles typos and
    // partial matches) OR ILIKE substring (handles 1–3 char queries like
    // "it" / "ai" that fall below the trigram threshold and get dropped by
    // the English-dictionary stopword list).
    b.add(
      `(
        search_tsv @@ plainto_tsquery('english', ?)
        OR lower(course_name) % lower(?)
        OR lower(university_name) % lower(?)
        OR lower(coalesce(course_location, '')) % lower(?)
        OR lower(coalesce(university_city, '')) % lower(?)
        OR course_name ILIKE ?
        OR university_name ILIKE ?
      )`,
      term, term, term, term, term, `%${term}%`, `%${term}%`,
    );
  }

  const location = typeof q.location === "string" ? q.location.trim() : "";
  if (location) {
    // Expand "syd" → ["sydney","syd","nsw","new south wales"], then run a
    // word_similarity check against city / country / per-course location.
    //
    // Why word_similarity and not the `%` operator?  Many rows store a CSV
    // of cities (e.g. "Sydney, Melbourne, Adelaide, Brisbane, Online").
    // similarity('sydney', 'sydney, melbourne, …') is ~0.17 — below the 0.3
    // pg_trgm threshold — so plain `%` misses them.  word_similarity scans
    // the *best matching word* inside the longer string, so 'sydney' scores
    // 1.0 and 'sidney' scores 0.43, both clearing 0.3.  Threshold is
    // injected literally to keep the query sargable / index-friendly.
    const candidates = expandLocation(location);
    const conds: string[] = [];
    const vals: unknown[] = [];
    for (const c of candidates) {
      conds.push(`word_similarity(lower(?), lower(university_city)) > 0.3`); vals.push(c);
      conds.push(`word_similarity(lower(?), lower(university_country)) > 0.3`); vals.push(c);
      conds.push(`word_similarity(lower(?), lower(coalesce(course_location, ''))) > 0.3`); vals.push(c);
      // ILIKE fallback for very short strings ("oz", "act") that even
      // word_similarity drops below 0.3.
      conds.push(`university_city ILIKE ?`); vals.push(`%${c}%`);
      conds.push(`university_country ILIKE ?`); vals.push(`%${c}%`);
      conds.push(`coalesce(course_location, '') ILIKE ?`); vals.push(`%${c}%`);
    }
    b.add(`(${conds.join(" OR ")})`, ...vals);
  }

  const universityIds = csvList(q.university_id);
  if (universityIds) {
    const ints = universityIds.map(Number).filter((n) => Number.isFinite(n));
    if (ints.length) b.add(`university_id = ANY(?::int[])`, ints);
  }

  const degreeLevels = csvList(q.degree_level);
  if (degreeLevels) b.add(`degree_level = ANY(?::text[])`, degreeLevels);

  const categories = csvList(q.category);
  if (categories) b.add(`category = ANY(?::text[])`, categories);

  const subCats = csvList(q.sub_category);
  if (subCats) b.add(`sub_category = ANY(?::text[])`, subCats);

  const intakes = csvList(q.intakes);
  if (intakes) b.add(`intakes && ?::text[]`, intakes); // any-overlap

  const dMin = num(q.duration_years_min);
  const dMax = num(q.duration_years_max);
  if (dMin !== null) b.add(`duration_years >= ?`, dMin);
  if (dMax !== null) b.add(`duration_years <= ?`, dMax);

  const feeMin = num(q.fee_min);
  const feeMax = num(q.fee_max);
  if (feeMin !== null) b.add(`international_fee_yearly >= ?`, feeMin);
  if (feeMax !== null) b.add(`international_fee_yearly <= ?`, feeMax);

  const exam = typeof q.english_exam === "string" ? q.english_exam.trim().toUpperCase() : "";
  const examScore = num(q.english_score_min); // legacy: overall band
  const overall = num(q.english_overall) ?? examScore;
  const reading = num(q.english_reading);
  const writing = num(q.english_writing);
  const listening = num(q.english_listening);
  const speaking = num(q.english_speaking);
  const hasBand = overall != null || reading != null || writing != null || listening != null || speaking != null;
  if (exam && ENGLISH_EXAM_TEST_TYPES[exam]) {
    const testTypes = ENGLISH_EXAM_TEST_TYPES[exam];
    if (hasBand) {
      // Course must have a row for this test_type where every band the user
      // supplied meets the course's required band (course_required <= user_score).
      const conds: string[] = ["er.course_id = course_search_view.id", "er.test_type = ANY(?::text[])"];
      const vals: unknown[] = [testTypes];
      const addBand = (col: string, val: number | null) => {
        if (val == null) return;
        conds.push(`(er.${col} IS NULL OR er.${col} <= ?)`);
        vals.push(val);
      };
      addBand("overall", overall);
      addBand("reading", reading);
      addBand("writing", writing);
      addBand("listening", listening);
      addBand("speaking", speaking);
      b.add(
        `EXISTS (SELECT 1 FROM english_requirements er WHERE ${conds.join(" AND ")})`,
        ...vals,
      );
    } else if (ENGLISH_EXAM_COLS[exam]) {
      // No bands provided — just require the course offers this exam.
      b.add(`${ENGLISH_EXAM_COLS[exam]} IS NOT NULL`);
    }
  }

  // ── Academic requirements: country / qualification / grading scheme ──
  const country = typeof q.country_residence === "string" ? q.country_residence.trim() : "";
  const qual = typeof q.highest_qualification === "string" ? q.highest_qualification.trim() : "";
  const scheme = typeof q.grading_scheme === "string" ? q.grading_scheme.trim() : "";
  const outOf = typeof q.grading_out_of === "string" ? q.grading_out_of.trim() : "";
  const score = num(q.grading_score);
  if (country || qual || scheme || score != null) {
    const conds: string[] = ["ar.course_id = course_search_view.id"];
    const vals: unknown[] = [];
    // Treat NULLs as "matches any" — most academic_requirement rows in
    // the dataset only specify the level, leaving country / score_type /
    // score blank. Strict equality would exclude virtually everything.
    if (country) { conds.push("(ar.academic_country IS NULL OR ar.academic_country = ?)"); vals.push(country); }
    if (qual) { conds.push("(ar.academic_level IS NULL OR ar.academic_level = ?)"); vals.push(qual); }
    if (scheme && outOf) {
      conds.push("(ar.score_type IS NULL OR ar.score_type = ?)");
      vals.push(`${scheme}/${outOf}`);
    } else if (scheme) {
      conds.push("(ar.score_type IS NULL OR ar.score_type ILIKE ?)");
      vals.push(`${scheme}%`);
    }
    if (score != null) {
      conds.push("(ar.academic_score IS NULL OR ar.academic_score <= ?)");
      vals.push(score);
    }
    b.add(
      `EXISTS (SELECT 1 FROM academic_requirements ar WHERE ${conds.join(" AND ")})`,
      ...vals,
    );
  }

  // ── Other exam (e.g. GMAT, GRE) — ILIKE on courses.other_test ──
  const otherExam = typeof q.other_exam === "string" ? q.other_exam.trim() : "";
  if (otherExam) {
    b.add(
      `EXISTS (SELECT 1 FROM courses c2 WHERE c2.id = course_search_view.id AND c2.other_test ILIKE ?)`,
      `%${otherExam}%`,
    );
  }

  return b;
}

const ALLOWED_SORTS: Record<string, string> = {
  fee_asc: "international_fee ASC NULLS LAST, id DESC",
  fee_desc: "international_fee DESC NULLS LAST, id DESC",
  duration_asc: "duration_years ASC NULLS LAST, id DESC",
  name_asc: "course_name ASC, id DESC",
  relevance: "id DESC", // overridden if q present
};

/* ─────────────────── GET /api/search/stats ─────────────────── */

router.get("/search/stats", async (_req: Request, res: Response) => {
  try {
    const { rows } = await pool.query(`
      SELECT
        (SELECT COUNT(DISTINCT u.id)
           FROM universities u
          WHERE EXISTS (SELECT 1 FROM courses c WHERE c.university_id = u.id))::int
          AS universities_with_courses,
        (SELECT COUNT(*) FROM universities)::int AS total_universities,
        (SELECT COUNT(*) FROM courses)::int      AS total_courses
    `);
    res.json(rows[0] ?? { universities_with_courses: 0, total_universities: 0, total_courses: 0 });
  } catch (err) {
    logger.error({ err: (err as Error).message }, "search/stats failed");
    res.status(500).json({ error: "stats_failed" });
  }
});

router.get("/search/courses", async (req: Request, res: Response) => {
  const t0 = Date.now();
  try {
    await warmup();

    const where = buildSearchWhere(req.query);
    const whereClause = whereSql(where);

    const page = int(req.query.page, 1, 1, 10_000);
    const limit = int(req.query.limit, 20, 1, 100);
    const offset = (page - 1) * limit;

    let sortKey = typeof req.query.sort === "string" ? req.query.sort : "relevance";
    if (!ALLOWED_SORTS[sortKey]) sortKey = "relevance";
    const term = typeof req.query.q === "string" ? req.query.q.trim() : "";
    const location = typeof req.query.location === "string" ? req.query.location.trim() : "";

    // Build values array. When relevance + q, we append the term once. The
    // ordering combines FTS rank (higher = better) with trigram similarity
    // (also higher = better) so typo'd queries still rank meaningfully.
    const baseValues = [...where.values];
    let nextIdx = baseValues.length + 1;
    let innerOrder = ALLOWED_SORTS[sortKey];
    if (sortKey === "relevance" && term) {
      const i = nextIdx;
      innerOrder = `(
        ts_rank(search_tsv, plainto_tsquery('english', $${i}))
          + GREATEST(
              similarity(lower(course_name), lower($${i})),
              similarity(lower(university_name), lower($${i}))
            ) * 0.5
      ) DESC, id DESC`;
      baseValues.push(term);
      nextIdx++;
    }
    // Featured universities always rank first regardless of the user's chosen
    // sort. Within each group (featured / non-featured) the user's sort applies.
    const orderBy = `featured DESC, featured_priority DESC, ${innerOrder}`;
    const limitIdx = nextIdx;
    const offsetIdx = nextIdx + 1;

    const resultsSql = `
      SELECT
        id,
        course_name,
        category, sub_category, degree_level,
        duration, duration_term, duration_years,
        study_mode, course_website, course_location,
        university_id, university_name, logo_url,
        university_city, university_country, university_website,
        featured, featured_priority,
        international_fee, currency, fee_term, application_fee,
        international_fee_yearly,
        intakes,
        ielts_overall, pte_overall, toefl_overall, cae_overall, duolingo_overall
      FROM ${COURSE_SEARCH_VIEW}
      ${whereClause}
      ORDER BY ${orderBy}
      LIMIT $${limitIdx} OFFSET $${offsetIdx}
    `;

    const countSql = `SELECT COUNT(*)::int AS total FROM ${COURSE_SEARCH_VIEW} ${whereClause}`;

    // Facets — same WHERE, grouped by each dimension. Run in parallel.
    const facetsValues = where.values;
    const facetsParts = [
      pool.query(
        `SELECT university_id AS id, university_name AS name, COUNT(*)::int AS count
         FROM ${COURSE_SEARCH_VIEW} ${whereClause}
         GROUP BY university_id, university_name
         ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT category AS name, COUNT(*)::int AS count
         FROM ${COURSE_SEARCH_VIEW} ${whereClause} ${whereClause ? "AND" : "WHERE"} category IS NOT NULL
         GROUP BY category ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT degree_level AS name, COUNT(*)::int AS count
         FROM ${COURSE_SEARCH_VIEW} ${whereClause} ${whereClause ? "AND" : "WHERE"} degree_level IS NOT NULL
         GROUP BY degree_level ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT m AS name, COUNT(*)::int AS count
         FROM ${COURSE_SEARCH_VIEW}, UNNEST(coalesce(course_search_view.intakes, ARRAY[]::text[])) m
         ${whereClause}
         GROUP BY m ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
    ];

    const [resultsRes, countRes, uniFacet, catFacet, degFacet, intakeFacet] = await Promise.all([
      pool.query(resultsSql, [...baseValues, limit, offset]),
      pool.query(countSql, where.values),
      ...facetsParts,
    ]);

    const totalCount: number = countRes.rows[0]?.total ?? 0;

    // "Did you mean" — only computed when we got zero results.  Two flavours:
    //   • course-name suggestion (when q was supplied)
    //   • location suggestion   (when location was supplied)
    // Both ignore all other filters since those filters are the most likely
    // reason for the zero result.
    let didYouMean: string | null = null;
    let didYouMeanLocation: string | null = null;
    if (location && totalCount === 0) {
      try {
        // Score over the *distinct* set of locations rather than every row,
        // so cost stays O(unique cities + unique countries) instead of
        // O(courses).  At the current ~1k MV size this is negligible, but
        // the distinct set keeps it cheap as the dataset grows.
        const sugg = await pool.query(
          `WITH cand AS (
             SELECT DISTINCT university_country AS s FROM course_search_view
              WHERE university_country IS NOT NULL AND university_country <> ''
              UNION
             SELECT DISTINCT university_city FROM course_search_view
              WHERE university_city IS NOT NULL AND university_city <> 'Unknown'
           )
           SELECT s, word_similarity(lower($1), lower(s)) AS score
             FROM cand
            ORDER BY score DESC
            LIMIT 1`,
          [location],
        );
        if (sugg.rows.length && sugg.rows[0].score >= 0.3) {
          const suggested = sugg.rows[0].s as string;
          if (suggested && suggested.toLowerCase() !== location.toLowerCase()) {
            didYouMeanLocation = suggested;
          }
        }
      } catch (err) {
        logger.warn({ err: (err as Error).message }, "did-you-mean (location) lookup failed");
      }
    }
    if (term && totalCount === 0) {
      try {
        // word_similarity scans the *best matching word* inside course_name,
        // which catches typos like "nursng" → "Bachelor of Nursing" that the
        // whole-string % operator misses. Scoring all rows is acceptable
        // because the MV is small (~thousands of rows) and this only fires
        // on zero-result queries.
        const sugg = await pool.query(
          `SELECT course_name,
                  GREATEST(
                    similarity(lower(course_name), lower($1)),
                    word_similarity(lower($1), lower(course_name))
                  ) AS score
             FROM course_search_view
            ORDER BY score DESC
            LIMIT 1`,
          [term],
        );
        if (sugg.rows.length && sugg.rows[0].score >= 0.35) {
          const suggested = sugg.rows[0].course_name as string;
          // Don't suggest the exact same term back to the user.
          if (suggested && suggested.toLowerCase() !== term.toLowerCase()) {
            didYouMean = suggested;
          }
        }
      } catch (err) {
        logger.warn({ err: (err as Error).message }, "did-you-mean lookup failed");
      }
    }

    const elapsed = Date.now() - t0;
    if (elapsed > 300) {
      logger.warn({ ms: elapsed, query: req.query }, "[SLOW-SEARCH]");
    }

    res.json({
      total: totalCount,
      page,
      limit,
      took_ms: elapsed,
      did_you_mean: didYouMean,
      did_you_mean_location: didYouMeanLocation,
      results: resultsRes.rows.map((r) => ({
        id: r.id,
        course_name: r.course_name,
        university: {
          id: r.university_id,
          name: r.university_name,
          logo_url: r.logo_url,
          city: r.university_city,
          country: r.university_country,
          website: r.university_website,
          featured: !!r.featured,
          featured_priority: r.featured_priority ?? 0,
        },
        course_location: r.course_location,
        degree_level: r.degree_level,
        category: r.category,
        sub_category: r.sub_category,
        duration: r.duration,
        duration_term: r.duration_term,
        duration_years: r.duration_years,
        intakes: r.intakes ?? [],
        international_fee: r.international_fee,
        international_fee_yearly: r.international_fee_yearly == null ? null : Number(r.international_fee_yearly),
        currency: r.currency,
        fee_term: r.fee_term,
        application_fee: r.application_fee,
        english_requirements: {
          ielts_overall: r.ielts_overall,
          pte_overall: r.pte_overall,
          toefl_overall: r.toefl_overall,
          cae_overall: r.cae_overall,
          duolingo_overall: r.duolingo_overall,
        },
        course_url: r.course_website,
      })),
      facets: {
        universities: uniFacet.rows,
        categories: catFacet.rows,
        degree_levels: degFacet.rows,
        intakes: intakeFacet.rows,
      },
    });
  } catch (err) {
    logger.error({ err: (err as Error).message, stack: (err as Error).stack }, "search/courses failed");
    res.status(500).json({ error: "search_failed", message: (err as Error).message });
  }
});

/* ─────────────────── GET /api/courses/compare?ids=1,2,3 ─────────────────── */

// Note: defined under /search/compare (not /courses/compare) to avoid being
// shadowed by the existing /courses/:id route which would parse "compare"
// as an integer ID and 400 with a Zod validation error.
router.get("/search/compare", async (req: Request, res: Response) => {
  try {
    await warmup();
    const ids = csvList(req.query.ids);
    if (!ids || ids.length === 0) {
      return res.status(400).json({ error: "ids_required", message: "Provide ids=1,2,3 (max 5)" });
    }
    const intIds = ids.map(Number).filter((n) => Number.isInteger(n) && n > 0);
    if (intIds.length === 0) {
      return res.status(400).json({ error: "ids_invalid" });
    }
    if (intIds.length > 5) {
      return res.status(400).json({ error: "too_many_ids", message: "Compare supports at most 5 courses" });
    }

    // Pull the row from the MV (cheap), plus full english + academic reqs from
    // base tables (full detail, all test types).
    const [mvRes, engRes, acadRes] = await Promise.all([
      pool.query(
        `SELECT * FROM ${COURSE_SEARCH_VIEW} WHERE id = ANY($1::int[])`,
        [intIds],
      ),
      pool.query(
        `SELECT course_id, test_type, test_name, overall, listening, reading, writing, speaking
           FROM english_requirements WHERE course_id = ANY($1::int[])`,
        [intIds],
      ),
      pool.query(
        `SELECT course_id, academic_level, academic_score, score_type, academic_country
           FROM academic_requirements WHERE course_id = ANY($1::int[])`,
        [intIds],
      ),
    ]);

    const engByCourse = new Map<number, unknown[]>();
    for (const r of engRes.rows) {
      if (!engByCourse.has(r.course_id)) engByCourse.set(r.course_id, []);
      engByCourse.get(r.course_id)!.push(r);
    }
    const acadByCourse = new Map<number, unknown[]>();
    for (const r of acadRes.rows) {
      if (!acadByCourse.has(r.course_id)) acadByCourse.set(r.course_id, []);
      acadByCourse.get(r.course_id)!.push(r);
    }

    // Preserve request order.
    const byId = new Map(mvRes.rows.map((r) => [r.id, r]));
    const courses = intIds
      .map((id) => byId.get(id))
      .filter(Boolean)
      .map((r) => ({
        id: r.id,
        course_name: r.course_name,
        university: {
          id: r.university_id,
          name: r.university_name,
          logo_url: r.logo_url,
          city: r.university_city,
          country: r.university_country,
          website: r.university_website,
        },
        course_location: r.course_location,
        degree_level: r.degree_level,
        category: r.category,
        sub_category: r.sub_category,
        duration: r.duration,
        duration_term: r.duration_term,
        duration_years: r.duration_years,
        study_mode: r.study_mode,
        intakes: r.intakes ?? [],
        international_fee: r.international_fee,
        international_fee_yearly: r.international_fee_yearly == null ? null : Number(r.international_fee_yearly),
        currency: r.currency,
        fee_term: r.fee_term,
        application_fee: r.application_fee,
        course_url: r.course_website,
        english_requirements: engByCourse.get(r.id) ?? [],
        academic_requirements: acadByCourse.get(r.id) ?? [],
      }));

    res.json({ courses });
  } catch (err) {
    logger.error({ err: (err as Error).message }, "courses/compare failed");
    res.status(500).json({ error: "compare_failed", message: (err as Error).message });
  }
});

/* ─────────────────── GET /api/search/options ───────────────────
 * Returns the dropdown values for the Advanced Filter sidebar:
 *   - countries / qualifications / schemes pulled from
 *     academic_requirements distinct values
 *   - exams pulled from english_requirements distinct test_types
 *   - universities (id + name) for the university dropdown
 * Cached in-memory for 60s — these change infrequently.
 */
let optionsCache: { at: number; data: unknown } | null = null;
router.get("/search/options", async (_req: Request, res: Response) => {
  try {
    if (optionsCache && Date.now() - optionsCache.at < 60_000) {
      return res.json(optionsCache.data);
    }
    await warmup();
    const [countries, qualifications, schemes, exams, universities] = await Promise.all([
      pool.query(
        `SELECT DISTINCT academic_country AS v FROM academic_requirements
          WHERE academic_country IS NOT NULL AND academic_country <> '' ORDER BY 1`,
      ),
      pool.query(
        `SELECT DISTINCT academic_level AS v FROM academic_requirements
          WHERE academic_level IS NOT NULL AND academic_level <> '' ORDER BY 1`,
      ),
      pool.query(
        `SELECT DISTINCT score_type AS v FROM academic_requirements
          WHERE score_type IS NOT NULL AND score_type <> '' ORDER BY 1`,
      ),
      pool.query(
        `SELECT DISTINCT test_type AS v FROM english_requirements
          WHERE test_type IS NOT NULL AND test_type <> '' ORDER BY 1`,
      ),
      pool.query(
        `SELECT DISTINCT university_id AS id, university_name AS name
           FROM course_search_view ORDER BY university_name`,
      ),
    ]);

    // Decompose score_type "GPA/4" into (scheme=GPA, outOf=4) options.
    const schemeMap = new Map<string, Set<string>>();
    for (const r of schemes.rows) {
      const raw: string = r.v;
      const idx = raw.indexOf("/");
      const scheme = idx > 0 ? raw.slice(0, idx) : raw;
      const out = idx > 0 ? raw.slice(idx + 1) : "";
      if (!schemeMap.has(scheme)) schemeMap.set(scheme, new Set());
      if (out) schemeMap.get(scheme)!.add(out);
    }

    const data = {
      countries: countries.rows.map((r) => r.v),
      qualifications: qualifications.rows.map((r) => r.v),
      grading_schemes: Array.from(schemeMap.entries()).map(([scheme, outs]) => ({
        scheme,
        out_of: Array.from(outs).sort((a, b) => Number(a) - Number(b)),
      })),
      // Canonicalize to the uppercase keys used by ENGLISH_EXAM_TEST_TYPES so
      // that the frontend can submit the value verbatim and the backend
      // matcher will always recognize it. Drop any value that does not
      // canonicalize so we never return options the backend cannot filter on.
      english_exams: (() => {
        const out = new Set<string>();
        for (const r of exams.rows) {
          const v = String(r.v).trim();
          const u = v.toUpperCase();
          if (u === "IELTS") out.add("IELTS");
          else if (u === "PTE" || u.startsWith("PTE ")) out.add("PTE");
          else if (u === "TOEFL" || u.startsWith("TOEFL")) out.add("TOEFL");
          else if (u === "CAE" || u === "CAMBRIDGE" || u === "CAMBRIDGE CAE") out.add("CAE");
          else if (u === "DET" || u === "DUOLINGO") out.add("DET");
        }
        return Array.from(out);
      })(),
      universities: universities.rows.map((r) => ({ id: r.id, name: r.name })),
    };
    optionsCache = { at: Date.now(), data };
    res.json(data);
  } catch (err) {
    logger.error({ err: (err as Error).message }, "search/options failed");
    res.status(500).json({ error: "options_failed", message: (err as Error).message });
  }
});

export default router;

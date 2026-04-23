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

/** Map known english_exam values to MV column names. */
const ENGLISH_EXAM_COLS: Record<string, string> = {
  IELTS: "ielts_overall",
  PTE: "pte_overall",
  TOEFL: "toefl_overall",
  CAE: "cae_overall",
  CAMBRIDGE: "cae_overall",
  DET: "duolingo_overall",
  DUOLINGO: "duolingo_overall",
};

/** Build the WHERE for /search/courses from request query params. */
function buildSearchWhere(q: Request["query"]): SqlBuilder {
  const b = sqlWhere();

  const term = typeof q.q === "string" ? q.q.trim() : "";
  if (term) {
    // ILIKE on both course + university name. Trigram index handles speed.
    b.add("(course_name ILIKE ? OR university_name ILIKE ?)", `%${term}%`, `%${term}%`);
  }

  const location = typeof q.location === "string" ? q.location.trim() : "";
  if (location) {
    b.add("(university_city ILIKE ? OR university_country ILIKE ?)", `%${location}%`, `%${location}%`);
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
  if (feeMin !== null) b.add(`international_fee >= ?`, feeMin);
  if (feeMax !== null) b.add(`international_fee <= ?`, feeMax);

  const exam = typeof q.english_exam === "string" ? q.english_exam.trim().toUpperCase() : "";
  const examScore = num(q.english_score_min); // user's score
  if (exam && ENGLISH_EXAM_COLS[exam]) {
    const col = ENGLISH_EXAM_COLS[exam];
    if (examScore !== null) {
      // Course requirement must be met by the user's score.
      b.add(`(${col} IS NOT NULL AND ${col} <= ?)`, examScore);
    } else {
      b.add(`${col} IS NOT NULL`);
    }
  }

  // application_fee_max — column not in current schema; skipped silently.
  // country_residence / highest_qualification / grading_scheme / other_exam:
  // these would require joining academic_requirements with country/level
  // matching logic. Not part of Phase 1 — silently ignored if provided.

  return b;
}

const ALLOWED_SORTS: Record<string, string> = {
  fee_asc: "international_fee ASC NULLS LAST, id DESC",
  fee_desc: "international_fee DESC NULLS LAST, id DESC",
  duration_asc: "duration_years ASC NULLS LAST, id DESC",
  name_asc: "course_name ASC, id DESC",
  relevance: "id DESC", // overridden if q present
};

/* ─────────────────── GET /api/search/courses ─────────────────── */

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
    const orderBy = sortKey === "relevance" && term
      ? `similarity(course_name, $${where.values.length + 1}) DESC, id DESC`
      : ALLOWED_SORTS[sortKey];

    // Build values array. When relevance + q, we append the term once for
    // similarity(), then page/limit values come after.
    const baseValues = [...where.values];
    let nextIdx = baseValues.length + 1;
    if (sortKey === "relevance" && term) {
      baseValues.push(term);
      nextIdx++;
    }
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
        international_fee, currency, fee_term, application_fee,
        intakes,
        ielts_overall, pte_overall, toefl_overall, cae_overall, duolingo_overall
      FROM course_search_view
      ${whereClause}
      ORDER BY ${orderBy}
      LIMIT $${limitIdx} OFFSET $${offsetIdx}
    `;

    const countSql = `SELECT COUNT(*)::int AS total FROM course_search_view ${whereClause}`;

    // Facets — same WHERE, grouped by each dimension. Run in parallel.
    const facetsValues = where.values;
    const facetsParts = [
      pool.query(
        `SELECT university_id AS id, university_name AS name, COUNT(*)::int AS count
         FROM course_search_view ${whereClause}
         GROUP BY university_id, university_name
         ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT category AS name, COUNT(*)::int AS count
         FROM course_search_view ${whereClause} ${whereClause ? "AND" : "WHERE"} category IS NOT NULL
         GROUP BY category ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT degree_level AS name, COUNT(*)::int AS count
         FROM course_search_view ${whereClause} ${whereClause ? "AND" : "WHERE"} degree_level IS NOT NULL
         GROUP BY degree_level ORDER BY count DESC, name ASC LIMIT 50`,
        facetsValues,
      ),
      pool.query(
        `SELECT m AS name, COUNT(*)::int AS count
         FROM course_search_view csv, UNNEST(coalesce(csv.intakes, ARRAY[]::text[])) m
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

    const elapsed = Date.now() - t0;
    if (elapsed > 300) {
      logger.warn({ ms: elapsed, query: req.query }, "[SLOW-SEARCH]");
    }

    res.json({
      total: countRes.rows[0]?.total ?? 0,
      page,
      limit,
      took_ms: elapsed,
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
        `SELECT * FROM course_search_view WHERE id = ANY($1::int[])`,
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

export default router;

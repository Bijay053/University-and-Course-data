import { Router } from "express";
import { pool } from "@workspace/db";

const router = Router();

const BACKUP_TABLES = [
  { name: "courses_backup",                 source: "courses" },
  { name: "fees_backup",                    source: "fees" },
  { name: "intakes_backup",                 source: "intakes" },
  { name: "english_requirements_backup",    source: "english_requirements" },
  { name: "academic_requirements_backup",   source: "academic_requirements" },
  { name: "scholarships_backup",            source: "scholarships" },
] as const;

// ── GET /api/backup ── list snapshot history ────────────────────────────────
router.get("/backup", async (_req, res) => {
  try {
    const client = await pool.connect();
    try {
      const stats: Record<string, unknown>[] = [];

      for (const t of BACKUP_TABLES) {
        const countRes = await client.query(
          `SELECT COUNT(*) AS total FROM ${t.name}`
        );
        const lastRes = await client.query(
          `SELECT MAX(backed_up_at) AS last_backed_up FROM ${t.name}`
        );
        const snapshotsRes = await client.query(
          `SELECT DISTINCT backed_up_at::date AS snap_date,
                  COUNT(*) AS rows
           FROM ${t.name}
           GROUP BY backed_up_at::date
           ORDER BY snap_date DESC
           LIMIT 10`
        );
        stats.push({
          table: t.name,
          source: t.source,
          totalBackedUpRows: Number(countRes.rows[0].total),
          lastBackedUp: lastRes.rows[0].last_backed_up,
          snapshots: snapshotsRes.rows,
        });
      }

      res.json({ ok: true, backups: stats });
    } finally {
      client.release();
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    res.status(500).json({ ok: false, error: msg });
  }
});

// ── POST /api/backup ── take a new snapshot of all 6 tables ─────────────────
router.post("/backup", async (_req, res) => {
  const snapTime = new Date();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    const inserted: Record<string, number> = {};

    // courses
    const c = await client.query(`
      INSERT INTO courses_backup (
        backed_up_at, id, university_id, name, category, sub_category,
        course_website, course_location, duration, duration_term, study_mode,
        degree_level, study_load, language, description, course_structure,
        career_outcomes, other_test, other_test_score, other_requirement,
        student_market, delivery_mode, international_eligible, on_campus_available,
        eligibility_status, eligibility_reason, eligibility_confidence,
        approval_status, approval_score, approved_at, last_reviewed_at,
        status, created_at, updated_at
      )
      SELECT $1, id, university_id, name, category, sub_category,
        course_website, course_location, duration, duration_term, study_mode,
        degree_level, study_load, language, description, course_structure,
        career_outcomes, other_test, other_test_score, other_requirement,
        student_market, delivery_mode, international_eligible, on_campus_available,
        eligibility_status, eligibility_reason, eligibility_confidence,
        approval_status, approval_score, approved_at, last_reviewed_at,
        status, created_at, updated_at
      FROM courses
    `, [snapTime]);
    inserted.courses = c.rowCount ?? 0;

    // fees
    const f = await client.query(`
      INSERT INTO fees_backup (backed_up_at, id, course_id, international_fee, fee_term, fee_year, currency, created_at)
      SELECT $1, id, course_id, international_fee, fee_term, fee_year, currency, created_at FROM fees
    `, [snapTime]);
    inserted.fees = f.rowCount ?? 0;

    // intakes
    const i = await client.query(`
      INSERT INTO intakes_backup (backed_up_at, id, course_id, intake_month, intake_day, intake_year, is_open, created_at)
      SELECT $1, id, course_id, intake_month, intake_day, intake_year, is_open, created_at FROM intakes
    `, [snapTime]);
    inserted.intakes = i.rowCount ?? 0;

    // english requirements
    const e = await client.query(`
      INSERT INTO english_requirements_backup (backed_up_at, id, course_id, test_type, listening, speaking, writing, reading, overall, test_name, created_at)
      SELECT $1, id, course_id, test_type, listening, speaking, writing, reading, overall, test_name, created_at FROM english_requirements
    `, [snapTime]);
    inserted.english_requirements = e.rowCount ?? 0;

    // academic requirements
    const a = await client.query(`
      INSERT INTO academic_requirements_backup (backed_up_at, id, course_id, academic_level, academic_score, score_type, academic_country, created_at)
      SELECT $1, id, course_id, academic_level, academic_score, score_type, academic_country, created_at FROM academic_requirements
    `, [snapTime]);
    inserted.academic_requirements = a.rowCount ?? 0;

    // scholarships
    const s = await client.query(`
      INSERT INTO scholarships_backup (backed_up_at, id, course_id, name, details, eligibility_criteria, amount, currency, created_at)
      SELECT $1, id, course_id, name, details, eligibility_criteria, amount, currency, created_at FROM scholarships
    `, [snapTime]);
    inserted.scholarships = s.rowCount ?? 0;

    await client.query("COMMIT");
    res.json({ ok: true, backedUpAt: snapTime, inserted });
  } catch (err: unknown) {
    await client.query("ROLLBACK");
    const msg = err instanceof Error ? err.message : String(err);
    res.status(500).json({ ok: false, error: msg });
  } finally {
    client.release();
  }
});

export default router;

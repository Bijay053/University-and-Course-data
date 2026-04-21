/**
 * Backup Mapping Routes
 * Maps previously backed-up manual data back onto staged (scraped) courses
 * before they are approved into production.
 *
 * Matching strategy: exact course name match (case-insensitive, trimmed)
 * within the same university_id.
 */
import { Router } from "express";
import { pool } from "@workspace/db";

const router = Router();

// ── GET /api/scrape/staged/:id/backup-match ───────────────────────────────────
// Looks up the backup tables for a course that matches the staged course by
// course name + university_id.  Returns all backed-up fields grouped by table.
router.get("/scrape/staged/:id/backup-match", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isFinite(id)) { res.status(400).json({ error: "Invalid id" }); return; }

  const client = await pool.connect();
  try {
    // 1. Load the staged course
    const stagRes = await client.query(
      `SELECT id, university_id, course_name FROM scraped_courses WHERE id = $1`,
      [id]
    );
    if (stagRes.rows.length === 0) { res.status(404).json({ error: "Staged course not found" }); return; }
    const stag = stagRes.rows[0] as { id: number; university_id: number; course_name: string };

    // 2. Find a matching course in courses_backup (most recent snapshot first)
    const matchRes = await client.query(
      `SELECT * FROM courses_backup
       WHERE university_id = $1
         AND LOWER(TRIM(name)) = LOWER(TRIM($2))
       ORDER BY backed_up_at DESC
       LIMIT 1`,
      [stag.university_id, stag.course_name]
    );

    if (matchRes.rows.length === 0) {
      res.json({ matched: false, stagedCourseName: stag.course_name });
      return;
    }

    const courseBack = matchRes.rows[0] as Record<string, unknown>;
    const backedCourseId = courseBack.id as number;

    // 3. Load related backup tables for this course
    const [feesRes, intakesRes, englishRes, academicRes, schRes] = await Promise.all([
      client.query(
        `SELECT * FROM fees_backup WHERE course_id = $1 ORDER BY backed_up_at DESC LIMIT 1`,
        [backedCourseId]
      ),
      client.query(
        `SELECT * FROM intakes_backup WHERE course_id = $1 ORDER BY backed_up_at DESC`,
        [backedCourseId]
      ),
      client.query(
        `SELECT * FROM english_requirements_backup WHERE course_id = $1 ORDER BY backed_up_at DESC`,
        [backedCourseId]
      ),
      client.query(
        `SELECT * FROM academic_requirements_backup WHERE course_id = $1 ORDER BY backed_up_at DESC`,
        [backedCourseId]
      ),
      client.query(
        `SELECT * FROM scholarships_backup WHERE course_id = $1 ORDER BY backed_up_at DESC`,
        [backedCourseId]
      ),
    ]);

    res.json({
      matched: true,
      stagedCourseId: id,
      stagedCourseName: stag.course_name,
      backedUpAt: courseBack.backed_up_at,
      course: courseBack,
      fees: feesRes.rows[0] ?? null,
      intakes: intakesRes.rows,
      english: englishRes.rows,
      academic: academicRes.rows,
      scholarships: schRes.rows,
    });
  } finally {
    client.release();
  }
});

// ── POST /api/scrape/staged/:id/apply-backup ──────────────────────────────────
// Merges backed-up data into the staged course record.
// By default only fills fields that are NULL in the staged course
// (pass forceOverwrite:true to overwrite even non-null fields).
router.post("/scrape/staged/:id/apply-backup", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isFinite(id)) { res.status(400).json({ error: "Invalid id" }); return; }

  const { forceOverwrite = false } = req.body as { forceOverwrite?: boolean };

  const client = await pool.connect();
  try {
    await client.query("BEGIN");

    // Load staged course
    const stagRes = await client.query(
      `SELECT * FROM scraped_courses WHERE id = $1`,
      [id]
    );
    if (stagRes.rows.length === 0) {
      await client.query("ROLLBACK");
      res.status(404).json({ error: "Staged course not found" });
      return;
    }
    const stag = stagRes.rows[0] as Record<string, unknown>;

    // Find matching backup course
    const matchRes = await client.query(
      `SELECT * FROM courses_backup
       WHERE university_id = $1
         AND LOWER(TRIM(name)) = LOWER(TRIM($2))
       ORDER BY backed_up_at DESC LIMIT 1`,
      [stag.university_id, stag.course_name]
    );
    if (matchRes.rows.length === 0) {
      await client.query("ROLLBACK");
      res.status(404).json({ error: "No backup match found for this course name + university" });
      return;
    }
    const cb = matchRes.rows[0] as Record<string, unknown>;
    const backedCourseId = cb.id as number;

    // Helper: pick value only if staged field is null (unless forceOverwrite)
    const pick = (backupVal: unknown, stagedVal: unknown) =>
      forceOverwrite ? backupVal : (stagedVal ?? backupVal);

    // Build updates for scraped_courses from courses_backup
    const updates: Record<string, unknown> = {
      duration:       pick(cb.duration,       stag.duration),
      duration_term:  pick(cb.duration_term,  stag.duration_term),
      study_mode:     pick(cb.study_mode,     stag.study_mode),
      course_location: pick(cb.course_location, stag.course_location),
    };

    // Merge fees
    const feesRes = await client.query(
      `SELECT * FROM fees_backup WHERE course_id = $1 ORDER BY backed_up_at DESC LIMIT 1`,
      [backedCourseId]
    );
    if (feesRes.rows.length > 0) {
      const fb = feesRes.rows[0] as Record<string, unknown>;
      updates.international_fee = pick(fb.international_fee, stag.international_fee);
      updates.fee_term          = pick(fb.fee_term,          stag.fee_term);
      updates.fee_year          = pick(fb.fee_year,          stag.fee_year);
      updates.currency          = pick(fb.currency,          stag.currency);
    }

    // Merge intakes → build intake_months JSON array
    const intakesRes = await client.query(
      `SELECT intake_month FROM intakes_backup WHERE course_id = $1 ORDER BY backed_up_at DESC`,
      [backedCourseId]
    );
    if (intakesRes.rows.length > 0 && (forceOverwrite || !stag.intake_months)) {
      const months = [...new Set(intakesRes.rows.map((r: Record<string, unknown>) => r.intake_month as string))];
      updates.intake_months = JSON.stringify(months);
    }

    // Merge first IELTS-type English requirement
    const englishRes = await client.query(
      `SELECT * FROM english_requirements_backup
       WHERE course_id = $1 AND LOWER(test_type) LIKE '%ielts%'
       ORDER BY backed_up_at DESC LIMIT 1`,
      [backedCourseId]
    );
    if (englishRes.rows.length > 0) {
      const er = englishRes.rows[0] as Record<string, unknown>;
      updates.ielts_overall   = pick(er.overall,   stag.ielts_overall);
      updates.ielts_listening = pick(er.listening, stag.ielts_listening);
      updates.ielts_speaking  = pick(er.speaking,  stag.ielts_speaking);
      updates.ielts_writing   = pick(er.writing,   stag.ielts_writing);
      updates.ielts_reading   = pick(er.reading,   stag.ielts_reading);
    }

    // Merge PTE
    const pteRes = await client.query(
      `SELECT * FROM english_requirements_backup
       WHERE course_id = $1 AND LOWER(test_type) LIKE '%pte%'
       ORDER BY backed_up_at DESC LIMIT 1`,
      [backedCourseId]
    );
    if (pteRes.rows.length > 0) {
      const pr = pteRes.rows[0] as Record<string, unknown>;
      updates.pte_overall   = pick(pr.overall,   stag.pte_overall);
      updates.pte_listening = pick(pr.listening, stag.pte_listening);
      updates.pte_speaking  = pick(pr.speaking,  stag.pte_speaking);
      updates.pte_writing   = pick(pr.writing,   stag.pte_writing);
      updates.pte_reading   = pick(pr.reading,   stag.pte_reading);
    }

    // Merge first academic requirement
    const acadRes = await client.query(
      `SELECT * FROM academic_requirements_backup
       WHERE course_id = $1 ORDER BY backed_up_at DESC LIMIT 1`,
      [backedCourseId]
    );
    if (acadRes.rows.length > 0) {
      const ar = acadRes.rows[0] as Record<string, unknown>;
      updates.academic_level   = pick(ar.academic_level,   stag.academic_level);
      updates.academic_score   = pick(ar.academic_score,   stag.academic_score);
      updates.score_type       = pick(ar.score_type,       stag.score_type);
      updates.academic_country = pick(ar.academic_country, stag.academic_country);
    }

    // Merge first scholarship name as the scholarship text
    const schRes = await client.query(
      `SELECT * FROM scholarships_backup
       WHERE course_id = $1 ORDER BY backed_up_at DESC LIMIT 1`,
      [backedCourseId]
    );
    if (schRes.rows.length > 0) {
      const sr = schRes.rows[0] as Record<string, unknown>;
      const schText = [sr.name, sr.details].filter(Boolean).join(" – ");
      updates.scholarship = pick(schText, stag.scholarship);
    }

    // Build SET clause dynamically
    const keys = Object.keys(updates);
    const setClauses = keys.map((k, i) => `${k} = $${i + 2}`).join(", ");
    const values = keys.map((k) => updates[k]);

    await client.query(
      `UPDATE scraped_courses SET ${setClauses} WHERE id = $1`,
      [id, ...values]
    );

    await client.query("COMMIT");

    // Return updated staged course
    const updated = await client.query(`SELECT * FROM scraped_courses WHERE id = $1`, [id]);
    res.json({ ok: true, appliedFields: keys, course: updated.rows[0] });
  } catch (err) {
    await client.query("ROLLBACK");
    const msg = err instanceof Error ? err.message : String(err);
    res.status(500).json({ ok: false, error: msg });
  } finally {
    client.release();
  }
});

export default router;

import { Router, type IRouter } from "express";
import multer from "multer";
import * as XLSX from "xlsx";
import { pool, db, universitiesTable, importJobsTable } from "@workspace/db";
import { eq, desc } from "drizzle-orm";

const router: IRouter = Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 20 * 1024 * 1024 } });

const MONTH_MAP: Record<string, string> = {
  Jan: "January", January: "January",
  Feb: "February", February: "February",
  Mar: "March", March: "March",
  Apr: "April", April: "April",
  May: "May",
  Jun: "June", June: "June",
  Jul: "July", July: "July",
  Aug: "August", August: "August",
  Sep: "September", Sept: "September", September: "September",
  Oct: "October", October: "October",
  Nov: "November", November: "November",
  Dec: "December", December: "December",
};

function parseMonths(str: string | null | undefined): string[] {
  if (!str) return [];
  const months: string[] = [];
  for (const [k, v] of Object.entries(MONTH_MAP)) {
    if (str.includes(k) && !months.includes(v)) months.push(v);
  }
  return months;
}

function toNum(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = parseFloat(String(v).replace(/,/g, ""));
  return isNaN(n) ? null : n;
}

function toInt(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = parseInt(String(v).replace(/,/g, ""));
  return isNaN(n) ? null : n;
}

function cleanText(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
}

function stripHtml(v: unknown): string | null {
  if (!v) return null;
  return String(v).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim() || null;
}

function getCol(row: Record<string, unknown>, ...keys: string[]): unknown {
  for (const k of keys) {
    for (const rk of Object.keys(row)) {
      if (rk.toLowerCase().trim() === k.toLowerCase().trim()) return row[rk];
    }
  }
  return null;
}

async function importRows(
  rows: Record<string, unknown>[],
  uniId: number,
): Promise<{ imported: number; skipped: number; errors: string[] }> {
  let imported = 0;
  let skipped = 0;
  const errors: string[] = [];

  for (const row of rows) {
    const courseName = cleanText(getCol(row, "Course Name"));
    if (!courseName) { skipped++; continue; }

    try {
      const dup = await pool.query(
        "SELECT id FROM courses WHERE university_id=$1 AND name=$2 LIMIT 1",
        [uniId, courseName],
      );
      let courseId: number;
      if (dup.rows.length > 0) {
        courseId = dup.rows[0].id;
      } else {
        const cRes = await pool.query(
          `INSERT INTO courses (university_id, name, category, sub_category, course_website, duration, duration_term, study_mode, degree_level, study_load, language, description, course_structure, career_outcomes, other_test, other_requirement, status)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,'active') RETURNING id`,
          [
            uniId,
            courseName,
            cleanText(getCol(row, "Category")),
            cleanText(getCol(row, "Sub Category", "Sub_Category")),
            cleanText(getCol(row, "Course Website")),
            toNum(getCol(row, "Duration")),
            cleanText(getCol(row, "Duration Term", "Duration_Term")),
            cleanText(getCol(row, "Study Mode", "Study mode", "Study_mode")),
            cleanText(getCol(row, "Degree Level", "Degree level", "Degree_level")),
            cleanText(getCol(row, "Study Load", "Study_Load")),
            cleanText(getCol(row, "Language")),
            stripHtml(getCol(row, "Course Description")),
            stripHtml(getCol(row, "Course Structure")),
            cleanText(getCol(row, "Career")),
            cleanText(getCol(row, "Other Test", "Other_Test")),
            cleanText(getCol(row, "Other Requirement", "Other_Requriment", "Other Requriment")),
          ],
        );
        courseId = cRes.rows[0].id;
        imported++;
      }

      const intakeStr = cleanText(getCol(row, "Intake Month", "Intake_Month"));
      const months = parseMonths(intakeStr);
      const intakeDay = toInt(getCol(row, "Intake Day", "Intake_Day"));
      for (const m of months) {
        const exists = await pool.query("SELECT id FROM intakes WHERE course_id=$1 AND intake_month=$2", [courseId, m]);
        if (exists.rows.length === 0) {
          await pool.query("INSERT INTO intakes (course_id, intake_month, intake_day) VALUES ($1,$2,$3)", [courseId, m, intakeDay]);
        }
      }

      const intlFee = toNum(getCol(row, "International Fee", "International_Fee"));
      if (intlFee) {
        const feeExists = await pool.query("SELECT id FROM fees WHERE course_id=$1", [courseId]);
        if (feeExists.rows.length === 0) {
          await pool.query(
            "INSERT INTO fees (course_id, international_fee, fee_term, fee_year, currency) VALUES ($1,$2,$3,$4,$5)",
            [courseId, intlFee, cleanText(getCol(row, "Fee Term", "Fee_Term")), toInt(getCol(row, "Fee Year", "Fee_Year")), cleanText(getCol(row, "Currency"))],
          );
        }
      }

      for (const test of ["IELTS", "PTE", "TOEFL"] as const) {
        const overall = toNum(getCol(row, `${test} Overall`, `${test}_Overall`));
        const listening = toNum(getCol(row, `${test} Listening`, `${test}_Listening`));
        const speaking = toNum(getCol(row, `${test} Speaking`, `${test}_Speaking`));
        const writing = toNum(getCol(row, `${test} Writing`, `${test}_Writing`));
        const reading = toNum(getCol(row, `${test} Reading`, `${test}_Reading`));
        if (overall || listening || speaking || writing || reading) {
          const eExists = await pool.query("SELECT id FROM english_requirements WHERE course_id=$1 AND test_type=$2", [courseId, test]);
          if (eExists.rows.length === 0) {
            await pool.query(
              "INSERT INTO english_requirements (course_id, test_type, listening, speaking, writing, reading, overall) VALUES ($1,$2,$3,$4,$5,$6,$7)",
              [courseId, test, listening, speaking, writing, reading, overall],
            );
          }
        }
      }

      const academicCountry = cleanText(getCol(row, "Academic Country", "Academic_Country"));
      const scoreType = cleanText(getCol(row, "Score Type", "Score_Type"));
      const academicLevel = cleanText(getCol(row, "Academic Level"));
      if (academicCountry || scoreType || academicLevel) {
        const acExists = await pool.query("SELECT id FROM academic_requirements WHERE course_id=$1 LIMIT 1", [courseId]);
        if (acExists.rows.length === 0) {
          await pool.query(
            "INSERT INTO academic_requirements (course_id, academic_level, score_type, academic_country) VALUES ($1,$2,$3,$4)",
            [courseId, academicLevel, scoreType, academicCountry],
          );
        }
      }

      const scholarship = cleanText(getCol(row, "Scholarship"));
      if (scholarship) {
        const scExists = await pool.query("SELECT id FROM scholarships WHERE course_id=$1 LIMIT 1", [courseId]);
        if (scExists.rows.length === 0) {
          await pool.query(
            "INSERT INTO scholarships (course_id, name, details) VALUES ($1,$2,$3)",
            [courseId, "Scholarship", scholarship],
          );
        }
      }
    } catch (err) {
      errors.push(`Row "${courseName}": ${(err as Error).message}`);
    }
  }

  return { imported, skipped, errors };
}

router.post("/import/excel", upload.single("file"), async (req, res): Promise<void> => {
  if (!req.file) {
    res.status(400).json({ error: "No file uploaded" });
    return;
  }

  const { universityId, universityName, universityCountry, universityCity } = req.body as Record<string, string>;

  let uniId: number;
  let uniName: string;

  try {
    if (universityId) {
      const u = await db.select().from(universitiesTable).where(eq(universitiesTable.id, parseInt(universityId)));
      if (!u[0]) { res.status(404).json({ error: "University not found" }); return; }
      uniId = u[0].id;
      uniName = u[0].name;
    } else if (universityName) {
      const existing = await db.select().from(universitiesTable).where(eq(universitiesTable.name, universityName));
      if (existing[0]) {
        uniId = existing[0].id;
        uniName = existing[0].name;
      } else {
        const [created] = await db.insert(universitiesTable).values({
          name: universityName,
          country: universityCountry || "Unknown",
          city: universityCity || "Unknown",
        }).returning();
        uniId = created.id;
        uniName = created.name;
      }
    } else {
      res.status(400).json({ error: "universityId or universityName required" });
      return;
    }

    const wb = XLSX.read(req.file.buffer, { type: "buffer" });
    const ws = wb.Sheets[wb.SheetNames[0]];
    const rows = XLSX.utils.sheet_to_json<Record<string, unknown>>(ws, { defval: null });

    const [job] = await db.insert(importJobsTable).values({
      universityId: uniId,
      universityName: uniName,
      fileName: req.file.originalname,
      status: "running",
      totalRows: rows.length,
    }).returning();

    const result = await importRows(rows, uniId);

    await db.update(importJobsTable).set({
      status: result.errors.length > 0 ? "completed_with_errors" : "completed",
      importedRows: result.imported,
      skippedRows: result.skipped,
      errorMessage: result.errors.length > 0 ? result.errors.slice(0, 5).join("; ") : null,
      completedAt: new Date(),
    }).where(eq(importJobsTable.id, job.id));

    res.json({
      jobId: job.id,
      universityId: uniId,
      universityName: uniName,
      totalRows: rows.length,
      imported: result.imported,
      skipped: result.skipped,
      errors: result.errors.slice(0, 10),
    });
  } catch (err) {
    res.status(500).json({ error: (err as Error).message });
  }
});

router.get("/import/history", async (_req, res): Promise<void> => {
  const jobs = await db
    .select()
    .from(importJobsTable)
    .orderBy(desc(importJobsTable.createdAt))
    .limit(50);
  res.json(jobs);
});

export default router;

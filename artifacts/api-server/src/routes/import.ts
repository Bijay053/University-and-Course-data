import { Router, type IRouter } from "express";
import multer from "multer";
import * as XLSX from "xlsx";
import {
  pool,
  db,
  universitiesTable,
  importJobsTable,
  scrapedCoursesTable,
  scrapedFieldEvidenceTable,
  fieldConflictsTable,
} from "@workspace/db";
import { eq, desc } from "drizzle-orm";
import { buildCourseReviewSnapshot, type ReviewCourseData } from "../lib/review-engine.js";
import { findUniversityByNameCaseInsensitive, formatDatabaseSetupHint } from "../lib/university-name-match.js";

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
  scrapeJobId: string,
): Promise<{ imported: number; skipped: number; errors: string[] }> {
  let imported = 0;
  let skipped = 0;
  const errors: string[] = [];

  for (const row of rows) {
    const courseName = cleanText(getCol(row, "Course Name"));
    if (!courseName) { skipped++; continue; }

    try {
      const dup = await pool.query(
        "SELECT id FROM scraped_courses WHERE scrape_job_id=$1 AND course_name=$2 LIMIT 1",
        [scrapeJobId, courseName],
      );
      if (dup.rows.length > 0) { skipped++; continue; }

      const courseWebsite = cleanText(getCol(row, "Course Website"));
      const intakeMonths = parseMonths(cleanText(getCol(row, "Intake Month", "Intake_Month")));
      const academicLevel = cleanText(getCol(row, "Academic Level"));
      const academicScore = toNum(getCol(row, "Academic Score", "Academic_Score"));
      const staged = {
        courseName,
        courseWebsite,
        courseLocation: cleanText(getCol(row, "Course Location", "Course_Location")),
        duration: toNum(getCol(row, "Duration")),
        durationTerm: cleanText(getCol(row, "Duration Term", "Duration_Term")),
        studyMode: cleanText(getCol(row, "Study Mode", "Study mode", "Study_mode")),
        degreeLevel: cleanText(getCol(row, "Degree Level", "Degree level", "Degree_level")),
        studyLoad: cleanText(getCol(row, "Study Load", "Study_Load")),
        language: cleanText(getCol(row, "Language")),
        description: stripHtml(getCol(row, "Course Description")),
        otherRequirement: cleanText(getCol(row, "Other Requirement", "Other_Requriment", "Other Requriment")),
        internationalFee: toNum(getCol(row, "International Fee", "International_Fee")),
        feeTerm: cleanText(getCol(row, "Fee Term", "Fee_Term")),
        feeYear: toInt(getCol(row, "Fee Year", "Fee_Year")),
        currency: cleanText(getCol(row, "Currency")),
        ieltsOverall: toNum(getCol(row, "IELTS Overall", "IELTS_Overall")),
        ieltsListening: toNum(getCol(row, "IELTS Listening", "IELTS_Listening")),
        ieltsSpeaking: toNum(getCol(row, "IELTS Speaking", "IELTS_Speaking")),
        ieltsWriting: toNum(getCol(row, "IELTS Writing", "IELTS_Writing")),
        ieltsReading: toNum(getCol(row, "IELTS Reading", "IELTS_Reading")),
        pteOverall: toNum(getCol(row, "PTE Overall", "PTE_Overall")),
        pteListening: toNum(getCol(row, "PTE Listening", "PTE_Listening")),
        pteSpeaking: toNum(getCol(row, "PTE Speaking", "PTE_Speaking")),
        pteWriting: toNum(getCol(row, "PTE Writing", "PTE_Writing")),
        pteReading: toNum(getCol(row, "PTE Reading", "PTE_Reading")),
        toeflOverall: toNum(getCol(row, "TOEFL Overall", "TOEFL_Overall")),
        toeflListening: toNum(getCol(row, "TOEFL Listening", "TOEFL_Listening")),
        toeflSpeaking: toNum(getCol(row, "TOEFL Speaking", "TOEFL_Speaking")),
        toeflWriting: toNum(getCol(row, "TOEFL Writing", "TOEFL_Writing")),
        toeflReading: toNum(getCol(row, "TOEFL Reading", "TOEFL_Reading")),
        intakeMonths,
        intakeDays: toInt(getCol(row, "Intake Day", "Intake_Day")),
        academicLevel,
        academicScore,
        scoreType: cleanText(getCol(row, "Score Type", "Score_Type")),
        academicCountry: cleanText(getCol(row, "Academic Country", "Academic_Country")),
        scholarship: cleanText(getCol(row, "Scholarship")),
      };
      const snapshot = buildCourseReviewSnapshot(
        staged as unknown as ReviewCourseData,
        [{
          url: courseWebsite || `import://excel/${encodeURIComponent(courseName)}`,
          pageType: "other",
          extractionMethod: "import",
          content: JSON.stringify(row),
        }],
      );
      const filled = [
        staged.duration,
        staged.internationalFee,
        staged.ieltsOverall,
        staged.degreeLevel,
        staged.studyMode,
        staged.courseLocation,
        staged.intakeMonths.length > 0 ? "intakes" : null,
      ].filter((value) => value != null).length;
      const completeness = Math.round((filled / 7) * 100);
      const notes = [
        "Imported from Excel; pending evidence-first review",
        snapshot.eligibility.eligibilityStatus !== "eligible" ? `Eligibility: ${snapshot.eligibility.reason}` : null,
        snapshot.conflicts.length > 0 ? `Conflicts: ${snapshot.conflicts.map((item) => item.fieldKey).join(", ")}` : null,
      ].filter(Boolean).join(" | ");

      const [inserted] = await db.insert(scrapedCoursesTable).values({
        scrapeJobId,
        universityId: uniId,
        courseName: staged.courseName,
        courseWebsite: staged.courseWebsite,
        courseLocation: staged.courseLocation,
        duration: staged.duration,
        durationTerm: staged.durationTerm,
        studyMode: staged.studyMode,
        degreeLevel: staged.degreeLevel,
        studyLoad: staged.studyLoad,
        language: staged.language,
        description: staged.description,
        otherRequirement: staged.otherRequirement,
        internationalFee: staged.internationalFee,
        feeTerm: staged.feeTerm,
        feeYear: staged.feeYear,
        currency: staged.currency,
        ieltsOverall: staged.ieltsOverall,
        ieltsListening: staged.ieltsListening,
        ieltsSpeaking: staged.ieltsSpeaking,
        ieltsWriting: staged.ieltsWriting,
        ieltsReading: staged.ieltsReading,
        pteOverall: staged.pteOverall,
        pteListening: staged.pteListening,
        pteSpeaking: staged.pteSpeaking,
        pteWriting: staged.pteWriting,
        pteReading: staged.pteReading,
        toeflOverall: staged.toeflOverall,
        toeflListening: staged.toeflListening,
        toeflSpeaking: staged.toeflSpeaking,
        toeflWriting: staged.toeflWriting,
        toeflReading: staged.toeflReading,
        intakeMonths: staged.intakeMonths,
        intakeDays: staged.intakeDays,
        academicLevel: staged.academicLevel,
        academicScore: staged.academicScore,
        scoreType: staged.scoreType,
        academicCountry: staged.academicCountry,
        scholarship: staged.scholarship,
        studentMarket: snapshot.eligibility.studentMarket,
        deliveryMode: snapshot.eligibility.deliveryMode,
        internationalEligible: snapshot.eligibility.internationalEligible,
        onCampusAvailable: snapshot.eligibility.onCampusAvailable,
        eligibilityStatus: snapshot.eligibility.eligibilityStatus,
        eligibilityReason: snapshot.eligibility.reason,
        eligibilityConfidence: snapshot.eligibility.confidence,
        autoPublishStatus: snapshot.autoPublishStatus,
        decisionScore: snapshot.decisionScore,
        status: "pending",
        notes,
        completeness,
      }).returning({ id: scrapedCoursesTable.id });

      if (snapshot.candidates.length > 0) {
        await db.insert(scrapedFieldEvidenceTable).values(snapshot.candidates.map((candidate) => ({
          scrapedCourseId: inserted.id,
          fieldKey: candidate.fieldKey,
          candidateValue: candidate.candidateValue,
          normalizedValue: candidate.normalizedValue,
          sourceUrl: candidate.sourceUrl,
          pageType: candidate.pageType,
          extractionMethod: candidate.extractionMethod,
          rawText: candidate.rawText,
          snippet: candidate.snippet,
          confidence: candidate.confidence,
          decisionScore: candidate.decisionScore,
          validationStatus: candidate.validationStatus,
          decisionStatus: candidate.decisionStatus,
          selected: candidate.selected,
        })));
      }
      if (snapshot.conflicts.length > 0) {
        await db.insert(fieldConflictsTable).values(snapshot.conflicts.map((conflict) => ({
          scrapedCourseId: inserted.id,
          fieldKey: conflict.fieldKey,
          valueA: conflict.valueA,
          valueB: conflict.valueB,
          conflictType: conflict.conflictType,
          reason: conflict.reason,
          status: "open",
        })));
      }
      imported++;
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
      const existing = await findUniversityByNameCaseInsensitive(universityName);
      if (existing) {
        uniId = existing.id;
        uniName = existing.name;
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

    const result = await importRows(rows, uniId, `import_${job.id}`);

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
    res.status(500).json({ error: formatDatabaseSetupHint(err) });
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

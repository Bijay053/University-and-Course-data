import { Router, type IRouter } from "express";
import { eq, inArray } from "drizzle-orm";
import { db, pool, academicRequirementsTable, coursesTable } from "@workspace/db";
import {
  ListCourseAcademicRequirementsParams,
  CreateCourseAcademicRequirementParams,
  CreateCourseAcademicRequirementBody,
  UpdateAcademicRequirementParams,
  UpdateAcademicRequirementBody,
  DeleteAcademicRequirementParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses/:courseId/academic-requirements", async (req, res): Promise<void> => {
  const params = ListCourseAcademicRequirementsParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const rows = await db.select().from(academicRequirementsTable).where(eq(academicRequirementsTable.courseId, params.data.courseId));
  res.json(rows);
});

router.post("/courses/:courseId/academic-requirements", async (req, res): Promise<void> => {
  const params = CreateCourseAcademicRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = CreateCourseAcademicRequirementBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.insert(academicRequirementsTable).values({ ...parsed.data, courseId: params.data.courseId }).returning();
  res.status(201).json(row);
});

router.patch("/academic-requirements/:id", async (req, res): Promise<void> => {
  const params = UpdateAcademicRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateAcademicRequirementBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.update(academicRequirementsTable).set(parsed.data).where(eq(academicRequirementsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "Academic requirement not found" });
    return;
  }
  res.json(row);
});

router.delete("/academic-requirements/:id", async (req, res): Promise<void> => {
  const params = DeleteAcademicRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [row] = await db.delete(academicRequirementsTable).where(eq(academicRequirementsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "Academic requirement not found" });
    return;
  }
  res.sendStatus(204);
});

// ── GET /universities/:universityId/academic-requirements ─────────────────────
// Returns ALL academic requirement rows for every course in this university,
// each row enriched with the course name and degree level.
router.get("/universities/:universityId/academic-requirements", async (req, res): Promise<void> => {
  const universityId = Number(req.params.universityId);
  if (!Number.isFinite(universityId)) { res.status(400).json({ error: "Invalid universityId" }); return; }
  const client = await pool.connect();
  try {
    const result = await client.query(`
      SELECT
        ar.id,
        ar.course_id        AS "courseId",
        c.name              AS "courseName",
        c.degree_level      AS "degreeLevel",
        ar.academic_level   AS "academicLevel",
        ar.academic_score   AS "academicScore",
        ar.score_type       AS "scoreType",
        ar.academic_country AS "academicCountry",
        ar.created_at       AS "createdAt"
      FROM academic_requirements ar
      JOIN courses c ON c.id = ar.course_id
      WHERE c.university_id = $1
      ORDER BY c.name, ar.academic_country NULLS LAST
    `, [universityId]);
    res.json(result.rows);
  } finally {
    client.release();
  }
});

// ── POST /universities/:universityId/bulk-academic ────────────────────────────
// Adds new academic requirements for selected courses.
// Each selected country creates a SEPARATE row per course.
// Returns 409 if any (course, country) pair already exists — nothing is saved.
router.post("/universities/:universityId/bulk-academic", async (req, res): Promise<void> => {
  const universityId = Number(req.params.universityId);
  if (!Number.isFinite(universityId)) { res.status(400).json({ error: "Invalid universityId" }); return; }

  const { courseIds, academicLevel, academicScore, scoreType, academicCountry } = req.body as {
    courseIds: number[];
    academicLevel?: string | null;
    academicScore?: number | null;
    scoreType?: string | null;
    academicCountry?: string | null;
  };
  if (!Array.isArray(courseIds) || courseIds.length === 0) {
    res.status(400).json({ error: "courseIds required" });
    return;
  }

  // Parse countries — multi-select sends comma-separated string or null
  const countries: (string | null)[] = academicCountry
    ? academicCountry.split(",").map((c) => c.trim()).filter(Boolean)
    : [null];

  // Load course names for human-readable conflict messages
  const courseRows = await db.select({ id: coursesTable.id, name: coursesTable.name })
    .from(coursesTable)
    .where(inArray(coursesTable.id, courseIds));
  const courseMap = Object.fromEntries(courseRows.map((c) => [c.id, c.name]));

  // Check for duplicates: same (course_id, academic_country) must not already exist
  const existing = await db.select()
    .from(academicRequirementsTable)
    .where(inArray(academicRequirementsTable.courseId, courseIds));

  const conflicts: { courseId: number; courseName: string; country: string | null }[] = [];
  for (const courseId of courseIds) {
    for (const country of countries) {
      const dup = existing.find((r) =>
        r.courseId === courseId &&
        (country === null ? r.academicCountry === null : r.academicCountry === country)
      );
      if (dup) {
        conflicts.push({ courseId, courseName: courseMap[courseId] ?? `Course #${courseId}`, country });
      }
    }
  }

  if (conflicts.length > 0) {
    res.status(409).json({ error: "duplicate", conflicts });
    return;
  }

  // No conflicts — insert one row per (courseId × country)
  const toInsert = courseIds.flatMap((courseId) =>
    countries.map((country) => ({
      courseId,
      academicLevel: academicLevel ?? null,
      academicScore: academicScore ?? null,
      scoreType: scoreType ?? null,
      academicCountry: country,
    }))
  );

  const rows = await db.insert(academicRequirementsTable).values(toInsert).returning();
  res.json({ updated: rows.length });
});

export default router;

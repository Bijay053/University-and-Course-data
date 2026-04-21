import { Router, type IRouter } from "express";
import { eq, inArray, and } from "drizzle-orm";
import { db, englishRequirementsTable } from "@workspace/db";
import {
  ListCourseEnglishRequirementsParams,
  CreateCourseEnglishRequirementParams,
  CreateCourseEnglishRequirementBody,
  UpdateEnglishRequirementParams,
  UpdateEnglishRequirementBody,
  DeleteEnglishRequirementParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses/:courseId/english-requirements", async (req, res): Promise<void> => {
  const params = ListCourseEnglishRequirementsParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const rows = await db.select().from(englishRequirementsTable).where(eq(englishRequirementsTable.courseId, params.data.courseId));
  res.json(rows);
});

router.post("/courses/:courseId/english-requirements", async (req, res): Promise<void> => {
  const params = CreateCourseEnglishRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = CreateCourseEnglishRequirementBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [req_] = await db.insert(englishRequirementsTable).values({ ...parsed.data, courseId: params.data.courseId }).returning();
  res.status(201).json(req_);
});

router.patch("/english-requirements/:id", async (req, res): Promise<void> => {
  const params = UpdateEnglishRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateEnglishRequirementBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.update(englishRequirementsTable).set(parsed.data).where(eq(englishRequirementsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "English requirement not found" });
    return;
  }
  res.json(row);
});

router.delete("/courses/:courseId/english-requirements", async (req, res): Promise<void> => {
  const courseId = Number(req.params.courseId);
  if (!Number.isFinite(courseId)) { res.status(400).json({ error: "Invalid courseId" }); return; }
  await db.delete(englishRequirementsTable).where(eq(englishRequirementsTable.courseId, courseId));
  res.sendStatus(204);
});

router.delete("/english-requirements/:id", async (req, res): Promise<void> => {
  const params = DeleteEnglishRequirementParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [row] = await db.delete(englishRequirementsTable).where(eq(englishRequirementsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "English requirement not found" });
    return;
  }
  res.sendStatus(204);
});

// Bulk upsert: apply one test-type entry to many courses at once
router.post("/universities/:universityId/bulk-english", async (req, res): Promise<void> => {
  const universityId = Number(req.params.universityId);
  if (!Number.isFinite(universityId)) { res.status(400).json({ error: "Invalid universityId" }); return; }
  const { courseIds, testType, listening, speaking, writing, reading, overall, testName } = req.body as {
    courseIds: number[];
    testType: string;
    listening?: number | null;
    speaking?: number | null;
    writing?: number | null;
    reading?: number | null;
    overall?: number | null;
    testName?: string | null;
  };
  if (!Array.isArray(courseIds) || courseIds.length === 0) { res.status(400).json({ error: "courseIds required" }); return; }
  if (!testType) { res.status(400).json({ error: "testType required" }); return; }
  // Delete existing records for these courses + this testType, then insert new ones
  await db.delete(englishRequirementsTable).where(
    and(inArray(englishRequirementsTable.courseId, courseIds), eq(englishRequirementsTable.testType, testType))
  );
  const rows = await db.insert(englishRequirementsTable).values(
    courseIds.map((courseId) => ({ courseId, testType, listening: listening ?? null, speaking: speaking ?? null, writing: writing ?? null, reading: reading ?? null, overall: overall ?? null, testName: testName ?? null }))
  ).returning();
  res.json({ updated: rows.length });
});

export default router;

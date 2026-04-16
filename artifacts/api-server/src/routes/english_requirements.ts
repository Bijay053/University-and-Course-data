import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
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

export default router;

import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, academicRequirementsTable } from "@workspace/db";
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

export default router;

import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, scholarshipsTable } from "@workspace/db";
import {
  ListCourseScholarshipsParams,
  CreateCourseScholarshipParams,
  CreateCourseScholarshipBody,
  UpdateScholarshipParams,
  UpdateScholarshipBody,
  DeleteScholarshipParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses/:courseId/scholarships", async (req, res): Promise<void> => {
  const params = ListCourseScholarshipsParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const rows = await db.select().from(scholarshipsTable).where(eq(scholarshipsTable.courseId, params.data.courseId));
  res.json(rows);
});

router.post("/courses/:courseId/scholarships", async (req, res): Promise<void> => {
  const params = CreateCourseScholarshipParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = CreateCourseScholarshipBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.insert(scholarshipsTable).values({ ...parsed.data, courseId: params.data.courseId }).returning();
  res.status(201).json(row);
});

router.patch("/scholarships/:id", async (req, res): Promise<void> => {
  const params = UpdateScholarshipParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateScholarshipBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [row] = await db.update(scholarshipsTable).set(parsed.data).where(eq(scholarshipsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "Scholarship not found" });
    return;
  }
  res.json(row);
});

router.delete("/scholarships/:id", async (req, res): Promise<void> => {
  const params = DeleteScholarshipParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [row] = await db.delete(scholarshipsTable).where(eq(scholarshipsTable.id, params.data.id)).returning();
  if (!row) {
    res.status(404).json({ error: "Scholarship not found" });
    return;
  }
  res.sendStatus(204);
});

export default router;

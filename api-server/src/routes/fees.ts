import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, feesTable } from "@workspace/db";
import {
  ListCourseFeesParams,
  CreateCourseFeeParams,
  CreateCourseFeeBody,
  UpdateFeeParams,
  UpdateFeeBody,
  DeleteFeeParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses/:courseId/fees", async (req, res): Promise<void> => {
  const params = ListCourseFeesParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const rows = await db.select().from(feesTable).where(eq(feesTable.courseId, params.data.courseId));
  res.json(rows);
});

router.post("/courses/:courseId/fees", async (req, res): Promise<void> => {
  const params = CreateCourseFeeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = CreateCourseFeeBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [fee] = await db.insert(feesTable).values({ ...parsed.data, courseId: params.data.courseId }).returning();
  res.status(201).json(fee);
});

router.patch("/fees/:id", async (req, res): Promise<void> => {
  const params = UpdateFeeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateFeeBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [fee] = await db.update(feesTable).set(parsed.data).where(eq(feesTable.id, params.data.id)).returning();
  if (!fee) {
    res.status(404).json({ error: "Fee not found" });
    return;
  }
  res.json(fee);
});

router.delete("/fees/:id", async (req, res): Promise<void> => {
  const params = DeleteFeeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [fee] = await db.delete(feesTable).where(eq(feesTable.id, params.data.id)).returning();
  if (!fee) {
    res.status(404).json({ error: "Fee not found" });
    return;
  }
  res.sendStatus(204);
});

export default router;

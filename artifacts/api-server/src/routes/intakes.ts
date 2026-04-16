import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, intakesTable } from "@workspace/db";
import {
  ListCourseIntakesParams,
  CreateCourseIntakeParams,
  CreateCourseIntakeBody,
  UpdateIntakeParams,
  UpdateIntakeBody,
  DeleteIntakeParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses/:courseId/intakes", async (req, res): Promise<void> => {
  const params = ListCourseIntakesParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const rows = await db.select().from(intakesTable).where(eq(intakesTable.courseId, params.data.courseId));
  res.json(rows);
});

router.post("/courses/:courseId/intakes", async (req, res): Promise<void> => {
  const params = CreateCourseIntakeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = CreateCourseIntakeBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [intake] = await db.insert(intakesTable).values({ ...parsed.data, courseId: params.data.courseId }).returning();
  res.status(201).json(intake);
});

router.patch("/intakes/:id", async (req, res): Promise<void> => {
  const params = UpdateIntakeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateIntakeBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [intake] = await db.update(intakesTable).set(parsed.data).where(eq(intakesTable.id, params.data.id)).returning();
  if (!intake) {
    res.status(404).json({ error: "Intake not found" });
    return;
  }
  res.json(intake);
});

router.delete("/intakes/:id", async (req, res): Promise<void> => {
  const params = DeleteIntakeParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [intake] = await db.delete(intakesTable).where(eq(intakesTable.id, params.data.id)).returning();
  if (!intake) {
    res.status(404).json({ error: "Intake not found" });
    return;
  }
  res.sendStatus(204);
});

export default router;

import { Router, type IRouter } from "express";
import { eq, ilike, and, type SQL } from "drizzle-orm";
import { db, coursesTable, universitiesTable, intakesTable, feesTable, englishRequirementsTable, academicRequirementsTable, scholarshipsTable } from "@workspace/db";
import {
  ListCoursesQueryParams,
  CreateCourseBody,
  GetCourseParams,
  UpdateCourseParams,
  UpdateCourseBody,
  DeleteCourseParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

router.get("/courses", async (req, res): Promise<void> => {
  const query = ListCoursesQueryParams.safeParse(req.query);
  if (!query.success) {
    res.status(400).json({ error: query.error.message });
    return;
  }
  const { search, universityId, category, degreeLevel, studyMode, page = 1, limit = 20 } = query.data;
  const conditions: SQL[] = [];
  if (search) conditions.push(ilike(coursesTable.name, `%${search}%`));
  if (universityId) conditions.push(eq(coursesTable.universityId, universityId));
  if (category) conditions.push(ilike(coursesTable.category, `%${category}%`));
  if (degreeLevel) conditions.push(eq(coursesTable.degreeLevel, degreeLevel));
  if (studyMode) conditions.push(eq(coursesTable.studyMode, studyMode));

  const offset = ((page ?? 1) - 1) * (limit ?? 20);
  const [rows, countRows] = await Promise.all([
    db
      .select({
        id: coursesTable.id,
        universityId: coursesTable.universityId,
        universityName: universitiesTable.name,
        name: coursesTable.name,
        category: coursesTable.category,
        subCategory: coursesTable.subCategory,
        courseWebsite: coursesTable.courseWebsite,
        duration: coursesTable.duration,
        durationTerm: coursesTable.durationTerm,
        studyMode: coursesTable.studyMode,
        degreeLevel: coursesTable.degreeLevel,
        studyLoad: coursesTable.studyLoad,
        language: coursesTable.language,
        description: coursesTable.description,
        courseStructure: coursesTable.courseStructure,
        careerOutcomes: coursesTable.careerOutcomes,
        otherTest: coursesTable.otherTest,
        otherTestScore: coursesTable.otherTestScore,
        otherRequirement: coursesTable.otherRequirement,
        status: coursesTable.status,
        createdAt: coursesTable.createdAt,
        updatedAt: coursesTable.updatedAt,
      })
      .from(coursesTable)
      .leftJoin(universitiesTable, eq(coursesTable.universityId, universitiesTable.id))
      .where(conditions.length ? and(...conditions) : undefined)
      .limit(limit ?? 20)
      .offset(offset),
    db.select({ id: coursesTable.id }).from(coursesTable).where(conditions.length ? and(...conditions) : undefined),
  ]);
  res.json({ data: rows, total: countRows.length, page: page ?? 1, limit: limit ?? 20 });
});

router.post("/courses", async (req, res): Promise<void> => {
  const parsed = CreateCourseBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const [course] = await db.insert(coursesTable).values({ ...parsed.data, status: "active" }).returning();
  res.status(201).json(course);
});

router.get("/courses/:id", async (req, res): Promise<void> => {
  const params = GetCourseParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const { id } = params.data;
  const [courseRow] = await db
    .select({
      id: coursesTable.id,
      universityId: coursesTable.universityId,
      universityName: universitiesTable.name,
      name: coursesTable.name,
      category: coursesTable.category,
      subCategory: coursesTable.subCategory,
      courseWebsite: coursesTable.courseWebsite,
      duration: coursesTable.duration,
      durationTerm: coursesTable.durationTerm,
      studyMode: coursesTable.studyMode,
      degreeLevel: coursesTable.degreeLevel,
      studyLoad: coursesTable.studyLoad,
      language: coursesTable.language,
      description: coursesTable.description,
      courseStructure: coursesTable.courseStructure,
      careerOutcomes: coursesTable.careerOutcomes,
      otherTest: coursesTable.otherTest,
      otherTestScore: coursesTable.otherTestScore,
      otherRequirement: coursesTable.otherRequirement,
      status: coursesTable.status,
      createdAt: coursesTable.createdAt,
      updatedAt: coursesTable.updatedAt,
    })
    .from(coursesTable)
    .leftJoin(universitiesTable, eq(coursesTable.universityId, universitiesTable.id))
    .where(eq(coursesTable.id, id));

  if (!courseRow) {
    res.status(404).json({ error: "Course not found" });
    return;
  }

  const [intakes, fees, englishReqs, academicReqs, scholarships] = await Promise.all([
    db.select().from(intakesTable).where(eq(intakesTable.courseId, id)),
    db.select().from(feesTable).where(eq(feesTable.courseId, id)),
    db.select().from(englishRequirementsTable).where(eq(englishRequirementsTable.courseId, id)),
    db.select().from(academicRequirementsTable).where(eq(academicRequirementsTable.courseId, id)),
    db.select().from(scholarshipsTable).where(eq(scholarshipsTable.courseId, id)),
  ]);

  res.json({
    ...courseRow,
    intakes,
    fees,
    englishRequirements: englishReqs,
    academicRequirements: academicReqs,
    scholarships,
  });
});

router.patch("/courses/:id", async (req, res): Promise<void> => {
  const params = UpdateCourseParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const parsed = UpdateCourseBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const updateData: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(parsed.data)) {
    if (v !== undefined && v !== null) updateData[k] = v;
  }
  const [course] = await db.update(coursesTable).set(updateData).where(eq(coursesTable.id, params.data.id)).returning();
  if (!course) {
    res.status(404).json({ error: "Course not found" });
    return;
  }
  res.json(course);
});

router.delete("/courses/:id", async (req, res): Promise<void> => {
  const params = DeleteCourseParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }
  const [course] = await db.delete(coursesTable).where(eq(coursesTable.id, params.data.id)).returning();
  if (!course) {
    res.status(404).json({ error: "Course not found" });
    return;
  }
  res.sendStatus(204);
});

export default router;

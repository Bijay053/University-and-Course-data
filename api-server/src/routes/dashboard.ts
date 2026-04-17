import { Router, type IRouter } from "express";
import { eq, count, sql } from "drizzle-orm";
import { db, universitiesTable, coursesTable, scholarshipsTable, scrapingChangesTable, scrapingJobsTable, intakesTable } from "@workspace/db";

const router: IRouter = Router();

router.get("/dashboard/stats", async (_req, res): Promise<void> => {
  const [
    uniCount,
    courseCount,
    scholarshipCount,
    pendingCount,
    activeJobCount,
  ] = await Promise.all([
    db.select({ count: count() }).from(universitiesTable),
    db.select({ count: count() }).from(coursesTable),
    db.select({ count: count() }).from(scholarshipsTable),
    db.select({ count: count() }).from(scrapingChangesTable).where(eq(scrapingChangesTable.status, "pending")),
    db.select({ count: count() }).from(scrapingJobsTable).where(eq(scrapingJobsTable.status, "active")),
  ]);

  res.json({
    totalUniversities: uniCount[0]?.count ?? 0,
    totalCourses: courseCount[0]?.count ?? 0,
    totalScholarships: scholarshipCount[0]?.count ?? 0,
    pendingChanges: pendingCount[0]?.count ?? 0,
    activeScrapingJobs: activeJobCount[0]?.count ?? 0,
    coursesThisMonth: courseCount[0]?.count ?? 0,
  });
});

router.get("/dashboard/recent-changes", async (_req, res): Promise<void> => {
  const rows = await db
    .select()
    .from(scrapingChangesTable)
    .orderBy(sql`${scrapingChangesTable.detectedAt} desc`)
    .limit(10);
  res.json(rows);
});

router.get("/dashboard/courses-by-level", async (_req, res): Promise<void> => {
  const rows = await db
    .select({
      label: coursesTable.degreeLevel,
      count: count(),
    })
    .from(coursesTable)
    .groupBy(coursesTable.degreeLevel);
  res.json(rows.map((r) => ({ label: r.label ?? "Unknown", count: r.count })));
});

router.get("/dashboard/upcoming-intakes", async (_req, res): Promise<void> => {
  const rows = await db
    .select({
      courseId: intakesTable.courseId,
      courseName: coursesTable.name,
      universityName: universitiesTable.name,
      intakeMonth: intakesTable.intakeMonth,
      intakeYear: intakesTable.intakeYear,
      isOpen: intakesTable.isOpen,
    })
    .from(intakesTable)
    .leftJoin(coursesTable, eq(intakesTable.courseId, coursesTable.id))
    .leftJoin(universitiesTable, eq(coursesTable.universityId, universitiesTable.id))
    .where(eq(intakesTable.isOpen, true))
    .limit(10);
  res.json(rows.map((r) => ({
    courseId: r.courseId,
    courseName: r.courseName ?? "Unknown",
    universityName: r.universityName ?? "Unknown",
    intakeMonth: r.intakeMonth,
    intakeYear: r.intakeYear,
    isOpen: r.isOpen,
  })));
});

export default router;

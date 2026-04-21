import { Router, type IRouter } from "express";
import { eq } from "drizzle-orm";
import { db, pool, coursesTable, universitiesTable, intakesTable, feesTable, englishRequirementsTable, academicRequirementsTable, scholarshipsTable } from "@workspace/db";
import {
  ListCoursesQueryParams,
  CreateCourseBody,
  GetCourseParams,
  UpdateCourseParams,
  UpdateCourseBody,
  DeleteCourseParams,
} from "@workspace/api-zod";

const router: IRouter = Router();

/** In production, list only catalog-eligible courses. In development, show all rows unless API_STRICT_CATALOG=1. */
function useCatalogCourseFilters(): boolean {
  if (process.env.API_STRICT_CATALOG === "1" || process.env.API_STRICT_CATALOG === "true") return true;
  return process.env.NODE_ENV === "production";
}

router.get("/courses", async (req, res): Promise<void> => {
  const query = ListCoursesQueryParams.safeParse(req.query);
  if (!query.success) {
    res.status(400).json({ error: query.error.message });
    return;
  }
  const { search, universityId, category, subCategory, degreeLevel, studyMode, page = 1, limit = 20 } = query.data;
  const pageNum = page ?? 1;
  const limitNum = limit ?? 20;
  const offset = (pageNum - 1) * limitNum;

  const whereClauses: string[] = [];
  const params: unknown[] = [];
  let pIdx = 1;

  if (search) { whereClauses.push(`c.name ILIKE $${pIdx++}`); params.push(`%${search}%`); }
  if (universityId) {
    whereClauses.push(`c.university_id = $${pIdx++}`); params.push(universityId);
    whereClauses.push(`EXISTS (SELECT 1 FROM scraped_courses sc WHERE sc.course_id = c.id AND sc.status = 'approved')`);
  }
  if (category) { whereClauses.push(`c.category ILIKE $${pIdx++}`); params.push(`%${category}%`); }
  if (subCategory) { whereClauses.push(`c.sub_category ILIKE $${pIdx++}`); params.push(`%${subCategory}%`); }
  if (degreeLevel) { whereClauses.push(`c.degree_level = $${pIdx++}`); params.push(degreeLevel); }
  if (studyMode) { whereClauses.push(`c.study_mode = $${pIdx++}`); params.push(studyMode); }

  if (useCatalogCourseFilters()) {
    whereClauses.push("c.approval_status = 'approved'");
    whereClauses.push("c.status = 'active'");
    whereClauses.push("(c.eligibility_status IS NULL OR c.eligibility_status <> 'rejected')");
    whereClauses.push("(c.international_eligible IS NULL OR c.international_eligible = true)");
    whereClauses.push("(c.on_campus_available IS NULL OR c.on_campus_available = true)");
  }
  const whereSQL = whereClauses.length ? `WHERE ${whereClauses.join(" AND ")}` : "";

  const dataQuery = `
    SELECT
      c.id, c.university_id AS "universityId", u.name AS "universityName", u.city,
      c.name, c.category, c.sub_category AS "subCategory", c.course_website AS "courseWebsite", c.course_location AS "courseLocation",
      c.duration, c.duration_term AS "durationTerm", c.study_mode AS "studyMode",
      c.degree_level AS "degreeLevel", c.study_load AS "studyLoad", c.language,
      c.description, c.course_structure AS "courseStructure", c.career_outcomes AS "careerOutcomes",
      c.other_test AS "otherTest", c.other_test_score AS "otherTestScore",
      c.other_requirement AS "otherRequirement", c.student_market AS "studentMarket", c.delivery_mode AS "deliveryMode",
      c.international_eligible AS "internationalEligible", c.on_campus_available AS "onCampusAvailable",
      c.eligibility_status AS "eligibilityStatus", c.approval_status AS "approvalStatus",
      c.status, c.created_at AS "createdAt", c.updated_at AS "updatedAt",
      (SELECT string_agg(DISTINCT i.intake_month, ', ' ORDER BY i.intake_month) FROM intakes i WHERE i.course_id = c.id) AS "intakeMonths",
      (SELECT string_agg(DISTINCT i.intake_day::text, ', ') FROM intakes i WHERE i.course_id = c.id AND i.intake_day IS NOT NULL) AS "intakeDays",
      (SELECT f.international_fee FROM fees f WHERE f.course_id = c.id LIMIT 1) AS "internationalFee",
      (SELECT f.fee_term FROM fees f WHERE f.course_id = c.id LIMIT 1) AS "feeTerm",
      (SELECT f.fee_year FROM fees f WHERE f.course_id = c.id LIMIT 1) AS "feeYear",
      (SELECT f.currency FROM fees f WHERE f.course_id = c.id LIMIT 1) AS "currency",
      (SELECT er.listening FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'IELTS' LIMIT 1) AS "ieltsListening",
      (SELECT er.speaking FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'IELTS' LIMIT 1) AS "ieltsSpeaking",
      (SELECT er.writing FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'IELTS' LIMIT 1) AS "ieltsWriting",
      (SELECT er.reading FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'IELTS' LIMIT 1) AS "ieltsReading",
      (SELECT er.overall FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'IELTS' LIMIT 1) AS "ieltsOverall",
      (SELECT er.listening FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'PTE' LIMIT 1) AS "pteListening",
      (SELECT er.speaking FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'PTE' LIMIT 1) AS "pteSpeaking",
      (SELECT er.writing FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'PTE' LIMIT 1) AS "pteWriting",
      (SELECT er.reading FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'PTE' LIMIT 1) AS "pteReading",
      (SELECT er.overall FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'PTE' LIMIT 1) AS "pteOverall",
      (SELECT er.listening FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'TOEFL' LIMIT 1) AS "toeflListening",
      (SELECT er.speaking FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'TOEFL' LIMIT 1) AS "toeflSpeaking",
      (SELECT er.writing FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'TOEFL' LIMIT 1) AS "toeflWriting",
      (SELECT er.reading FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'TOEFL' LIMIT 1) AS "toeflReading",
      (SELECT er.overall FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'TOEFL' LIMIT 1) AS "toeflOverall",
      (SELECT er.test_name FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishTestName",
      (SELECT er.reading FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishReading",
      (SELECT er.listening FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishListening",
      (SELECT er.speaking FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishSpeaking",
      (SELECT er.writing FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishWriting",
      (SELECT er.overall FROM english_requirements er WHERE er.course_id = c.id AND er.test_type = 'Other' LIMIT 1) AS "otherEnglishOverall",
      (SELECT ar.academic_level FROM academic_requirements ar WHERE ar.course_id = c.id LIMIT 1) AS "academicLevel",
      (SELECT ar.academic_score FROM academic_requirements ar WHERE ar.course_id = c.id LIMIT 1) AS "academicScore",
      (SELECT ar.score_type FROM academic_requirements ar WHERE ar.course_id = c.id LIMIT 1) AS "scoreType",
      (SELECT ar.academic_country FROM academic_requirements ar WHERE ar.course_id = c.id LIMIT 1) AS "academicCountry",
      (SELECT s.details FROM scholarships s WHERE s.course_id = c.id LIMIT 1) AS "scholarshipDetails",
      (SELECT s.eligibility_criteria FROM scholarships s WHERE s.course_id = c.id LIMIT 1) AS "scholarshipEligibility",
      (SELECT s.amount FROM scholarships s WHERE s.course_id = c.id LIMIT 1) AS "scholarshipAmount",
      (SELECT s.percentage FROM scholarships s WHERE s.course_id = c.id LIMIT 1) AS "scholarshipPercentage",
      (SELECT s.currency FROM scholarships s WHERE s.course_id = c.id LIMIT 1) AS "scholarshipCurrency"
    FROM courses c
    LEFT JOIN universities u ON c.university_id = u.id
    ${whereSQL}
    ORDER BY c.id
    LIMIT $${pIdx++} OFFSET $${pIdx++}
  `;
  const countQuerySQL = `SELECT COUNT(*) FROM courses c LEFT JOIN universities u ON c.university_id = u.id ${whereSQL}`;

  const [dataRows, countRow] = await Promise.all([
    pool.query(dataQuery, [...params, limitNum, offset]),
    pool.query(countQuerySQL, params),
  ]);

  res.json({
    data: dataRows.rows,
    total: parseInt(countRow.rows[0].count, 10),
    page: pageNum,
    limit: limitNum,
  });
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
      studentMarket: coursesTable.studentMarket,
      deliveryMode: coursesTable.deliveryMode,
      internationalEligible: coursesTable.internationalEligible,
      onCampusAvailable: coursesTable.onCampusAvailable,
      eligibilityStatus: coursesTable.eligibilityStatus,
      approvalStatus: coursesTable.approvalStatus,
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
  if (
    useCatalogCourseFilters() &&
    (courseRow.approvalStatus !== "approved" ||
      courseRow.status !== "active" ||
      courseRow.eligibilityStatus === "rejected" ||
      courseRow.internationalEligible === false ||
      courseRow.onCampusAvailable === false)
  ) {
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

import { pgTable, serial, integer, timestamp, text, real, boolean } from "drizzle-orm/pg-core";
import { scrapedCoursesTable } from "./scraped_courses";
import { coursesTable } from "./courses";
import { universitiesTable } from "./universities";

export const scrapedFieldEvidenceTable = pgTable("scraped_field_evidence", {
  id: serial("id").primaryKey(),
  scrapedCourseId: integer("scraped_course_id").notNull().references(() => scrapedCoursesTable.id, { onDelete: "cascade" }),
  fieldKey: text("field_key").notNull(),
  candidateValue: text("candidate_value"),
  normalizedValue: text("normalized_value"),
  sourceUrl: text("source_url"),
  pageType: text("page_type"),
  extractionMethod: text("extraction_method"),
  rawText: text("raw_text"),
  snippet: text("snippet"),
  confidence: real("confidence"),
  decisionScore: real("decision_score"),
  validationStatus: text("validation_status").notNull().default("pending"),
  decisionStatus: text("decision_status").notNull().default("needs_review"),
  selected: boolean("selected").notNull().default(false),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const fieldConflictsTable = pgTable("field_conflicts", {
  id: serial("id").primaryKey(),
  scrapedCourseId: integer("scraped_course_id").references(() => scrapedCoursesTable.id, { onDelete: "cascade" }),
  courseId: integer("course_id").references(() => coursesTable.id, { onDelete: "cascade" }),
  fieldKey: text("field_key").notNull(),
  valueA: text("value_a"),
  valueB: text("value_b"),
  evidenceAId: integer("evidence_a_id").references(() => scrapedFieldEvidenceTable.id, { onDelete: "set null" }),
  evidenceBId: integer("evidence_b_id").references(() => scrapedFieldEvidenceTable.id, { onDelete: "set null" }),
  conflictType: text("conflict_type").notNull().default("mismatch"),
  reason: text("reason"),
  status: text("status").notNull().default("open"),
  resolution: text("resolution"),
  reviewedAt: timestamp("reviewed_at", { withTimezone: true }),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const courseFieldApprovalsTable = pgTable("course_field_approvals", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").notNull().references(() => coursesTable.id, { onDelete: "cascade" }),
  fieldKey: text("field_key").notNull(),
  finalValue: text("final_value"),
  sourceEvidenceId: integer("source_evidence_id").references(() => scrapedFieldEvidenceTable.id, { onDelete: "set null" }),
  decisionScore: real("decision_score"),
  approvalStatus: text("approval_status").notNull().default("approved"),
  approvedBy: text("approved_by"),
  approvedAt: timestamp("approved_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow().$onUpdate(() => new Date()),
});

export const courseAuditLogTable = pgTable("course_audit_log", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").references(() => coursesTable.id, { onDelete: "cascade" }),
  scrapedCourseId: integer("scraped_course_id").references(() => scrapedCoursesTable.id, { onDelete: "set null" }),
  sourceEvidenceId: integer("source_evidence_id").references(() => scrapedFieldEvidenceTable.id, { onDelete: "set null" }),
  fieldKey: text("field_key"),
  action: text("action").notNull(),
  oldValue: text("old_value"),
  newValue: text("new_value"),
  reason: text("reason"),
  actor: text("actor"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const scrapeFeedbackTable = pgTable("scrape_feedback", {
  id: serial("id").primaryKey(),
  universityId: integer("university_id").references(() => universitiesTable.id, { onDelete: "cascade" }),
  scrapedCourseId: integer("scraped_course_id").references(() => scrapedCoursesTable.id, { onDelete: "set null" }),
  courseName: text("course_name"),
  fieldKey: text("field_key"),
  issueType: text("issue_type").notNull().default("generic"),
  reason: text("reason").notNull(),
  preferredValue: text("preferred_value"),
  status: text("status").notNull().default("active"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

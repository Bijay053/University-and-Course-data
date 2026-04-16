import { pgTable, text, serial, timestamp, integer, real } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { universitiesTable } from "./universities";

export const coursesTable = pgTable("courses", {
  id: serial("id").primaryKey(),
  universityId: integer("university_id").notNull().references(() => universitiesTable.id, { onDelete: "cascade" }),
  name: text("name").notNull(),
  category: text("category"),
  subCategory: text("sub_category"),
  courseWebsite: text("course_website"),
  duration: real("duration"),
  durationTerm: text("duration_term"),
  studyMode: text("study_mode"),
  degreeLevel: text("degree_level"),
  studyLoad: text("study_load"),
  language: text("language"),
  description: text("description"),
  courseStructure: text("course_structure"),
  careerOutcomes: text("career_outcomes"),
  otherTest: text("other_test"),
  otherTestScore: text("other_test_score"),
  otherRequirement: text("other_requirement"),
  status: text("status").notNull().default("active"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow().$onUpdate(() => new Date()),
});

export const insertCourseSchema = createInsertSchema(coursesTable).omit({ id: true, createdAt: true, updatedAt: true });
export type InsertCourse = z.infer<typeof insertCourseSchema>;
export type Course = typeof coursesTable.$inferSelect;

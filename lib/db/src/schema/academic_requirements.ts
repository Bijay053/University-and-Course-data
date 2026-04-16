import { pgTable, serial, integer, timestamp, text, real } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { coursesTable } from "./courses";

export const academicRequirementsTable = pgTable("academic_requirements", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").notNull().references(() => coursesTable.id, { onDelete: "cascade" }),
  academicLevel: text("academic_level"),
  academicScore: real("academic_score"),
  scoreType: text("score_type"),
  academicCountry: text("academic_country"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const insertAcademicRequirementSchema = createInsertSchema(academicRequirementsTable).omit({ id: true, createdAt: true });
export type InsertAcademicRequirement = z.infer<typeof insertAcademicRequirementSchema>;
export type AcademicRequirement = typeof academicRequirementsTable.$inferSelect;

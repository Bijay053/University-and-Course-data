import { pgTable, serial, integer, timestamp, text, real } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { coursesTable } from "./courses";

export const englishRequirementsTable = pgTable("english_requirements", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").notNull().references(() => coursesTable.id, { onDelete: "cascade" }),
  testType: text("test_type").notNull(),
  listening: real("listening"),
  speaking: real("speaking"),
  writing: real("writing"),
  reading: real("reading"),
  overall: real("overall"),
  testName: text("test_name"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const insertEnglishRequirementSchema = createInsertSchema(englishRequirementsTable).omit({ id: true, createdAt: true });
export type InsertEnglishRequirement = z.infer<typeof insertEnglishRequirementSchema>;
export type EnglishRequirement = typeof englishRequirementsTable.$inferSelect;

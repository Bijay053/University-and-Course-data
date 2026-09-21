import { pgTable, serial, integer, timestamp, text, real, uniqueIndex } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { sql } from "drizzle-orm";
import { z } from "zod/v4";
import { coursesTable } from "./courses";

export const academicLevelOptionsTable = pgTable("academic_level_options", {
  id: serial("id").primaryKey(),
  name: text("name").notNull().unique(),
  mappingKey: text("mapping_key"),
  sortOrder: integer("sort_order").notNull().default(0),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
}, (table) => [
  uniqueIndex("uq_academic_level_options_mapping_key")
    .on(table.mappingKey)
    .where(sql`${table.mappingKey} IS NOT NULL`),
]);

export const academicRequirementsTable = pgTable("academic_requirements", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").notNull().references(() => coursesTable.id, { onDelete: "cascade" }),
  academicLevel: text("academic_level"),
  academicLevelOptionId: integer("academic_level_option_id")
    .references(() => academicLevelOptionsTable.id, { onDelete: "restrict" }),
  academicScore: real("academic_score"),
  scoreType: text("score_type"),
  academicCountry: text("academic_country"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const insertAcademicRequirementSchema = createInsertSchema(academicRequirementsTable).omit({ id: true, createdAt: true });
export type InsertAcademicRequirement = z.infer<typeof insertAcademicRequirementSchema>;
export type AcademicRequirement = typeof academicRequirementsTable.$inferSelect;

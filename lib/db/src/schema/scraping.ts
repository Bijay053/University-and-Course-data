import { pgTable, serial, integer, timestamp, text } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { universitiesTable } from "./universities";
import { scrapedCoursesTable } from "./scraped_courses";
import { coursesTable } from "./courses";

export const scrapingJobsTable = pgTable("scraping_jobs", {
  id: serial("id").primaryKey(),
  universityId: integer("university_id").references(() => universitiesTable.id, { onDelete: "set null" }),
  url: text("url").notNull(),
  frequency: text("frequency").notNull().default("weekly"),
  status: text("status").notNull().default("active"),
  lastRun: timestamp("last_run", { withTimezone: true }),
  nextRun: timestamp("next_run", { withTimezone: true }),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const scrapingChangesTable = pgTable("scraping_changes", {
  id: serial("id").primaryKey(),
  scrapingJobId: integer("scraping_job_id").references(() => scrapingJobsTable.id, { onDelete: "set null" }),
  scrapedCourseId: integer("scraped_course_id").references(() => scrapedCoursesTable.id, { onDelete: "set null" }),
  courseId: integer("course_id").references(() => coursesTable.id, { onDelete: "set null" }),
  universityName: text("university_name"),
  courseName: text("course_name"),
  fieldChanged: text("field_changed").notNull(),
  oldValue: text("old_value"),
  newValue: text("new_value"),
  reason: text("reason"),
  status: text("status").notNull().default("pending"),
  detectedAt: timestamp("detected_at", { withTimezone: true }).notNull().defaultNow(),
  reviewedAt: timestamp("reviewed_at", { withTimezone: true }),
});

export const insertScrapingJobSchema = createInsertSchema(scrapingJobsTable).omit({ id: true, createdAt: true });
export type InsertScrapingJob = z.infer<typeof insertScrapingJobSchema>;
export type ScrapingJob = typeof scrapingJobsTable.$inferSelect;

export const insertScrapingChangeSchema = createInsertSchema(scrapingChangesTable).omit({ id: true });
export type InsertScrapingChange = z.infer<typeof insertScrapingChangeSchema>;
export type ScrapingChange = typeof scrapingChangesTable.$inferSelect;

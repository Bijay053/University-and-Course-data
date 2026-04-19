import { pgTable, serial, integer, timestamp, text, jsonb, boolean, uniqueIndex } from "drizzle-orm/pg-core";
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

export const scrapeRuntimeJobsTable = pgTable("scrape_runtime_jobs", {
  runtimeJobId: text("runtime_job_id").primaryKey(),
  scrapingJobId: integer("scraping_job_id").references(() => scrapingJobsTable.id, { onDelete: "set null" }),
  universityId: integer("university_id").references(() => universitiesTable.id, { onDelete: "set null" }),
  universityName: text("university_name"),
  url: text("url"),
  jobType: text("job_type").notNull(),
  status: text("status").notNull().default("queued"),
  requestPayload: jsonb("request_payload").$type<Record<string, unknown> | null>(),
  discoveredConfig: jsonb("discovered_config").$type<Record<string, unknown> | null>(),
  approvalSummary: jsonb("approval_summary").$type<Record<string, unknown> | null>(),
  approvalDecision: boolean("approval_decision"),
  stopRequested: boolean("stop_requested").notNull().default(false),
  fastMode: boolean("fast_mode").notNull().default(false),
  imported: integer("imported").notNull().default(0),
  skipped: integer("skipped").notNull().default(0),
  errors: integer("errors").notNull().default(0),
  totalFound: integer("total_found").notNull().default(0),
  current: integer("current").notNull().default(0),
  logCount: integer("log_count").notNull().default(0),
  claimCount: integer("claim_count").notNull().default(0),
  workerId: text("worker_id"),
  workerPid: integer("worker_pid"),
  heartbeatAt: timestamp("heartbeat_at", { withTimezone: true }),
  claimedAt: timestamp("claimed_at", { withTimezone: true }),
  startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
  completedAt: timestamp("completed_at", { withTimezone: true }),
  errorMessage: text("error_message"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

export const scrapeRuntimeLogsTable = pgTable("scrape_runtime_logs", {
  id: serial("id").primaryKey(),
  runtimeJobId: text("runtime_job_id").notNull().references(() => scrapeRuntimeJobsTable.runtimeJobId, { onDelete: "cascade" }),
  sequence: integer("sequence").notNull(),
  event: text("event").notNull(),
  payload: jsonb("payload").$type<Record<string, unknown>>().notNull().default({}),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
}, (table) => ({
  runtimeJobSequenceIdx: uniqueIndex("scrape_runtime_logs_runtime_job_sequence_idx").on(table.runtimeJobId, table.sequence),
}));

export const insertScrapingJobSchema = createInsertSchema(scrapingJobsTable).omit({ id: true, createdAt: true });
export type InsertScrapingJob = z.infer<typeof insertScrapingJobSchema>;
export type ScrapingJob = typeof scrapingJobsTable.$inferSelect;

export const insertScrapingChangeSchema = createInsertSchema(scrapingChangesTable).omit({ id: true });
export type InsertScrapingChange = z.infer<typeof insertScrapingChangeSchema>;
export type ScrapingChange = typeof scrapingChangesTable.$inferSelect;

export const insertScrapeRuntimeJobSchema = createInsertSchema(scrapeRuntimeJobsTable).omit({
  createdAt: true,
  updatedAt: true,
});
export type InsertScrapeRuntimeJob = z.infer<typeof insertScrapeRuntimeJobSchema>;
export type ScrapeRuntimeJob = typeof scrapeRuntimeJobsTable.$inferSelect;
export type ScrapeRuntimeLog = typeof scrapeRuntimeLogsTable.$inferSelect;

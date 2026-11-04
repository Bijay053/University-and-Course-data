import { pgTable, text, integer, jsonb, timestamp, unique, index } from "drizzle-orm/pg-core";

// Private staff conversations, separate from immutable scrape request evidence.
export const scrapeAssistantThreadsTable = pgTable("scrape_assistant_threads", {
  id: text("id").primaryKey(),
  jobId: text("job_id").notNull(),
  actorId: integer("actor_id").notNull(),
  turnId: text("turn_id").notNull(),
  status: text("status").notNull().default("idle"),
  phase: text("phase").notNull().default(""),
  error: text("error"),
  messages: jsonb("messages").notNull().default([]),
  activity: jsonb("activity").notNull().default([]),
  runId: text("run_id"),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
}, (table) => [
  unique("uq_scrape_assistant_owner").on(table.jobId, table.actorId),
  index("ix_scrape_assistant_threads_job_id").on(table.jobId),
]);

import { pgTable, serial, integer, timestamp, text } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { universitiesTable } from "./universities";

export const importJobsTable = pgTable("import_jobs", {
  id: serial("id").primaryKey(),
  universityId: integer("university_id").references(() => universitiesTable.id, { onDelete: "set null" }),
  universityName: text("university_name").notNull(),
  fileName: text("file_name").notNull(),
  status: text("status").notNull().default("pending"),
  totalRows: integer("total_rows"),
  importedRows: integer("imported_rows"),
  skippedRows: integer("skipped_rows"),
  errorMessage: text("error_message"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  completedAt: timestamp("completed_at", { withTimezone: true }),
});

export const insertImportJobSchema = createInsertSchema(importJobsTable).omit({ id: true, createdAt: true });
export type InsertImportJob = z.infer<typeof insertImportJobSchema>;
export type ImportJob = typeof importJobsTable.$inferSelect;

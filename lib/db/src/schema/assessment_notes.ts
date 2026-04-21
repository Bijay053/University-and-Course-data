import { pgTable, serial, integer, timestamp, text, jsonb } from "drizzle-orm/pg-core";
import { universitiesTable } from "./universities";

export const assessmentNotesTable = pgTable("assessment_notes", {
  id: serial("id").primaryKey(),
  universityId: integer("university_id").notNull().references(() => universitiesTable.id, { onDelete: "cascade" }),
  country: text("country").notNull(),
  rawText: text("raw_text").notNull(),
  parsedData: jsonb("parsed_data"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
});

export type AssessmentNote = typeof assessmentNotesTable.$inferSelect;

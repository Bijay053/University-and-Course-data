import { pgTable, serial, integer, timestamp, text, boolean } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";
import { coursesTable } from "./courses";

export const intakesTable = pgTable("intakes", {
  id: serial("id").primaryKey(),
  courseId: integer("course_id").notNull().references(() => coursesTable.id, { onDelete: "cascade" }),
  intakeMonth: text("intake_month").notNull(),
  intakeDay: integer("intake_day"),
  intakeYear: integer("intake_year"),
  isOpen: boolean("is_open").notNull().default(true),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const insertIntakeSchema = createInsertSchema(intakesTable).omit({ id: true, createdAt: true });
export type InsertIntake = z.infer<typeof insertIntakeSchema>;
export type Intake = typeof intakesTable.$inferSelect;

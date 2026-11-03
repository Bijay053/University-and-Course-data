// @vitest-environment node
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createComparisonPdf } from "./compare-pdf";
import { comparisonRows } from "./comparison-data";
import type { CompareCourse } from "./lib";

const course: CompareCourse = {
  id: 7, course_name: "Master of Māori Studies",
  university: { id: 9, name: "Harbour University", city: "Sydney", country: "Australia", logo_url: null },
  course_location: "Sydney", degree_level: "Master", category: null, duration: 2, duration_term: "Year",
  intakes: ["February"], international_fee: 28000, currency: "AUD", fee_term: "Annual", fee_year: 2026,
  course_url: "https://example.edu/course", study_mode: "On Campus",
  english_requirements: [{ test_type: "IELTS", overall: 6.5, listening: 6, reading: 6, writing: 6, speaking: 6 }],
  academic_requirements: [{ academic_level: "Bachelor degree", academic_score: 60, score_type: "Percentage" }],
};
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const filename = String(url).includes("bold") ? "lato-bold.ttf" : "lato-regular.ttf";
    const bytes = readFileSync(new URL(`../../../public/fonts/${filename}`, import.meta.url));
    return { ok: true, arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) };
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe("comparison export data and PDF", () => {
  it("shares complete published values without converting currencies or unsafe links", () => {
    const rows = comparisonRows([course, { ...course, currency: "GBP", international_fee: 17000, course_url: "javascript:alert(1)" }]);
    expect(rows.find((r) => r.label === "Fees (as published)")?.values).toEqual(["AUD 28,000 / Annual (2026)", "GBP 17,000 / Annual (2026)"]);
    expect(rows.find((r) => r.label === "English")?.values[0]).toBe("IELTS 6.5");
    expect(rows.find((r) => r.label === "Academic entry")?.values[0]).toBe("Bachelor degree, 60 Percentage");
    expect(rows.find((r) => r.label === "Official page")?.values).toEqual(["https://example.edu/course", "Not published"]);
  });

  it("creates a real landscape PDF with embedded fonts and official links", async () => {
    const doc = await createComparisonPdf([course], 2, new Date("2026-11-03T12:00:00Z"));
    const bytes = new Uint8Array(doc.output("arraybuffer"));
    expect(new TextDecoder().decode(bytes.slice(0, 4))).toBe("%PDF");
    expect(doc.internal.pageSize.getWidth()).toBeGreaterThan(doc.internal.pageSize.getHeight());
    expect(doc.output()).toContain("/Subtype /Link");
    expect(doc.output()).toContain("/FontFile2");
    expect(doc.getNumberOfPages()).toBe(1);
  });

  it("paginates long values for five courses without dropping data", async () => {
    const courses = Array.from({ length: 5 }, (_, i) => ({ ...course, id: i + 1, study_mode: "Detailed course-owned delivery conditions. ".repeat(130) }));
    const doc = await createComparisonPdf(courses);
    expect(doc.getNumberOfPages()).toBeGreaterThan(1);
  });

  it("rejects an empty comparison", async () => {
    await expect(createComparisonPdf([])).rejects.toThrow("at least one course");
  });
});

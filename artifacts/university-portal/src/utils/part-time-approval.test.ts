import { describe, expect, it } from "vitest";
import { blockedApprovalIds, isPartTimeOnlyCourse } from "./part-time-approval";

describe("part-time-only approval guard", () => {
  it("blocks canonical part-time-only rows and legacy PT-only modes despite defaulted FullTime loads", () => {
    expect(isPartTimeOnlyCourse({ studyLoad: "PartTimeOnly" })).toBe(true);
    expect(isPartTimeOnlyCourse({ study_load: "Part-time only" })).toBe(true);
    expect(isPartTimeOnlyCourse({ studyLoad: "FullTime", studyMode: "Part-time only" })).toBe(true);
    expect(isPartTimeOnlyCourse({ study_load: "FullTime", study_mode: "PartTimeOnly" })).toBe(true);
    expect(isPartTimeOnlyCourse({ studyMode: "Part-time only; 1 year equivalent full-time study", studyLoad: "Full Time" })).toBe(true);
  });

  it("allows mixed/full-time and unknown rows", () => {
    expect(isPartTimeOnlyCourse({ studyLoad: "Fulltime/Parttime" })).toBe(false);
    expect(isPartTimeOnlyCourse({ study_mode: "Part-time and Full-time" })).toBe(false);
    expect(isPartTimeOnlyCourse({ studyMode: "Both", studyLoad: "Part Time" })).toBe(false);
    expect(isPartTimeOnlyCourse({ studyLoad: "FullTime" })).toBe(false);
    expect(isPartTimeOnlyCourse({})).toBe(false);
  });

  it("reports blocked IDs separately without excluding allowed selected rows", () => {
    expect(blockedApprovalIds([
      { id: 1, studyLoad: "PartTimeOnly" },
      { id: 2, studyLoad: "FullTime/PartTime" },
      { id: 3, studyMode: null },
    ], [1, 2, 3])).toEqual([1]);
  });
});
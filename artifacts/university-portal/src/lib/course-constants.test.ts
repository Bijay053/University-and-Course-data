import { describe, expect, it } from "vitest";

import { FEE_TERM_OPTIONS, FEE_TERMS } from "./course-constants";

describe("fee term options", () => {
  it("includes every supported fee period with annual and semester visible first", () => {
    expect(FEE_TERM_OPTIONS.slice(0, 2)).toEqual([
      { value: "Annual", label: "Annual (Per Year)" },
      { value: "Semester", label: "Per Semester" },
    ]);
    expect(FEE_TERMS).toContain("Full Course");
    expect(FEE_TERMS).toContain("Per Credit Hour");
    expect(new Set(FEE_TERMS).size).toBe(FEE_TERMS.length);
  });
});
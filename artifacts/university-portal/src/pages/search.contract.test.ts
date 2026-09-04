import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { appendAcademicCredentialParams } from "./search-query";

const source = readFileSync(new URL("./search.tsx", import.meta.url), "utf8");

describe("course search destination-country contract", () => {
  it("sends the backend country filter instead of the ignored residence parameter", () => {
    expect(source).toContain('params.set("country", country)');
    expect(source).not.toContain('params.set("country_residence", country)');
  });

  it("labels university-country options as the study destination", () => {
    expect(source).toContain("Study Destination Country");
    expect(source).not.toContain("Country of Residence");
  });
});

describe("course search academic credential contract", () => {
  it("sends qualification, scheme, scale, and achieved score together", () => {
    const params = new URLSearchParams();
    appendAcademicCredentialParams(params, {
      qualification: "Bachelor's degree",
      scheme: "GPA",
      outOf: "5",
      gradingScore: "4.2",
    });

    expect(params.toString()).toBe(
      "highest_qualification=Bachelor%27s+degree"
      + "&grading_scheme=GPA&grading_out_of=5&grading_score=4.2",
    );
  });

  it("explains that unknown course requirements remain eligible", () => {
    expect(source).toContain(
      "Courses with unpublished academic requirements remain included.",
    );
  });

  it("constrains GPA input to the selected scale", () => {
    expect(source).toContain("max={outOf ? Number(outOf) : undefined}");
    expect(source).toContain("disabled={!outOf}");
  });
});

describe("course search filter summary", () => {
  it("shows the number of active filters beside the filter heading", () => {
    expect(source).toContain("activeFilterCount");
    expect(source).toContain("{activeFilterCount} active");
    expect(source).toContain('aria-live="polite"');
  });
});

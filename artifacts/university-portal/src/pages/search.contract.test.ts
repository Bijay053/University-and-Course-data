import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

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

  it("shows the number of active filters beside the filter heading", () => {
    expect(source).toContain("activeFilterCount");
    expect(source).toContain("{activeFilterCount} active");
    expect(source).toContain('aria-live="polite"');
  });
});
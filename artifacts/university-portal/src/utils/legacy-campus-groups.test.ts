import { describe, expect, it } from "vitest";
import { groupLegacyCampusRows } from "./legacy-campus-groups";

function campusRow(id: number, campus: string, extras: Record<string, unknown> = {}) {
  return {
    id,
    university_id: 5,
    scrape_job_id: "job-1",
    course_name: "MSc Healthcare Management",
    course_website: "https://example.edu/msc-healthcare-management/",
    course_location: campus,
    degree_level: "Master",
    fee_year: 2026,
    fee_term: "Annual",
    currency: "GBP",
    international_fee: campus === "Manchester" ? 18000 : 19500,
    extraction_method: {
      campus_fee_scope: { split_from_id: 72, original_name: "MSc Healthcare Management" },
      fee_variants: {
        selected: [{ campus, amount: campus === "Manchester" ? 18000 : 19500, year: 2026, period: "Annual", study_variant: "Standard" }],
      },
    },
    ...extras,
  };
}

describe("legacy campus logical course grouping", () => {
  it("renders a two-city legacy split as one logical course without removing persisted entries", () => {
    const rows = [campusRow(101, "Manchester"), campusRow(102, "Birmingham")];
    const groups = groupLegacyCampusRows(rows);
    expect(groups).toHaveLength(1);
    expect(groups[0].ids).toEqual([101, 102]);
    expect(groups[0].members.map(row => row.course_location)).toEqual(["Manchester", "Birmingham"]);
    expect(rows).toHaveLength(2);
  });

  it("leaves ordinary same-title/source rows ungrouped and does not preselect the visual group", () => {
    const rows = [campusRow(1, "Manchester", { extraction_method: {} }), campusRow(2, "Birmingham", { extraction_method: {} })];
    const groups = groupLegacyCampusRows(rows);
    expect(groups).toHaveLength(2);
    expect(groups.every(group => group.ids.length === 1)).toBe(true);
  });

  it("isolates different awards, study variants, source runs and fee cohorts", () => {
    const rows = [
      campusRow(1, "Manchester"),
      campusRow(2, "Birmingham", { degree_level: "Bachelor" }),
      campusRow(3, "Leeds", { extraction_method: {
        campus_fee_scope: { split_from_id: 72, original_name: "MSc Healthcare Management" },
        fee_variants: { selected: [{ campus: "Leeds", year: 2026, period: "Annual", study_variant: "Placement" }] },
      } }),
      campusRow(4, "London", { fee_year: 2025 }),
      campusRow(5, "Bristol", { scrape_job_id: "job-2" }),
    ];
    expect(groupLegacyCampusRows(rows)).toHaveLength(5);
  });
});

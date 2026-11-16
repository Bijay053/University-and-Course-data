// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { FixFieldCounts } from "./fix-field-counts";
import { fixFieldChanges } from "@/utils/fix-field-changes";

describe("persisted correction counts", () => {
  const rows = [
    ...Array.from({ length: 40 }, () => ({
      ok: true, outcome: "no_progress", updated_fields: ["intake_months"],
      refreshed_evidence_fields: ["intake_months"],
    })),
    ...Array.from({ length: 60 }, () => ({
      ok: true, outcome: "no_progress", updated_fields: ["scrape_warnings"],
      refreshed_evidence_fields: ["intake_months"],
    })),
    ...Array.from({ length: 77 }, () => ({ ok: true, outcome: "no_progress" })),
    ...Array.from({ length: 5 }, () => ({ ok: false, outcome: "failed" })),
  ];
  it("repairs the historical 182-course summary without rerunning the repair", () => {
    expect(fixFieldChanges(rows, ["intake_months"])).toEqual({
      valueUpdated: 40, valueUnchanged: 137, evidenceRefreshedOnly: 60,
    });
    render(<FixFieldCounts results={rows} requested={["intake_months"]} />);
    expect(screen.getByText("40 courses with field values corrected")).toBeTruthy();
    expect(screen.getByText("137 with field values unchanged")).toBeTruthy();
    expect(screen.getByText("60 of these had evidence refreshed")).toBeTruthy();
  });
  it("excludes failures, unattempted work, metadata and unrelated updates", () => {
    expect(fixFieldChanges([
      { ok: false, updated_fields: ["intake_months"] },
      { attempted: false, updated_fields: ["intake_months"] },
      { outcome: "already_resolved" },
      { outcome: "skipped" },
      { updated_fields: ["category", "scrape_warnings"] },
    ], ["intake_months"])).toEqual({
      valueUpdated: 0, valueUnchanged: 1, evidenceRefreshedOnly: 0,
    });
  });
  it("counts semantic companions once per course without counting refreshed proof as a value change", () => {
    expect(fixFieldChanges([
      { updated_fields: ["ielts_reading", "ielts_writing"] },
      { refreshed_evidence_fields: ["ielts_listening"] },
    ], ["ielts_overall"])).toEqual({
      valueUpdated: 1, valueUnchanged: 1, evidenceRefreshedOnly: 1,
    });
    expect(fixFieldChanges([{ updated_fields: ["fee_year", "currency"] }], ["international_fee"]).valueUpdated).toBe(1);
    expect(fixFieldChanges([], ["intake_months"]).valueUpdated).toBe(0);
  });
});

// @vitest-environment jsdom
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { IntakeFeeSchedules, type IntakeFeeSchedule } from "./intake-fee-schedules";
import { AssistantIntakeConsolidation } from "./assistant-intake-consolidation";

afterEach(cleanup);
const schedules: IntakeFeeSchedule[] = [
  { feeYear: 2026, amount: 24800, currency: "GBP", basis: "Annual",
    entryYear: 2027, intakes: ["January", "April"], sourceRowIds: [1, 2],
    sourceUrls: ["https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/january-2027"],
    applicabilityNeedsReview: false },
  { feeYear: 2027, amount: 24800, currency: "GBP", basis: "Annual",
    entryYear: 2027, intakes: ["September"], sourceRowIds: [3],
    sourceUrls: ["https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/september-2027"],
    applicabilityNeedsReview: false },
];
describe("intake-specific tuition", () => {
  it("shows both fee years and their intake applicability without annualising or inventing academic labels", () => {
    render(<IntakeFeeSchedules schedules={schedules} />);
    expect(screen.getByText("Fee year 2026:")).toBeTruthy();
    expect(screen.getByText("Fee year 2027:")).toBeTruthy();
    expect(screen.getByText("Proposed intake applicability: January, April 2027")).toBeTruthy();
    expect(screen.getByText("Proposed intake applicability: September 2027")).toBeTruthy();
    expect(screen.getByText(/Manual approval required/)).toBeTruthy();
    expect(screen.queryByText("2026/27")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });
  it("warns when current data differs from the retained snapshot", () => {
    render(<IntakeFeeSchedules schedules={schedules} snapshotMatchesCurrent={false} />);
    expect(screen.getByRole("alert").textContent).toContain("Current course values differ");
  });
  it("keeps mismatched intake applicability visibly unverified", () => {
    render(<IntakeFeeSchedules schedules={[{ ...schedules[1], applicabilityNeedsReview: true }]} />);
    expect(screen.getByText("Intake mismatch needs correction before merging.")).toBeTruthy();
  });
  it("never renders an unsafe source URL", () => {
    render(<IntakeFeeSchedules schedules={[{ ...schedules[0], sourceUrls: ["javascript:alert(1)"] }]} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
  it("includes the full schedules in the merge confirmation preview", () => {
    render(<AssistantIntakeConsolidation preview={{
      courseName: "Advanced Computer Science MSc", canonicalUrl: "https://www.aston.ac.uk",
      entryYear: 2027, intakes: ["January", "April", "September"],
      keepId: 1, excludeIds: [2, 3], conflicts: {}, rows: [], feeSchedules: schedules,
    }} />);
    expect(screen.getByRole("region", { name: "Intake-specific tuition schedules" })).toBeTruthy();
    expect(screen.getByText("Fee year 2026:")).toBeTruthy();
    expect(screen.getByText("Fee year 2027:")).toBeTruthy();
  });
});

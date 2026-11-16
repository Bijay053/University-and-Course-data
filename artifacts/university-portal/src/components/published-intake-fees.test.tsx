// @vitest-environment jsdom
import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { PublishedFeeSchedule } from "@workspace/api-client-react";
import { PublishedIntakeFees } from "./published-intake-fees";
import { CourseCampusFees, courseFeeExportColumns } from "./course-campus-fees";
import { feeLines, formatFeeLine, headlineFee, type CourseResult } from "../pages/degrees/lib";

afterEach(cleanup);
const schedules: PublishedFeeSchedule[] = [
  { id: 1, feeYear: 2026, entryYear: 2027, amount: 24800, currency: "GBP", basis: "Annual",
    intakes: ["January", "April"], sourceUrls: ["https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/april-2027"] },
  { id: 2, feeYear: 2027, entryYear: 2027, amount: 24800, currency: "GBP", basis: "Annual",
    intakes: ["September"], sourceUrls: ["https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/september-2027"] },
];

describe("published intake-specific tuition", () => {
  it("keeps academic fee year separate from entry year, including equal-price fees", () => {
    render(<PublishedIntakeFees schedules={schedules} />);
    expect(screen.getByText("January, April 2027")).toBeTruthy();
    expect(screen.getByText("September 2027")).toBeTruthy();
    expect(screen.getByText("Fee year 2026")).toBeTruthy();
    expect(screen.getByText("Fee year 2027")).toBeTruthy();
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(screen.queryByText(/unverified|Publication held/i)).toBeNull();
  });
  it("does not expose unsafe source URLs", () => {
    render(<PublishedIntakeFees schedules={[{ ...schedules[0], sourceUrls: ["javascript:alert(1)"] }]} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
  it("never falls back to an unrelated scalar fee", () => {
    render(<CourseCampusFees course={{ feeSchedules: schedules, internationalFee: 999999 }} />);
    expect(screen.getByText("January, April 2027")).toBeTruthy();
    expect(screen.queryByText(/999,999/)).toBeNull();
    expect(courseFeeExportColumns({ feeSchedules: schedules, internationalFee: 999999 })["Campus fees"])
      .toContain("September 2027");
  });
  it("reports damaged applicability rather than inventing a headline fee", () => {
    render(<PublishedIntakeFees schedules={schedules} reviewRequired />);
    expect(screen.getByRole("alert").textContent).toContain("requires review");
    expect(screen.queryByText("Fee year 2026")).toBeNull();
  });
  it("preserves both schedules in search cards, comparison and PDF text", () => {
    const course = { fee_schedules: schedules, international_fee: 999999 } as CourseResult;
    const lines = feeLines(course).map(formatFeeLine);
    expect(lines).toEqual([
      "GBP 24,800 / Annual (2026) · January, April 2027",
      "GBP 24,800 / Annual (2027) · September 2027",
    ]);
    expect(headlineFee(course)).toContain("January, April 2027");
    expect(headlineFee(course)).toContain("September 2027");
    expect(headlineFee(course)).not.toContain("locations");
  });
});

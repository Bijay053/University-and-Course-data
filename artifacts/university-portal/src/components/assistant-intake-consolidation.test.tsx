// @vitest-environment jsdom
import React from "react";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AssistantIntakeConsolidation, type IntakeConsolidation } from "./assistant-intake-consolidation";
import { actionNeedsConfirmation, type AssistantAction } from "./scrape-prompt-assistant";

afterEach(cleanup);
const preview: IntakeConsolidation = {
  courseName: "Advanced Computer Science MSc",
  canonicalUrl: "https://www.aston.ac.uk/study/courses/advanced-computer-science-msc",
  entryYear: 2027, keepId: 59825, excludeIds: [59826, 59827],
  intakes: ["January", "April", "September"], conflicts: {},
  rows: [
    { id: 59825, name: "Advanced Computer Science MSc (January 2027)", url: "https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/january-2027", intakes: ["January"], fee: 24800, currency: "GBP", feeYear: 2026 },
    { id: 59826, name: "Advanced Computer Science MSc (April 2027)", url: "https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/april-2027", intakes: ["April"], fee: 24800, currency: "GBP", feeYear: 2026 },
    { id: 59827, name: "Advanced Computer Science MSc (September 2027)", url: "https://www.aston.ac.uk/study/courses/advanced-computer-science-msc/september-2027", intakes: ["January"], fee: 24800, currency: "GBP", feeYear: 2027 },
  ],
};

describe("intake consolidation preview", () => {
  it("shows one proposed course, all intakes, exact retained and excluded rows", () => {
    render(<AssistantIntakeConsolidation preview={preview} />);
    expect(screen.getByRole("heading").textContent).toBe("Advanced Computer Science MSc");
    expect(screen.getByText("Proposed intakes: January, April, September 2027")).toBeTruthy();
    expect(screen.getByText(/Keep row 59825; softly exclude 59826, 59827/)).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(4);
    expect(screen.getAllByRole("link")).toHaveLength(3);
    expect(screen.getByText("Keep and rename")).toBeTruthy();
    expect(screen.getAllByText("Soft exclude after merge")).toHaveLength(2);
  });

  it("shows fee-year and intake conflicts instead of pretending a merge succeeded", () => {
    render(<AssistantIntakeConsolidation preview={{ ...preview, conflicts: {
      fee_year: [{ id: 59825, value: "2026" }, { id: 59827, value: "2027" }],
      intake_months: [{ id: 59827, value: ["January"], expectedIntake: "September" }],
    } }} />);
    expect(screen.getByRole("status").textContent).toContain("Merge blocked");
    expect(screen.getByRole("status").textContent).toContain("fee_year: row 59825: 2026");
    expect(screen.getByRole("status").textContent).toContain("intake offering: September");
  });

  it("does not render unsafe source links", () => {
    render(<AssistantIntakeConsolidation preview={{ ...preview, rows: [
      { ...preview.rows[0], url: "javascript:alert(1)" },
    ] }} />);
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("honours explicit confirmation and always confirms undo", () => {
    const action: AssistantAction = { id: "merge", kind: "merge_intakes", label: "Merge",
      courseCount: 3, state: "proposed", fields: [], requiresConfirmation: false };
    expect(actionNeedsConfirmation(action)).toBe(false);
    expect(actionNeedsConfirmation({ ...action, requiresConfirmation: true })).toBe(true);
    expect(actionNeedsConfirmation({ ...action, kind: "undo_intake_merge" })).toBe(true);
  });
});

// @vitest-environment jsdom
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DocumentReviewView } from "./assistant-document-review";

afterEach(cleanup);

it("shows all five current and proposed IELTS scores without applying changes", () => {
  const fields = ["overall", "listening", "reading", "writing", "speaking"];
  render(<DocumentReviewView testId="report" review={{
    checked: 1, proposed: 1, unmatched: 0,
    rows: [{ id: 10, name: "Accounting and Finance", status: "proposed", applicability: "verified",
      currentValues: { ielts_overall: 6 },
      proposedValues: Object.fromEntries(fields.map(f => [`ielts_${f}`, f === "overall" ? 6 : 5.5])),
      changes: fields.filter(f => f !== "overall").map(f => ({
        field: `ielts_${f}`, oldValue: null, newValue: 5.5, quote: "No skill below 5.5.", page: 1,
      })),
    }],
  }} />);
  const preview = screen.getByTestId("document-ielts-preview-10");
  for (const field of fields) expect(preview.textContent).toContain(field);
  expect(preview.textContent).toContain("Existing");
  expect(preview.textContent).toContain("Proposed");
  expect(preview.textContent).toContain("no changes applied");
  expect(preview.querySelectorAll("tbody tr")).toHaveLength(5);
});

it("retains unverified and excluded rows alongside exact applicability citations", () => {
  render(<DocumentReviewView testId="report" review={{
    checked: 2, proposed: 1, unmatched: 1, notChecked: 1,
    rows: [
      { id: 1, name: "Business", status: "proposed", category: "undergraduate",
        applicability: "verified", evidence: [{ url: "https://uni.ac.uk/english.pdf", page: 2,
          quote: "All undergraduate courses require IELTS overall 6.5." }] },
      { id: 2, name: "Nursing", status: "excluded", category: "health",
        applicability: "excluded", reason: "Course-specific evidence required." },
      { id: 3, name: "Research", status: "not_checked", category: "postgraduate_research",
        applicability: "unverified", reason: "Robots access unavailable." },
    ],
  }} />);
  expect(screen.getByTestId("document-row-1").textContent).toContain("Applicability: verified");
  expect(screen.getByRole("link").getAttribute("href")).toBe("https://uni.ac.uk/english.pdf");
  expect(screen.getByRole("link").textContent).toContain("page 2");
  expect(screen.getByTestId("document-row-2").textContent).toContain("health");
  expect(screen.getByTestId("document-row-3").textContent).toContain("Applicability: unverified");
  expect(screen.getByTestId("report-counts").textContent).toContain("1 not checked");
});

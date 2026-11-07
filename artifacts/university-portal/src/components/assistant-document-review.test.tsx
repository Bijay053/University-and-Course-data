// @vitest-environment jsdom
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DocumentReviewView } from "./assistant-document-review";

afterEach(cleanup);

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

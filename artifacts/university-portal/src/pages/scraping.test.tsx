// @vitest-environment jsdom

import React from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScrapingForTest, type ScrapingInitialReviewState } from "./scraping";

vi.mock("@workspace/api-client-react", () => ({
  useListUniversities: () => ({
    data: { data: [{ id: 7, name: "Batch University", country: "AU", city: "Sydney" }] },
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/components/can", () => ({
  Can: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useCan: () => ({ can: () => true, canAny: () => true }),
}));

vi.mock("@/components/scrape-job-card", () => ({
  ScrapeJobCard: () => null,
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function initialReview(): ScrapingInitialReviewState {
  return {
    universityId: 7,
    jobId: "repair-job",
    courses: Array.from({ length: 51 }, (_, index) => ({
      id: index + 1,
      scrapeJobId: "repair-job",
      universityId: 7,
      courseName: `Course ${index + 1}`,
      courseWebsite: `https://example.test/courses/${index + 1}`,
      status: "pending",
      intakeMonths: [],
      scrapeWarnings: [],
      createdAt: "2026-09-03T00:00:00Z",
    })) as unknown as ScrapingInitialReviewState["courses"],
  };
}

describe("Scraping repair reviewer", () => {
  it("renders field summaries from every re-extract batch with value updates taking precedence", async () => {
    const review = initialReview();
    const reextractBodies: Array<{ ids: number[]; universityId: number }> = [];
    let analyzeCalls = 0;

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse(review.courses);

      if (url === "/api/scrape/staged/analyze") {
        analyzeCalls += 1;
        const body = JSON.parse(String(init?.body));
        return jsonResponse({
          total: body.ids.length,
          courses_with_url: body.ids.length,
          issues: analyzeCalls <= 2
            ? [{ field: "international_fee", label: "Missing fee", missing: body.ids.length, total: body.ids.length, current_pct: 0, expected_fill_pct: 80 }]
            : [],
        });
      }

      if (url === "/api/scrape/staged/re-extract") {
        const body = JSON.parse(String(init?.body));
        reextractBodies.push(body);
        return reextractBodies.length === 1
          ? jsonResponse({
              updated: 50, skipped: 0, errors: 0, total: 50,
              results: [{
                updated_fields: ["international_fee"],
                refreshed_evidence_fields: ["duration", "ielts_overall"],
              }],
            })
          : jsonResponse({
              updated: 1, skipped: 0, errors: 0, total: 1,
              results: [{
                updated_fields: ["ielts_overall"],
                refreshed_evidence_fields: ["study_mode", "international_fee"],
              }],
            });
      }

      return jsonResponse({});
    }));

    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);

    const selectAll = screen.getAllByRole("checkbox")[0];
    await user.click(selectAll);
    await user.click(selectAll);
    await user.click(screen.getByRole("button", { name: "Fix (51)" }));
    await user.click(await screen.findByRole("button", { name: "Confirm Fix (51)" }));

    const dialog = await screen.findByRole("dialog", { name: "Fix Results" });
    expect(within(dialog).getByText("Re-extracted 51 of 51")).toBeTruthy();
    const valueSummary = within(dialog).getByText("Values updated").parentElement;
    const sourceSummary = within(dialog).getByText("Sources refreshed — values unchanged").parentElement;
    expect(valueSummary?.textContent).toContain("IELTS, International Fee");
    expect(sourceSummary?.textContent).toContain("Duration, Study Mode");
    expect(sourceSummary?.textContent).not.toContain("IELTS");
    expect(sourceSummary?.textContent).not.toContain("International Fee");

    await waitFor(() => expect(reextractBodies).toHaveLength(2));
    expect(reextractBodies.map(({ ids }) => ids.length)).toEqual([50, 1]);
    expect(reextractBodies.flatMap(({ ids }) => ids)).toEqual(
      Array.from({ length: 51 }, (_, index) => index + 1),
    );
  });
});
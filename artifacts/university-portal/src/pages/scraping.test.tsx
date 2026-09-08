// @vitest-environment jsdom

import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getFixResultHeading,
  ScrapingForTest,
  type ScrapingInitialReviewState,
} from "./scraping";

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
  localStorage.clear();
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
  it("does not call an all-no-progress Fix successful", () => {
    expect(getFixResultHeading({
      total: 3,
      updated: 0,
      skipped: 3,
      errors: 0,
    })).toBe("No progress");
  });

  it("uses unchanged requested gaps instead of unrelated updates for the result heading", () => {
    const issue = {
      field: "international_fee",
      label: "International Fee",
      missing: 3,
      total: 3,
      current_pct: 0,
      expected_fill_pct: 80,
    };
    expect(getFixResultHeading({
      total: 3,
      updated: 3,
      skipped: 0,
      errors: 0,
      beforeIssues: [issue],
      afterIssues: [issue],
      afterAnalysisComplete: true,
    })).toBe("No progress");
  });

  it("forces a fresh authenticated staged-course request when Refresh is clicked", async () => {
    const review = initialReview();
    const stagedRequests: RequestInit[] = [];

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url === "/api/scrape/staged/repair-job") {
        stagedRequests.push(init ?? {});
        return jsonResponse(review.courses);
      }
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      return jsonResponse({});
    }));

    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);

    const refreshButton = screen.getByTitle(
      "Reload staged courses and refresh quality scores",
    ) as HTMLButtonElement;
    await user.click(refreshButton);

    await waitFor(() => expect(stagedRequests).toHaveLength(1));
    expect(stagedRequests[0]).toMatchObject({
      credentials: "include",
      cache: "no-store",
    });
    await waitFor(() => {
      expect(refreshButton.disabled).toBe(false);
    });
  });

  it("renders target field summaries from a completed background Fix job", async () => {
    const review = initialReview();
    const fixBodies: Array<{
      ids: number[];
      universityId: number;
      sourceJobId: string;
      targetFields: string[];
      forceFields: string[];
      forceReasons: Record<string, string>;
    }> = [];
    let analyzeCalls = 0;

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse(review.courses);
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);

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

      if (url === "/api/scrape/staged/fix-jobs") {
        const body = JSON.parse(String(init?.body));
        fixBodies.push(body);
        return jsonResponse({
          jobId: "fix-test",
          sourceJobId: "repair-job",
          status: "queued",
          total: 51,
          queued: 51,
          running: 0,
          completed: 0,
          noProgress: 0,
          failed: 0,
          processed: 0,
          results: [],
          errorMessage: null,
        });
      }

      if (url === "/api/scrape/staged/fix-jobs/fix-test") {
        return jsonResponse({
          jobId: "fix-test",
          sourceJobId: "repair-job",
          status: "completed",
          total: 51,
          queued: 0,
          running: 0,
          completed: 51,
          noProgress: 0,
          failed: 0,
          processed: 51,
          results: [
            {
              id: 1,
              ok: true,
              outcome: "completed",
              updated_fields: ["international_fee", "ielts_overall"],
              refreshed_evidence_fields: ["duration"],
            },
            {
              id: 51,
              ok: true,
              outcome: "completed",
              updated_fields: ["ielts_overall"],
              refreshed_evidence_fields: ["study_mode", "international_fee"],
            },
          ],
          errorMessage: null,
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
    const previewDialog = await screen.findByRole("dialog", {
      name: "Review Before Fixing",
      description: "Review the pending re-extraction action before applying it to the selected courses.",
    });
    await user.click(within(previewDialog).getByRole("button", { name: "Confirm Fix (51)" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Fix Results",
      description: "Review the completed re-extraction summary for the selected courses.",
    }, { timeout: 6000 });
    expect(within(dialog).getByText("Processed 51 of 51")).toBeTruthy();
    expect(within(dialog).getByText("Successful")).toBeTruthy();
    const valueSummary = within(dialog).getByText("Requested values updated").parentElement;
    const sourceSummary = within(dialog).getByText("Sources refreshed — values unchanged").parentElement;
    expect(valueSummary?.textContent).toContain("IELTS, International Fee");
    expect(sourceSummary?.textContent).toContain("Duration, Study Mode");
    expect(sourceSummary?.textContent).not.toContain("IELTS");
    expect(sourceSummary?.textContent).not.toContain("International Fee");

    expect(fixBodies).toHaveLength(1);
    expect(fixBodies[0]).toEqual({
      ids: Array.from({ length: 51 }, (_, index) => index + 1),
      universityId: 7,
      sourceJobId: "repair-job",
      targetFields: ["international_fee"],
      forceFields: [],
      forceReasons: {},
    });
  }, 10_000);

  it("requires reasons for forced fields and sends them with the union of detected targets", async () => {
    const review = initialReview();
    const fixBodies: Array<{
      targetFields: string[];
      forceFields: string[];
      forceReasons: Record<string, string>;
    }> = [];

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse(review.courses);
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      if (url === "/api/scrape/staged/analyze") {
        const body = JSON.parse(String(init?.body));
        return jsonResponse({
          total: body.ids.length,
          courses_with_url: body.ids.length,
          issues: [{ field: "ielts_overall", label: "Missing IELTS", missing: body.ids.length, total: body.ids.length, current_pct: 0, expected_fill_pct: 80 }],
        });
      }
      if (url === "/api/scrape/staged/fix-jobs") {
        fixBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({
          jobId: "forced-fix",
          sourceJobId: "repair-job",
          status: "queued",
          total: 51,
          queued: 51,
          running: 0,
          completed: 0,
          noProgress: 0,
          failed: 0,
          processed: 0,
          results: [],
          errorMessage: null,
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

    const previewDialog = await screen.findByRole("dialog", { name: "Review Before Fixing" });
    expect(within(previewDialog).getByText(/Existing values will be overwritten/)).toBeTruthy();
    await user.click(within(previewDialog).getByLabelText("Force International Fee"));
    await user.click(within(previewDialog).getByLabelText("Force Course Location"));
    await user.click(within(previewDialog).getByLabelText("Force Intake"));

    const confirmButton = within(previewDialog).getByRole("button", { name: "Confirm Fix (51)" });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(true);
    const reasonInputs = within(previewDialog).getAllByLabelText(/Correction reason/);
    fireEvent.change(reasonInputs[0], { target: { value: "Published fee is outdated" } });
    fireEvent.change(reasonInputs[1], { target: { value: "Campus list is incomplete" } });
    fireEvent.change(reasonInputs[2], { target: { value: "Intake includes unrelated months" } });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(false);
    await user.click(confirmButton);

    await waitFor(() => expect(fixBodies).toHaveLength(1));
    expect(fixBodies[0]).toMatchObject({
      targetFields: ["ielts_overall", "international_fee", "course_location", "intake"],
      forceFields: ["international_fee", "course_location", "intake"],
      forceReasons: {
        international_fee: "Published fee is outdated",
        course_location: "Campus list is incomplete",
        intake: "Intake includes unrelated months",
      },
    });
  }, 10_000);
});
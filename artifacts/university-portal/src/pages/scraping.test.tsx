// @vitest-environment jsdom

import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  annualFeeEquivalentForDisplay,
  formatRecoveryDiagnosticMessage,
  getFixResultHeading,
  SmartFixDetails,
  smartFixReportPrefill,
  isRequestedFixField,
  normalizeRequirementStatus,
  requirementRepairFields,
  ScrapingForTest,
  type ScrapingInitialReviewState,
  visibleScrapeStatus,
} from "./scraping";

describe("annualFeeEquivalentForDisplay", () => {
  it("does not annualize full-course fees shorter than 12 months", () => {
    expect(annualFeeEquivalentForDisplay(48_300, 8, "Month")).toBeNull();
    expect(annualFeeEquivalentForDisplay(24_000, 51, "Week")).toBeNull();
  });

  it("annualizes full-course fees lasting at least one year", () => {
    expect(annualFeeEquivalentForDisplay(48_000, 12, "Month")).toBe(48_000);
    expect(annualFeeEquivalentForDisplay(96_000, 2, "Year")).toBe(48_000);
  });
});

describe("Smart Fix results contract", () => {
  const issue = { field: "international_fee", label: "Fee", missing: 1, total: 1, current_pct: 0, expected_fill_pct: 0 };
  const base = {
    smart: true, total: 1, updated: 1, skipped: 0, errors: 0,
    requestedFields: ["international_fee", "duration"],
    afterAnalysisComplete: true, afterIssues: [],
    courseResults: [{ id: 1, ok: true, outcome: "completed" as const, attempted: true, resolved_fields: ["duration"], unresolved_fields: ["international_fee"] }],
  };
  it("uses fresh issue analysis for partial, full, unchanged and failed results", () => {
    expect(getFixResultHeading({ ...base, afterIssues: [issue] })).toBe("Partially successful");
    expect(getFixResultHeading(base)).toBe("Successful");
    expect(getFixResultHeading({ ...base, beforeIssues: [issue], afterIssues: [issue] })).toBe("No progress");
    expect(getFixResultHeading({ ...base, updated: 0, errors: 1, courseResults: [] })).toBe("Failed");
    expect(getFixResultHeading({ ...base, afterAnalysisComplete: false })).toBe("Resolution not verified");
    expect(getFixResultHeading({ ...base, courseResults: [{ ...base.courseResults[0], unsupported_fields: ["score_type"] }] })).toBe("Partially successful");
  });
  it("groups explicit reasons, separates unattempted work, and only offers the indicated report action", async () => {
    const onReport = vi.fn();
    const row = { ...base.courseResults[0], outcome: "no_progress" as const, reason_code: "unresolved_after_official_recovery", next_action: "report_official_url", target_fields: ["duration", "international_fee"] };
    render(<SmartFixDetails result={{
      ...base, afterIssues: [issue], attempted: 2, notAttempted: 1,
      courseResults: [
        row,
        { ...row, id: 2, next_action: undefined },
        { id: 3, ok: true, outcome: "skipped", attempted: false, reason_code: "unsupported_targets", unsupported_fields: ["score_type"] },
      ],
    }} onReport={onReport} />);
    expect(screen.getByText("Attempted — unchanged: Selected issues remain after official-source recovery (2)")).toBeTruthy();
    expect(screen.getByText("Not attempted: Selected fields have no supported automatic check (1)")).toBeTruthy();
    expect(screen.getByText("1 selected issues remaining in fresh analysis")).toBeTruthy();
    const user = userEvent.setup();
    await user.click(screen.getByText("Attempted — unchanged: Selected issues remain after official-source recovery (2)"));
    expect(screen.getAllByRole("button", { name: "Report official URL" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Report official URL" }));
    expect(onReport).toHaveBeenCalledWith(row);
    expect(smartFixReportPrefill(row, { courseName: "Science", courseWebsite: "https://uni.test/science" })).toEqual({
      courseName: "Science", courseUrl: "https://uni.test/science", fields: ["other"],
      description: "Smart Fix: Science. Please verify these unresolved fields using the exact official source: International Fee.",
    });
  });
  it("renders sparse smart results without inventing a next action", () => {
    render(<SmartFixDetails result={{ ...base, courseResults: [{ id: 4, ok: false, outcome: "failed" }] }} onReport={vi.fn()} />);
    expect(screen.getByText("Failed: No detailed reason supplied (1)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Report official URL" })).toBeNull();
  });
  it.each(["initial_analysis_failed", "post_analysis_failed", "course_changed_during_fix"])("does not count saved values as resolution after %s", async (reason_code) => {
    const row = { id: 9, ok: false, outcome: "failed" as const, reason_code,
      resolved_fields: [], updated_fields: ["international_fee"], attempted: true };
    const result = { ...base, errors: 1, updated: 0, courseResults: [row] };
    expect(getFixResultHeading(result)).toBe("Resolution not verified");
    render(<SmartFixDetails result={result} />);
    await userEvent.setup().click(screen.getByText(/Failed: .*resolution unverified; recheck/));
    expect(screen.getByText("Resolved: Not verified — recheck this course")).toBeTruthy();
    expect(screen.getByText("Saved value changes (not verified as issue resolution): International Fee")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Report official URL" })).toBeNull();
  });
});

describe("visibleScrapeStatus", () => {
  it("shows completed lifecycle jobs with errors as a warning", () => {
    expect(visibleScrapeStatus("completed", 82)).toBe("completed_with_errors");
  });

  it("keeps clean lifecycle completion successful", () => {
    expect(visibleScrapeStatus("completed", 0)).toBe("completed");
  });
});

describe("formatRecoveryDiagnosticMessage", () => {
  it("uses backend operator labels for required recovery facts", () => {
    expect(formatRecoveryDiagnosticMessage({
      message: "AI extraction finished.",
      missing_required_fields: ["international_fee", "english_score"],
      missing_required_field_labels: ["International Fee", "English Requirements"],
    })).toBe(
      "AI extraction finished. Missing required facts: International Fee, English Requirements.",
    );
  });

  it("keeps future required facts readable when labels are absent", () => {
    expect(formatRecoveryDiagnosticMessage({
      message: "AI extraction finished.",
      missing_required_fields: ["professional_accreditation"],
    })).toBe(
      "AI extraction finished. Missing required facts: Professional Accreditation.",
    );
  });

  it("does not duplicate readable facts already present in the log message", () => {
    expect(formatRecoveryDiagnosticMessage({
      message: "AI extraction finished. Missing required facts: Duration.",
      missing_required_fields: ["duration"],
      missing_required_field_labels: ["Duration"],
    })).toBe("AI extraction finished. Missing required facts: Duration.");
  });
});

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
  it("shows linked Manchester/Birmingham campus rows as one unselected course and approves both staged IDs", async () => {
    const review = initialReview();
    const makeCampusRow = (id: number, courseLocation: string, fee: number) => ({
      ...review.courses[0],
      id,
      courseName: "MSc Healthcare Management",
      courseWebsite: "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/",
      courseLocation,
      degreeLevel: "Master",
      internationalFee: fee,
      currency: "GBP",
      feeTerm: "Annual",
      feeYear: 2026,
      extraction_method: {
        campus_fee_scope: { split_from_id: 190, original_name: "MSc Healthcare Management" },
        fee_variants: {
          selected: [{ amount: fee, currency: "GBP", year: 2026, period: "Annual", campus: courseLocation, study_variant: "Standard" }],
        },
      },
    });
    review.courses = [makeCampusRow(101, "Manchester", 18000), makeCampusRow(102, "Birmingham", 19500)] as ScrapingInitialReviewState["courses"];
    let submittedIds: number[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected") {
        const courseIds = JSON.parse(String(init?.body)).courseIds as number[];
        submittedIds.push(...courseIds);
        return jsonResponse({ approvedIds: submittedIds, approvedCount: submittedIds.length, failed: [], attempted: courseIds.length });
      }
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: review.courses });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.getByTestId("text-review-logical-course-count").textContent).toContain("1 course groups");
    expect(screen.getByTestId("text-review-entry-count").textContent).toContain("2 pending entries");
    expect(screen.getByText(/Campus-specific rows stay separate/)).toBeTruthy();
    expect(screen.getByTestId("text-campus-fees-101").textContent).toContain("Manchester:");
    expect(screen.getByTestId("text-campus-fees-101").textContent).toContain("18,000");
    expect(screen.getByTestId("text-campus-fees-101").textContent).toContain("Birmingham:");
    expect(screen.getByTestId("text-campus-fees-101").textContent).toContain("19,500");
    const checkbox = screen.getByTestId("checkbox-logical-course-101") as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
    await userEvent.click(checkbox);
    expect((screen.getByRole("button", { name: "Approve (2)" }) as HTMLButtonElement).disabled).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(submittedIds.sort()).toEqual([101, 102]));
  });

  it("submits one staged course with multiple campus fees without choosing a fee", async () => {
    const review = initialReview();
    const selected = [17500, 19050].map((amount, index) => ({
      amount, currency: "GBP", year: 2026, period: "Full Course",
      campus: index ? "London" : "Outside London", study_variant: "Standard",
      source_url: "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/",
      snippet: "International Students | 2026 | Full Course",
    }));
    review.courses = [{
      ...review.courses[0], internationalFee: null,
      extraction_method: { fee_variants: { status: "range", selected, options: selected } },
    }] as ScrapingInitialReviewState["courses"];
    let approved = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected") {
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({ courseIds: [1], force: false });
        approved = true;
        return jsonResponse({ approvedIds: [1], approvedCount: 1, failed: [], attempted: 1 });
      }
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: approved ? [] : review.courses });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.queryByTestId("button-split-campus-fees")).toBeNull();
    expect(screen.getByRole("button", { name: "Approve (1)" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Approve (1)" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("fee-summary-1")).toBeNull());
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/1/approve"))).toBe(false);
  });

  it("submits an unmatched course but leaves it selected and pending when the server cannot safely match locations", async () => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0], internationalFee: null,
      extraction_method: { fee_variants: { status: "range", selected: [], options: [] } },
    }] as ScrapingInitialReviewState["courses"];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected") {
        expect(JSON.parse(String(init?.body))).toEqual({ courseIds: [1], force: false });
        return jsonResponse({ approvedIds: [], approvedCount: 0, splitCount: 0, failed: [{ id: 1, error: "Campus mapping is ambiguous" }], attempted: 1 });
      }
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: review.courses });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve (1)" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toBe(true));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve (1)" }).hasAttribute("disabled")).toBe(false));
    expect(screen.getByTestId("fee-summary-1")).toBeTruthy();
  });

  it("submits mixed uniform and range fees together, retaining only failed original IDs", async () => {
    const review = initialReview();
    const options = (amounts: number[]) => amounts.map((amount, index) => ({
      amount, currency: "GBP", year: 2026, period: "Full Course",
      campus: index ? "London" : "Outside London", study_variant: "Standard",
      source_url: "https://www.law.ac.uk/study/postgraduate/business/mba/",
      snippet: "International Students | 2026 | Full Course",
    }));
    review.courses = review.courses.slice(0, 2).map((course, index) => {
      const selected = options(index ? [17500, 19050] : [20600, 20600]);
      return {
        ...course, internationalFee: index ? null : 20600,
        extraction_method: { fee_variants: {
          status: index ? "range" : "uniform", selected, options: selected,
        } },
      };
    }) as ScrapingInitialReviewState["courses"];
    let approved = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected") {
        const { courseIds, force } = JSON.parse(String(init?.body));
        expect(courseIds).toHaveLength(1);
        expect(force).toBe(false);
        if (courseIds[0] === 1) {
          approved = true;
          return jsonResponse({ approvedIds: [1], approvedCount: 1, splitCount: 0, failed: [], attempted: 1 });
        }
        return jsonResponse({ approvedIds: [], approvedCount: 0, splitCount: 0, failed: [{ id: 2, error: "Campus mapping is ambiguous" }], attempted: 1 });
      }
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: approved ? [review.courses[1]] : review.courses });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [{ id: 2, score: 40, tier: "risky", issues: [], breakdown: {} }] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.getByText(/One course can include multiple locations and their fee evidence/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve (1)" }).hasAttribute("disabled")).toBe(false));
    expect(fetchMock.mock.calls.filter(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toHaveLength(2);
    expect(screen.getByTestId("fee-summary-2")).toBeTruthy();
    expect(screen.queryByTestId("fee-summary-1")).toBeNull();
  });

  it("retains every selected course when the approval request fails", async () => {
    const review = initialReview();
    review.courses = review.courses.slice(0, 2);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected")
        return new Response(JSON.stringify({ detail: "Approval temporarily unavailable" }), { status: 503 });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: review.courses });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toBe(true));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve (2)" }).hasAttribute("disabled")).toBe(false));
    expect(screen.getByText("Course 1")).toBeTruthy();
    expect(screen.getByText("Course 2")).toBeTruthy();
  });

  it("limits concurrent approval page fetches to two and shows live progress", async () => {
    const review = initialReview();
    review.courses = review.courses.slice(0, 4);
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let active = 0;
    let maxActive = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/scrape/staged/approve-selected") {
        const { courseIds } = JSON.parse(String(init?.body));
        expect(courseIds).toHaveLength(1);
        active++;
        maxActive = Math.max(maxActive, active);
        await gate;
        active--;
        return jsonResponse({ approvedIds: courseIds, approvedCount: 1, splitCount: 0, failed: [], attempted: 1 });
      }
      if (url === "/api/scrape/staged/repair-job") return jsonResponse({ courses: [] });
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve (4)" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toHaveLength(2));
    expect(screen.getByRole("button", { name: /Approving 0\/4/ })).toBeTruthy();
    release();
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url) === "/api/scrape/staged/approve-selected")).toHaveLength(4));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Approving \d+\/4/ })).toBeNull());
    expect(maxActive).toBe(2);
  });

  it("renders persisted campus fee evidence without requiring a manual fee choice", async () => {
    const review = initialReview();
    const options = [17500, 19050].map((amount, index) => ({
      amount, currency: "GBP", year: 2026, period: "Full Course",
      campus: index ? "London" : "Outside London", study_variant: "Standard",
      source_url: "https://www.law.ac.uk/study/postgraduate/business/msc-healthcare-management/",
      snippet: "International Students | 2026 | Full Course",
    }));
    review.courses = [{
      ...review.courses[0], internationalFee: null,
      extraction_method: { fee_variants: { status: "range", selected: options, options } },
    }] as ScrapingInitialReviewState["courses"];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.getByTestId(`fee-summary-${review.courses[0].id}`).textContent)
      .toBe("£17,500–£19,050 GBP · 2026 · Full Course");
    const approve = screen.getByTitle("Approve and publish this course") as HTMLButtonElement;
    expect(approve.disabled).toBe(false);
    expect(screen.queryByText("Choose a published fee before approval")).toBeNull();
  });

  it("does not require manual fee choices and submits all unresolved rows to safe bulk approval", async () => {
    const review = initialReview();
    const published = [
      { optionId: "annual-2026", amount: 17500, currency: "GBP", year: 2026, period: "Annual", campus: "London", studyVariant: "Standard", sourceUrl: "https://law.ac.uk/fees", snippet: "Annual 2026" },
      { optionId: "full-2027", amount: 17500, currency: "GBP", year: 2027, period: "Full Course", campus: "London", studyVariant: "Standard", sourceUrl: "https://law.ac.uk/fees", snippet: "Full course 2027" },
    ];
    const extraction = { fee_variants: { status: "range", selected: published.map(o => ({
      amount: o.amount, currency: o.currency, year: o.year, period: o.period,
      campus: o.campus, study_variant: o.studyVariant, source_url: o.sourceUrl, snippet: o.snippet,
    })), options: [] } };
    review.courses = review.courses.slice(0, 2).map(c => ({
      ...c, extraction_method: extraction,
      feeSelection: { snapshotToken: `token-${c.id}`, options: published, selectedOptionId: null },
    })) as ScrapingInitialReviewState["courses"];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/fee-selection")) {
        const { optionId } = JSON.parse(String(init?.body));
        return jsonResponse({ success: true, course: {
          ...review.courses.find(c => url.includes(`/${c.id}/`)),
          internationalFee: 17500, currency: "GBP", feeYear: 2027, feeTerm: "Full Course",
          feeSelection: { snapshotToken: "token-1", options: published, selectedOptionId: optionId },
        } });
      }
      if (url === "/api/scrape/staged/approve-selected") {
        const { courseIds, force } = JSON.parse(String(init?.body));
        expect(courseIds).toHaveLength(1);
        expect(force).toBe(false);
        return courseIds[0] === 1
          ? jsonResponse({ approvedIds: [1], approvedCount: 1, splitCount: 0, failed: [], attempted: 1 })
          : jsonResponse({ approvedIds: [], approvedCount: 0, splitCount: 0, failed: [{ id: 2, error: "Select a fee option from the source" }], attempted: 1 });
      }
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.getByRole("button", { name: "Approve (2)" })).toBeTruthy();
    expect(screen.queryByText("Choose a published fee before approval")).toBeNull();
    expect(screen.queryByTestId("fee-choice-1-full-2027")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Approve (2)" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(args => String(args[0]) === "/api/scrape/staged/approve-selected")).toHaveLength(2));
  });

  it.each([6.0, 6.5, null])("keeps available IELTS %s visible alongside Unverified", async (score) => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0],
      courseName: "Course with unverified English",
      ieltsOverall: score,
      requirementStatus: {
        academic: { state: "numeric" },
        englishComponents: {
          state: "unknown",
          missingFields: ["ielts_writing"],
          sourceUrl: "https://uni.test/english",
        },
      },
    }] as ScrapingInitialReviewState["courses"];
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    render(<ScrapingForTest initialReviewState={review} />);
    const row = screen.getByText("Course with unverified English").closest("tr")!;
    const cells = within(row).getAllByRole("cell");
    const ielts = cells.find(cell => cell.textContent?.includes("Unverified")
      && (score === null || cell.textContent.includes(String(score)))
      && !cell.textContent.includes("Course with unverified English"))!;
    expect(ielts).toBeTruthy();
    expect(within(ielts).getByText("Unverified")).toBeTruthy();
    if (score !== null) {
      expect(within(ielts).getByText(String(score))).toBeTruthy();
      expect(within(ielts).getByText(/Missing:.*Writing/i)).toBeTruthy();
      expect(within(ielts).getByRole("link", { name: "Official source" }).getAttribute("href"))
        .toBe("https://uni.test/english");
    } else {
      expect(ielts.textContent).toBe("Unverified");
    }
  });

  it("normalizes snake-case requirement status without inventing a numeric score", () => {
    const status = normalizeRequirementStatus({
      academic: {
        state: "qualification_based",
        requirement_text: "A recognised bachelor degree in a related discipline",
        source_url: "https://uni.test/entry",
      },
      english_components: {
        state: "missing",
        missing_fields: ["ielts_listening", "ielts_writing"],
      },
    });
    expect(status?.academic.requirementText).toBe("A recognised bachelor degree in a related discipline");
    expect(status?.academic.sourceUrl).toBe("https://uni.test/entry");
    expect(status?.englishComponents.missingFields).toEqual(["ielts_listening", "ielts_writing"]);
    expect(requirementRepairFields({ requirementStatus: status })).toEqual(["english_requirements"]);
  });

  it("renders qualification evidence and IELTS component gaps even with an overall score", async () => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0],
      courseName: "Master of Evidence",
      academicScore: null,
      ieltsOverall: 6.5,
      completeness: 100,
      requirementStatus: {
        academic: {
          state: "qualification_based",
          requirementText: "A recognised bachelor degree in a related discipline",
          sourceUrl: "https://uni.test/entry",
        },
        englishComponents: {
          state: "missing",
          missingFields: ["ielts_listening", "ielts_writing"],
          sourceUrl: "https://uni.test/english",
        },
      },
    }] as ScrapingInitialReviewState["courses"];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      return jsonResponse({});
    }));

    render(<ScrapingForTest initialReviewState={review} />);
    expect(screen.getByText("Verified qualification requirement")).toBeTruthy();
    expect(screen.getByText(/A recognised bachelor degree/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Official requirement source" }).getAttribute("href"))
      .toBe("https://uni.test/entry");
    expect(screen.getByText("Missing: Listening, Writing")).toBeTruthy();
    expect(screen.queryByText("100%")).toBeNull();
  });

  it("sends every unresolved requirement group through the durable Fix request", async () => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0],
      requirementStatus: {
        academic: { state: "missing" },
        englishComponents: { state: "missing", missingFields: ["ielts_reading"] },
      },
    }] as ScrapingInitialReviewState["courses"];
    let requestBody: Record<string, unknown> | null = null;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      if (url === "/api/scrape/staged/analyze") {
        return jsonResponse({ total: 1, courses_with_url: 1, issues: [] });
      }
      if (url === "/api/scrape/staged/fix-jobs") {
        requestBody = JSON.parse(String(init?.body));
        return jsonResponse({
          jobId: "requirements-fix", sourceJobId: "repair-job", targetFields: [],
          status: "queued", total: 1, queued: 1, running: 0, completed: 0,
          noProgress: 0, failed: 0, processed: 0, results: [], errorMessage: null,
        });
      }
      return jsonResponse({});
    }));

    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);
    await user.click(screen.getByRole("button", { name: "Smart Fix (1)" }));
    const dialog = await screen.findByRole("dialog", { name: "Review Before Smart Fix" });
    await user.click(within(dialog).getByRole("button", { name: "Confirm Smart Fix (1)" }));
    await waitFor(() => expect(requestBody).not.toBeNull());
    expect(requestBody).toMatchObject({
      ids: [1],
      targetFields: [
        "academic_level", "academic_score", "score_type", "other_requirement",
        "english_requirements",
      ],
    });
    expect(requestBody).toEqual(expect.objectContaining({
      targetFields: expect.not.arrayContaining(["requirement_status"]),
    }));
    expect(localStorage.getItem("activeBulkFixJob")).toBe("requirements-fix");
  });

  it("opens the official-source recovery form for unresolved requirements", async () => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0],
      requirementStatus: {
        academic: { state: "unverified" },
        englishComponents: { state: "unknown" },
      },
    }] as ScrapingInitialReviewState["courses"];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      if (url.endsWith("/course-reports")) return jsonResponse({ reports: [], source_exclusions: {} });
      return jsonResponse({});
    }));
    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);
    await user.click(screen.getByRole("button", { name: "Add an official source for recovery" }));
    expect(await screen.findByText("Official course URLs (one per line; processed in bounded batches)")).toBeTruthy();
    expect(screen.getByTestId("input-report-urls")).toBeTruthy();
    expect((screen.getByTestId("select-report-kind") as HTMLSelectElement).value).toBe("incorrect");
    expect((screen.getByTestId("input-report-urls") as HTMLTextAreaElement).value)
      .toBe("https://example.test/courses/1");
    expect((screen.getByTestId("checkbox-report-english") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("checkbox-report-other") as HTMLInputElement).checked).toBe(true);
  });

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

  it("reports a remaining requested gap as partial after the preview state is lost", () => {
    const remainingFee = {
      field: "international_fee",
      label: "International Fee",
      missing: 1,
      total: 4,
      current_pct: 75,
      expected_fill_pct: 90,
    };
    expect(getFixResultHeading({
      total: 4,
      updated: 4,
      skipped: 0,
      errors: 0,
      beforeIssues: [],
      afterIssues: [remainingFee],
      afterAnalysisComplete: true,
      requestedFields: ["international_fee", "study_mode"],
      valueUpdatedFields: ["international_fee", "study_mode"],
    })).toBe("Partially successful");
  });

  it("maps component updates to their requested Fix field groups", () => {
    expect(isRequestedFixField("pte_overall", ["english_requirements"])).toBe(true);
    expect(isRequestedFixField("requirement_status", ["academic_score"])).toBe(true);
    expect(isRequestedFixField("fee_term", ["international_fee"])).toBe(true);
    expect(isRequestedFixField("duration", ["international_fee"])).toBe(false);
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

  it("loads and selects all expanded campus rows despite legacy source counters, without URL/name dedup", async () => {
    const review = initialReview();
    review.courses = Array.from({ length: 90 }, (_, i) => ({
      ...review.courses[0], id: i + 1, courseName: "Same programme",
      courseWebsite: "https://example.test/shared",
    }));
    const expanded = Array.from({ length: 142 }, (_, i) => ({
      ...review.courses[0], id: i + 1, courseLocation: `Campus ${i + 1}`,
    }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url === "/api/scrape/staged/repair-job")
        return jsonResponse({ courses: expanded, lastScrape: { staged: 90 } });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [
        ...expanded.map(c => ({ id: c.id, score: 96, issues: [] })),
        { id: 99999, score: 20, issues: [{ label: "Unrelated", severity: "error" }] },
      ] });
      return jsonResponse({});
    }));
    render(<ScrapingForTest initialReviewState={review} />);
    await userEvent.setup().click(screen.getByTitle("Reload staged courses and refresh quality scores"));
    await waitFor(() => expect(screen.getByText("142 pending entries")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Approve (142)" })).toBeTruthy();
    expect(screen.getAllByText("Same programme")).toHaveLength(142);
    expect(screen.queryByText("Unrelated")).toBeNull();
  }, 20000);

  it.each([false, true])("keeps multi-location staged courses as one row across refresh, deselection=%s", async (deselect) => {
    const review = initialReview();
    review.courses = [{
      ...review.courses[0], courseLocation: "London, Leeds",
      extractionMethod: { fee_variants: { status: "range", selected: [], options: [] },
        campus_fee_scope: { locations: ["London", "Leeds"] } },
    }] as ScrapingInitialReviewState["courses"];
    let loaded = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url === "/api/scrape/staged/repair-job")
        return jsonResponse({ courses: review.courses.slice(0, ++loaded) });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ScrapingForTest initialReviewState={review} />);
    if (deselect) {
      const tableRow = screen.getByText("Course 1").closest("tr")!;
      await userEvent.click(within(tableRow).getByRole("checkbox"));
    }
    await userEvent.click(screen.getByTitle("Reload staged courses and refresh quality scores"));
    await waitFor(() => expect(screen.getByRole("button", { name: `Approve (${deselect ? 0 : 1})` })).toBeTruthy());
    expect(screen.getByText("1 pending entries")).toBeTruthy();
    expect(screen.getByText("London, Leeds")).toBeTruthy();
    await userEvent.click(screen.getByTitle("Reload staged courses and refresh quality scores"));
    await waitFor(() => expect(loaded).toBe(2));
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/prepare-campus-courses"))).toHaveLength(0);
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
          completed: 50,
          noProgress: 1,
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
            {
              id: 2,
              course_name: "Master of Applied Science",
              ok: true,
              outcome: "no_progress",
              updated_fields: [],
              refreshed_evidence_fields: [],
              reason: "Requested target fields and selected evidence were unchanged",
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
    await user.click(screen.getByRole("button", { name: "Smart Fix (51)" }));
    const previewDialog = await screen.findByRole("dialog", {
      name: "Review Before Smart Fix",
      description: "Review the pending re-extraction action before applying it to the selected courses.",
    });
    await user.click(within(previewDialog).getByRole("button", { name: "Confirm Smart Fix (51)" }));

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
    expect(within(dialog).getByText(
      "Master of Applied Science (Course 2) — Requested target fields and selected evidence were unchanged",
    )).toBeTruthy();

    expect(fixBodies).toHaveLength(1);
    expect(fixBodies[0]).toEqual({
      smart: true,
      ids: Array.from({ length: 51 }, (_, index) => index + 1),
      universityId: 7,
      sourceJobId: "repair-job",
      targetFields: ["international_fee"],
      forceFields: [],
      forceReasons: {},
    });
  }, 10_000);

  it("restores a completed background Fix with errors and opens its detailed results", async () => {
    const review = initialReview();
    localStorage.setItem("activeBulkFixJob", "restored-fix");

    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse(review.courses);
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      if (url === "/api/scrape/staged/analyze") {
        return jsonResponse({ total: 3, courses_with_url: 3, issues: [] });
      }
      if (url === "/api/scrape/staged/fix-jobs/restored-fix") {
        return jsonResponse({
          jobId: "restored-fix",
          sourceJobId: "repair-job",
          targetFields: ["international_fee"],
          status: "completed_with_errors",
          total: 3,
          queued: 0,
          running: 0,
          completed: 1,
          noProgress: 1,
          failed: 1,
          processed: 3,
          results: [
            {
              id: 1,
              ok: true,
              outcome: "completed",
              updated_fields: ["international_fee"],
              refreshed_evidence_fields: ["duration"],
            },
            {
              id: 2,
              ok: true,
              outcome: "no_progress",
              updated_fields: [],
              refreshed_evidence_fields: [],
              reason: "International fee remained unavailable",
            },
            {
              id: 3,
              ok: false,
              outcome: "failed",
              error: "Course page timed out",
            },
          ],
          errorMessage: null,
        });
      }
      return jsonResponse({});
    }));

    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);

    const viewResults = await screen.findByRole("button", { name: "View Fix results" });
    expect(screen.queryByRole("dialog", { name: "Fix Results" })).toBeNull();
    await user.click(viewResults);

    const dialog = await screen.findByRole("dialog", { name: "Fix Results" });
    expect(within(dialog).getByText("Processed 2 of 3")).toBeTruthy();
    expect(within(dialog).getByText("· 1 failed")).toBeTruthy();
    expect(within(dialog).getByText("Requested values updated")).toBeTruthy();
    expect(within(dialog).getByText("International Fee")).toBeTruthy();
    expect(within(dialog).getByText("Sources refreshed — values unchanged")).toBeTruthy();
    expect(within(dialog).getByText("Duration")).toBeTruthy();
    expect(within(dialog).getByText(
      "Course 2 — International fee remained unavailable",
    )).toBeTruthy();
    expect(within(dialog).getByText("Course 3 — Course page timed out")).toBeTruthy();
  });

  it("restores the exact Smart Fix review, analyzes its IDs, and opens a prefilled official URL report", async () => {
    const review = initialReview();
    localStorage.setItem("activeBulkFixJob", "smart-restored");
    localStorage.setItem("bulkFixBefore:smart-restored", JSON.stringify([
      { field: "international_fee", label: "Fee", missing: 1, total: 1, current_pct: 0, expected_fill_pct: 0 },
    ]));
    const analyses: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/import/history") return jsonResponse([]);
      if (url.startsWith("/api/courses?")) return jsonResponse({ total: 0 });
      if (url.endsWith("/course-quality")) return jsonResponse({ courses: [] });
      if (url === "/api/scrape/staged/repair-job") return jsonResponse(review.courses);
      if (url.startsWith("/api/scrape/staged/fix-jobs?")) return jsonResponse(null);
      if (url === "/api/scrape/staged/analyze") {
        analyses.push(JSON.parse(String(init?.body)));
        return jsonResponse({ total: 1, courses_with_url: 1, issues: [{ field: "international_fee", label: "Fee", missing: 1 }] });
      }
      if (url === "/api/scrape/staged/fix-jobs/smart-restored") return jsonResponse({
        jobId: "smart-restored", sourceJobId: "repair-job", smart: true,
        targetFields: ["international_fee"], status: "completed", total: 1,
        queued: 0, running: 0, completed: 0, noProgress: 1, failed: 0,
        processed: 1, attempted: 1, notAttempted: 0, skipped: 0, alreadyResolved: 0,
        results: [{ id: 1, ok: true, attempted: true, outcome: "no_progress",
          target_fields: ["international_fee"], resolved_fields: [], unresolved_fields: ["international_fee"],
          reason_code: "unresolved_after_official_recovery", next_action: "report_official_url" }],
      });
      return jsonResponse({});
    }));
    const user = userEvent.setup();
    render(<ScrapingForTest initialReviewState={review} />);
    await user.click(await screen.findByRole("button", { name: "View Fix results" }));
    const dialog = await screen.findByRole("dialog", { name: "Fix Results" });
    expect(within(dialog).getByText("No progress")).toBeTruthy();
    expect(within(dialog).getByText("Before vs After")).toBeTruthy();
    expect(analyses).toEqual([{ ids: [1], universityId: 7 }]);
    await user.click(within(dialog).getByText("Attempted — unchanged: Selected issues remain after official-source recovery (1)"));
    await user.click(within(dialog).getByRole("button", { name: "Report official URL" }));
    expect(await screen.findByDisplayValue("https://example.test/courses/1")).toBeTruthy();
    expect((screen.getByTestId("input-report-description") as HTMLTextAreaElement).value).toContain("International Fee");
  });

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
    await user.click(screen.getByRole("button", { name: "Smart Fix (51)" }));

    const previewDialog = await screen.findByRole("dialog", { name: "Review Before Smart Fix" });
    expect(previewDialog.classList.contains("h-[calc(100dvh-2rem)]")).toBe(true);
    expect(previewDialog.classList.contains("max-h-[calc(100dvh-2rem)]")).toBe(true);
    expect(previewDialog.classList.contains("overflow-hidden")).toBe(true);
    expect(within(previewDialog).getByText(/Existing values will be overwritten/).closest(".overflow-y-auto")).not.toBeNull();
    expect(within(previewDialog).getByText(/Existing values will be overwritten/)).toBeTruthy();
    await user.click(within(previewDialog).getByLabelText("Force International Fee"));
    await user.click(within(previewDialog).getByLabelText("Force Course Location"));
    await user.click(within(previewDialog).getByLabelText("Force English Requirements"));
    await user.click(within(previewDialog).getByLabelText("Force Intake"));

    const confirmButton = within(previewDialog).getByRole("button", { name: "Confirm Smart Fix (51)" });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(true);
    const reasonInputs = within(previewDialog).getAllByLabelText(/Correction reason/);
    fireEvent.change(reasonInputs[0], { target: { value: "Published fee is outdated" } });
    fireEvent.change(reasonInputs[1], { target: { value: "Campus list is incomplete" } });
    fireEvent.change(reasonInputs[2], { target: { value: "English scores are incorrect" } });
    fireEvent.change(reasonInputs[3], { target: { value: "Intake includes unrelated months" } });
    expect((confirmButton as HTMLButtonElement).disabled).toBe(false);
    await user.click(confirmButton);

    await waitFor(() => expect(fixBodies).toHaveLength(1));
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Review Before Smart Fix" })).toBeNull();
    });
    expect(localStorage.getItem("activeBulkFixJob")).toBe("forced-fix");
    expect(fixBodies[0]).toMatchObject({
      smart: true,
      targetFields: [
        "ielts_overall",
        "international_fee",
        "course_location",
        "english_requirements",
        "intake_months",
      ],
      forceFields: [
        "international_fee",
        "course_location",
        "english_requirements",
        "intake_months",
      ],
      forceReasons: {
        international_fee: "Published fee is outdated",
        course_location: "Campus list is incomplete",
        english_requirements: "English scores are incorrect",
        intake_months: "Intake includes unrelated months",
      },
    });
  }, 10_000);
});
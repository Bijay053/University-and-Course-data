// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseReport, Exclusions } from "./course-report";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (body: unknown, ok = true) => ({ ok, text: async () => JSON.stringify(body) });

describe("course report recovery", () => {
  it("submits official missing URLs and numeric expected count, then shows durable progress", async () => {
    const report = { job_id: "child", status: "queued", found: 0, staged: 0, skipped: 0, errors: 0,
      exclusions: {}, request: { kind: "missing", eligibility_review: true, expected_count: 10 } };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [], source_exclusions: {} }))
      .mockImplementationOnce(async () => response({ reports: [], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport jobId="parent" onReview={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText("Loading report history…")).toBeNull());
    fireEvent.click(screen.getByTestId("button-report-courses"));
    fireEvent.change(screen.getByTestId("input-report-urls"), { target: { value: "https://uni.edu/course" } });
    fireEvent.change(screen.getByTestId("input-report-expected"), { target: { value: "10" } });
    fireEvent.click(screen.getByTestId("checkbox-report-eligibility"));
    fetcher.mockResolvedValue(response({ reports: [report], source_exclusions: {} }))
      .mockResolvedValueOnce(response(report));
    fireEvent.click(screen.getByTestId("button-submit-report"));
    await screen.findByTestId("report-child");
    const request = fetcher.mock.calls.find(call => call[1]?.method === "POST");
    expect(JSON.parse(request![1].body)).toMatchObject({
      kind: "missing", course_urls: ["https://uni.edu/course"], expected_count: 10,
      eligibility_review: true,
    });
    expect(screen.getByText(/Eligibility review requested/)).toBeTruthy();
    expect(screen.getByText(/Catalogue coverage: not verified/)).toBeTruthy();
  });

  it("requires description and fields for incorrect reports without dispatching", async () => {
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport jobId="parent" onReview={vi.fn()} />);
    fireEvent.click(screen.getByTestId("button-report-courses"));
    fireEvent.change(screen.getByTestId("select-report-kind"), { target: { value: "incorrect" } });
    fireEvent.click(screen.getByTestId("button-submit-report"));
    await screen.findByText("Provide course URLs, affected fields and a description.");
    expect(fetcher.mock.calls.some(call => call[1]?.method === "POST")).toBe(false);
  });

  it("hydrates history on mount and navigates to the actual recovery job", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ reports: [{
      job_id: "child", status: "completed", found: 2, staged: 2, skipped: 1, errors: 0,
      exclusions: {}, request: { kind: "incorrect", description: "Wrong fee", fields: ["fee"] },
    }], source_exclusions: {} })));
    const review = vi.fn();
    render(<CourseReport jobId="parent" onReview={review} />);
    fireEvent.click(await screen.findByTestId("button-review-report-child"));
    expect(review).toHaveBeenCalledWith("child");
    expect(screen.getByText(/finished run does not confirm/)).toBeTruthy();
  });

  it("offers a self-service retry instead of an empty review when every reported page was skipped", async () => {
    const report = {
      job_id: "child-empty", status: "completed", found: 1, staged: 0, skipped: 1, errors: 0,
      exclusions: { category_landing_page_missing_degree_qualifier: 1 },
      request: { kind: "missing", course_urls: ["https://uni.edu/foundation"] },
      retry: { available: true, remaining_urls: ["https://uni.edu/foundation"], remaining_count: 1 },
    };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    const review = vi.fn();
    render(<CourseReport jobId="parent" onReview={review} />);

    expect(await screen.findByText(/No recovered courses are available to review/)).toBeTruthy();
    expect(screen.queryByTestId("button-review-report-child-empty")).toBeNull();
    expect(review).not.toHaveBeenCalled();

    fetcher.mockResolvedValueOnce(response({ ...report, job_id: "child-retry", status: "queued" }));
    fireEvent.click(screen.getByTestId("button-retry-report-child-empty"));
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
      "/api/scrape/jobs/parent/course-reports/child-empty/retry",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    ));
  });

  it("shows staging exclusion reasons separately from the coverage claim", () => {
    render(<Exclusions counts={{ domestic: 4, staging_rejections: { reasons: { non_degree: 2 } } }} />);
    expect(screen.getByText(/domestic: 4 · non degree: 2/)).toBeTruthy();
  });

  it("requires explicit remaining URL and budget acknowledgement before continuing", async () => {
    const report = {
      job_id: "child-continuation", report_id: "stable-report", original_job_id: "parent/source",
      status: "completed", found: 50, processed: 50, staged: 7, skipped: 2, errors: 0,
      exclusions: {}, request: { kind: "missing", catalogue_url: "https://uni.edu/catalogue" },
      continuation: {
        available: true, remaining_urls: ["https://uni.edu/course/51", "https://uni.edu/course/52"],
        remaining_count: 2, completed_count: 50, selected_count: 52, run_count: 1,
      },
    };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport jobId="parent/source" onReview={vi.fn()} />);

    const continueButton = await screen.findByTestId("button-continue-report-child-continuation");
    expect((continueButton as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("report-processed-child-continuation").textContent).toContain("50");
    expect(screen.getByTestId("report-staged-child-continuation").textContent).toContain("7");
    expect(screen.getByTestId("report-remaining-child-continuation").textContent).toContain("2");
    expect(screen.getByText(/up to 50 courses, 600 seconds, and \$2/)).toBeTruthy();
    expect(screen.getByText(/does not verify the full catalogue/)).toBeTruthy();

    fireEvent.click(screen.getByTestId("checkbox-continuation-review-child-continuation"));
    expect((continueButton as HTMLButtonElement).disabled).toBe(false);
    fetcher.mockResolvedValueOnce(response({ ...report, status: "queued", continuation: { ...report.continuation, available: false } }));
    fireEvent.click(continueButton);

    await waitFor(() => expect(fetcher).toHaveBeenCalledWith(
      "/api/scrape/jobs/parent%2Fsource/course-reports/child-continuation/continue",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ reviewed: true }) }),
    ));
  });

  it("keeps prior children with staged rows reviewable", async () => {
    const report = {
      job_id: "run-2", report_id: "stable-report", original_job_id: "source",
      status: "completed", found: 30, processed: 30, staged: 4, skipped: 0, errors: 0,
      exclusions: {}, request: { kind: "missing" },
      children: [
        { job_id: "run-1", status: "completed", processed: 17, found: 50, staged: 3, skipped: 12, errors: 2, verification: {} },
        { job_id: "run-2", status: "completed", processed: 30, staged: 4 },
      ],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} })));
    const review = vi.fn();
    render(<CourseReport jobId="source" onReview={review} />);

    fireEvent.click(await screen.findByTestId("button-review-report-run-1"));
    expect(review).toHaveBeenCalledWith("run-1");
    expect(screen.getByTestId("button-review-report-run-2")).toBeTruthy();
    expect(screen.getByTestId("report-child-run-1").textContent).toContain("3 staged");
    expect(screen.getByTestId("report-child-run-1").textContent).toContain("17 processed");
    expect(screen.getByTestId("report-child-run-1").textContent).not.toContain("50 processed");
  });

  it("keeps continuation available and reports a failed continuation", async () => {
    const report = {
      job_id: "failed-next", status: "completed", found: 50, staged: 0, skipped: 0, errors: 0,
      exclusions: {}, request: { kind: "missing" },
      continuation: {
        available: true, remaining_urls: ["https://uni.edu/course/51"],
        remaining_count: 1, completed_count: 50, selected_count: 51, run_count: 1,
      },
    };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport jobId="parent" onReview={vi.fn()} />);

    const checkbox = await screen.findByTestId("checkbox-continuation-review-failed-next");
    fireEvent.click(checkbox);
    fetcher.mockResolvedValueOnce(response({ detail: "Continuation budget unavailable" }, false));
    fireEvent.click(screen.getByTestId("button-continue-report-failed-next"));

    expect((await screen.findByRole("alert")).textContent).toContain("Continuation budget unavailable");
    expect((checkbox as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("button-continue-report-failed-next") as HTMLButtonElement).disabled).toBe(false);
  });
});
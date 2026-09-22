// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseReport, Exclusions } from "./course-report";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (body: unknown, ok = true) => ({ ok, text: async () => JSON.stringify(body) });

describe("course report recovery", () => {
  it("submits official missing URLs and numeric expected count, then shows durable progress", async () => {
    const report = {
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
    };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
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
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
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

  it("shows animated progress while a recovery is running", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ reports: [{
      job_id: "child-running", status: "running", found: 2, processed: 1,
      staged: 0, skipped: 0, errors: 0, exclusions: {},
      request: { kind: "missing", course_urls: ["https://uni.edu/foundation"] },
    }], source_exclusions: {} })));
    render(<CourseReport jobId="parent" onReview={vi.fn()} />);

    const progress = await screen.findByTestId("report-progress-child-running");
    expect(progress.textContent).toContain("Processing reported pages… 1 of 2 complete");
    const bar = screen.getByRole("progressbar", { name: "Recovery progress" });
    expect(bar.getAttribute("aria-valuenow")).toBe("1");
    expect(bar.getAttribute("aria-valuemax")).toBe("2");
    expect(screen.getByText(/updates automatically about every 15 seconds/)).toBeTruthy();
  });

  it("offers a self-service retry instead of an empty review when every reported page was skipped", async () => {
    const report = {
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
    };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} }));
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
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
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
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
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
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
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

  it("shows submitted and related programme URLs with every stable status", async () => {
    const statuses = ["queued", "processing", "staged", "skipped", "error"] as const;
    const report = {
      job_id: "url-progress", status: "running", found: 5, staged: 1, skipped: 1, errors: 1,
      exclusions: {}, request: { kind: "missing" },
      programme_urls: statuses.map((status, index) => ({
        url: `https://uni.edu/course/${status}`,
        origin: index === 0 ? "submitted" : "related",
        status,
      })),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ reports: [report], source_exclusions: {} })));
    render(<CourseReport jobId="source" onReview={vi.fn()} />);

    fireEvent.click(await screen.findByTestId("button-programme-urls-url-progress"));
    statuses.forEach((status, index) => {
      const row = screen.getByTestId(`programme-url-url-progress-${index}`);
      expect(row.textContent).toContain(status);
      expect(row.textContent).toContain(index === 0 ? "Submitted" : "Related");
    });
  });
});

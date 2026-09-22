// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseReport, Exclusions } from "./course-report";
import { setAuthToken } from "@/lib/api";

afterEach(() => { cleanup(); setAuthToken(null); vi.unstubAllGlobals(); });
const response = (body: unknown, ok = true) => ({ ok, text: async () => JSON.stringify(body) });

describe("course report recovery", () => {
  it("submits the affected staged course context and human-supplied official source", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(response({ reports: [], source_exclusions: {} }))
      .mockResolvedValueOnce(response({
        job_id: "requirement-recovery", status: "queued", found: 0, staged: 0,
        skipped: 0, errors: 0, exclusions: {},
        request: { kind: "incorrect", fields: ["english", "other"] },
      }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport
      jobId="source-job"
      onReview={vi.fn()}
      openRequest={1}
      prefillCourses={[{
        courseName: "Master of Evidence",
        courseUrl: "https://uni.edu/courses/evidence",
        fields: ["english", "other"],
        description: "Source verification needed for Master of Evidence: academic and English requirements.",
      }]}
    />);

    expect((await screen.findByTestId("report-prefill-course")).textContent).toContain("Master of Evidence");
    expect((screen.getByTestId("select-report-kind") as HTMLSelectElement).value).toBe("incorrect");
    expect((screen.getByTestId("input-report-urls") as HTMLTextAreaElement).value)
      .toBe("https://uni.edu/courses/evidence");
    expect((screen.getByTestId("input-report-source") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("checkbox-report-english") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("checkbox-report-other") as HTMLInputElement).checked).toBe(true);

    fireEvent.change(screen.getByTestId("input-report-source"), {
      target: { value: "https://uni.edu/admissions/requirements" },
    });
    fireEvent.click(screen.getByTestId("button-submit-report"));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    const [, init] = fetcher.mock.calls[1];
    expect(fetcher.mock.calls[1][0]).toBe("/api/scrape/jobs/source-job/course-reports");
    expect(JSON.parse(init.body)).toEqual(expect.objectContaining({
      kind: "incorrect",
      course_urls: ["https://uni.edu/courses/evidence"],
      fields: ["english", "other"],
      source_url: "https://uni.edu/admissions/requirements",
    }));
    expect(JSON.parse(init.body)).not.toHaveProperty("course_id");
    expect(JSON.parse(init.body)).not.toHaveProperty("staged_id");
  });

  it("shows pending state and preserves the prefilled form when recovery submission fails", async () => {
    let resolvePost: ((value: ReturnType<typeof response>) => void) | undefined;
    const pendingPost = new Promise<ReturnType<typeof response>>((resolve) => { resolvePost = resolve; });
    const fetcher = vi.fn()
      .mockResolvedValueOnce(response({ reports: [], source_exclusions: {} }))
      .mockImplementationOnce(() => pendingPost);
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport
      jobId="source-job"
      onReview={vi.fn()}
      openRequest={1}
      prefillCourses={[{
        courseName: "Master of Evidence",
        courseUrl: "https://uni.edu/courses/evidence",
        fields: ["other"],
        description: "Academic requirement needs an official source.",
      }]}
    />);
    await screen.findByTestId("report-prefill-course");
    fireEvent.click(screen.getByTestId("button-submit-report"));
    expect((await screen.findByRole("button", { name: "Starting recovery…" }) as HTMLButtonElement).disabled).toBe(true);

    resolvePost?.(response({ detail: "Official source could not be verified" }, false));
    expect((await screen.findByRole("alert")).textContent).toContain("Official source could not be verified");
    expect((screen.getByTestId("button-submit-report") as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByTestId("input-report-urls") as HTMLTextAreaElement).value)
      .toBe("https://uni.edu/courses/evidence");
  });

  it("provides a course picker for multi-row requirement recovery", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ reports: [], source_exclusions: {} })));
    render(<CourseReport
      jobId="source-job"
      onReview={vi.fn()}
      openRequest={1}
      prefillCourses={[
        {
          courseName: "Course One", courseUrl: "https://uni.edu/one",
          fields: ["english"], description: "English requirement is unverified.",
        },
        {
          courseName: "Course Two", courseUrl: "https://uni.edu/two",
          fields: ["other"], description: "Academic requirement is unverified.",
        },
      ]}
    />);
    const picker = await screen.findByTestId("select-report-prefill-course");
    expect((screen.getByTestId("input-report-urls") as HTMLTextAreaElement).value).toBe("https://uni.edu/one");
    fireEvent.change(picker, { target: { value: "1" } });
    expect((screen.getByTestId("input-report-urls") as HTMLTextAreaElement).value).toBe("https://uni.edu/two");
    expect((screen.getByTestId("checkbox-report-other") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("input-report-source") as HTMLInputElement).value).toBe("");
  });

  it("uses the saved bearer token when the session cookie is unavailable", async () => {
    setAuthToken("saved-session-token");
    const fetcher = vi.fn().mockResolvedValue(response({
      reports: [],
      source_exclusions: {},
    }));
    vi.stubGlobal("fetch", fetcher);

    render(<CourseReport jobId="parent" onReview={vi.fn()} />);

    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    expect(headers.get("Authorization")).toBe("Bearer saved-session-token");
    expect(fetcher.mock.calls[0][1]?.credentials).toBe("include");
  });

  it("opens the official URL form when automatic repair requests user input", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      reports: [],
      source_exclusions: {},
    })));
    const { rerender } = render(
      <CourseReport jobId="parent" onReview={vi.fn()} openRequest={0} />,
    );
    await waitFor(() => expect(screen.queryByText("Loading report history…")).toBeNull());
    expect(screen.queryByTestId("input-report-urls")).toBeNull();

    rerender(<CourseReport jobId="parent" onReview={vi.fn()} openRequest={1} />);

    expect(await screen.findByTestId("input-report-urls")).toBeTruthy();
    expect((screen.getByTestId("select-report-kind") as HTMLSelectElement).value).toBe("missing");
  });

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

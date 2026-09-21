// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseReport, Exclusions } from "./course-report";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const response = (body: unknown, ok = true) => ({ ok, text: async () => JSON.stringify(body) });

describe("course report recovery", () => {
  it("submits official missing URLs and numeric expected count, then shows durable progress", async () => {
    const report = { job_id: "child", status: "queued", found: 0, staged: 0, skipped: 0, errors: 0,
      exclusions: {}, request: { kind: "missing", expected_count: 10 } };
    const fetcher = vi.fn().mockResolvedValue(response({ reports: [], source_exclusions: {} }))
      .mockImplementationOnce(async () => response({ reports: [], source_exclusions: {} }));
    vi.stubGlobal("fetch", fetcher);
    render(<CourseReport jobId="parent" onReview={vi.fn()} />);
    await waitFor(() => expect(screen.queryByText("Loading report history…")).toBeNull());
    fireEvent.click(screen.getByTestId("button-report-courses"));
    fireEvent.change(screen.getByTestId("input-report-urls"), { target: { value: "https://uni.edu/course" } });
    fireEvent.change(screen.getByTestId("input-report-expected"), { target: { value: "10" } });
    fetcher.mockResolvedValue(response({ reports: [report], source_exclusions: {} }))
      .mockResolvedValueOnce(response(report));
    fireEvent.click(screen.getByTestId("button-submit-report"));
    await screen.findByTestId("report-child");
    const request = fetcher.mock.calls.find(call => call[1]?.method === "POST");
    expect(JSON.parse(request![1].body)).toMatchObject({
      kind: "missing", course_urls: ["https://uni.edu/course"], expected_count: 10,
    });
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

  it("shows staging exclusion reasons separately from the coverage claim", () => {
    render(<Exclusions counts={{ domestic: 4, staging_rejections: { reasons: { non_degree: 2 } } }} />);
    expect(screen.getByText(/domestic: 4 · non degree: 2/)).toBeTruthy();
  });
});
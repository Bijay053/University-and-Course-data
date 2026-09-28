// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ReviewScrapedCoursesTable, type ReviewStagedCourse } from "./review-scraped-courses-table";

const row = (id: number, job = "job-one", extras: Partial<ReviewStagedCourse> = {}): ReviewStagedCourse => ({
  id, scrapeJobId: job, universityId: 7, courseName: `Course ${id}`,
  category: null, courseWebsite: null, courseLocation: null, duration: null,
  durationTerm: null, studyMode: null, degreeLevel: null, internationalFee: null,
  feeTerm: null, currency: null, ieltsOverall: null, pteOverall: null,
  toeflOverall: null, cambridgeOverall: null, duolingoOverall: null,
  intakeMonths: null, autoPublishStatus: null, eligibilityStatus: null,
  notes: null, completeness: null, evidence: [], evidenceLoaded: false, evidenceCount: 1,
  ...extras,
});
const item = { id: 11, fieldKey: "course_name", candidateValue: "From source",
  sourceUrl: "https://example.edu/course", pageType: "course", extractionMethod: "html",
  snippet: "Original evidence", confidence: 0.9, selected: true };
const endpoint = (id: number, job = "job-one") =>
  `/api/scrape/staged/${id}/evidence?jobId=${job}&universityId=7`;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("does not fetch detail initially, then hydrates only the expanded row", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL) => Response.json({ course: row(1, "job-one", { evidence: [item] }) }));
  vi.stubGlobal("fetch", fetcher);
  render(<ReviewScrapedCoursesTable courses={[row(1), row(2)]} showEvidence readOnly />);
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId("sources-toggle-1"));
  await waitFor(() => expect(screen.getByText(/Original evidence/)).toBeTruthy());
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(String(fetcher.mock.calls[0][0])).toBe(endpoint(1));
});

it("shows error with retry and retains legacy embedded evidence without a request", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response("Unavailable", { status: 503 }))
    .mockResolvedValueOnce(Response.json({ course: row(3, "job-one", { evidence: [item] }) }));
  vi.stubGlobal("fetch", fetcher);
  render(<ReviewScrapedCoursesTable courses={[row(3), row(4, "job-one", {
    evidenceLoaded: true, evidence: [{ ...item, id: 12, snippet: "Legacy evidence" }],
  })]} showEvidence readOnly />);
  fireEvent.click(screen.getByTestId("sources-toggle-4"));
  expect(screen.getByText(/Legacy evidence/)).toBeTruthy();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId("sources-toggle-3"));
  await waitFor(() => expect(screen.getByTestId("sources-error-3")).toBeTruthy());
  fireEvent.click(screen.getByTestId("sources-retry-3"));
  await waitFor(() => expect(screen.getByText(/Original evidence/)).toBeTruthy());
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("discards an old response when the job changes for the same row", async () => {
  let resolveOld!: (response: Response) => void;
  const old = new Promise<Response>(resolve => { resolveOld = resolve; });
  const fetcher = vi.fn().mockReturnValueOnce(old)
    .mockResolvedValueOnce(Response.json({ course: row(1, "job-two", {
      evidence: [{ ...item, snippet: "New job evidence" }],
    }) }));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<ReviewScrapedCoursesTable courses={[row(1)]} showEvidence readOnly />);
  fireEvent.click(screen.getByTestId("sources-toggle-1"));
  view.rerender(<ReviewScrapedCoursesTable courses={[row(1, "job-two")]} showEvidence readOnly />);
  resolveOld(Response.json({ course: row(1, "job-one", { evidence: [item] }) }));
  await waitFor(() => expect(screen.queryByTestId("sources-loading-1")).toBeNull());
  fireEvent.click(screen.getByTestId("sources-toggle-1"));
  await waitFor(() => expect(screen.getByText(/New job evidence/)).toBeTruthy());
  expect(screen.queryByText(/Original evidence/)).toBeNull();
  expect(String(fetcher.mock.calls[1][0])).toBe(endpoint(1, "job-two"));
});
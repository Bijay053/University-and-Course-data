// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/can", () => ({
  useCan: () => ({ can: () => true }),
}));

import { DatedCatalogueReview } from "./dated-catalogue-review";

const course = {
  id: 41,
  scrapeJobId: "job-winchester-1",
  universityId: 87,
  courseName: "MA Politics 2025",
  courseWebsite: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/",
  scrapeWarnings: ["dated_catalogue_page_review"],
};

function row(decision: string | null = null) {
  return {
    id: 41,
    jobId: "job-winchester-1",
    universityId: 87,
    courseName: "MA Politics 2025",
    courseUrl: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/",
    warningPresent: true,
    review: {
      revision: decision ? 4 : 3,
      evidence: {
        checkedAt: "2026-07-20T12:00:00Z",
        original: {
          url: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/",
          verified: true,
          status: 200,
          title: "MA Politics" as string | null,
          awards: ["MA"],
          reason: null as string | null,
        },
        candidate: {
          url: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics/",
          verified: true,
          status: 200,
          title: "MA Politics" as string | null,
          awards: ["MA"],
          reason: null as string | null,
        },
        suggestion: "current_counterpart" as string | null,
        reason: "Official titles and awards align.",
        referenceOnly: true,
      },
      decision,
      reviewer: decision ? { name: "Reviewer One" } : null,
      decidedAt: decision ? "2026-07-20T12:05:00Z" : null,
    },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("DatedCatalogueReview", () => {
  it("reloads durable evidence and saves an exact-row optimistic decision", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ rows: [row()] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(row("keep_separate")), { status: 200 }));

    render(<DatedCatalogueReview courses={[course]} />);
    expect(await screen.findByTestId("text-source-title-41-Archived/original source")).toBeTruthy();
    expect(screen.getByText(/Suggestions are reference only/)).toBeTruthy();

    fireEvent.click(screen.getByTestId("button-decision-keep_separate-41"));
    await waitFor(() => expect(screen.getByText(/Saved by Reviewer One/)).toBeTruthy());

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/scrape/staged/dated-catalogue-reviews/41/decision",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          universityId: 87,
          jobId: "job-winchester-1",
          expectedRevision: 3,
          decision: "keep_separate",
        }),
      }),
    );
  });

  it("shows unverified official evidence without claiming a counterpart", async () => {
    const unverified = row();
    unverified.review.evidence.original = {
      ...unverified.review.evidence.original,
      verified: false,
      title: null,
      awards: [],
      reason: "Official source returned an access challenge; evidence is unverified",
    };
    unverified.review.evidence.suggestion = null;
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ rows: [unverified] }), { status: 200 }),
    );

    render(<DatedCatalogueReview courses={[course]} />);
    expect(await screen.findByText(/Unverified: Official source returned an access challenge/)).toBeTruthy();
    expect(screen.queryByText(/Conservative suggestion:/)).toBeNull();
    expect((screen.getByTestId("button-decision-current_counterpart-41") as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps the staged original visible alongside a redirect destination", async () => {
    const redirected = row();
    redirected.review.evidence.original = {
      ...redirected.review.evidence.original,
      finalUrl: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics/",
    } as typeof redirected.review.evidence.original & { finalUrl: string };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ rows: [redirected] }), { status: 200 }),
    );

    render(<DatedCatalogueReview courses={[course]} />);
    const staged = await screen.findByTestId("link-staged-original-41");
    const requested = screen.getByTestId("link-original-41");
    const final = screen.getByTestId("link-original-final-41");
    expect(staged.getAttribute("href")).toBe(course.courseWebsite);
    expect(requested.getAttribute("href")).toBe(course.courseWebsite);
    expect(final.getAttribute("href")).toBe(
      "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics/",
    );
  });

  it("preserves a save conflict after reloading the current durable row", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ rows: [row()] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Audit evidence changed; reload before saving a decision." }), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ rows: [row("not_counterpart")] }), { status: 200 }));

    render(<DatedCatalogueReview courses={[course]} />);
    await screen.findByTestId("text-checked-at-41");
    fireEvent.click(screen.getByTestId("button-decision-keep_separate-41"));

    expect(await screen.findByText(/Audit evidence changed; reload before saving a decision/)).toBeTruthy();
    expect(screen.getByText(/Saved by Reviewer One/)).toBeTruthy();
  });

  it("pages through 51 dated rows using stable exact identities", async () => {
    const courses = Array.from({ length: 51 }, (_, index) => ({
      ...course,
      id: index + 1,
      scrapeJobId: `job-${index + 1}`,
      courseName: `Course ${index + 1}`,
      courseWebsite: `https://www.winchester.ac.uk/study/Courses/MA-Course-${index + 1}-2025/`,
    }));
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ rows: [] }), { status: 200 }),
    );

    render(<DatedCatalogueReview courses={courses} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstBody = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(firstBody.rows).toHaveLength(50);
    expect(firstBody.rows[0]).toEqual({ id: 1, jobId: "job-1" });
    expect(firstBody.rows[49]).toEqual({ id: 50, jobId: "job-50" });

    fireEvent.click(screen.getByTestId("button-dated-page-next"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const secondBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    expect(secondBody.rows).toEqual([{ id: 51, jobId: "job-51" }]);
    expect(screen.getByTestId("text-dated-page").textContent).toBe("Page 2 of 2");
  });

  it("loads the real historical payload identity contract in read-only mode", async () => {
    const historicalCourse = {
      id: 41,
      scrapeJobId: "job-winchester-1",
      universityId: 87,
      courseName: "MA Politics 2025",
      courseWebsite: "https://www.winchester.ac.uk/study/Postgraduate/Courses/MA-Politics-2025/",
      scrapeWarnings: ["dated_catalogue_page_review"],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ rows: [row("keep_separate")] }), { status: 200 }),
    );
    render(<DatedCatalogueReview courses={[historicalCourse]} readOnly />);

    expect(await screen.findByText("Read-only historical view.")).toBeTruthy();
    expect(screen.queryByTestId("button-audit-dated-catalogue")).toBeNull();
    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({
      universityId: 87,
      rows: [{ id: 41, jobId: "job-winchester-1" }],
    });
  });
});
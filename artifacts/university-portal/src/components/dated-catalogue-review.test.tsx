// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const permissions = vi.hoisted(() => ({ edit: true }));
vi.mock("@/components/can", () => ({
  useCan: () => ({ can: () => permissions.edit }),
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
  permissions.edit = true;
  cleanup();
  vi.restoreAllMocks();
});

describe("DatedCatalogueReview", () => {
  const auditEntry = () => ({
    type: "official_source_audit", at: "2026-07-20T12:00:00Z", evidence: row().review.evidence,
  });
  const decisionEntry = () => ({
    type: "reviewer_decision", at: "2026-07-20T12:05:00Z",
    decision: "current_counterpart", reviewer: { name: "Earlier Reviewer" }, evidenceRevision: 3,
  });

  it("shows superseded confirmation and audit reasons after re-audit without writing", async () => {
    const latest = row();
    latest.review.revision = 5;
    latest.review.evidence.checkedAt = "2026-07-21T12:00:00Z";
    latest.review.evidence.reason = "Candidate now unavailable.";
    const history = [auditEntry(), decisionEntry(), {
      type: "official_source_audit", at: latest.review.evidence.checkedAt, evidence: latest.review.evidence,
    }];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ rows: [{ ...latest, review: { ...latest.review, history } }] })),
    );
    const original = JSON.stringify(course);
    render(<DatedCatalogueReview courses={[course]} />);
    const toggle = await screen.findByRole("button", { name: "Show review history (3)" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("list", { name: "Review history" })).toBeNull();
    fireEvent.click(toggle);
    const list = screen.getByRole("list", { name: "Review history" });
    const items = within(list).getAllByRole("listitem");
    expect(items[0].textContent).toContain("Current evidence");
    expect(items[0].textContent).toContain("Candidate now unavailable.");
    expect(items[1].textContent).toContain("Superseded decision");
    expect(items[1].textContent).toContain("Current counterpart · By Earlier Reviewer");
    expect(items[1].textContent).toContain(new Date(decisionEntry().at).toLocaleString());
    expect(items[2].textContent).toContain("Superseded evidence");
    expect(items[2].textContent).toContain("Official titles and awards align.");
    expect(screen.getByTestId("text-current-revision-41").textContent).toContain("Current revision 5 · No current reviewer decision");
    fireEvent.click(screen.getByRole("button", { name: "Hide review history (3)" }));
    expect(screen.queryByRole("list", { name: "Review history" })).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(course)).toBe(original);
  });

  it.each(["historical", "no-permission"])("allows read-only history in %s mode with no mutation controls enabled", async (mode) => {
    permissions.edit = mode !== "no-permission";
    const latest = row("current_counterpart");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({
      rows: [{ ...latest, review: { ...latest.review, history: [auditEntry(), decisionEntry()] } }],
    })));
    render(<DatedCatalogueReview courses={[course]} readOnly={mode === "historical"} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show review history (2)" }));
    expect(screen.getByText("Reviewer decision · Current decision")).toBeTruthy();
    expect(screen.queryByTestId("button-audit-dated-catalogue")).toBeNull();
    for (const decision of ["keep_separate", "not_counterpart", "current_counterpart"]) {
      expect((screen.getByTestId(`button-decision-${decision}-41`) as HTMLButtonElement).disabled).toBe(true);
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("pages a large timeline locally and handles URL changes and incomplete legacy events", async () => {
    const history = [
      auditEntry(), decisionEntry(),
      { type: "source_url_changed", at: "2026-07-22T12:00:00Z", fromUrl: course.courseWebsite, toUrl: "https://www.winchester.ac.uk/new/" },
      ...Array.from({ length: 9 }, () => ({ type: "legacy_event" })),
    ];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({
      rows: [{ ...row(), review: { ...row().review, evidenceStale: true, history } }],
    })));
    render(<DatedCatalogueReview courses={[course]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show review history (12)" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(10);
    expect(screen.getAllByText("Timestamp unavailable")).toHaveLength(9);
    expect(screen.getByText(`From: ${course.courseWebsite}`)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Newer history" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Older history" }));
    expect(screen.getByText("History page 2 of 2")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("Reviewer decision · Superseded decision")).toBeTruthy();
    expect(screen.getByText("Official-source audit · Superseded evidence")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Older history" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Newer history" }));
    expect(screen.getByText("History page 1 of 2")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("handles missing history without inventing prior events", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({ rows: [row()] })));
    render(<DatedCatalogueReview courses={[course]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show review history (0)" }));
    expect(screen.getByText("No review history recorded.")).toBeTruthy();
  });

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
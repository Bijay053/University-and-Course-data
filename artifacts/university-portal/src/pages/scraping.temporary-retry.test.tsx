// @vitest-environment jsdom
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ScrapingForTest } from "./scraping";

vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock("@/components/can", () => ({
  Can: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useCan: () => ({ can: () => true, canAny: () => true }),
}));
vi.mock("@/components/scrape-job-card", () => ({ ScrapeJobCard: () => null }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("retries only displayed temporary failures via normal approval, once per click, retaining other failures", async () => {
  const jobId = "temporary-failures-job";
  const rows = [201, 202, 203, 204, 205, 206, 207].map(id => ({
    id, universityId: 7, scrapeJobId: jobId, courseName: `Course ${id}`,
    courseLocation: `Campus ${id}`, status: "pending", intakeMonths: ["October"],
    scrapeWarnings: [], createdAt: "2026-09-27T00:00:00Z",
  }));
  // 201 and 202 have temporary failures. 203–206 have other causes; 207
  // remains selected but is unrelated to the retry.
  const reasons: Record<number, string> = {
    201: "official_source_unavailable", 202: "official_source_unavailable",
    203: "changed_cohort", 204: "unverified_page", 205: "invalid_stored_scope",
    206: "future_unknown_reason",
  };
  let approved201 = false;
  let releaseRetry!: () => void;
  const retryGate = new Promise<void>(resolve => { releaseRetry = resolve; });
  const approvalBodies: { courseIds: number[]; force: boolean }[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/scrape/staged/approve-selected") {
      const body = JSON.parse(String(init?.body)) as { courseIds: number[]; force: boolean };
      approvalBodies.push(body);
      expect(init?.method).toBe("POST");
      const retry = approvalBodies.length > 6;
      if (retry) await retryGate;
      const approvedIds = retry && body.courseIds.includes(201) ? [201] : [];
      if (approvedIds.length) approved201 = true;
      return Response.json({
        approvedIds, approvedCount: approvedIds.length, attempted: body.courseIds.length,
        failed: body.courseIds.filter(id => !approvedIds.includes(id)).map(id => ({
          id, reasonCode: reasons[id], error: `Source failure ${id}`,
        })),
      });
    }
    if (url === `/api/scrape/staged/${jobId}?view=summary`) return Response.json({
      courses: rows.filter(row => !approved201 || row.id !== 201).map(row => ({
        ...row,
        lastQualificationApproval: reasons[row.id] ? {
          rowId: row.id, jobId, universityId: 7, reasonCode: reasons[row.id],
          attemptedAt: "2026-09-27T01:00:00Z",
        } : null,
      })),
    });
    if (url.startsWith("/api/universities")) return Response.json({ data: [], total: 0 });
    if (url === "/api/import/history") return Response.json([]);
    if (url.endsWith("/course-quality")) return Response.json({ courses: [] });
    if (url.startsWith("/api/scrape/staged/fix-jobs?")) return Response.json(null);
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <ScrapingForTest initialReviewState={{ universityId: 7, jobId, courses: rows as never }} />
  </QueryClientProvider>);

  fireEvent.click(screen.getByTestId("checkbox-logical-course-207"));
  fireEvent.click(screen.getByRole("button", { name: "Approve (6 courses)" }));
  await waitFor(() => expect(screen.getByTestId("approval-failure-205")).toBeTruthy());
  expect(approvalBodies).toHaveLength(6);
  expect(screen.queryByRole("region", { name: "Temporary approval failures" })).toBeTruthy();
  const section = screen.getByRole("region", { name: "Temporary approval failures" });
  expect(within(section).getAllByRole("listitem").map(item => item.textContent)).toEqual([
    "Course 201 — Campus 201 (row 201)",
    "Course 202 — Campus 202 (row 202)",
  ]);
  expect(within(section).getByRole("button", { name: "Retry temporary failures (2)" })).toBeTruthy();
  for (const id of [201, 202]) {
    expect(screen.getByTestId(`retry-approval-${id}`)).toBeTruthy();
  }
  for (const id of [203, 204, 205, 206]) {
    if (id !== 206) expect(screen.getByTestId(`approval-failure-${id}`)).toBeTruthy();
    expect(within(section).queryByText(new RegExp(`row ${id}\\)`))).toBeNull();
  }
  fireEvent.click(screen.getByTestId("checkbox-logical-course-207"));
  expect((screen.getByTestId("checkbox-logical-course-207") as HTMLInputElement).checked).toBe(true);
  const retryButton = within(section).getByRole("button", { name: "Retry temporary failures (2)" });
  fireEvent.click(retryButton);
  await waitFor(() => expect(approvalBodies).toHaveLength(8));
  expect(retryButton.hasAttribute("disabled")).toBe(true);
  fireEvent.click(retryButton);
  expect(approvalBodies).toHaveLength(8);
  expect(approvalBodies.slice(6).map(body => body.courseIds).sort((a, b) => a[0] - b[0])).toEqual([[201], [202]]);
  expect(approvalBodies.every(body => body.force === false)).toBe(true);
  releaseRetry();

  await waitFor(() => expect(screen.queryByTestId("approval-failure-201")).toBeNull());
  expect(screen.queryByText("Course 201")).toBeNull();
  expect(within(screen.getByRole("region", { name: "Temporary approval failures" })).getAllByRole("listitem")
    .map(item => item.textContent)).toEqual(["Course 202 — Campus 202 (row 202)"]);
  expect(screen.getByTestId("retry-approval-202")).toBeTruthy();
  for (const id of [203, 204, 205]) expect(screen.getByTestId(`approval-failure-${id}`)).toBeTruthy();
  // Unknown persisted reasons never become trusted recovery guidance.
  expect(screen.queryByTestId("retry-approval-206")).toBeNull();
  await waitFor(() => expect((screen.getByTestId("checkbox-logical-course-207") as HTMLInputElement).checked).toBe(true));
  expect(approvalBodies).toHaveLength(8); // No background/automatic retry.
  // A fresh attempt may identify a non-temporary guard: remove it from bulk
  // retry immediately rather than using the original source-unavailable state.
  reasons[202] = "changed_cohort";
  fireEvent.click(screen.getByRole("button", { name: "Retry temporary failures (1)" }));
  await waitFor(() => expect(screen.queryByRole("region", { name: "Temporary approval failures" })).toBeNull());
  expect(approvalBodies.slice(8)).toEqual([{ courseIds: [202], force: false }]);
  expect(screen.getByTestId("approval-failure-202").textContent).toContain("Verified official fees or intakes differ");
  expect(screen.queryByTestId("retry-approval-202")).toBeNull();
}, 20000);
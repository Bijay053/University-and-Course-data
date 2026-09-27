// @vitest-environment jsdom
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

it("discovers an approved-only cohort from the full page, previews via HTTP, applies to its exact job and uses normal Review approval", async () => {
  const jobId = "original-approved-job";
  let status = "approved";
  const rows = [101, 102, 103, 104].map((id, index) => ({
    id, universityId: 7, scrapeJobId: jobId, courseName: `${index < 2 ? "PG Dip" : "PG Cert"} Legal Technology — ${index % 2 ? "Bristol" : "London Moorgate"}`,
    courseWebsite: "https://www.law.ac.uk/study/postgraduate/law/pg-dip-and-pg-cert-legal-technology/",
    courseLocation: index % 2 ? "Bristol" : "London Moorgate", internationalFee: index < 2 ? 13500 : 6750,
    feeYear: 2027, feeTerm: "Full Course", intakeMonths: ["October"], scrapeWarnings: [],
    createdAt: "2026-09-27T00:00:00Z", extractionMethod: { ulaw_qualification_scope: { award: index < 2 ? "PG Dip" : "PG Cert" } },
  }));
  const requests: string[] = [];
  const approvalBodies: { courseIds: number[]; force: boolean }[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push(url);
    if (url.startsWith("/api/universities")) return Response.json({ data: [{ id: 7, name: "University of Law" }], total: 1 });
    if (url === "/api/scrape/universities/7/approved-qualification-cohorts") {
      expect(status).toBe("approved");
      return Response.json({ cohorts: [{
        rowId: 101, splitFromId: 101, jobId, latest: true, createdAt: "2026-09-27T00:00:00Z",
        label: "PG Dip and PG Cert Legal Technology", feeYears: [2026], courseIds: [101, 102, 103],
      }] });
    }
    if (url.endsWith("/101/qualification-refresh/preview")) {
      expect(status).toBe("approved");
      return Response.json({ token: "exact-job-preview-token", courseIds: [101, 102, 103],
        sourceUrl: rows[0].courseWebsite, message: "No staging changes yet.", changes: [{
          award: "PG Cert", campus: "Bristol", kind: "new", stagedId: null, old: null,
          new: { amount: 6300, year: 2027, term: "Full Course" },
          intakes: [{ intake: "October 2027", study_load: "Part-time", locations: ["Bristol"] }],
        }] });
    }
    if (url.endsWith("/101/qualification-refresh/apply")) {
      expect(JSON.parse(String(init?.body))).toEqual({ token: "exact-job-preview-token" });
      status = "pending";
      return Response.json({ status: "applied", courseIds: rows.map(r => r.id) });
    }
    if (url === `/api/scrape/staged/${jobId}`) return Response.json({
      courses: (status === "approved" ? rows.slice(0, 3) : rows).map(r => ({ ...r, status })),
    });
    if (url === "/api/scrape/staged/approve-selected") {
      approvalBodies.push(JSON.parse(String(init?.body)));
      status = "approved";
      return Response.json({ approvedIds: rows.map(r => r.id), approvedCount: 4, splitCount: 0, failed: [], attempted: 4 });
    }
    if (url === "/api/import/history") return Response.json([]);
    if (url.startsWith("/api/courses?")) return Response.json({ data: [], total: 2 });
    if (url.endsWith("/course-quality")) return Response.json({ courses: [] });
    if (url.startsWith("/api/scrape/staged/fix-jobs?")) return Response.json(null);
    return Response.json({});
  });
  vi.stubGlobal("fetch", fetcher);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <ScrapingForTest initialReviewState={{ universityId: 7, jobId: "unrelated-empty-review", courses: [] }} />
  </QueryClientProvider>);
  expect(screen.queryByText("Apply to staging only")).toBeNull();
  await screen.findByRole("option", { name: "University of Law" });
  fireEvent.change(screen.getByLabelText("University for approved cohort"), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Find approved award cohorts" }));
  expect(await screen.findByText(/Source job: original-approved-job/)).toBeTruthy();
  expect(status).toBe("approved");
  fireEvent.click(screen.getByRole("button", { name: "Preview current award cohort" }));
  expect(await screen.findByText(/Bristol · New campus/)).toBeTruthy();
  expect(status).toBe("approved");
  fireEvent.click(screen.getByRole("button", { name: "Apply to staging only" }));
  await waitFor(() => expect(requests).toContain(`/api/scrape/staged/${jobId}`));
  expect(await screen.findByText("PG Cert Legal Technology — Bristol")).toBeTruthy();
  const approve = await screen.findByRole("button", { name: "Approve (4 courses)" });
  expect(status).toBe("pending");
  expect(requests).not.toContain("/api/scrape/staged/unrelated-empty-review");
  fireEvent.click(approve);
  await waitFor(() => expect(requests).toContain("/api/scrape/staged/approve-selected"));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Approve (4 courses)" })).toBeNull());
  expect(new Set(approvalBodies.flatMap(body => body.courseIds))).toEqual(new Set(rows.map(r => r.id)));
  expect(approvalBodies.every(body => body.force === false)).toBe(true);
  expect(status).toBe("approved");
}, 20000);
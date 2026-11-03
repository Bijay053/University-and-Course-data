// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const perms = { set: new Set<string>() };
vi.mock("@/context/auth", () => ({ useAuth: () => ({ can: (k: string) => perms.set.has(k) }) }));
import { CourseErrorReportsPanel } from "./course-error-reports-panel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });
const report = { id: 5, course_id: 3, course_name: "BSc Nursing", university_name: "Harbour Uni", kind: "incorrect", field: "fees", message: "Fee shows 2023 amount", source_url: "https://h.example", status: "new", created_at: "2025-03-02T10:00:00Z", updated_at: "2025-03-02T10:00:00Z" };
const wrap = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CourseErrorReportsPanel /></QueryClientProvider>);

describe("CourseErrorReportsPanel", () => {
  it("labels insight reports with their university and country", async () => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      reports: [{ ...report, course_id: null, course_name: null, university_id: 4, field: "key_insights", insight_country: "Nepal" }], total: 1,
    }), { status: 200 })));
    wrap();
    await screen.findByText("Key Insights — Nepal");
    expect(screen.getByText("Harbour Uni")).toBeTruthy();
    expect(screen.queryByText("Deleted course")).toBeNull();
    const link = screen.getByTestId("link-report-page-5");
    expect(link.getAttribute("href")).toBe("/universities/4?tab=assessment");
    expect(screen.getByTestId("link-report-university-5").getAttribute("href")).toBe("/universities/4?tab=assessment");
    fireEvent.click(link);
    expect(window.location.pathname).toBe("/universities/4");
    expect(window.location.search).toBe("?tab=assessment");
  });

  it.each(["fees", "english", "academic", "location", "scholarships"])("opens the university Courses tab from both names for a %s report", async (field) => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      reports: [{ ...report, field, university_id: 4 }], total: 1,
    }), { status: 200 })));
    wrap();
    const course = await screen.findByTestId("link-report-page-5");
    expect(course.getAttribute("href")).toBe("/universities/4?tab=courses");
    fireEvent.click(course);
    expect(window.location.pathname).toBe("/universities/4");
    expect(window.location.search).toBe("?tab=courses");
    const university = screen.getByTestId("link-report-university-5");
    expect(university.getAttribute("href")).toBe("/universities/4?tab=courses");
    fireEvent.click(university);
    expect(window.location.pathname).toBe("/universities/4");
    expect(window.location.search).toBe("?tab=courses");
  });

  it("falls back to the dashboard university when a reported course was deleted", async () => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      reports: [{ ...report, course_id: null, university_id: 4 }], total: 1,
    }), { status: 200 })));
    wrap();
    const link = await screen.findByTestId("link-report-page-5");
    expect(link.getAttribute("href")).toBe("/universities/4?tab=courses");
    fireEvent.click(link);
    expect(window.location.pathname).toBe("/universities/4");
    expect(window.location.search).toBe("?tab=courses");
  });

  it("keeps deleted target snapshots without broken navigation links", async () => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      reports: [{ ...report, course_id: null, university_id: null }], total: 1,
    }), { status: 200 })));
    wrap();
    await screen.findByText("BSc Nursing");
    expect(screen.getByText("Harbour Uni")).toBeTruthy();
    expect(screen.queryByTestId("link-report-page-5")).toBeNull();
    expect(screen.queryByTestId("link-report-university-5")).toBeNull();
  });
  it("does not substitute a separate course page when the university is unknown", async () => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      reports: [{ ...report, university_id: null }], total: 1,
    }), { status: 200 })));
    wrap();
    await screen.findByText("BSc Nursing");
    expect(screen.queryByTestId("link-report-page-5")).toBeNull();
    expect(screen.queryByTestId("link-report-university-5")).toBeNull();
  });
  it("renders nothing and does not fetch without courses.view", () => {
    perms.set = new Set(); const f = vi.fn(); vi.stubGlobal("fetch", f);
    wrap();
    expect(screen.queryByTestId("panel-course-error-reports")).toBeNull();
    expect(f).not.toHaveBeenCalled();
  });

  it("lists reports and patches status for editors", async () => {
    perms.set = new Set(["courses.view", "courses.edit"]);
    const f = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") return new Response(JSON.stringify({ ...report, status: "resolved" }));
      return new Response(JSON.stringify({ reports: [report], total: 1 }));
    });
    vi.stubGlobal("fetch", f);
    wrap();
    expect((await screen.findByTestId("text-report-course-5")).textContent).toBe("BSc Nursing");
    expect(String(f.mock.calls[0][0])).toContain("status=new");
    expect(screen.getByTestId("link-report-source-5")).toBeTruthy();
    fireEvent.click(screen.getByTestId("button-report-resolved-5"));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => i?.method === "PATCH" && String(u).endsWith("/api/course-error-reports/5") && JSON.parse(i.body as string).status === "resolved")).toBe(true));
  });

  it("hides actions without courses.edit and handles 403", async () => {
    perms.set = new Set(["courses.view"]);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ reports: [report], total: 1 }))));
    wrap();
    await screen.findByTestId("row-report-5");
    expect(screen.queryByTestId("button-report-resolved-5")).toBeNull();
    cleanup();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 403 })));
    wrap();
    await screen.findByTestId("text-reports-denied");
  });
});

// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import PublicCoursePage from "./public-course";

const course = {
  id: 7, course_name: "Master of Data Science",
  university: { id: 9, name: "Harbour University", logo_url: null, city: "Sydney", country: "Australia" },
  course_location: "Sydney", degree_level: "Master", category: "Computing", sub_category: null, duration: 1.5, duration_term: "Year",
  study_mode: null, intakes: ["February", "July"], international_fee: null, currency: null, fee_term: null, fee_year: null, course_url: null,
  offerings: [
    { id: "a", location: "Sydney", feeAmount: 41200, feeCurrency: "AUD", feeTerm: "Year", feeYear: 2026 },
    { id: "b", location: "Melbourne", feeAmount: 39850, feeCurrency: "AUD", feeTerm: "Semester", feeYear: null },
  ],
  english_requirements: [
    { test_type: "IELTS", overall: 6.5, listening: 6, reading: 6, writing: 6, speaking: 6 },
    { test_type: "PTE", overall: 58, listening: null, reading: null, writing: null, speaking: null },
  ],
  academic_requirements: [{ academic_level: "Bachelor degree", academic_score: 65, score_type: "%" }, { academic_level: null, academic_score: null, score_type: null }],
};

let calls: string[] = [];
let res: () => unknown;
beforeEach(() => {
  calls = []; localStorage.clear();
  res = () => ({ ok: true, status: 200, json: async () => ({ courses: [course] }) });
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = String(input); calls.push(url);
    if (url.endsWith("/insights")) return { ok: true, status: 200, json: async () => ({ insights: [] }) };
    return res();
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const renderAt = (path: string) => {
  window.history.replaceState(null, "", path);
  return render(<Router><Route path="/toolkit/courses/:id" component={PublicCoursePage} /></Router>);
};

describe("public course page", () => {
  it("renders full details with fees, bands and academic entry, hiding missing fields", async () => {
    renderAt("/toolkit/courses/7");
    expect((await screen.findByTestId("text-course-name")).textContent).toBe("Master of Data Science");
    expect(calls).toContain("/api/search/compare?ids=7");
    const fees = screen.getByTestId("list-course-fees").textContent ?? "";
    expect(fees).toContain("AUD 41,200 \u00b7 Year (2026)");
    expect(fees).toContain("AUD 39,850 \u00b7 Semester");
    expect(screen.getByTestId("list-course-locations").textContent).toContain("Melbourne");
    expect(screen.getByTestId("list-course-intakes").textContent).toBe("FebruaryJuly");
    expect(screen.getByTestId("row-english-0").textContent).toBe("IELTS6.56666");
    expect(screen.getByTestId("row-english-1").textContent).toContain("PTE58");
    expect(screen.getByTestId("list-academic").children).toHaveLength(1);
    expect(screen.queryByText(/Not published/i)).toBeNull();
    expect(screen.queryByText("Study mode")).toBeNull();
    expect(screen.getByTestId("link-university-page").getAttribute("href")).toBe("/toolkit/universities/9");
    await waitFor(() => expect(calls.some((u) => u.endsWith("/insights"))).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("section-insights")).toBeNull());
    expect(screen.queryByTestId("link-official-course")).toBeNull();
    fireEvent.click(screen.getByTestId("button-course-shortlist"));
    expect(JSON.parse(localStorage.getItem("sic.degrees.shortlist.v1") ?? "[]")[0].id).toBe(7);
  });

  it("shows not found for an empty (unapproved) response and invalid ids", async () => {
    res = () => ({ ok: true, status: 200, json: async () => ({ courses: [] }) });
    renderAt("/toolkit/courses/8");
    expect(await screen.findByTestId("state-course-not-found")).toBeTruthy();
    cleanup(); calls = [];
    renderAt("/toolkit/courses/abc");
    expect(await screen.findByTestId("state-course-not-found")).toBeTruthy();
    expect(calls).toHaveLength(0);
  });

  it("retries after an error", async () => {
    res = () => ({ ok: false, status: 500, json: async () => ({}) });
    renderAt("/toolkit/courses/7");
    const retry = await screen.findByTestId("button-retry-course");
    res = () => ({ ok: true, status: 200, json: async () => ({ courses: [course] }) });
    fireEvent.click(retry);
    expect(await screen.findByTestId("text-course-name")).toBeTruthy();
  });
});

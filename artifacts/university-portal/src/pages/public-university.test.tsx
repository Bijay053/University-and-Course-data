// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Router } from "wouter";
import PublicUniversityPage from "./public-university";

const english = { ielts_overall: 6.5, pte_overall: null, toefl_overall: null, cae_overall: null, duolingo_overall: null };
const mk = (id: number, name: string) => ({
  id, course_name: name, university: { id: 9, name: "Harbour University", logo_url: null, city: "Sydney", country: "Australia" },
  course_location: "Sydney", offerings: [], degree_level: "Master", category: null, duration: 2, duration_term: "Year",
  intakes: ["February"], international_fee: 41200, currency: "AUD", fee_term: "Year", fee_year: 2025,
  english_requirements: english, course_url: null,
});
const cards = [{ title: "Sponsor requirements", emoji: "👤", fields: [{ label: "Under 18", value: "No", badge: "no" }],
  sections: [{ label: "Parents", fields: [{ label: "Min income", value: "AUD 30,000", badge: null }] }] }];

let calls: string[] = [];
let mode: "ok" | "empty" | "error" = "ok";
let insightsEmpty = false;
beforeEach(() => {
  calls = []; mode = "ok"; insightsEmpty = false; localStorage.clear();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = String(input); calls.push(url);
    const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
    if (url.includes("/api/search/options")) return json({ countries: [], universities: [{ id: 9, name: "Harbour University" }] });
    if (url.endsWith("/insights")) return json({ insights: insightsEmpty ? [] : [
      { id: 1, country: "Nepal", text: "raw", updated_at: null, parsed_data: cards },
      { id: 2, country: "India", text: "Plain text guidance only", updated_at: null, parsed_data: null },
    ] });
    if (mode === "error") return { ok: false, status: 500, json: async () => ({}) };
    if (mode === "empty") return json({ total: 0, page: 1, limit: 12, results: [] });
    const page = Number(new URL(url, "http://x").searchParams.get("page") ?? 1);
    return json({ total: 14, page, limit: 12, results: page === 1 ? [mk(1, "Master of Data Science")] : [mk(13, "Bachelor of Nursing")] });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const renderAt = (path: string) => {
  window.history.replaceState(null, "", path);
  return render(<Router><Route path="/toolkit/universities/:id" component={PublicUniversityPage} /></Router>);
};

describe("public university page", () => {
  it("hides an unavailable insights tab and redirects direct empty-tab links to courses", async () => {
    insightsEmpty = true;
    renderAt("/toolkit/universities/9?tab=insights");
    expect(await screen.findByTestId("card-course-1")).toBeTruthy();
    await waitFor(() => expect(screen.queryByTestId("tab-uni-insights")).toBeNull());
    expect(window.location.search).toBe("");
    expect(screen.queryByText(/No Key Insights/)).toBeNull();
  });

  it("shows the university's courses with pagination", async () => {
    renderAt("/toolkit/universities/9");
    expect((await screen.findByTestId("text-university-name")).textContent).toBe("Harbour University");
    expect(await screen.findByTestId("card-course-1")).toBeTruthy();
    expect(calls.some((u) => u.includes("university_id=9"))).toBe(true);
    expect(screen.getByTestId("text-uni-course-count").textContent).toBe("14 approved courses");
    fireEvent.click(screen.getByTestId("button-next-page"));
    expect(await screen.findByTestId("card-course-13")).toBeTruthy();
    expect(screen.getByTestId("text-page").textContent).toBe("Page 2 of 2");
  });

  it("handles empty and error course states", async () => {
    mode = "empty";
    renderAt("/toolkit/universities/9");
    expect(await screen.findByTestId("state-uni-courses-empty")).toBeTruthy();
    cleanup(); mode = "error";
    renderAt("/toolkit/universities/9");
    fireEvent.click(await screen.findByTestId("button-retry-uni-courses"));
  });

  it("renders Key Insights cards read-only and persists the tab in the URL", async () => {
    renderAt("/toolkit/universities/9");
    await screen.findByTestId("card-course-1");
    fireEvent.click(screen.getByTestId("tab-uni-insights"));
    expect(window.location.search).toBe("?tab=insights");
    expect(await screen.findByText("Sponsor requirements")).toBeTruthy();
    expect(screen.getByText("✕ No")).toBeTruthy();
    expect(screen.getByText("Min income")).toBeTruthy();
    expect(screen.getByText("Plain text guidance only")).toBeTruthy();
    fireEvent.click(screen.getByTestId("pill-country-India"));
    expect(screen.queryByText("Sponsor requirements")).toBeNull();
    const section = screen.getByTestId("section-insights");
    expect(within(section).queryByTitle(/edit|delete/i)).toBeNull();
    expect(within(section).queryByText(/Add Key Insight/)).toBeNull();
  });

  it("opens insights directly from a link and handles unknown universities", async () => {
    renderAt("/toolkit/universities/9?tab=insights");
    expect(await screen.findByText("Sponsor requirements")).toBeTruthy();
    expect(calls.some((u) => u.includes("/api/search/courses"))).toBe(false);
    cleanup();
    renderAt("/toolkit/universities/404");
    expect(await screen.findByTestId("state-university-not-found")).toBeTruthy();
  });
});

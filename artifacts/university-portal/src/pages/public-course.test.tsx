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
  it("keeps degree level in a responsive grid when study mode contains long text", async () => {
    const longMode = "This degree equips you with the technical knowledge and practical skills needed to begin your career as a chartered accountant. Focusing on UK accounting standards in a real-world context, the course prepares you for a range of finance roles in global business environments.";
    res = () => ({ ok: true, status: 200, json: async () => ({ courses: [{
      ...course, degree_level: "Bachelor", duration: null, study_mode: longMode,
      category: "Business & Management", sub_category: "Accounting",
    }] }) });
    renderAt("/toolkit/courses/7");
    const facts = await screen.findByTestId("list-course-facts");
    expect(facts.style.gridTemplateColumns).toBe("repeat(auto-fit, minmax(min(100%, 12rem), 1fr))");
    expect(facts.className).not.toContain("sm:flex");
    expect(facts.children[0].textContent).toBe("LevelBachelor");
    expect(facts.children[1].textContent).toBe(`Study mode${longMode}`);
    expect(facts.children[2].textContent).toBe("FieldBusiness & Management / Accounting");
  });
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

  it("splits combined location strings into clean unique places", async () => {
    res = () => ({ ok: true, status: 200, json: async () => ({ courses: [{ ...course, offerings: [{ id: "a", location: "Sydney, Melbourne; Brisbane", feeAmount: 30000, feeCurrency: "AUD", feeTerm: "Year", feeYear: null }], course_location: " sydney ,Online|Adelaide " }] }) });
    renderAt("/toolkit/courses/7");
    const list = await screen.findByTestId("list-course-locations");
    expect(Array.from(list.querySelectorAll("li")).map((li) => li.textContent)).toEqual(["Sydney", "Melbourne", "Brisbane", "Online", "Adelaide"]);
    expect(screen.queryByTestId("section-scholarships")).toBeNull();
    expect(screen.queryByText(/scholarship/i)).toBeNull();
  });

  it("shows course scholarships with full details, original currency and zero values", async () => {
    const scholarships = [
      { id: 1, name: "Vice-Chancellor International Merit Award", details: "Applied to each year of study.", eligibility_criteria: "GPA of 3.0 or higher", amount: 5000, percentage: 20, currency: "AUD" },
      { id: 2, name: "Early Acceptance Grant", details: null, eligibility_criteria: null, amount: 0, percentage: null, currency: null },
      { id: 3, name: "  ", details: "nameless", eligibility_criteria: null, amount: null, percentage: null, currency: null },
    ];
    res = () => ({ ok: true, status: 200, json: async () => ({ courses: [{ ...course, scholarships }] }) });
    renderAt("/toolkit/courses/7");
    const card = await screen.findByTestId("card-scholarship-1");
    expect(card.textContent).toContain("Vice-Chancellor International Merit Award");
    expect(card.textContent).toContain("Applied to each year of study.");
    expect(card.textContent).toContain("GPA of 3.0 or higher");
    expect(screen.getAllByTestId("text-scholarship-value-1").map((b) => b.textContent)).toEqual(["AUD 5,000", "20%"]);
    expect(screen.getByTestId("text-scholarship-value-2").textContent).toBe("0");
    expect(screen.getByTestId("card-scholarship-2").textContent).not.toContain("Eligibility");
    expect(screen.queryByTestId("card-scholarship-3")).toBeNull();
    expect(screen.getByTestId("link-summary-scholarships").textContent).toBe("2 scholarships for this course");
  });
});

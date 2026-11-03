// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DegreesPage from "./degrees";

const english = { ielts_overall: 6.5, pte_overall: null, toefl_overall: null, cae_overall: null, duolingo_overall: null };
const mk = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id, course_name: name, university: { id: 9, name: "Harbour University", logo_url: null, city: "Sydney", country: "Australia" },
  course_location: "Sydney", offerings: [], degree_level: "Master", category: null, duration: 2, duration_term: "Year",
  intakes: ["February"], international_fee: 41200, currency: "AUD", fee_term: "Year", fee_year: 2025,
  english_requirements: english, course_url: "https://harbour.example.edu/msc", ...extra,
});
const results = [mk(1, "Master of Data Science"), mk(2, "Master of Nursing", { international_fee: null, course_url: "javascript:alert(1)" })];

let calls: string[] = [];
beforeEach(() => {
  calls = [];
  localStorage.clear();
  window.history.replaceState(null, "", "/toolkit");
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = String(input);
    calls.push(url);
    const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
    if (url.endsWith("/insights")) return json({ insights: [{ id: 1, country: "Nepal", text: "Scholarship: 20% for eligible applicants", updated_at: "2026-11-03T00:00:00Z" }] });
    if (url.includes("/api/search/options")) return json({ countries: ["Australia"], universities: [{ id: 9, name: "Harbour University" }], degree_levels: ["Master"], intake_months: ["February"], currencies: ["AUD", "GBP"] });
    if (url.includes("/api/search/compare")) return json({ courses: [{ ...mk(1, "Master of Data Science"), study_mode: null, english_requirements: [{ test_type: "IELTS", overall: 6.5, listening: null, reading: null, writing: null, speaking: null }], academic_requirements: [] }, { ...mk(2, "Master of Nursing"), english_requirements: [], academic_requirements: [] }] });
    return json({ total: 2, page: 1, limit: 12, results });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("public degrees page", () => {
  it("finds universities by name and applies the selected university", async () => {
    render(<DegreesPage />);
    await screen.findByRole("option", { name: "Harbour University" });
    const search = screen.getByRole("searchbox", { name: "Search universities" });
    fireEvent.change(search, { target: { value: "HARBOUR" } });
    fireEvent.click(screen.getByRole("button", { name: "Harbour University" }));
    await waitFor(() => expect(calls.some((url) => url.includes("university_id=9"))).toBe(true));
    expect((screen.getByTestId("select-university") as HTMLSelectElement).value).toBe("9");
    expect((search as HTMLInputElement).value).toBe("");
    expect(await screen.findByText("Scholarship: 20% for eligible applicants")).toBeTruthy();
    fireEvent.change(search, { target: { value: "missing university" } });
    expect(screen.getByText("No universities found.")).toBeTruthy();
    fireEvent.click(screen.getByTestId("button-clear-filters"));
    expect((search as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("select-university") as HTMLSelectElement).value).toBe("");
  });

  it("renders results, sets metadata and shows honest detail", async () => {
    render(<DegreesPage />);
    expect(await screen.findByTestId("card-course-1")).toBeTruthy();
    expect(document.title).toContain("Study Info Centre");
    expect(screen.getByTestId("text-fee-2").textContent).toBe("Fee not published");
    fireEvent.click(screen.getByTestId("button-select-course-2"));
    const panels = screen.getAllByTestId("panel-detail");
    expect(within(panels[0]).queryByTestId("link-official-course")).toBeNull();
    fireEvent.click(screen.getByTestId("button-select-course-1"));
    expect(screen.getAllByTestId("link-official-course")[0].getAttribute("href")).toBe("https://harbour.example.edu/msc");
    await waitFor(() => expect(screen.getAllByText("Scholarship: 20% for eligible applicants").length).toBeGreaterThan(0));
  });

  it("sends currency independently and only enables fee sort with a currency", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    const feeOpt = screen.getByRole("option", { name: /Fee amount, low to high/ }) as HTMLOptionElement;
    expect(feeOpt.disabled).toBe(true);
    await waitFor(() => expect(screen.getAllByRole("option", { name: "GBP" }).length).toBeGreaterThan(0));
    fireEvent.change(screen.getAllByTestId("select-budget-currency")[0], { target: { value: "GBP" } });
    await waitFor(() => expect(calls.some((u) => u.includes("currency=GBP") && !u.includes("fee_max"))).toBe(true));
    expect((screen.getByRole("option", { name: /Fee amount, low to high/ }) as HTMLOptionElement).disabled).toBe(false);
    fireEvent.change(screen.getByTestId("select-sort"), { target: { value: "fee_asc" } });
    await waitFor(() => expect(calls.some((u) => u.includes("sort=fee_asc") && u.includes("currency=GBP"))).toBe(true));
    expect(window.location.search).toContain("currency=GBP");
  });

  it("shortlists, persists and compares via the public endpoint", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    fireEvent.click(screen.getByTestId("button-shortlist-1"));
    fireEvent.click(screen.getByTestId("button-shortlist-2"));
    expect(JSON.parse(localStorage.getItem("sic.degrees.shortlist.v1") ?? "[]")).toHaveLength(2);
    fireEvent.click(screen.getByTestId("button-open-shortlist"));
    fireEvent.click(screen.getByTestId("checkbox-compare-1"));
    fireEvent.click(screen.getByTestId("checkbox-compare-2"));
    fireEvent.click(screen.getByTestId("button-compare"));
    const dialog = await screen.findByTestId("dialog-compare");
    await waitFor(() => expect(within(dialog).getAllByText("Master of Nursing").length).toBeGreaterThan(0));
    expect(calls.some((u) => u.includes("/api/search/compare?ids=1,2"))).toBe(true);
  });

  it("switches to university mode, searches and links each card to its own page", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    fireEvent.click(screen.getByTestId("tab-universities"));
    expect(screen.queryByTestId("select-university")).toBeNull();
    expect(screen.queryByTestId("input-search")).toBeNull();
    const card = await screen.findByTestId("card-university-9");
    expect(card.getAttribute("href")).toBe("/toolkit/universities/9");
    expect(screen.queryByTestId("panel-university")).toBeNull();
    expect(screen.getByTestId("text-university-count").textContent).toBe("1 university");
    fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "zzz" } });
    expect(screen.getByTestId("state-university-empty")).toBeTruthy();
    fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "harb" } });
    await waitFor(() => expect(window.location.search).toBe("?mode=universities&uq=harb"));
    fireEvent.click(screen.getByTestId("card-university-9"));
    await waitFor(() => expect(window.location.pathname).toBe("/toolkit/universities/9"));
  });

  it("restores university mode from the URL", async () => {
    window.history.replaceState(null, "", "/toolkit?mode=universities&uq=harbour");
    render(<DegreesPage />);
    expect(await screen.findByTestId("card-university-9")).toBeTruthy();
    expect((screen.getByTestId("input-university-search") as HTMLInputElement).value).toBe("harbour");
    expect(calls.some((u) => u.includes("/api/search/courses"))).toBe(false);
    expect(window.location.search).toBe("?mode=universities&uq=harbour");
  });
});

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
    if (url.includes("/api/search/options")) return json({ countries: ["Australia"], universities: [{ id: 9, name: "Harbour University" }], degree_levels: ["Master"], intake_months: ["February"], currencies: ["AUD", "GBP"], qualifications: ["Bachelor", "Diploma"], grading_schemes: [{ scheme: "GPA", out_of: ["4", "10"] }, { scheme: "Percentage", out_of: ["100"] }], english_exams: ["IELTS", "PTE"] });
    if (url.includes("/api/search/compare")) return json({ courses: [{ ...mk(1, "Master of Data Science"), study_mode: null, english_requirements: [{ test_type: "IELTS", overall: 6.5, listening: null, reading: null, writing: null, speaking: null }], academic_requirements: [] }, { ...mk(2, "Master of Nursing"), english_requirements: [], academic_requirements: [] }] });
    return json({ total: 2, page: 1, limit: 12, results });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("public degrees page", () => {
  it("labels the public toolkit as beta beside its branding", () => {
    render(<DegreesPage />);
    expect(within(screen.getByTestId("link-home")).getByTestId("badge-toolkit-beta").textContent).toBe("Beta");
  });
  it("searches with pending course-name/location inputs from the bottom filter button", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    fireEvent.change(screen.getByTestId("input-course-name"), { target: { value: "engineering" } });
    fireEvent.change(screen.getByTestId("input-course-location"), { target: { value: "London" } });
    fireEvent.click(screen.getByTestId("button-search-filters"));
    await waitFor(() => expect(calls.some((url) => url.includes("course_name=engineering") && url.includes("location=London"))).toBe(true));
    const count = calls.filter((url) => url.includes("/api/search/courses")).length;
    fireEvent.click(screen.getByTestId("button-search-filters"));
    await waitFor(() => expect(calls.filter((url) => url.includes("/api/search/courses")).length).toBeGreaterThan(count));
  });
  it("combines course-name and location filters above results and clears both", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    fireEvent.change(screen.getByTestId("input-course-name"), { target: { value: "nursing" } });
    fireEvent.change(screen.getByTestId("input-course-location"), { target: { value: "Sydney" } });
    await waitFor(() => expect(calls.some((url) => url.includes("course_name=nursing") && url.includes("location=Sydney"))).toBe(true));
    expect(window.location.search).toContain("location=Sydney");
    expect(screen.queryByTestId("input-search")).toBeNull();
    expect(screen.getByTestId("select-country")).toBeTruthy();
    fireEvent.click(screen.getByTestId("button-clear-filters"));
    expect((screen.getByTestId("input-course-name") as HTMLInputElement).value).toBe("");
    expect((screen.getByTestId("input-course-location") as HTMLInputElement).value).toBe("");
  });

  it("applies every staff-style advanced filter with dependent resets", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    const pick = async (id: string, label: string) => {
      await waitFor(() => expect((screen.getByTestId(id) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(screen.getByTestId(id));
      fireEvent.click(within(screen.getByTestId("panel-filters")).getByRole("option", { name: label }));
    };
    const last = () => new URL(calls.filter((u) => u.includes("/api/search/courses")).at(-1) as string, "http://x").searchParams;
    fireEvent.change(screen.getByTestId("input-course-name"), { target: { value: "data" } });
    fireEvent.change(screen.getByTestId("input-course-location"), { target: { value: "Sydney" } });
    await pick("select-country", "Australia");
    expect((screen.getByTestId("select-grading-scheme") as HTMLButtonElement).disabled).toBe(true);
    await pick("select-qualification", "Bachelor");
    await pick("select-grading-scheme", "GPA");
    expect((screen.getByTestId("input-grading-score") as HTMLInputElement).disabled).toBe(true);
    await pick("select-grading-out-of", "Out of 4");
    fireEvent.change(screen.getByTestId("input-grading-score"), { target: { value: "3.2" } });
    await pick("select-english-exam", "IELTS");
    fireEvent.change(screen.getByTestId("input-english-overall"), { target: { value: "6.5" } });
    fireEvent.change(screen.getByTestId("input-english-speaking"), { target: { value: "6" } });
    fireEvent.change(screen.getByTestId("input-other-exam"), { target: { value: "GMAT" } });
    fireEvent.click(screen.getByTestId("select-intakes"));
    fireEvent.click(screen.getByTestId("toggle-intake-February"));
    fireEvent.click(screen.getByTestId("preset-duration-1-2"));
    expect((screen.getByTestId("input-fee-min") as HTMLInputElement).disabled).toBe(true);
    await pick("select-budget-currency", "GBP");
    fireEvent.click(screen.getByTestId("preset-fee-20000-40000"));
    await waitFor(() => expect(last().get("fee_max")).toBe("40000"));
    const p = last();
    expect(Object.fromEntries(p)).toMatchObject({
      course_name: "data", location: "Sydney", country: "Australia", highest_qualification: "Bachelor", grading_scheme: "GPA",
      grading_out_of: "4", grading_score: "3.2", english_exam: "IELTS", english_overall: "6.5", english_speaking: "6",
      other_exam: "GMAT", intakes: "February", duration_years_min: "1", duration_years_max: "2", currency: "GBP", fee_min: "20000",
    });
    expect(window.location.search).toContain("qual=Bachelor");
    // exam switch clears bands; qualification change clears grading
    await pick("select-english-exam", "PTE");
    expect((screen.getByTestId("input-english-overall") as HTMLInputElement).value).toBe("");
    await pick("select-qualification", "Diploma");
    expect(screen.getByTestId("select-grading-scheme").getAttribute("data-value")).toBe("");
    expect(screen.queryByTestId("input-grading-score")).toBeNull();
    await waitFor(() => expect(last().has("grading_score")).toBe(false));
    expect(last().has("english_overall")).toBe(false);
    fireEvent.click(screen.getByTestId("button-clear-filters"));
    await waitFor(() => expect(window.location.search).toBe(""));
    expect(screen.getByTestId("select-country").getAttribute("data-value")).toBe("");
  });

  it("restores advanced filters from the URL and keeps them across pagination", async () => {
    window.history.replaceState(null, "", "/toolkit?q=law&location=Perth&qual=Bachelor&scheme=Percentage&grade=72&exam=IELTS&eo=7&other=GRE&dmin=1&dmax=3&currency=AUD&budgetmin=5000&budget=30000&page=2");
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    const url = calls.find((u) => u.includes("/api/search/courses")) as string;
    for (const part of ["course_name=law", "location=Perth", "highest_qualification=Bachelor", "grading_scheme=Percentage", "grading_score=72", "english_exam=IELTS", "english_overall=7", "other_exam=GRE", "duration_years_min=1", "duration_years_max=3", "currency=AUD", "fee_min=5000", "fee_max=30000", "page=2"]) expect(url).toContain(part);
    expect((screen.getByTestId("input-grading-score") as HTMLInputElement).value).toBe("72");
    expect((screen.getByTestId("input-duration-max") as HTMLInputElement).value).toBe("3");
  });

  it("omits the removed university and degree-level sidebar filters", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    expect(screen.queryByTestId("input-university-search")).toBeNull();
    expect(screen.queryByTestId("select-university")).toBeNull();
    expect(screen.queryByTestId("select-degree-level")).toBeNull();
    expect(screen.getByTestId("select-intakes")).toBeTruthy();
  });

  it("renders results, sets metadata and shows honest detail", async () => {
    render(<DegreesPage />);
    expect(await screen.findByTestId("card-course-1")).toBeTruthy();
    expect(document.title).toContain("Study Info Centre");
    expect(screen.queryByTestId("text-fee-2")).toBeNull();
    fireEvent.click(screen.getByTestId("button-select-course-2"));
    const panels = screen.getAllByTestId("panel-detail");
    expect(within(panels[0]).getByTestId("link-view-details").getAttribute("href")).toBe("/toolkit/courses/2");
    expect(within(panels[0]).queryByTestId("link-official-course")).toBeNull();
    fireEvent.click(screen.getByTestId("button-select-course-1"));
    expect(screen.getAllByTestId("link-view-details")[0].getAttribute("href")).toBe("/toolkit/courses/1");
    expect(screen.queryByTestId("link-official-course")).toBeNull();
    expect(within(panels[0]).queryByTestId("section-insights")).toBeNull();
    expect(calls.some((url) => url.endsWith("/insights"))).toBe(false);
  });

  it("sends currency independently and only enables fee sort with a currency", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    const feeOpt = screen.getByRole("option", { name: "Fee: low to high" }) as HTMLOptionElement;
    expect(feeOpt.disabled).toBe(true);
    await waitFor(() => expect((screen.getByTestId("select-budget-currency") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("select-budget-currency"));
    fireEvent.change(screen.getByTestId("select-budget-currency-filter"), { target: { value: "gb" } });
    fireEvent.click(within(screen.getByTestId("panel-filters")).getByRole("option", { name: "GBP" }));
    await waitFor(() => expect(calls.some((u) => u.includes("currency=GBP") && !u.includes("fee_max"))).toBe(true));
    expect((screen.getByRole("option", { name: "Fee: low to high" }) as HTMLOptionElement).disabled).toBe(false);
    fireEvent.change(screen.getByTestId("select-sort"), { target: { value: "fee_asc" } });
    await waitFor(() => expect(calls.some((u) => u.includes("sort=fee_asc") && u.includes("currency=GBP"))).toBe(true));
    expect(window.location.search).toContain("currency=GBP");
  });

  it("lets users enable fee sorting beside the compact sort box and keeps the sidebar synchronized", async () => {
    render(<DegreesPage />);
    await screen.findByTestId("card-course-1");
    const sort = screen.getByTestId("select-sort");
    expect(sort.className).toContain("w-44");
    fireEvent.change(screen.getByTestId("select-sort-currency"), { target: { value: "AUD" } });
    expect(screen.getByTestId("select-budget-currency").textContent).toContain("AUD");
    fireEvent.change(sort, { target: { value: "fee_desc" } });
    await waitFor(() => expect(calls.some((u) => u.includes("currency=AUD") && u.includes("sort=fee_desc"))).toBe(true));
    fireEvent.change(screen.getByTestId("select-sort-currency"), { target: { value: "" } });
    expect((sort as HTMLSelectElement).value).toBe("relevance");
    expect(screen.getByText("Choose a currency to sort by fee.")).toBeTruthy();
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

  describe("university directory pagination", () => {
    beforeEach(() => {
      const universities = Array.from({ length: 60 }, (_, i) => ({
        id: 100 + i, name: `University ${String(i + 1).padStart(2, "0")}`,
        country: i < 50 ? "Australia" : "Canada", city: i < 50 ? "Sydney" : "Toronto",
      }));
      vi.stubGlobal("fetch", vi.fn(async () => ({
        ok: true, status: 200, json: async () => ({ countries: [], universities }),
      })));
    });
    const cards = () => screen.queryAllByTestId(/^card-university-\d+$/);
    const pick = (id: string, label: string) => {
      fireEvent.click(screen.getByTestId(id));
      fireEvent.click(screen.getByRole("option", { name: label }));
    };

    it("paginates every university, updates the URL and disables boundary controls", async () => {
      window.history.replaceState(null, "", "/toolkit?mode=universities");
      render(<DegreesPage />);
      await screen.findByTestId("card-university-100");
      expect(within(screen.getByRole("complementary", { name: "University filters" })).getByTestId("input-university-search")).toBeTruthy();
      const universitySearch = screen.getByTestId("input-university-search");
      expect(universitySearch.getAttribute("autocomplete")).toBe("off");
      expect(universitySearch.closest("form")?.getAttribute("autocomplete")).toBe("off");
      expect(universitySearch.closest("form")?.classList.contains("sic-university-search")).toBe(true);
      expect(document.querySelector("header [data-testid='input-university-search']")).toBeNull();
      expect(cards()).toHaveLength(24);
      expect(screen.getByTestId("text-university-range").textContent).toBe("Showing 1–24 of 60 universities");
      expect((screen.getByTestId("button-prev-university-page") as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(screen.getByTestId("button-next-university-page"));
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 2 of 3");
      expect(screen.getByTestId("card-university-124")).toBeTruthy();
      expect(screen.queryByTestId("card-university-100")).toBeNull();
      expect(new URLSearchParams(window.location.search).get("upage")).toBe("2");
      fireEvent.click(screen.getByTestId("button-next-university-page"));
      expect(cards()).toHaveLength(12);
      expect(screen.getByTestId("text-university-range").textContent).toBe("Showing 49–60 of 60 universities");
      expect((screen.getByTestId("button-next-university-page") as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(screen.getByTestId("button-prev-university-page"));
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 2 of 3");
    });

    it("restores shared pages and resets pagination when city, search or country changes", async () => {
      window.history.replaceState(null, "", "/toolkit?mode=universities&upage=2");
      render(<DegreesPage />);
      await screen.findByTestId("card-university-124");
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 2 of 3");
      pick("select-university-city", "Sydney");
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 1 of 3");
      expect(new URLSearchParams(window.location.search).has("upage")).toBe(false);
      fireEvent.click(screen.getByTestId("button-next-university-page"));
      fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "University" } });
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 1 of 3");
      fireEvent.click(screen.getByTestId("button-next-university-page"));
      pick("select-university-country", "Australia");
      expect(screen.getByTestId("text-university-page").textContent).toBe("Page 1 of 3");
      fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "University 01" } });
      expect(cards()).toHaveLength(1);
      expect(screen.queryByRole("navigation", { name: "University pagination" })).toBeNull();
      expect(window.location.search).toContain("ucountry=Australia");
      expect(window.location.search).toContain("ucity=Sydney");
      fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "No matching institution" } });
      expect(await screen.findByTestId("state-university-empty")).toBeTruthy();
      expect(screen.queryByTestId("text-university-range")).toBeNull();
    });

    it.each([["-1", 1], ["oops", 1], ["1.5", 1], ["999", 3]])("normalizes invalid or out-of-range page %s", async (requested, expected) => {
      window.history.replaceState(null, "", `/toolkit?mode=universities&upage=${requested}`);
      render(<DegreesPage />);
      await screen.findByTestId(expected === 3 ? "card-university-148" : "card-university-100");
      expect(screen.getByTestId("text-university-page").textContent).toBe(`Page ${expected} of 3`);
      await waitFor(() => expect(new URLSearchParams(window.location.search).get("upage")).toBe(expected === 1 ? null : "3"));
    });
  });

  describe("university directory location filters", () => {
    const unis = [
      { id: 9, name: "Harbour University", country: "Australia", city: "Sydney" },
      { id: 10, name: "Harbour Institute", country: "Australia", city: "Melbourne" },
      { id: 11, name: "Lakeside College", country: "Canada", city: "Toronto" },
      { id: 12, name: "Coastal Academy", country: "Australia", city: "  " },
      { id: 13, name: "Ridge University", country: "Unknown", city: null },
    ];
    beforeEach(() => {
      vi.stubGlobal("fetch", vi.fn(async (input: string) => {
        const url = String(input); calls.push(url);
        const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
        if (url.includes("/api/search/options")) return json({ countries: [], universities: unis });
        return json({ total: 0, page: 1, limit: 12, results: [] });
      }));
    });
    const country = () => ({ value: screen.getByTestId("select-university-country").getAttribute("data-value") });
    const city = () => ({ value: screen.getByTestId("select-university-city").getAttribute("data-value") });
    const choices = (id: string) => {
      fireEvent.click(screen.getByTestId(id));
      const out = screen.getAllByRole("option").map((o) => o.textContent);
      fireEvent.keyDown(screen.getByTestId(`${id}-filter`), { key: "Escape" });
      return out;
    };
    const pick = (id: string, label: string) => {
      fireEvent.click(screen.getByTestId(id));
      fireEvent.click(screen.getByRole("option", { name: label }));
    };
    const ids = () => screen.queryAllByTestId(/^card-university-\d+$/).map((el) => el.getAttribute("data-testid"));

    it("shows clean locations, derives choices and combines name, country and city", async () => {
      window.history.replaceState(null, "", "/toolkit?mode=universities");
      render(<DegreesPage />);
      await screen.findByTestId("card-university-9");
      expect(screen.getByTestId("text-university-location-9").textContent).toBe("Sydney, Australia");
      expect(screen.getByTestId("text-university-location-12").textContent).toBe("Australia");
      expect(screen.queryByTestId("text-university-location-13")).toBeNull();
      expect(choices("select-university-country")).toEqual(["All countries", "Australia", "Canada"]);
      pick("select-university-country", "Australia");
      expect(choices("select-university-city")).toEqual(["All cities in Australia", "Melbourne", "Sydney"]);
      expect(ids()).toEqual(["card-university-9", "card-university-10", "card-university-12"]);
      fireEvent.change(screen.getByTestId("input-university-search"), { target: { value: "harbour" } });
      pick("select-university-city", "Melbourne");
      expect(ids()).toEqual(["card-university-10"]);
      expect(screen.getByTestId("text-university-count").textContent).toBe("1 of 5 universities");
      await waitFor(() => expect(window.location.search).toBe("?mode=universities&uq=harbour&ucountry=Australia&ucity=Melbourne"));
      pick("select-university-country", "Canada");
      expect(city().value).toBe("");
      expect(screen.getByTestId("state-university-empty")).toBeTruthy();
      fireEvent.click(screen.getByTestId("button-university-empty-clear"));
      expect(ids()).toHaveLength(5);
      expect(country().value).toBe("");
      await waitFor(() => expect(window.location.search).toBe("?mode=universities"));
    });

    it("restores country and city from the URL and resets them", async () => {
      window.history.replaceState(null, "", "/toolkit?mode=universities&ucountry=Australia&ucity=Sydney");
      render(<DegreesPage />);
      await screen.findByTestId("card-university-9");
      expect(country().value).toBe("Australia");
      expect(city().value).toBe("Sydney");
      expect(ids()).toEqual(["card-university-9"]);
      expect(screen.getByTestId("card-university-9").getAttribute("href")).toBe("/toolkit/universities/9");
      fireEvent.click(screen.getByTestId("chip-university-city"));
      expect(ids()).toHaveLength(3);
      fireEvent.click(screen.getByTestId("button-reset-university-filters"));
      expect(ids()).toHaveLength(5);
      fireEvent.click(screen.getByTestId("tab-courses"));
      await waitFor(() => expect(window.location.search).not.toContain("ucountry"));
    });
  });
});

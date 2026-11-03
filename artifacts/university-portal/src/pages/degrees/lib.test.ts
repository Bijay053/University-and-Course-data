import { describe, expect, it } from "vitest";
import { EMPTY_FILTERS, buildSearchParams, filtersFromQuery, filtersToQuery, headlineFee, safeHttpUrl, readShortlist, writeShortlist, type CourseResult } from "./lib";

const course = (over: Partial<CourseResult> = {}): CourseResult => ({
  id: 1, course_name: "MSc Data", university: { id: 2, name: "Uni", logo_url: null, city: null, country: null },
  course_location: null, offerings: [], degree_level: null, category: null, duration: null, duration_term: null, intakes: [],
  international_fee: 20000, currency: "GBP", fee_term: "Year", fee_year: 2025,
  english_requirements: { ielts_overall: null, pte_overall: null, toefl_overall: null, cae_overall: null, duolingo_overall: null },
  course_url: null, ...over,
});

describe("degrees lib", () => {
  it("sends currency with fee_max only when budget complete", () => {
    expect(buildSearchParams({ ...EMPTY_FILTERS, budgetMax: "5000" }).has("fee_max")).toBe(false);
    const p = buildSearchParams({ ...EMPTY_FILTERS, budgetMax: "5000", budgetCurrency: "aud", universityId: "4", intakes: ["July", "March"] });
    expect(p.get("currency")).toBe("AUD");
    expect(p.get("fee_max")).toBe("5000");
    expect(p.get("university_id")).toBe("4");
    expect(p.get("intakes")).toBe("July,March");
  });
  it("round-trips URL state and rejects junk", () => {
    const f = { ...EMPTY_FILTERS, q: "law", location: "London", country: "UK", sort: "fee_asc" as const, page: 3, budgetCurrency: "GBP", budgetMax: "9000" };
    expect(filtersFromQuery(filtersToQuery(f))).toEqual(f);
    expect(filtersFromQuery("sort=evil&page=-2&university=x").sort).toBe("relevance");
  });
  it("sends currency independently and gates fee sorting on it", () => {
    const p = buildSearchParams({ ...EMPTY_FILTERS, budgetCurrency: "GBP", sort: "fee_asc" });
    expect(p.get("currency")).toBe("GBP");
    expect(p.has("fee_max")).toBe(false);
    expect(p.get("sort")).toBe("fee_asc");
    expect(buildSearchParams({ ...EMPTY_FILTERS, sort: "fee_desc" }).has("sort")).toBe(false);
    expect(filtersFromQuery("sort=fee_asc").sort).toBe("relevance");
  });
  it("labels missing fees and mixed currencies honestly", () => {
    expect(headlineFee(course({ international_fee: null }))).toBe("Fee not published");
    expect(headlineFee(course({ offerings: [
      { id: "a", location: "A", feeAmount: 1, feeCurrency: "GBP", feeTerm: null, feeYear: null },
      { id: "b", location: "B", feeAmount: 2, feeCurrency: "EUR", feeTerm: null, feeYear: null },
    ] }))).toMatch(/different currencies/);
  });
  it("allows only http(s) links", () => {
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("https://x.edu/c")).toBe("https://x.edu/c");
  });
  it("persists a sanitized shortlist", () => {
    const store = new Map<string, string>();
    const s = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) } as unknown as Storage;
    writeShortlist([{ id: 5, course_name: "A", university: "U" }], s);
    expect(readShortlist(s)).toEqual([{ id: 5, course_name: "A", university: "U" }]);
  });
});

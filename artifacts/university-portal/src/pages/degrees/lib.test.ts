import { describe, expect, it } from "vitest";
import { EMPTY_FILTERS, buildSearchParams, filtersFromQuery, filtersToQuery, headlineFee, locationText, safeHttpUrl, readShortlist, writeShortlist, type CourseResult } from "./lib";

const course = (over: Partial<CourseResult> = {}): CourseResult => ({
  id: 1, course_name: "MSc Data", university: { id: 2, name: "Uni", logo_url: null, city: null, country: null },
  course_location: null, offerings: [], degree_level: null, category: null, duration: null, duration_term: null, intakes: [],
  international_fee: 20000, currency: "GBP", fee_term: "Year", fee_year: 2025,
  english_requirements: { ielts_overall: null, pte_overall: null, toefl_overall: null, cae_overall: null, duolingo_overall: null },
  course_url: null, ...over,
});

describe("degrees lib", () => {
  it("shows only the offering fees which passed the selected currency and range", () => {
    const all = [
      { id: "1", location: "London", feeAmount: 20000, feeCurrency: "GBP", feeTerm: "Annual", feeYear: 2026 },
      { id: "2", location: "Sydney", feeAmount: 1000, feeCurrency: "AUD", feeTerm: "Annual", feeYear: 2026 },
    ];
    const c = course({ offerings: all, matched_offerings: [all[0]] });
    expect(headlineFee(c)).toBe("GBP 20,000 · Annual (2026)");
    expect(headlineFee(course({ offerings: all }))).toContain("fees in different currencies");
  });
  it("scopes course-name input to names and displays the actual matched campus", () => {
    const params = buildSearchParams({ ...EMPTY_FILTERS, q: "Bachelor of Information Technology", location: "melborne" });
    expect(params.get("course_name")).toBe("Bachelor of Information Technology");
    expect(params.has("q")).toBe(false);
    expect(params.get("location")).toBe("melborne");
    expect(locationText(course({ course_location: "Preston", matched_locations: ["Melbourne"] }))).toBe("Melbourne");
  });
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
  it("round-trips every advanced filter and sanitizes dependent junk", () => {
    const f = { ...EMPTY_FILTERS, q: "it", location: "Perth", country: "Australia", intakes: ["July"], qualification: "Bachelor", scheme: "GPA", outOf: "4", gradingScore: "3.5",
      englishExam: "IELTS", eOverall: "6.5", eReading: "6", eWriting: "6", eListening: "6.5", eSpeaking: "7", otherExam: "GMAT",
      durationMin: 1, durationMax: 2.5, budgetCurrency: "AUD", budgetMin: "10000", budgetMax: "45000", page: 2 };
    expect(filtersFromQuery(filtersToQuery(f))).toEqual(f);
    const junk = filtersFromQuery("scheme=GPA&grade=3&eo=7&dmin=9&dmax=2&budgetmin=abc&qual=Bachelor&outof=x");
    expect(junk).toMatchObject({ scheme: "GPA", outOf: "", gradingScore: "", eOverall: "", durationMin: 0, durationMax: 6, budgetMin: "" });
    expect(filtersFromQuery("scheme=GPA&outof=4&grade=3").scheme).toBe("");
    expect(filtersFromQuery("qual=B&scheme=GPA&outof=4&grade=9").gradingScore).toBe("");
  });
  it("guards fee amounts behind a currency and omits untouched defaults", () => {
    expect(buildSearchParams({ ...EMPTY_FILTERS, budgetMin: "1000", budgetMax: "9000" }).has("fee_min")).toBe(false);
    const d = buildSearchParams(EMPTY_FILTERS);
    expect([...d.keys()]).toEqual(["limit"]);
    const p = buildSearchParams({ ...EMPTY_FILTERS, budgetCurrency: "GBP", budgetMin: "1000", durationMin: 0.5, durationMax: 6, englishExam: "", eOverall: "7" });
    expect(p.get("fee_min")).toBe("1000");
    expect(p.has("fee_max")).toBe(false);
    expect(p.get("duration_years_min")).toBe("0.5");
    expect(p.has("duration_years_max")).toBe(false);
    expect(p.has("english_overall")).toBe(false);
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

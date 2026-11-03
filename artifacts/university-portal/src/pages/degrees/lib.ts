import { appendAcademicCredentialParams } from "../search-query";

export const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
export const MAX_COMPARE = 5;
export const PAGE_SIZE = 12;
export const NOT_PUBLISHED = "Not published";

export type Offering = {
  id: string; location: string;
  feeAmount: number | null; feeCurrency: string | null; feeTerm: string | null; feeYear: number | null;
};
export type CourseResult = {
  matched_locations?: string[] | null;
  matched_offerings?: Offering[] | null;
  id: number;
  course_name: string;
  university: { id: number; name: string; logo_url: string | null; city: string | null; country: string | null };
  course_location: string | null;
  offerings?: Offering[] | null;
  degree_level: string | null;
  category: string | null;
  sub_category?: string | null;
  duration: number | null;
  duration_term: string | null;
  intakes: string[];
  international_fee: number | null;
  currency: string | null;
  fee_term: string | null;
  fee_year?: number | null;
  english_requirements: {
    ielts_overall: number | null; pte_overall: number | null; toefl_overall: number | null;
    cae_overall: number | null; duolingo_overall: number | null;
  };
  course_url: string | null;
};
export type SearchResponse = { total: number; page: number; limit: number; results: CourseResult[] };
export type OptionsResponse = {
  countries: string[];
  universities: { id: number; name: string; country?: string | null; city?: string | null }[];
  degree_levels?: string[];
  intake_months?: string[];
  currencies?: string[];
  qualifications?: string[];
  grading_schemes?: { scheme: string; out_of: string[] }[];
  english_exams?: string[];
  intakes?: string[];
};
export type CompareEnglish = {
  test_type: string | null; test_name?: string | null; overall: number | null;
  listening: number | null; reading: number | null; writing: number | null; speaking: number | null;
};
export type Scholarship = {
  id: number; name: string; details: string | null; eligibility_criteria: string | null;
  amount: number | null; percentage: number | null; currency: string | null;
};
export type CompareCourse = Omit<CourseResult, "english_requirements"> & {
  study_mode?: string | null;
  english_requirements: CompareEnglish[];
  scholarships?: Scholarship[] | null;
  academic_requirements: { academic_level: string | null; academic_score: number | null; score_type: string | null }[];
};

export type SortKey = "relevance" | "name" | "fee_asc" | "fee_desc" | "duration_asc" | "duration_desc";
export const SORTS: { value: SortKey; label: string }[] = [
  { value: "relevance", label: "Best match" },
  { value: "name", label: "Course name A to Z" },
  { value: "fee_asc", label: "Fee: low to high" },
  { value: "fee_desc", label: "Fee: high to low" },
  { value: "duration_asc", label: "Shortest duration" },
  { value: "duration_desc", label: "Longest duration" },
];

export const isFeeSort = (s: SortKey) => s === "fee_asc" || s === "fee_desc";

export const DURATION_DEFAULT: [number, number] = [0, 6];
export const DURATION_LIMIT = 10;
export const FEE_SLIDER_MAX = 100000;
export const FEE_INPUT_MAX = 200000;
export const GRADING_SCHEMES = ["GPA", "Percentage"] as const;
export const ENGLISH_EXAMS_FALLBACK = ["IELTS", "PTE", "TOEFL", "CAE", "DET"];
export const ENGLISH_BANDS = [["eOverall", "Overall", "english_overall", "eo"], ["eReading", "Reading", "english_reading", "er"], ["eWriting", "Writing", "english_writing", "ew"], ["eListening", "Listening", "english_listening", "el"], ["eSpeaking", "Speaking", "english_speaking", "es"]] as const;
export type EnglishBandKey = typeof ENGLISH_BANDS[number][0];

export type DegreeFilters = {
  q: string; location: string; country: string; universityId: string; degreeLevel: string; intakes: string[];
  qualification: string; scheme: string; outOf: string; gradingScore: string;
  englishExam: string; eOverall: string; eReading: string; eWriting: string; eListening: string; eSpeaking: string;
  otherExam: string; durationMin: number; durationMax: number;
  budgetCurrency: string; budgetMin: string; budgetMax: string; sort: SortKey; page: number;
};
export const EMPTY_FILTERS: DegreeFilters = {
  q: "", location: "", country: "", universityId: "", degreeLevel: "", intakes: [],
  qualification: "", scheme: "", outOf: "", gradingScore: "",
  englishExam: "", eOverall: "", eReading: "", eWriting: "", eListening: "", eSpeaking: "",
  otherExam: "", durationMin: DURATION_DEFAULT[0], durationMax: DURATION_DEFAULT[1],
  budgetCurrency: "", budgetMin: "", budgetMax: "", sort: "relevance", page: 1,
};

const NUM = /^\d+(\.\d+)?$/;
const posNum = (v: string) => { const t = v.trim(); return NUM.test(t) ? Number(t) : null; };

/** Fee amounts apply only alongside a currency; nothing is converted across currencies. */
export function activeBudget(f: DegreeFilters): { currency: string; min: number | null; max: number | null } | null {
  if (!f.budgetCurrency) return null;
  const min = posNum(f.budgetMin); const max = posNum(f.budgetMax);
  const lo = min != null && min > 0 ? min : null;
  const hi = max != null && max > 0 ? max : null;
  if (lo == null && hi == null) return null;
  return { currency: f.budgetCurrency.toUpperCase(), min: lo, max: hi != null && lo != null && hi < lo ? lo : hi };
}

/** Academic score is only meaningful once its scheme (and GPA scale) is chosen. */
function academic(f: DegreeFilters) {
  if (!f.qualification) return { qualification: "", scheme: "", outOf: "", gradingScore: "" };
  const scheme = (GRADING_SCHEMES as readonly string[]).includes(f.scheme) ? f.scheme : "";
  const outOf = scheme === "GPA" && posNum(f.outOf) ? f.outOf.trim() : "";
  const score = posNum(f.gradingScore);
  const cap = scheme === "Percentage" ? 100 : outOf ? Number(outOf) : null;
  const gradingScore = cap != null && score != null && score <= cap ? f.gradingScore.trim() : "";
  return { qualification: f.qualification, scheme, outOf, gradingScore };
}

export function countActiveFilters(f: DegreeFilters): number {
  return [
    f.q.trim(), f.location.trim(), f.country, f.universityId, f.degreeLevel, f.qualification, f.englishExam, f.otherExam.trim(),
    f.intakes.length, f.durationMin !== DURATION_DEFAULT[0] || f.durationMax !== DURATION_DEFAULT[1],
    f.budgetCurrency || f.budgetMin.trim() || f.budgetMax.trim(),
  ].filter(Boolean).length;
}

export function buildSearchParams(f: DegreeFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (f.q.trim()) p.set("course_name", f.q.trim());
  if (f.location.trim()) p.set("location", f.location.trim());
  if (f.country) p.set("country", f.country);
  if (f.universityId) p.set("university_id", f.universityId);
  if (f.degreeLevel) p.set("degree_level", f.degreeLevel);
  if (f.intakes.length) p.set("intakes", f.intakes.join(","));
  if (f.durationMin > DURATION_DEFAULT[0]) p.set("duration_years_min", String(f.durationMin));
  if (f.durationMax < DURATION_DEFAULT[1]) p.set("duration_years_max", String(f.durationMax));
  if (f.englishExam) {
    p.set("english_exam", f.englishExam);
    for (const [key, , param] of ENGLISH_BANDS) { const v = f[key].trim(); if (posNum(v) != null) p.set(param, v); }
  }
  appendAcademicCredentialParams(p, academic(f));
  if (f.otherExam.trim()) p.set("other_exam", f.otherExam.trim());
  const budget = activeBudget(f);
  if (f.budgetCurrency) p.set("currency", f.budgetCurrency.toUpperCase());
  if (budget?.min != null) p.set("fee_min", String(budget.min));
  if (budget?.max != null) p.set("fee_max", String(budget.max));
  if (f.sort !== "relevance" && (!isFeeSort(f.sort) || f.budgetCurrency)) p.set("sort", f.sort);
  if (f.page > 1) p.set("page", String(f.page));
  p.set("limit", String(PAGE_SIZE));
  return p;
}

export function filtersToQuery(f: DegreeFilters): string {
  const p = new URLSearchParams();
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.location.trim()) p.set("location", f.location.trim());
  if (f.country) p.set("country", f.country);
  if (f.universityId) p.set("university", f.universityId);
  if (f.degreeLevel) p.set("level", f.degreeLevel);
  if (f.intakes.length) p.set("intakes", f.intakes.join(","));
  const a = academic(f);
  if (a.qualification) p.set("qual", a.qualification);
  if (a.scheme) p.set("scheme", a.scheme);
  if (a.outOf) p.set("outof", a.outOf);
  if (a.gradingScore) p.set("grade", a.gradingScore);
  if (f.englishExam) {
    p.set("exam", f.englishExam);
    for (const [key, , , short] of ENGLISH_BANDS) if (posNum(f[key]) != null) p.set(short, f[key].trim());
  }
  if (f.otherExam.trim()) p.set("other", f.otherExam.trim());
  if (f.durationMin !== DURATION_DEFAULT[0]) p.set("dmin", String(f.durationMin));
  if (f.durationMax !== DURATION_DEFAULT[1]) p.set("dmax", String(f.durationMax));
  if (f.budgetCurrency) p.set("currency", f.budgetCurrency);
  if (f.budgetMin.trim()) p.set("budgetmin", f.budgetMin.trim());
  if (f.budgetMax.trim()) p.set("budget", f.budgetMax.trim());
  if (f.sort !== "relevance") p.set("sort", f.sort);
  if (f.page > 1) p.set("page", String(f.page));
  return p.toString();
}

export function filtersFromQuery(search: string): DegreeFilters {
  const p = new URLSearchParams(search);
  const get = (k: string) => (p.get(k) ?? "").trim();
  const sort = p.get("sort") as SortKey | null;
  const page = Number(p.get("page"));
  const uni = get("university");
  const half = (k: string, fallback: number) => {
    const n = posNum(get(k));
    return n != null && n <= DURATION_LIMIT ? Math.round(n * 2) / 2 : fallback;
  };
  let dMin = half("dmin", DURATION_DEFAULT[0]); let dMax = half("dmax", DURATION_DEFAULT[1]);
  if (dMin > dMax) { dMin = DURATION_DEFAULT[0]; dMax = DURATION_DEFAULT[1]; }
  const numStr = (k: string) => (posNum(get(k)) != null ? get(k) : "");
  const exam = get("exam").slice(0, 40);
  const bands = Object.fromEntries(ENGLISH_BANDS.map(([key, , , short]) => [key, exam ? numStr(short) : ""])) as Record<EnglishBandKey, string>;
  const base: DegreeFilters = {
    ...EMPTY_FILTERS,
    q: p.get("q") ?? "",
    location: p.get("location") ?? "",
    country: p.get("country") ?? "",
    universityId: /^\d+$/.test(uni) ? uni : "",
    degreeLevel: p.get("level") ?? "",
    intakes: get("intakes").split(",").map((s) => s.trim()).filter(Boolean),
    qualification: get("qual").slice(0, 120), scheme: get("scheme"), outOf: get("outof"), gradingScore: get("grade"),
    englishExam: exam, ...bands,
    otherExam: get("other").slice(0, 60),
    durationMin: dMin, durationMax: dMax,
    budgetCurrency: get("currency").toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3),
    budgetMin: numStr("budgetmin"),
    budgetMax: numStr("budget"),
    sort: sort && SORTS.some((s) => s.value === sort) && (!isFeeSort(sort) || /^[A-Z]{3}$/i.test(get("currency"))) ? sort : "relevance",
    page: Number.isInteger(page) && page > 1 ? page : 1,
  };
  return { ...base, ...academic(base) };
}

export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.toString() : null;
  } catch { return null; }
}

export type FeeLine = { label: string | null; amount: number | null; currency: string | null; term: string | null; year: number | null };

export function feeLines(c: Pick<CourseResult, "offerings" | "matched_offerings" | "international_fee" | "currency" | "fee_term" | "fee_year" | "course_location">): FeeLine[] {
  const offs = c.matched_offerings ?? c.offerings ?? [];
  if (offs.length) {
    return offs.map((o) => ({ label: o.location, amount: o.feeAmount, currency: o.feeCurrency, term: o.feeTerm, year: o.feeYear }));
  }
  return [{ label: c.course_location, amount: c.international_fee, currency: c.currency, term: c.fee_term, year: c.fee_year ?? null }];
}

export function formatFeeLine(l: FeeLine): string {
  if (l.amount == null) return `Fee ${NOT_PUBLISHED.toLowerCase()}`;
  const cur = l.currency || "";
  const term = l.term ? ` \u00b7 ${l.term}` : "";
  const year = l.year != null ? ` (${l.year})` : "";
  return `${cur ? `${cur} ` : ""}${l.amount.toLocaleString()}${term}${year}`;
}

/** Headline fee only when a single unambiguous published amount exists. */
export function headlineFee(c: CourseResult): string {
  const lines = feeLines(c).filter((l) => l.amount != null);
  if (!lines.length) return `Fee ${NOT_PUBLISHED.toLowerCase()}`;
  if (lines.length === 1) return formatFeeLine(lines[0]);
  const currencies = new Set(lines.map((l) => l.currency ?? ""));
  if (currencies.size === 1) {
    const min = Math.min(...lines.map((l) => l.amount as number));
    return `From ${lines[0].currency ? `${lines[0].currency} ` : ""}${min.toLocaleString()} (${lines.length} locations)`;
  }
  return `${lines.length} location fees in different currencies`;
}


export function formatDuration(d: number | null, term: string | null): string {
  if (d == null) return NOT_PUBLISHED;
  const r = Math.round(d * 10) / 10;
  const unit = term || "Year";
  return `${r % 1 === 0 ? Math.round(r) : r} ${unit}${r !== 1 && !/s$/i.test(unit) ? "s" : ""}`;
}

export function englishList(e: CourseResult["english_requirements"]): { exam: string; score: number }[] {
  const out: { exam: string; score: number }[] = [];
  const map: [keyof CourseResult["english_requirements"], string][] = [
    ["ielts_overall", "IELTS"], ["pte_overall", "PTE"], ["toefl_overall", "TOEFL"],
    ["cae_overall", "Cambridge (CAE)"], ["duolingo_overall", "Duolingo"],
  ];
  for (const [k, label] of map) { const v = e?.[k]; if (v != null) out.push({ exam: label, score: v }); }
  return out;
}

export function locationText(c: CourseResult): string {
  return c.matched_locations?.filter(Boolean).join(", ") || c.course_location || [c.university.city, c.university.country].filter(Boolean).join(", ") || NOT_PUBLISHED;
}

// ── Shortlist storage ──
export const SHORTLIST_KEY = "sic.degrees.shortlist.v1";
export type ShortlistItem = Pick<CourseResult, "id" | "course_name"> & { university: string };

export function readShortlist(storage: Storage = localStorage): ShortlistItem[] {
  try {
    const raw = JSON.parse(storage.getItem(SHORTLIST_KEY) ?? "[]");
    if (!Array.isArray(raw)) return [];
    return raw.filter((x) => x && Number.isInteger(x.id) && typeof x.course_name === "string")
      .map((x) => ({ id: x.id, course_name: x.course_name, university: String(x.university ?? "") }));
  } catch { return []; }
}
export function writeShortlist(items: ShortlistItem[], storage: Storage = localStorage) {
  try { storage.setItem(SHORTLIST_KEY, JSON.stringify(items)); } catch { /* storage unavailable */ }
}

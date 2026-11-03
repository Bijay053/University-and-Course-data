export const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
export const MAX_COMPARE = 5;
export const PAGE_SIZE = 12;
export const NOT_PUBLISHED = "Not published";

export type Offering = {
  id: string; location: string;
  feeAmount: number | null; feeCurrency: string | null; feeTerm: string | null; feeYear: number | null;
};
export type CourseResult = {
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
  universities: { id: number; name: string }[];
  degree_levels?: string[];
  intake_months?: string[];
  currencies?: string[];
};
export type CompareEnglish = {
  test_type: string | null; test_name?: string | null; overall: number | null;
  listening: number | null; reading: number | null; writing: number | null; speaking: number | null;
};
export type CompareCourse = Omit<CourseResult, "english_requirements"> & {
  study_mode?: string | null;
  english_requirements: CompareEnglish[];
  academic_requirements: { academic_level: string | null; academic_score: number | null; score_type: string | null }[];
};

export type SortKey = "relevance" | "name" | "fee_asc" | "fee_desc" | "duration_asc" | "duration_desc";
export const SORTS: { value: SortKey; label: string }[] = [
  { value: "relevance", label: "Best match" },
  { value: "name", label: "Course name A to Z" },
  { value: "fee_asc", label: "Fee amount, low to high" },
  { value: "fee_desc", label: "Fee amount, high to low" },
  { value: "duration_asc", label: "Shortest duration" },
  { value: "duration_desc", label: "Longest duration" },
];

export const isFeeSort = (s: SortKey) => s === "fee_asc" || s === "fee_desc";

export type DegreeFilters = {
  q: string; country: string; universityId: string; degreeLevel: string; intakes: string[];
  budgetCurrency: string; budgetMax: string; sort: SortKey; page: number;
};
export const EMPTY_FILTERS: DegreeFilters = {
  q: "", country: "", universityId: "", degreeLevel: "", intakes: [],
  budgetCurrency: "", budgetMax: "", sort: "relevance", page: 1,
};

/** A budget applies only when both a currency and a positive amount are set. */
export function activeBudget(f: DegreeFilters): { currency: string; max: number } | null {
  const max = Number(f.budgetMax);
  if (!f.budgetCurrency || !f.budgetMax.trim() || !Number.isFinite(max) || max <= 0) return null;
  return { currency: f.budgetCurrency.toUpperCase(), max };
}

export function buildSearchParams(f: DegreeFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.country) p.set("country", f.country);
  if (f.universityId) p.set("university_id", f.universityId);
  if (f.degreeLevel) p.set("degree_level", f.degreeLevel);
  if (f.intakes.length) p.set("intakes", f.intakes.join(","));
  const budget = activeBudget(f);
  if (f.budgetCurrency) p.set("currency", f.budgetCurrency.toUpperCase());
  if (budget) p.set("fee_max", String(budget.max));
  if (f.sort !== "relevance" && (!isFeeSort(f.sort) || f.budgetCurrency)) p.set("sort", f.sort);
  if (f.page > 1) p.set("page", String(f.page));
  p.set("limit", String(PAGE_SIZE));
  return p;
}

export function filtersToQuery(f: DegreeFilters): string {
  const p = new URLSearchParams();
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.country) p.set("country", f.country);
  if (f.universityId) p.set("university", f.universityId);
  if (f.degreeLevel) p.set("level", f.degreeLevel);
  if (f.intakes.length) p.set("intakes", f.intakes.join(","));
  if (f.budgetCurrency) p.set("currency", f.budgetCurrency);
  if (f.budgetMax.trim()) p.set("budget", f.budgetMax.trim());
  if (f.sort !== "relevance") p.set("sort", f.sort);
  if (f.page > 1) p.set("page", String(f.page));
  return p.toString();
}

export function filtersFromQuery(search: string): DegreeFilters {
  const p = new URLSearchParams(search);
  const sort = p.get("sort") as SortKey | null;
  const page = Number(p.get("page"));
  const uni = p.get("university") ?? "";
  return {
    q: p.get("q") ?? "",
    country: p.get("country") ?? "",
    universityId: /^\d+$/.test(uni) ? uni : "",
    degreeLevel: p.get("level") ?? "",
    intakes: (p.get("intakes") ?? "").split(",").map((s) => s.trim()).filter(Boolean),
    budgetCurrency: (p.get("currency") ?? "").toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3),
    budgetMax: /^\d+(\.\d+)?$/.test(p.get("budget") ?? "") ? (p.get("budget") as string) : "",
    sort: sort && SORTS.some((s) => s.value === sort) && (!isFeeSort(sort) || /^[A-Z]{3}$/i.test(p.get("currency") ?? "")) ? sort : "relevance",
    page: Number.isInteger(page) && page > 1 ? page : 1,
  };
}

export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw.trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.toString() : null;
  } catch { return null; }
}

export type FeeLine = { label: string | null; amount: number | null; currency: string | null; term: string | null; year: number | null };

export function feeLines(c: Pick<CourseResult, "offerings" | "international_fee" | "currency" | "fee_term" | "fee_year" | "course_location">): FeeLine[] {
  const offs = c.offerings ?? [];
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
  return c.course_location || [c.university.city, c.university.country].filter(Boolean).join(", ") || NOT_PUBLISHED;
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

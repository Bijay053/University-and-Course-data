import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "wouter";
import { Search, MapPin, X, Filter, Scale, ExternalLink, Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Slider } from "@/components/ui/slider";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const COMPARE_KEY = "courseCompareTray";
const MAX_COMPARE = 5;

type FacetItem = { id?: number | string; name: string; count: number };
type CourseResult = {
  id: number;
  course_name: string;
  university: { id: number; name: string; logo_url: string | null; city: string | null; country: string | null; website: string | null };
  course_location: string | null;
  degree_level: string | null;
  category: string | null;
  sub_category: string | null;
  duration: number | null;
  duration_term: string | null;
  duration_years: number | null;
  intakes: string[];
  international_fee: number | null;
  currency: string | null;
  fee_term: string | null;
  application_fee: number | null;
  english_requirements: {
    ielts_overall: number | null; pte_overall: number | null;
    toefl_overall: number | null; cae_overall: number | null;
    duolingo_overall: number | null;
  };
  course_url: string | null;
};
type SearchResponse = {
  total: number; page: number; limit: number; took_ms: number;
  results: CourseResult[];
  facets: {
    universities: FacetItem[]; categories: FacetItem[];
    degree_levels: FacetItem[]; intakes: FacetItem[];
  };
};

function loadCompareTray(): number[] {
  try {
    const raw = sessionStorage.getItem(COMPARE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((n) => Number.isInteger(n)).slice(0, MAX_COMPARE) : [];
  } catch { return []; }
}
function saveCompareTray(ids: number[]) {
  sessionStorage.setItem(COMPARE_KEY, JSON.stringify(ids.slice(0, MAX_COMPARE)));
  window.dispatchEvent(new CustomEvent("compareTrayChange"));
}

function formatFee(amount: number | null, currency: string | null, term: string | null) {
  if (amount == null) return "Fee not listed";
  const cur = currency || "AUD";
  const t = term ? ` / ${term}` : "";
  return `${cur} ${Math.round(amount).toLocaleString()}${t}`;
}
function formatDuration(d: number | null, term: string | null) {
  if (d == null) return "—";
  const unit = term || "Year";
  return `${d} ${unit}${d !== 1 ? (unit.endsWith("s") ? "" : "s") : ""}`;
}

export default function SearchPage() {
  const [, setLocation] = useLocation();

  // ─── filter state ───────────────────────────────────────────
  const [q, setQ] = useState("");
  const [location, setLocFilter] = useState("");
  const [selectedUnis, setSelectedUnis] = useState<number[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedLevels, setSelectedLevels] = useState<string[]>([]);
  const [selectedIntakes, setSelectedIntakes] = useState<string[]>([]);
  const [feeRange, setFeeRange] = useState<[number, number]>([0, 100000]);
  const [durationRange, setDurationRange] = useState<[number, number]>([0, 6]);
  const [englishExam, setEnglishExam] = useState<string>("");
  const [englishScore, setEnglishScore] = useState<string>("");
  const [sort, setSort] = useState("relevance");
  const [page, setPage] = useState(1);

  // ─── data state ────────────────────────────────────────────
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tray, setTray] = useState<number[]>(loadCompareTray());

  // Listen for compare tray changes from other parts of the page.
  useEffect(() => {
    const handler = () => setTray(loadCompareTray());
    window.addEventListener("compareTrayChange", handler);
    window.addEventListener("storage", handler);
    return () => {
      window.removeEventListener("compareTrayChange", handler);
      window.removeEventListener("storage", handler);
    };
  }, []);

  // Build the request URL from the current filter state.
  const requestUrl = useMemo(() => {
    const params = new URLSearchParams();
    if (q.trim()) params.set("q", q.trim());
    if (location.trim()) params.set("location", location.trim());
    if (selectedUnis.length) params.set("university_id", selectedUnis.join(","));
    if (selectedCategories.length) params.set("category", selectedCategories.join(","));
    if (selectedLevels.length) params.set("degree_level", selectedLevels.join(","));
    if (selectedIntakes.length) params.set("intakes", selectedIntakes.join(","));
    if (feeRange[0] > 0) params.set("fee_min", String(feeRange[0]));
    if (feeRange[1] < 100000) params.set("fee_max", String(feeRange[1]));
    if (durationRange[0] > 0) params.set("duration_years_min", String(durationRange[0]));
    if (durationRange[1] < 6) params.set("duration_years_max", String(durationRange[1]));
    if (englishExam) params.set("english_exam", englishExam);
    if (englishScore.trim()) params.set("english_score_min", englishScore.trim());
    if (sort) params.set("sort", sort);
    if (page > 1) params.set("page", String(page));
    params.set("limit", "20");
    return `${BASE}/api/search/courses?${params.toString()}`;
  }, [q, location, selectedUnis, selectedCategories, selectedLevels, selectedIntakes,
      feeRange, durationRange, englishExam, englishScore, sort, page]);

  // Debounce: wait 300ms after the URL stabilizes before firing.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchRef = useRef<AbortController | null>(null);
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      if (fetchRef.current) fetchRef.current.abort();
      const ac = new AbortController();
      fetchRef.current = ac;
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(requestUrl, { signal: ac.signal });
        if (!res.ok) throw new Error(await res.text());
        const json = (await res.json()) as SearchResponse;
        setData(json);
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError((err as Error).message);
      } finally {
        if (fetchRef.current === ac) setLoading(false);
      }
    }, 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [requestUrl]);

  // Reset to page 1 whenever any non-page filter changes.
  const resetPage = useCallback(() => setPage(1), []);

  const toggleInArray = <T,>(arr: T[], val: T): T[] =>
    arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val];

  const addToCompare = (id: number) => {
    if (tray.includes(id)) return;
    if (tray.length >= MAX_COMPARE) return;
    saveCompareTray([...tray, id]);
  };
  const removeFromCompare = (id: number) => saveCompareTray(tray.filter((x) => x !== id));
  const clearCompare = () => saveCompareTray([]);
  const goCompare = () => setLocation(`/compare?ids=${tray.join(",")}`);

  // For the compare tray, we need the names of the selected courses. We
  // remember them as the user adds courses (so they survive page navigation
  // within /search). When tray changes externally we may not have a label —
  // fall back to "Course #ID".
  const labelMap = useRef<Record<number, string>>({});
  useEffect(() => {
    if (data?.results) {
      for (const r of data.results) labelMap.current[r.id] = r.course_name;
    }
  }, [data]);

  const clearAllFilters = () => {
    setQ(""); setLocFilter(""); setSelectedUnis([]); setSelectedCategories([]);
    setSelectedLevels([]); setSelectedIntakes([]); setFeeRange([0, 100000]);
    setDurationRange([0, 6]); setEnglishExam(""); setEnglishScore(""); setSort("relevance"); setPage(1);
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;

  return (
    <div className="space-y-4">
      {/* ── Top bar ─────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border p-4 sticky top-0 z-10">
        <div className="flex flex-col md:flex-row gap-3">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <Input
              value={q}
              onChange={(e) => { setQ(e.target.value); resetPage(); }}
              placeholder="Search course name or keywords..."
              className="pl-9 h-11"
            />
          </div>
          <div className="md:w-72 relative">
            <MapPin className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <Input
              value={location}
              onChange={(e) => { setLocFilter(e.target.value); resetPage(); }}
              placeholder="City or country (e.g. Sydney)"
              className="pl-9 h-11"
            />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[280px_1fr] gap-4">
        {/* ── Filters ──────────────────────────────────────── */}
        <aside className="bg-white rounded-xl border p-4 space-y-4 self-start lg:sticky lg:top-[88px]">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-sm flex items-center gap-2"><Filter className="w-4 h-4" /> Filters</h2>
            <Button variant="ghost" size="sm" onClick={clearAllFilters}>Reset</Button>
          </div>

          <Accordion type="multiple" defaultValue={["fee", "duration", "english", "intakes", "level"]} className="w-full">
            <AccordionItem value="english">
              <AccordionTrigger className="text-sm">English Proficiency</AccordionTrigger>
              <AccordionContent className="space-y-2 pt-1">
                <Select value={englishExam || "none"} onValueChange={(v) => { setEnglishExam(v === "none" ? "" : v); resetPage(); }}>
                  <SelectTrigger className="h-9"><SelectValue placeholder="Choose exam" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">— Any —</SelectItem>
                    {["IELTS", "PTE", "TOEFL", "CAE", "DET"].map((e) => <SelectItem key={e} value={e}>{e}</SelectItem>)}
                  </SelectContent>
                </Select>
                {englishExam && (
                  <Input
                    type="number" step="0.5" placeholder="Your score (e.g. 6.5)"
                    value={englishScore}
                    onChange={(e) => { setEnglishScore(e.target.value); resetPage(); }}
                    className="h-9"
                  />
                )}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="intakes">
              <AccordionTrigger className="text-sm">Intakes</AccordionTrigger>
              <AccordionContent className="space-y-1 pt-1">
                {(data?.facets.intakes ?? []).slice(0, 12).map((f) => (
                  <label key={f.name} className="flex items-center gap-2 text-sm py-0.5">
                    <Checkbox
                      checked={selectedIntakes.includes(f.name)}
                      onCheckedChange={() => { setSelectedIntakes((arr) => toggleInArray(arr, f.name)); resetPage(); }}
                    />
                    <span className="flex-1">{f.name}</span>
                    <span className="text-gray-400 text-xs">{f.count}</span>
                  </label>
                ))}
                {(data?.facets.intakes ?? []).length === 0 && <p className="text-xs text-gray-400">No intakes match.</p>}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="duration">
              <AccordionTrigger className="text-sm">Duration (years)</AccordionTrigger>
              <AccordionContent className="space-y-2 pt-2">
                <Slider
                  value={durationRange}
                  min={0} max={6} step={0.5}
                  onValueChange={(v) => { setDurationRange(v as [number, number]); resetPage(); }}
                />
                <p className="text-xs text-gray-500">{durationRange[0]} — {durationRange[1]} years</p>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="fee">
              <AccordionTrigger className="text-sm">Tuition Fee (AUD/yr)</AccordionTrigger>
              <AccordionContent className="space-y-2 pt-2">
                <Slider
                  value={feeRange}
                  min={0} max={100000} step={1000}
                  onValueChange={(v) => { setFeeRange(v as [number, number]); resetPage(); }}
                />
                <p className="text-xs text-gray-500">${feeRange[0].toLocaleString()} — ${feeRange[1].toLocaleString()}</p>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="level">
              <AccordionTrigger className="text-sm">Degree Level</AccordionTrigger>
              <AccordionContent className="space-y-1 pt-1">
                {(data?.facets.degree_levels ?? []).slice(0, 12).map((f) => (
                  <label key={f.name} className="flex items-center gap-2 text-sm py-0.5">
                    <Checkbox
                      checked={selectedLevels.includes(f.name)}
                      onCheckedChange={() => { setSelectedLevels((arr) => toggleInArray(arr, f.name)); resetPage(); }}
                    />
                    <span className="flex-1">{f.name}</span>
                    <span className="text-gray-400 text-xs">{f.count}</span>
                  </label>
                ))}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="category">
              <AccordionTrigger className="text-sm">Category</AccordionTrigger>
              <AccordionContent className="space-y-1 pt-1">
                {(data?.facets.categories ?? []).slice(0, 15).map((f) => (
                  <label key={f.name} className="flex items-center gap-2 text-sm py-0.5">
                    <Checkbox
                      checked={selectedCategories.includes(f.name)}
                      onCheckedChange={() => { setSelectedCategories((arr) => toggleInArray(arr, f.name)); resetPage(); }}
                    />
                    <span className="flex-1 truncate" title={f.name}>{f.name}</span>
                    <span className="text-gray-400 text-xs">{f.count}</span>
                  </label>
                ))}
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="university">
              <AccordionTrigger className="text-sm">University</AccordionTrigger>
              <AccordionContent className="space-y-1 pt-1 max-h-64 overflow-y-auto">
                {(data?.facets.universities ?? []).slice(0, 30).map((f) => (
                  <label key={f.id} className="flex items-center gap-2 text-sm py-0.5">
                    <Checkbox
                      checked={selectedUnis.includes(Number(f.id))}
                      onCheckedChange={() => { setSelectedUnis((arr) => toggleInArray(arr, Number(f.id))); resetPage(); }}
                    />
                    <span className="flex-1 truncate" title={f.name}>{f.name}</span>
                    <span className="text-gray-400 text-xs">{f.count}</span>
                  </label>
                ))}
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </aside>

        {/* ── Results ─────────────────────────────────────── */}
        <main className="space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="text-sm text-gray-700">
              {loading ? (
                <span className="flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Searching…</span>
              ) : data ? (
                <span><strong>{data.total.toLocaleString()}</strong> course{data.total === 1 ? "" : "s"} found
                  <span className="text-xs text-gray-400 ml-2">({data.took_ms}ms)</span></span>
              ) : "—"}
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-xs text-gray-500">Sort</Label>
              <Select value={sort} onValueChange={(v) => { setSort(v); resetPage(); }}>
                <SelectTrigger className="h-9 w-[180px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="relevance">Relevance</SelectItem>
                  <SelectItem value="fee_asc">Fee (low → high)</SelectItem>
                  <SelectItem value="fee_desc">Fee (high → low)</SelectItem>
                  <SelectItem value="duration_asc">Duration (shortest)</SelectItem>
                  <SelectItem value="name_asc">Name (A → Z)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {error && (
            <div className="border rounded-xl bg-red-50 text-red-700 px-4 py-3 text-sm">
              Search failed: {error}
            </div>
          )}

          <div className="space-y-3">
            {(data?.results ?? []).map((r) => {
              const inTray = tray.includes(r.id);
              const trayFull = tray.length >= MAX_COMPARE && !inTray;
              return (
                <div key={r.id} className="bg-white rounded-xl border p-4 hover:shadow-md transition-shadow">
                  <div className="flex gap-4">
                    {r.university.logo_url ? (
                      <img src={r.university.logo_url} alt={r.university.name} className="w-14 h-14 object-contain rounded border bg-gray-50 flex-shrink-0" />
                    ) : (
                      <div className="w-14 h-14 rounded border bg-gray-100 flex items-center justify-center text-gray-400 text-[10px] font-medium flex-shrink-0">
                        {r.university.name.split(" ").slice(0, 2).map((s) => s[0]).join("")}
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <h3 className="font-semibold text-base text-gray-900 leading-snug">{r.course_name}</h3>
                      <p className="text-sm text-gray-600 mt-0.5">{r.university.name}</p>
                      <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-600">
                        {(r.course_location || r.university.city) && (
                          <span className="flex items-center gap-1"><MapPin className="w-3 h-3" />{r.course_location || `${r.university.city}, ${r.university.country}`}</span>
                        )}
                        <span>💰 {formatFee(r.international_fee, r.currency, r.fee_term)}</span>
                        <span>⏱ {formatDuration(r.duration, r.duration_term)}</span>
                        {r.intakes.length > 0 && <span>📅 {r.intakes.join(", ")}</span>}
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {r.degree_level && <Badge variant="secondary" className="text-[10px]">{r.degree_level}</Badge>}
                        {r.category && <Badge variant="outline" className="text-[10px]">{r.category}</Badge>}
                      </div>
                    </div>
                    <div className="flex flex-col gap-2 flex-shrink-0">
                      <Button
                        variant={inTray ? "secondary" : "outline"}
                        size="sm"
                        onClick={() => inTray ? removeFromCompare(r.id) : addToCompare(r.id)}
                        disabled={trayFull}
                        title={trayFull ? `Compare tray full (max ${MAX_COMPARE})` : ""}
                      >
                        <Scale className="w-4 h-4 mr-1" />
                        {inTray ? "In Compare" : "Compare"}
                      </Button>
                      {r.course_url && (
                        <a href={r.course_url} target="_blank" rel="noreferrer">
                          <Button variant="outline" size="sm" className="w-full">
                            View <ExternalLink className="w-3 h-3 ml-1" />
                          </Button>
                        </a>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}

            {!loading && data && data.results.length === 0 && (
              <div className="bg-white border rounded-xl p-12 text-center text-gray-500">
                No courses match your filters. Try resetting some filters.
              </div>
            )}
          </div>

          {/* ── Pagination ────────────────────────────────── */}
          {data && data.total > data.limit && (
            <div className="flex items-center justify-center gap-2 pt-4">
              <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>← Prev</Button>
              <span className="text-sm text-gray-600 px-3">Page {page} of {totalPages}</span>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next →</Button>
            </div>
          )}
        </main>
      </div>

      {/* ── Compare tray (floating) ──────────────────────── */}
      {tray.length > 0 && (
        <div className="fixed bottom-0 left-0 right-0 bg-white border-t shadow-lg z-20 px-4 py-3">
          <div className="max-w-6xl mx-auto flex items-center gap-3 flex-wrap">
            <span className="text-sm font-medium">{tray.length} of {MAX_COMPARE} courses selected</span>
            <div className="flex flex-wrap gap-2 flex-1">
              {tray.map((id) => (
                <Badge key={id} variant="secondary" className="text-xs gap-1">
                  <span className="max-w-[200px] truncate">{labelMap.current[id] ?? `Course #${id}`}</span>
                  <button onClick={() => removeFromCompare(id)} className="hover:text-red-600">
                    <X className="w-3 h-3" />
                  </button>
                </Badge>
              ))}
            </div>
            <Button variant="ghost" size="sm" onClick={clearCompare}>Clear</Button>
            <Button size="sm" onClick={goCompare} disabled={tray.length < 2}>
              Compare {tray.length} Course{tray.length === 1 ? "" : "s"} →
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "wouter";
import {
  Search, MapPin, X, Filter, Scale, ExternalLink, Loader2,
  GraduationCap, Calendar, DollarSign, Clock, BookOpen, Globe2, Award,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { MultiSelect } from "@/components/multi-select";

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

type OptionsResponse = {
  countries: string[];
  qualifications: string[];
  grading_schemes: { scheme: string; out_of: string[] }[];
  english_exams: string[];
  universities: { id: number; name: string }[];
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
  if (amount == null) return null;
  const cur = currency || "AUD";
  const t = term ? ` / ${term}` : "";
  return `${cur} ${Math.round(amount).toLocaleString()}${t}`;
}
function formatDuration(d: number | null, term: string | null) {
  if (d == null) return null;
  const unit = term || "Year";
  return `${d} ${unit}${d !== 1 && !unit.endsWith("s") ? "s" : ""}`;
}

export default function SearchPage() {
  const [, setLocation] = useLocation();

  // ─── core filters ──────────────────────────────────────
  const [q, setQ] = useState("");
  const [location, setLocFilter] = useState("");
  const [selectedUnis, setSelectedUnis] = useState<string[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedLevels, setSelectedLevels] = useState<string[]>([]);
  const [selectedIntakes, setSelectedIntakes] = useState<string[]>([]);
  const [feeRange, setFeeRange] = useState<[number, number]>([0, 100000]);
  const [durationRange, setDurationRange] = useState<[number, number]>([0, 6]);
  const [sort, setSort] = useState("relevance");
  const [page, setPage] = useState(1);

  // ─── advanced (academic) filters ───────────────────────
  const [country, setCountry] = useState("");
  const [qualification, setQualification] = useState("");
  const [scheme, setScheme] = useState("");
  const [outOf, setOutOf] = useState("");
  const [gradingScore, setGradingScore] = useState("");

  // ─── english (per-band) filters ────────────────────────
  const [englishExam, setEnglishExam] = useState("");
  const [eOverall, setEOverall] = useState("");
  const [eReading, setEReading] = useState("");
  const [eWriting, setEWriting] = useState("");
  const [eListening, setEListening] = useState("");
  const [eSpeaking, setESpeaking] = useState("");

  // ─── other exam ────────────────────────────────────────
  const [otherExam, setOtherExam] = useState("");

  // ─── data ──────────────────────────────────────────────
  const [data, setData] = useState<SearchResponse | null>(null);
  const [options, setOptions] = useState<OptionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tray, setTray] = useState<number[]>(loadCompareTray());

  // Fetch dropdown options once.
  useEffect(() => {
    fetch(`${BASE}/api/search/options`)
      .then((r) => r.ok ? r.json() : null)
      .then((j) => j && setOptions(j))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const handler = () => setTray(loadCompareTray());
    window.addEventListener("compareTrayChange", handler);
    window.addEventListener("storage", handler);
    return () => {
      window.removeEventListener("compareTrayChange", handler);
      window.removeEventListener("storage", handler);
    };
  }, []);

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
    if (eOverall) params.set("english_overall", eOverall);
    if (eReading) params.set("english_reading", eReading);
    if (eWriting) params.set("english_writing", eWriting);
    if (eListening) params.set("english_listening", eListening);
    if (eSpeaking) params.set("english_speaking", eSpeaking);
    if (country) params.set("country_residence", country);
    if (qualification) params.set("highest_qualification", qualification);
    if (scheme) params.set("grading_scheme", scheme);
    if (outOf) params.set("grading_out_of", outOf);
    if (gradingScore) params.set("grading_score", gradingScore);
    if (otherExam.trim()) params.set("other_exam", otherExam.trim());
    if (sort) params.set("sort", sort);
    if (page > 1) params.set("page", String(page));
    params.set("limit", "20");
    return `${BASE}/api/search/courses?${params.toString()}`;
  }, [q, location, selectedUnis, selectedCategories, selectedLevels, selectedIntakes,
      feeRange, durationRange, englishExam, eOverall, eReading, eWriting, eListening, eSpeaking,
      country, qualification, scheme, outOf, gradingScore, otherExam, sort, page]);

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

  const resetPage = useCallback(() => setPage(1), []);

  const addToCompare = (id: number) => {
    if (tray.includes(id) || tray.length >= MAX_COMPARE) return;
    saveCompareTray([...tray, id]);
  };
  const removeFromCompare = (id: number) => saveCompareTray(tray.filter((x) => x !== id));
  const clearCompare = () => saveCompareTray([]);
  const goCompare = () => setLocation(`/compare?ids=${tray.join(",")}`);

  const labelMap = useRef<Record<number, string>>({});
  useEffect(() => {
    if (data?.results) for (const r of data.results) labelMap.current[r.id] = r.course_name;
  }, [data]);

  const clearAllFilters = () => {
    setQ(""); setLocFilter(""); setSelectedUnis([]); setSelectedCategories([]);
    setSelectedLevels([]); setSelectedIntakes([]); setFeeRange([0, 100000]);
    setDurationRange([0, 6]); setEnglishExam("");
    setEOverall(""); setEReading(""); setEWriting(""); setEListening(""); setESpeaking("");
    setCountry(""); setQualification(""); setScheme(""); setOutOf(""); setGradingScore("");
    setOtherExam(""); setSort("relevance"); setPage(1);
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;

  // Universities options for the dropdown — prefer faceted (filtered) when present, else full.
  const uniOptions = useMemo(() => {
    const facets = data?.facets.universities ?? [];
    if (facets.length > 0) {
      return facets.map((f) => ({ value: String(f.id), label: f.name, count: f.count }));
    }
    return (options?.universities ?? []).map((u) => ({ value: String(u.id), label: u.name }));
  }, [data, options]);

  const intakeOptions = useMemo(() => {
    return (data?.facets.intakes ?? []).map((f) => ({ value: f.name, label: f.name, count: f.count }));
  }, [data]);

  const currentSchemeOuts = useMemo(() => {
    if (!scheme || !options) return [] as string[];
    return options.grading_schemes.find((s) => s.scheme === scheme)?.out_of ?? [];
  }, [scheme, options]);

  return (
    <div className="space-y-4">
      {/* ── Hero search bar ────────────────────────────────── */}
      <div className="bg-gradient-to-r from-indigo-600 via-blue-600 to-cyan-500 rounded-2xl p-6 shadow-lg">
        <div className="text-white mb-4">
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <GraduationCap className="w-6 h-6" /> Find Your Perfect Course
          </h1>
          <p className="text-blue-100 text-sm mt-1">
            Search across {options?.universities.length ?? "all"} universities and thousands of programs.
          </p>
        </div>
        <div className="flex flex-col md:flex-row gap-3">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <Input
              value={q}
              onChange={(e) => { setQ(e.target.value); resetPage(); }}
              placeholder="Search course name or keywords..."
              className="pl-9 h-12 bg-white border-0 shadow-sm text-base"
            />
          </div>
          <div className="md:w-72 relative">
            <MapPin className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
            <Input
              value={location}
              onChange={(e) => { setLocFilter(e.target.value); resetPage(); }}
              placeholder="City or country (e.g. Sydney)"
              className="pl-9 h-12 bg-white border-0 shadow-sm text-base"
            />
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr] gap-4">
        {/* ── Filters ──────────────────────────────────────── */}
        <aside className="bg-white rounded-xl border p-4 space-y-4 self-start lg:sticky lg:top-4">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-sm flex items-center gap-2">
              <Filter className="w-4 h-4 text-indigo-600" /> Filters
            </h2>
            <Button variant="ghost" size="sm" onClick={clearAllFilters}>Reset</Button>
          </div>

          <Accordion type="multiple" defaultValue={["uni", "intakes", "fee", "duration", "level", "advanced", "english"]} className="w-full">
            {/* University */}
            <AccordionItem value="uni">
              <AccordionTrigger className="text-sm">University</AccordionTrigger>
              <AccordionContent className="pt-1">
                <MultiSelect
                  options={uniOptions}
                  value={selectedUnis}
                  onChange={(v) => { setSelectedUnis(v); resetPage(); }}
                  placeholder="Any university"
                  searchPlaceholder="Search universities..."
                />
              </AccordionContent>
            </AccordionItem>

            {/* Intakes */}
            <AccordionItem value="intakes">
              <AccordionTrigger className="text-sm">Intakes</AccordionTrigger>
              <AccordionContent className="pt-1">
                <MultiSelect
                  options={intakeOptions}
                  value={selectedIntakes}
                  onChange={(v) => { setSelectedIntakes(v); resetPage(); }}
                  placeholder="Any intake"
                  searchPlaceholder="Search intakes..."
                  maxBadgeCount={3}
                />
              </AccordionContent>
            </AccordionItem>

            {/* Degree level */}
            <AccordionItem value="level">
              <AccordionTrigger className="text-sm">Degree Level</AccordionTrigger>
              <AccordionContent className="pt-1">
                <MultiSelect
                  options={(data?.facets.degree_levels ?? []).map((f) => ({ value: f.name, label: f.name, count: f.count }))}
                  value={selectedLevels}
                  onChange={(v) => { setSelectedLevels(v); resetPage(); }}
                  placeholder="Any level"
                />
              </AccordionContent>
            </AccordionItem>

            {/* Category */}
            <AccordionItem value="cat">
              <AccordionTrigger className="text-sm">Category</AccordionTrigger>
              <AccordionContent className="pt-1">
                <MultiSelect
                  options={(data?.facets.categories ?? []).map((f) => ({ value: f.name, label: f.name, count: f.count }))}
                  value={selectedCategories}
                  onChange={(v) => { setSelectedCategories(v); resetPage(); }}
                  placeholder="Any category"
                  searchPlaceholder="Search categories..."
                />
              </AccordionContent>
            </AccordionItem>

            {/* Duration */}
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

            {/* Fee */}
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

            {/* ── Advanced Filter ── */}
            <AccordionItem value="advanced">
              <AccordionTrigger className="text-sm font-semibold text-indigo-700">
                Advanced Filter
              </AccordionTrigger>
              <AccordionContent className="space-y-3 pt-2">
                <div>
                  <Label className="text-xs text-gray-600 mb-1 block">
                    Country of Residence <span className="text-red-500">*</span>
                  </Label>
                  <Select value={country || "any"} onValueChange={(v) => { setCountry(v === "any" ? "" : v); resetPage(); }}>
                    <SelectTrigger className="h-9"><SelectValue placeholder="Any" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="any">— Any —</SelectItem>
                      {(options?.countries ?? []).map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label className="text-xs text-gray-600 mb-1 block">
                    Highest Qualification Studied <span className="text-red-500">*</span>
                  </Label>
                  <Select value={qualification || "any"} onValueChange={(v) => { setQualification(v === "any" ? "" : v); resetPage(); }}>
                    <SelectTrigger className="h-9"><SelectValue placeholder="Any" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="any">— Any —</SelectItem>
                      {(options?.qualifications ?? []).map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label className="text-xs text-gray-600 mb-1 block">
                    Grading Scheme (12th)
                  </Label>
                  <Select value={scheme || "any"} onValueChange={(v) => { setScheme(v === "any" ? "" : v); setOutOf(""); resetPage(); }}>
                    <SelectTrigger className="h-9"><SelectValue placeholder="Any" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="any">— Any —</SelectItem>
                      {(options?.grading_schemes ?? []).map((s) => <SelectItem key={s.scheme} value={s.scheme}>{s.scheme}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>

                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <Label className="text-xs text-gray-600 mb-1 block">Out of</Label>
                    <Select value={outOf || "any"} onValueChange={(v) => { setOutOf(v === "any" ? "" : v); resetPage(); }} disabled={!scheme}>
                      <SelectTrigger className="h-9"><SelectValue placeholder="—" /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="any">— Any —</SelectItem>
                        {currentSchemeOuts.map((o) => <SelectItem key={o} value={o}>Out of {o}</SelectItem>)}
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label className="text-xs text-gray-600 mb-1 block">Grading Score</Label>
                    <Input
                      type="number" step="0.01" min="0"
                      value={gradingScore}
                      onChange={(e) => { setGradingScore(e.target.value); resetPage(); }}
                      placeholder="e.g. 3.5"
                      className="h-9"
                    />
                  </div>
                </div>
              </AccordionContent>
            </AccordionItem>

            {/* ── English Proficiency Exam ── */}
            <AccordionItem value="english">
              <AccordionTrigger className="text-sm font-semibold text-indigo-700">
                English Proficiency Exam
              </AccordionTrigger>
              <AccordionContent className="space-y-2 pt-2">
                <Select value={englishExam || "any"} onValueChange={(v) => { setEnglishExam(v === "any" ? "" : v); resetPage(); }}>
                  <SelectTrigger className="h-9"><SelectValue placeholder="Choose exam" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="any">— Any —</SelectItem>
                    {(options?.english_exams ?? ["IELTS", "PTE", "TOEFL", "CAE", "DET"]).map((e) =>
                      <SelectItem key={e} value={e}>{e}</SelectItem>
                    )}
                  </SelectContent>
                </Select>
                {englishExam && (
                  <>
                    <div className="grid grid-cols-2 gap-2 pt-1">
                      <div>
                        <Label className="text-xs text-gray-600">Overall</Label>
                        <Input type="number" step="0.5" placeholder="Score"
                          value={eOverall} onChange={(e) => { setEOverall(e.target.value); resetPage(); }} className="h-9" />
                      </div>
                      <div>
                        <Label className="text-xs text-gray-600">Reading</Label>
                        <Input type="number" step="0.5" placeholder="Score"
                          value={eReading} onChange={(e) => { setEReading(e.target.value); resetPage(); }} className="h-9" />
                      </div>
                      <div>
                        <Label className="text-xs text-gray-600">Writing</Label>
                        <Input type="number" step="0.5" placeholder="Score"
                          value={eWriting} onChange={(e) => { setEWriting(e.target.value); resetPage(); }} className="h-9" />
                      </div>
                      <div>
                        <Label className="text-xs text-gray-600">Listening</Label>
                        <Input type="number" step="0.5" placeholder="Score"
                          value={eListening} onChange={(e) => { setEListening(e.target.value); resetPage(); }} className="h-9" />
                      </div>
                      <div className="col-span-2">
                        <Label className="text-xs text-gray-600">Speaking</Label>
                        <Input type="number" step="0.5" placeholder="Score"
                          value={eSpeaking} onChange={(e) => { setESpeaking(e.target.value); resetPage(); }} className="h-9" />
                      </div>
                    </div>
                  </>
                )}
              </AccordionContent>
            </AccordionItem>

            {/* ── Other Exam ── */}
            <AccordionItem value="other">
              <AccordionTrigger className="text-sm">Other Exam (GMAT/GRE)</AccordionTrigger>
              <AccordionContent className="pt-2">
                <Input
                  placeholder="e.g. GMAT, GRE"
                  value={otherExam}
                  onChange={(e) => { setOtherExam(e.target.value); resetPage(); }}
                  className="h-9"
                />
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </aside>

        {/* ── Results ─────────────────────────────────────── */}
        <main className="space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2 bg-white rounded-xl border px-4 py-3">
            <div className="text-sm text-gray-700">
              {loading ? (
                <span className="flex items-center gap-2"><Loader2 className="w-4 h-4 animate-spin" /> Searching…</span>
              ) : data ? (
                <span><strong className="text-indigo-700">{data.total.toLocaleString()}</strong> course{data.total === 1 ? "" : "s"} found
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
              const fee = formatFee(r.international_fee, r.currency, r.fee_term);
              const dur = formatDuration(r.duration, r.duration_term);
              const cityCountry = r.course_location || [r.university.city, r.university.country].filter(Boolean).join(", ");
              const meta: Array<{ icon: React.ReactNode; text: string }> = [];
              if (cityCountry) meta.push({ icon: <MapPin className="w-3.5 h-3.5" />, text: cityCountry });
              if (fee) meta.push({ icon: <DollarSign className="w-3.5 h-3.5" />, text: fee });
              if (dur) meta.push({ icon: <Clock className="w-3.5 h-3.5" />, text: dur });
              if (r.intakes.length > 0) meta.push({ icon: <Calendar className="w-3.5 h-3.5" />, text: r.intakes.join(", ") });

              return (
                <div key={r.id} className="bg-white rounded-xl border border-gray-200 p-4 hover:shadow-lg hover:border-indigo-300 transition-all">
                  <div className="flex gap-4">
                    {r.university.logo_url ? (
                      <img src={r.university.logo_url} alt={r.university.name} className="w-16 h-16 object-contain rounded-lg border bg-white p-1 flex-shrink-0" />
                    ) : (
                      <div className="w-16 h-16 rounded-lg border bg-gradient-to-br from-indigo-50 to-blue-50 flex items-center justify-center text-indigo-600 text-xs font-bold flex-shrink-0">
                        {r.university.name.split(" ").slice(0, 2).map((s) => s[0]).join("")}
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <Link href={`/courses/${r.id}`}>
                        <h3 className="font-semibold text-base text-gray-900 leading-snug hover:text-indigo-700 cursor-pointer">{r.course_name}</h3>
                      </Link>
                      <Link href={`/universities/${r.university.id}`}>
                        <p className="text-sm text-gray-600 mt-0.5 hover:text-indigo-700 cursor-pointer inline-flex items-center gap-1">
                          <BookOpen className="w-3.5 h-3.5" /> {r.university.name}
                        </p>
                      </Link>
                      {meta.length > 0 && (
                        <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-gray-600">
                          {meta.map((m, i) => (
                            <span key={i} className="flex items-center gap-1">{m.icon} {m.text}</span>
                          ))}
                        </div>
                      )}
                      <div className="flex flex-wrap gap-1.5 mt-2.5">
                        {r.degree_level && <Badge variant="secondary" className="text-[10px] bg-indigo-50 text-indigo-700 hover:bg-indigo-100">{r.degree_level}</Badge>}
                        {r.category && <Badge variant="outline" className="text-[10px]">{r.category}</Badge>}
                        {r.english_requirements.ielts_overall && (
                          <Badge variant="outline" className="text-[10px] border-emerald-200 text-emerald-700">
                            <Award className="w-3 h-3 mr-0.5" />IELTS {r.english_requirements.ielts_overall}
                          </Badge>
                        )}
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
                      <Link href={`/courses/${r.id}`}>
                        <Button size="sm" className="w-full bg-indigo-600 hover:bg-indigo-700">
                          View Details
                        </Button>
                      </Link>
                      {r.course_url && (
                        <a href={r.course_url} target="_blank" rel="noreferrer" className="text-[11px] text-gray-500 hover:text-indigo-600 inline-flex items-center justify-center gap-1">
                          <Globe2 className="w-3 h-3" /> Website
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

          {data && data.total > data.limit && (
            <div className="flex items-center justify-center gap-2 pt-4">
              <Button variant="outline" size="sm" disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>← Prev</Button>
              <span className="text-sm text-gray-600 px-3">Page {page} of {totalPages}</span>
              <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next →</Button>
            </div>
          )}
        </main>
      </div>

      {tray.length > 0 && (
        <div className="fixed bottom-0 left-0 right-0 bg-white border-t shadow-2xl z-20 px-4 py-3">
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
            <Button size="sm" onClick={goCompare} disabled={tray.length < 2} className="bg-indigo-600 hover:bg-indigo-700">
              Compare {tray.length} Course{tray.length === 1 ? "" : "s"} →
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

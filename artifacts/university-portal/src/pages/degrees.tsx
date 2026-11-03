import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import { Search, LayoutGrid, List, Bookmark, SlidersHorizontal, X, ChevronLeft, ChevronRight, GraduationCap, Building2 } from "lucide-react";
import "./degrees/degrees.css";
import {
  BASE, EMPTY_FILTERS, SORTS, countActiveFilters, buildSearchParams, filtersFromQuery, filtersToQuery, isFeeSort,
  type CourseResult, type DegreeFilters, type OptionsResponse, type SearchResponse, type SortKey,
} from "./degrees/lib";
import { useShortlist } from "./degrees/use-shortlist";
import { usePageMeta } from "./degrees/use-page-meta";
import { FilterPanel } from "./degrees/filter-panel";
import { CourseCard, CourseCardSkeleton } from "./degrees/course-card";
import { DetailPanel } from "./degrees/detail-panel";
import { ShortlistDrawer } from "./degrees/shortlist-drawer";
import { CompareView } from "./degrees/compare-view";
import { UniversityInsights } from "./degrees/university-insights";
import { UniversityDirectory } from "./degrees/university-directory";

type Mode = "courses" | "universities";
function readUniState(search: string) {
  const p = new URLSearchParams(search);
  const mode: Mode = p.get("mode") === "universities" ? "universities" : "courses";
  const requestedPage = Number(p.get("upage") ?? "1");
  return { mode, uq: p.get("uq") ?? "", ucountry: (p.get("ucountry") ?? "").trim(), ucity: (p.get("ucity") ?? "").trim(),
    page: Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1 };
}

const countActive = countActiveFilters;

export default function DegreesPage() {
  usePageMeta(
    "Browse courses | Study Info Centre",
    "Search approved university courses by country, university, degree level, intake and budget. Compare published fees, durations and English requirements.",
  );
  const [filters, setFilters] = useState<DegreeFilters>(() => filtersFromQuery(window.location.search));
  const [qDraft, setQDraft] = useState(filters.q);
  const [locationDraft, setLocationDraft] = useState(filters.location);
  const [initialUni] = useState(() => readUniState(window.location.search));
  const [mode, setMode] = useState<Mode>(initialUni.mode);
  const [uniQ, setUniQValue] = useState(initialUni.uq);
  const [uniCountry, setUniCountryValue] = useState(initialUni.ucountry);
  const [uniCity, setUniCityValue] = useState(initialUni.ucity);
  const [uniPage, setUniPage] = useState(initialUni.page);
  const setUniQ = useCallback((v: string) => { setUniQValue(v); setUniPage(1); }, []);
  const setUniCountry = useCallback((v: string) => { setUniCountryValue(v); setUniPage(1); }, []);
  const setUniCity = useCallback((v: string) => { setUniCityValue(v); setUniPage(1); }, []);
  const [view, setView] = useState<"grid" | "list">("grid");
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [options, setOptions] = useState<OptionsResponse | null>(null);
  const [optionsError, setOptionsError] = useState(false);
  const [selected, setSelected] = useState<CourseResult | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [shortlistOpen, setShortlistOpen] = useState(false);
  const [comparing, setComparing] = useState<number[] | null>(null);
  const shortlist = useShortlist();
  const resultsTop = useRef<HTMLDivElement>(null);

  const patch = useCallback((p: Partial<DegreeFilters>) => {
    setFilters((f) => ({ ...f, ...p, page: "page" in p ? (p.page as number) : 1 }));
  }, []);

  // Debounce keyword typing into filters.
  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => f.q === qDraft && f.location === locationDraft ? f : { ...f, q: qDraft, location: locationDraft, page: 1 }), 350);
    return () => clearTimeout(t);
  }, [qDraft, locationDraft]);

  // Reflect state in URL for reloads and sharing.
  useEffect(() => {
    let qs = filtersToQuery(filters);
    if (mode === "universities") {
      const p = new URLSearchParams({ mode: "universities" });
      if (uniQ.trim()) p.set("uq", uniQ.trim());
      if (uniCountry) p.set("ucountry", uniCountry);
      if (uniCity) p.set("ucity", uniCity);
      if (uniPage > 1) p.set("upage", String(uniPage));
      qs = p.toString();
    }
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    if (next !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(window.history.state, "", next);
  }, [filters, mode, uniQ, uniCountry, uniCity, uniPage]);

  const loadOptions = useCallback(async () => {
    setOptionsError(false);
    try {
      const res = await fetch(`${BASE}/api/search/options`);
      if (!res.ok) throw new Error();
      setOptions(await res.json());
    } catch { setOptionsError(true); }
  }, []);
  useEffect(() => { void loadOptions(); }, [loadOptions]);

  const requestQs = useMemo(() => buildSearchParams(filters).toString(), [filters]);
  useEffect(() => {
    if (mode !== "courses") return;
    const ac = new AbortController();
    setLoading(true); setError(null);
    fetch(`${BASE}/api/search/courses?${requestQs}`, { signal: ac.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.status === 422 ? "Some filters were not accepted. Try clearing them." : "Courses could not be loaded.");
        return res.json() as Promise<SearchResponse>;
      })
      .then((json) => { setData(json); setLoading(false); })
      .catch((e: Error) => { if (e.name !== "AbortError") { setError(e.message || "Courses could not be loaded."); setLoading(false); } });
    return () => ac.abort();
  }, [requestQs, reloadKey, mode]);


  const results = data?.results ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;
  const activeCount = countActive(filters);

  useEffect(() => { if (!loading) resultsTop.current?.scrollIntoView({ block: "start", behavior: "smooth" }); }, [filters.page]); // eslint-disable-line react-hooks/exhaustive-deps

  const clearAll = () => { setQDraft(""); setLocationDraft(""); setFilters(EMPTY_FILTERS); };
  const searchFilters = () => {
    patch({ q: qDraft, location: locationDraft });
    setReloadKey((key) => key + 1);
    setFiltersOpen(false);
    resultsTop.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const openCourseById = (id: number) => {
    const found = data?.results.find((c) => c.id === id);
    if (found) { setSelected(found); setShortlistOpen(false); }
    else { setShortlistOpen(false); setComparing([id]); }
  };

  return (
    <div className="sic-degrees min-h-[100dvh] max-w-full overflow-x-hidden">
      <header className="sic-hero sic-contour text-white">
        <div className="mx-auto max-w-[1400px] px-4 sm:px-6 pt-5 pb-10">
          <nav className="flex items-center justify-between" aria-label="Site">
            <Link href="/toolkit" className="flex items-center gap-2.5" data-testid="link-home">
              <span className="grid h-9 w-9 place-items-center rounded-lg bg-primary text-primary-foreground sic-display font-semibold">SI</span>
              <span className="leading-tight">
                <span className="block font-semibold">Study Info Centre</span>
                <span className="flex items-center gap-2">
                  <span className="text-[11px] uppercase tracking-[0.16em] text-white/80">Course toolkit</span>
                  <span data-testid="badge-toolkit-beta" className="sic-beta shrink-0 rounded-full bg-red-600 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-white">Beta</span>
                </span>
              </span>
            </Link>
            <div className="flex items-center gap-2">
              <button type="button" onClick={() => setShortlistOpen(true)} data-testid="button-open-shortlist"
                className="inline-flex h-10 items-center gap-2 rounded-full bg-white/10 px-4 text-sm font-medium hover:bg-white/20">
                <Bookmark className="h-4 w-4" aria-hidden /> Shortlist <span className="rounded-full bg-white px-2 text-xs text-[hsl(var(--sic-navy))]" data-testid="text-shortlist-count">{shortlist.items.length}</span>
              </button>
              <Link href="/login" className="hidden sm:inline-flex h-10 items-center rounded-full px-4 text-sm text-white/80 hover:text-white" data-testid="link-staff-login">Staff sign in</Link>
            </div>
          </nav>
          <div className="mt-10 max-w-3xl">
            <h1 className="sic-display text-4xl sm:text-5xl leading-[1.05]">Find your course.<br /><span className="text-red-400">Explore your options.</span></h1>
          </div>
<div className="mt-7 max-w-3xl">
            <div role="tablist" aria-label="Search mode" className="mb-3 inline-flex rounded-full bg-white/10 p-1 ring-1 ring-white/15">
              {([["courses", "Courses", GraduationCap], ["universities", "Universities", Building2]] as const).map(([m, label, Icon]) => (
                <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => setMode(m)} data-testid={`tab-${m}`}
                  className={`inline-flex h-10 items-center gap-2 rounded-full px-5 text-sm font-semibold transition-colors ${mode === m ? "bg-white text-[hsl(var(--sic-navy))] shadow" : "text-white/80 hover:text-white"}`}>
                  <Icon className="h-4 w-4" aria-hidden /> {label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </header>

      {mode === "universities" ? (
      <UniversityDirectory options={options} optionsError={optionsError} onRetry={() => void loadOptions()}
        page={uniPage} onPage={setUniPage}
        q={uniQ} country={uniCountry} city={uniCity} onQ={setUniQ} onCountry={setUniCountry} onCity={setUniCity} />
      ) : (
      <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-8 grid gap-6 lg:grid-cols-[260px_minmax(0,1fr)] xl:grid-cols-[260px_minmax(0,1fr)_380px]">
        <aside className="hidden lg:block self-start lg:sticky lg:top-6" aria-label="Filters">
          <FilterPanel filters={filters} options={options} optionsError={optionsError} onRetryOptions={loadOptions} onChange={patch} onClear={clearAll} activeCount={activeCount} onSearch={searchFilters} />
        </aside>

        <section aria-labelledby="results-heading" className="min-w-0" ref={resultsTop}>
          {filters.universityId && (
            <UniversityInsights compact key={filters.universityId} universityId={Number(filters.universityId)}
              universityName={options?.universities.find((u) => String(u.id) === filters.universityId)?.name ?? "Selected university"} />
          )}
          <div className="grid gap-3 mb-4 min-w-0 sm:grid-cols-[minmax(0,1fr)_auto] items-start" data-testid="course-results-header">
            <div className="min-w-0">
              <h2 id="results-heading" className="sic-display text-2xl">Courses</h2>
              <p className="text-sm text-[hsl(var(--sic-muted))]" aria-live="polite" data-testid="text-result-count">
                {loading && !data ? "Loading courses" : data ? `${data.total.toLocaleString()} matching course${data.total === 1 ? "" : "s"}` : ""}
              </p>
            </div>
            <div className="flex min-w-0 flex-col gap-1 sm:items-end" data-testid="course-results-toolbar">
              <div className="flex flex-wrap sm:flex-nowrap items-center justify-end gap-2 min-w-0 max-w-full">
              <button type="button" onClick={() => setFiltersOpen(true)} className="lg:hidden inline-flex h-10 items-center gap-2 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm" data-testid="button-open-filters">
                <SlidersHorizontal className="h-4 w-4" aria-hidden /> Filters{activeCount ? ` (${activeCount})` : ""}
              </button>
              <label htmlFor="sort" className="sr-only">Sort by</label>
              <select id="sort" value={filters.sort} onChange={(e) => patch({ sort: e.target.value as SortKey })} data-testid="select-sort"
                aria-describedby={!filters.budgetCurrency ? "fee-sort-help" : undefined}
                className="h-9 w-44 min-w-0 max-w-full rounded-lg border border-[hsl(var(--sic-line))] bg-white px-2 text-xs">
                {SORTS.map((s) => <option key={s.value} value={s.value} disabled={isFeeSort(s.value) && !filters.budgetCurrency}>{s.label}</option>)}
              </select>
              <label htmlFor="sort-currency" className="sr-only">Sorting currency</label>
              <select id="sort-currency" value={filters.budgetCurrency} data-testid="select-sort-currency"
                disabled={!options?.currencies?.length}
                onChange={(e) => patch({ budgetCurrency: e.target.value, ...(!e.target.value && isFeeSort(filters.sort) ? { sort: "relevance" as SortKey } : {}) })}
                className="h-9 w-24 min-w-0 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-2 text-xs">
                <option value="">Currency</option>
                {options?.currencies?.map((currency) => <option key={currency} value={currency}>{currency}</option>)}
              </select>
              <div className="flex rounded-lg border border-[hsl(var(--sic-line))] bg-white p-0.5" role="group" aria-label="Layout">
                {(["grid", "list"] as const).map((v) => (
                  <button key={v} type="button" aria-pressed={view === v} aria-label={`${v} view`} onClick={() => setView(v)} data-testid={`button-view-${v}`}
                    className={`grid h-9 w-9 place-items-center rounded-md ${view === v ? "bg-[hsl(var(--sic-navy))] text-white" : "text-[hsl(var(--sic-muted))]"}`}>
                    {v === "grid" ? <LayoutGrid className="h-4 w-4" aria-hidden /> : <List className="h-4 w-4" aria-hidden />}
                  </button>
                ))}
              </div>
              </div>
              {!filters.budgetCurrency && <p id="fee-sort-help" className="text-right text-xs text-[hsl(var(--sic-muted))]">Choose a currency to sort by fee.</p>}
            </div>
          </div>

          <form role="search" aria-label="Filter course results"
            onSubmit={(event) => { event.preventDefault(); patch({ q: qDraft, location: locationDraft }); }}
            className="mb-4 grid gap-3 rounded-xl border border-[hsl(var(--sic-line))] bg-white p-3 sm:grid-cols-2">
            <label className="text-xs font-semibold text-[hsl(var(--sic-muted))]">
              Course name
              <input type="search" value={qDraft} onChange={(event) => setQDraft(event.target.value)}
                placeholder="Search course name" data-testid="input-course-name"
                className="mt-1.5 h-10 w-full rounded-lg border border-[hsl(var(--sic-line))] px-3 text-sm font-normal text-[hsl(var(--sic-ink))]" />
            </label>
            <label className="text-xs font-semibold text-[hsl(var(--sic-muted))]">
              Location
              <input type="search" value={locationDraft} onChange={(event) => setLocationDraft(event.target.value)}
                placeholder="City, campus or region" data-testid="input-course-location"
                className="mt-1.5 h-10 w-full rounded-lg border border-[hsl(var(--sic-line))] px-3 text-sm font-normal text-[hsl(var(--sic-ink))]" />
            </label>
            <button type="submit" className="sr-only">Apply course name and location</button>
          </form>

          {error ? (
            <div role="alert" className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-8 text-center">
              <p className="sic-display text-xl">Something interrupted the search</p>
              <p className="text-sm text-[hsl(var(--sic-muted))] mt-1">{error}</p>
              <button type="button" onClick={() => setReloadKey((k) => k + 1)} className="mt-4 h-10 rounded-lg bg-[hsl(var(--sic-navy))] px-5 text-sm font-semibold text-white" data-testid="button-retry-search">Try again</button>
            </div>
          ) : loading && !data ? (
            <div className={view === "grid" ? "grid gap-4 sm:grid-cols-2" : "space-y-3"} aria-busy="true">
              {Array.from({ length: 6 }).map((_, i) => <CourseCardSkeleton key={i} view={view} />)}
            </div>
          ) : results.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-10 text-center" data-testid="state-empty">
              <p className="sic-display text-2xl">No courses match yet</p>
              <p className="text-sm text-[hsl(var(--sic-muted))] mt-2 max-w-sm mx-auto">Try a broader keyword, a different intake, or remove the currency or budget.</p>
              {activeCount > 0 && <button type="button" onClick={clearAll} className="mt-4 h-10 rounded-lg border border-[hsl(var(--sic-navy))] px-5 text-sm font-semibold" data-testid="button-empty-clear">Clear filters</button>}
            </div>
          ) : (
            <div className={`${view === "grid" ? "grid gap-4 sm:grid-cols-2" : "space-y-3"} transition-opacity ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
              {results.map((c, i) => (
                <CourseCard key={c.id} index={i} course={c} view={view} selected={selected?.id === c.id}
                  shortlisted={shortlist.has(c.id)} onSelect={() => setSelected(c)} onToggleShortlist={() => shortlist.toggle(c)} />
              ))}
            </div>
          )}

          {data && data.total > data.limit && !error && (
            <nav className="mt-8 flex items-center justify-center gap-3" aria-label="Pagination">
              <button type="button" disabled={filters.page <= 1 || loading} onClick={() => patch({ page: filters.page - 1 })} data-testid="button-prev-page"
                className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm disabled:opacity-40"><ChevronLeft className="h-4 w-4" aria-hidden /> Previous</button>
              <span className="text-sm tabular-nums" data-testid="text-page">Page {filters.page} of {totalPages}</span>
              <button type="button" disabled={filters.page >= totalPages || loading} onClick={() => patch({ page: filters.page + 1 })} data-testid="button-next-page"
                className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm disabled:opacity-40">Next <ChevronRight className="h-4 w-4" aria-hidden /></button>
            </nav>
          )}
        </section>

        <aside className="hidden xl:block self-start sticky top-6" aria-label="Selected course">
          <DetailPanel course={selected} shortlisted={!!selected && shortlist.has(selected.id)} onToggleShortlist={() => selected && shortlist.toggle(selected)} onClose={() => setSelected(null)} />
        </aside>
      </main>
      )}

      {selected && mode === "courses" && (
        <div className="xl:hidden fixed inset-0 z-40 flex items-end sm:items-center justify-center bg-[hsl(213_60%_12%/0.45)] p-0 sm:p-6" role="dialog" aria-modal="true" aria-label="Course details" aria-description="Published fees, duration, intakes and English requirements for the selected course"
          onKeyDown={(e) => e.key === "Escape" && setSelected(null)}>
          <div className="w-full max-w-lg max-h-[90dvh] overflow-y-auto">
            <DetailPanel course={selected} shortlisted={shortlist.has(selected.id)} onToggleShortlist={() => shortlist.toggle(selected)} onClose={() => setSelected(null)} />
          </div>
        </div>
      )}

      {filtersOpen && mode === "courses" && (
        <div className="lg:hidden fixed inset-0 z-50 flex" role="dialog" aria-modal="true" aria-label="Filters" aria-description="Narrow courses by country, university, level, intake and currency" onKeyDown={(e) => e.key === "Escape" && setFiltersOpen(false)}>
          <button type="button" aria-label="Close filters" className="absolute inset-0 bg-[hsl(213_60%_12%/0.45)]" onClick={() => setFiltersOpen(false)} />
          <div className="sic-rise relative w-[88%] max-w-sm h-full overflow-y-auto bg-[hsl(var(--sic-paper))] p-5">
            <FilterPanel filters={filters} options={options} optionsError={optionsError} onRetryOptions={loadOptions} onChange={patch} onClear={clearAll} activeCount={activeCount} onSearch={searchFilters} />
          </div>
        </div>
      )}

      <ShortlistDrawer open={shortlistOpen} items={shortlist.items} compareIds={shortlist.compareIds}
        onClose={() => setShortlistOpen(false)} onRemove={shortlist.remove} onClear={shortlist.clear}
        onToggleCompare={shortlist.toggleCompare} onOpenCourse={openCourseById}
        onCompare={() => { setComparing(shortlist.compareIds); setShortlistOpen(false); }} />
      {comparing && <CompareView ids={comparing} onClose={() => setComparing(null)} />}

      <footer className="border-t border-[hsl(var(--sic-line))] py-8 text-center text-xs text-[hsl(var(--sic-muted))]">
        Study Info Centre. Course details are reproduced from institution publications; confirm fees and entry rules with the university.
      </footer>
    </div>
  );
}

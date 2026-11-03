import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import { Search, LayoutGrid, List, Bookmark, SlidersHorizontal, X, ChevronLeft, ChevronRight } from "lucide-react";
import "./degrees/degrees.css";
import {
  BASE, EMPTY_FILTERS, SORTS, buildSearchParams, filtersFromQuery, filtersToQuery, isFeeSort,
  type CourseResult, type DegreeFilters, type OptionsResponse, type SearchResponse, type SortKey,
} from "./degrees/lib";
import { useShortlist } from "./degrees/use-shortlist";
import { usePageMeta } from "./degrees/use-page-meta";
import { FilterPanel } from "./degrees/filter-panel";
import { CourseCard, CourseCardSkeleton } from "./degrees/course-card";
import { DetailPanel } from "./degrees/detail-panel";
import { ShortlistDrawer } from "./degrees/shortlist-drawer";
import { CompareView } from "./degrees/compare-view";

function countActive(f: DegreeFilters) {
  return [f.q.trim(), f.country, f.universityId, f.degreeLevel, f.budgetCurrency || f.budgetMax].filter(Boolean).length + (f.intakes.length ? 1 : 0);
}

export default function DegreesPage() {
  usePageMeta(
    "Browse courses | Study Info Centre",
    "Search approved university courses by country, university, degree level, intake and budget. Compare published fees, durations and English requirements.",
  );
  const [filters, setFilters] = useState<DegreeFilters>(() => filtersFromQuery(window.location.search));
  const [qDraft, setQDraft] = useState(filters.q);
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
    const t = setTimeout(() => setFilters((f) => f.q === qDraft ? f : { ...f, q: qDraft, page: 1 }), 350);
    return () => clearTimeout(t);
  }, [qDraft]);

  // Reflect state in URL for reloads and sharing.
  useEffect(() => {
    const qs = filtersToQuery(filters);
    const next = `${window.location.pathname}${qs ? `?${qs}` : ""}`;
    if (next !== `${window.location.pathname}${window.location.search}`) window.history.replaceState(window.history.state, "", next);
  }, [filters]);

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
  }, [requestQs, reloadKey]);

  const results = data?.results ?? [];
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;
  const activeCount = countActive(filters);

  useEffect(() => { if (!loading) resultsTop.current?.scrollIntoView({ block: "start", behavior: "smooth" }); }, [filters.page]); // eslint-disable-line react-hooks/exhaustive-deps

  const clearAll = () => { setQDraft(""); setFilters(EMPTY_FILTERS); };
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
            <Link href="/degrees" className="flex items-center gap-2.5" data-testid="link-home">
              <span className="grid h-9 w-9 place-items-center rounded-lg bg-primary text-primary-foreground sic-display font-semibold">SI</span>
              <span className="leading-tight"><span className="block font-semibold">Study Info Centre</span><span className="block text-[11px] uppercase tracking-[0.16em] text-white/80">Course toolkit</span></span>
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
            <h1 className="sic-display text-4xl sm:text-5xl leading-[1.05]">Find the course,<br /><span className="text-red-400">then read the fine print.</span></h1>
            <p className="mt-3 text-white/75 max-w-xl">Every listing comes from published university data. Where a detail has not been published, we say so.</p>
          </div>
          <form role="search" className="mt-7 flex max-w-3xl items-center gap-2 rounded-2xl bg-white p-2 shadow-xl" onSubmit={(e) => { e.preventDefault(); setFilters((f) => ({ ...f, q: qDraft, page: 1 })); }}>
            <Search className="ml-2 h-5 w-5 text-[hsl(var(--sic-muted))]" aria-hidden />
            <label htmlFor="degree-q" className="sr-only">Search courses</label>
            <input id="degree-q" value={qDraft} onChange={(e) => setQDraft(e.target.value)} placeholder="Course name, subject or university"
              className="h-11 min-w-0 flex-1 bg-transparent text-[hsl(var(--sic-ink))] outline-none placeholder:text-[hsl(var(--sic-muted))]" data-testid="input-search" />
            {qDraft && <button type="button" aria-label="Clear search" onClick={() => setQDraft("")} className="p-2 text-[hsl(var(--sic-muted))]" data-testid="button-clear-search"><X className="h-4 w-4" aria-hidden /></button>}
            <button type="submit" className="h-11 rounded-xl bg-[hsl(var(--sic-teal))] px-5 text-sm font-semibold text-white hover:bg-[hsl(var(--sic-navy))]" data-testid="button-search">Search</button>
          </form>
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-8 grid gap-6 lg:grid-cols-[260px_minmax(0,1fr)] xl:grid-cols-[260px_minmax(0,1fr)_380px]">
        <aside className="hidden lg:block self-start lg:sticky lg:top-6" aria-label="Filters">
          <FilterPanel filters={filters} options={options} optionsError={optionsError} onRetryOptions={loadOptions} onChange={patch} onClear={clearAll} activeCount={activeCount} />
        </aside>

        <section aria-labelledby="results-heading" className="min-w-0" ref={resultsTop}>
          <div className="flex flex-wrap items-center gap-3 justify-between mb-4 min-w-0">
            <div>
              <h2 id="results-heading" className="sic-display text-2xl">Courses</h2>
              <p className="text-sm text-[hsl(var(--sic-muted))]" aria-live="polite" data-testid="text-result-count">
                {loading && !data ? "Loading courses" : data ? `${data.total.toLocaleString()} matching course${data.total === 1 ? "" : "s"}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 min-w-0 max-w-full">
              <button type="button" onClick={() => setFiltersOpen(true)} className="lg:hidden inline-flex h-10 items-center gap-2 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm" data-testid="button-open-filters">
                <SlidersHorizontal className="h-4 w-4" aria-hidden /> Filters{activeCount ? ` (${activeCount})` : ""}
              </button>
              <label htmlFor="sort" className="sr-only">Sort by</label>
              <select id="sort" value={filters.sort} onChange={(e) => patch({ sort: e.target.value as SortKey })} data-testid="select-sort"
                className="h-10 min-w-0 max-w-full flex-1 sm:flex-none rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm">
                {SORTS.map((s) => { const off = isFeeSort(s.value) && !filters.budgetCurrency; return <option key={s.value} value={s.value} disabled={off}>{s.label}{off ? " (choose a currency)" : ""}</option>; })}
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
          </div>

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

      {selected && (
        <div className="xl:hidden fixed inset-0 z-40 flex items-end sm:items-center justify-center bg-[hsl(213_60%_12%/0.45)] p-0 sm:p-6" role="dialog" aria-modal="true" aria-label="Course details" aria-description="Published fees, duration, intakes and English requirements for the selected course"
          onKeyDown={(e) => e.key === "Escape" && setSelected(null)}>
          <div className="w-full max-w-lg max-h-[90dvh] overflow-y-auto">
            <DetailPanel course={selected} shortlisted={shortlist.has(selected.id)} onToggleShortlist={() => shortlist.toggle(selected)} onClose={() => setSelected(null)} />
          </div>
        </div>
      )}

      {filtersOpen && (
        <div className="lg:hidden fixed inset-0 z-50 flex" role="dialog" aria-modal="true" aria-label="Filters" aria-description="Narrow courses by country, university, level, intake and currency" onKeyDown={(e) => e.key === "Escape" && setFiltersOpen(false)}>
          <button type="button" aria-label="Close filters" className="absolute inset-0 bg-[hsl(213_60%_12%/0.45)]" onClick={() => setFiltersOpen(false)} />
          <div className="sic-rise relative w-[88%] max-w-sm h-full overflow-y-auto bg-[hsl(var(--sic-paper))] p-5">
            <FilterPanel filters={filters} options={options} optionsError={optionsError} onRetryOptions={loadOptions} onChange={patch} onClear={clearAll} activeCount={activeCount} />
            <button type="button" autoFocus onClick={() => setFiltersOpen(false)} className="mt-6 h-11 w-full rounded-lg bg-[hsl(var(--sic-navy))] text-sm font-semibold text-white" data-testid="button-apply-filters">Show courses</button>
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

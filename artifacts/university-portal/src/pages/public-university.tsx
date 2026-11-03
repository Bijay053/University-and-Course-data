import { useEffect, useState } from "react";
import { Link, useLocation, useParams, useSearch } from "wouter";
import { ArrowLeft, Bookmark, ChevronLeft, ChevronRight, GraduationCap, Lightbulb, Search, X } from "lucide-react";
import "./degrees/degrees.css";
import { BASE, PAGE_SIZE, type CourseResult, type OptionsResponse, type SearchResponse } from "./degrees/lib";
import { useShortlist } from "./degrees/use-shortlist";
import { usePageMeta } from "./degrees/use-page-meta";
import { CourseCard, CourseCardSkeleton } from "./degrees/course-card";
import { DetailPanel } from "./degrees/detail-panel";
import { ShortlistDrawer } from "./degrees/shortlist-drawer";
import { CompareView } from "./degrees/compare-view";
import { UniversityInsights } from "./degrees/university-insights";

type Tab = "courses" | "insights";

export default function PublicUniversityPage() {
  const params = useParams<{ id: string }>();
  const id = /^\d+$/.test(params.id ?? "") ? Number(params.id) : null;
  const search = useSearch();
  const [, navigate] = useLocation();
  const tab: Tab = new URLSearchParams(search).get("tab") === "insights" ? "insights" : "courses";
  const setTab = (t: Tab) => navigate(`/toolkit/universities/${id}${t === "insights" ? "?tab=insights" : ""}`);
  const [insightsAvailability, setInsightsAvailability] = useState<{ id: number; available: boolean } | null>(null);
  const hasNoInsights = insightsAvailability?.id === id && !insightsAvailability.available;
  useEffect(() => {
    if (id == null) return;
    const controller = new AbortController();
    fetch(`${BASE}/api/search/universities/${id}/insights`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return;
        const body = await response.json();
        if (!controller.signal.aborted && Array.isArray(body.insights)) {
          setInsightsAvailability({ id, available: body.insights.length > 0 });
        }
      })
      .catch(() => { /* A failed request is not proof that insights are absent. */ });
    return () => controller.abort();
  }, [id]);
  useEffect(() => {
    if (hasNoInsights && tab === "insights") navigate(`/toolkit/universities/${id}`, { replace: true });
  }, [hasNoInsights, tab, id, navigate]);

  const [options, setOptions] = useState<OptionsResponse | null>(null);
  const [optionsError, setOptionsError] = useState(false);
  const [optionsAttempt, setOptionsAttempt] = useState(0);
  useEffect(() => {
    const ac = new AbortController();
    setOptionsError(false);
    fetch(`${BASE}/api/search/options`, { signal: ac.signal })
      .then(async (r) => { if (!r.ok) throw new Error(); setOptions(await r.json()); })
      .catch((e: Error) => { if (e.name !== "AbortError") setOptionsError(true); });
    return () => ac.abort();
  }, [optionsAttempt]);
  const uni = id != null ? options?.universities.find((u) => u.id === id) ?? null : null;
  const notFound = id == null || (!!options && !uni);
  usePageMeta(`${uni?.name ?? "University"} | Study Info Centre`, `Approved courses and Key Insights for ${uni?.name ?? "this university"}.`);

  const [qDraft, setQDraft] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState<CourseResult | null>(null);
  const [shortlistOpen, setShortlistOpen] = useState(false);
  const [comparing, setComparing] = useState<number[] | null>(null);
  const shortlist = useShortlist();

  useEffect(() => { const t = setTimeout(() => { setQ((prev) => { if (prev !== qDraft) setPage(1); return qDraft; }); }, 350); return () => clearTimeout(t); }, [qDraft]);

  useEffect(() => {
    if (id == null || tab !== "courses") return;
    const ac = new AbortController();
    const p = new URLSearchParams({ university_id: String(id), limit: String(PAGE_SIZE) });
    if (q.trim()) p.set("q", q.trim());
    if (page > 1) p.set("page", String(page));
    setLoading(true); setError(false);
    fetch(`${BASE}/api/search/courses?${p}`, { signal: ac.signal })
      .then(async (r) => { if (!r.ok) throw new Error(); return r.json() as Promise<SearchResponse>; })
      .then((j) => { setData(j); setLoading(false); })
      .catch((e: Error) => { if (e.name !== "AbortError") { setError(true); setLoading(false); } });
    return () => ac.abort();
  }, [id, q, page, reload, tab]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1;
  const results = data?.results ?? [];
  const name = uni?.name ?? (notFound ? "University not found" : "");

  return (
    <div className="sic-degrees min-h-[100dvh] max-w-full overflow-x-hidden">
      <header className="sic-hero sic-contour text-white">
        <div className="mx-auto max-w-[1400px] px-4 sm:px-6 pt-5 pb-0">
          <nav className="flex items-center justify-between" aria-label="Site">
            <Link href="/toolkit?mode=universities" className="inline-flex items-center gap-2 text-sm text-white/80 hover:text-white" data-testid="link-back-universities">
              <ArrowLeft className="h-4 w-4" aria-hidden /> All universities
            </Link>
            <button type="button" onClick={() => setShortlistOpen(true)} data-testid="button-open-shortlist"
              className="inline-flex h-10 items-center gap-2 rounded-full bg-white/10 px-4 text-sm font-medium hover:bg-white/20">
              <Bookmark className="h-4 w-4" aria-hidden /> Shortlist <span className="rounded-full bg-white px-2 text-xs text-[hsl(var(--sic-navy))]">{shortlist.items.length}</span>
            </button>
          </nav>
          <div className="mt-8 pb-6">
            <p className="text-[11px] uppercase tracking-[0.16em] text-white/70">University</p>
            {name ? <h1 className="sic-display text-3xl sm:text-5xl leading-[1.05] break-words" data-testid="text-university-name">{name}</h1>
              : <div className="mt-2 h-12 w-80 max-w-full animate-pulse rounded-lg bg-white/15" aria-label="Loading university" />}
          </div>
          {!notFound && (
            <div role="tablist" aria-label="University sections" className="flex gap-1">
              {([["courses", "Courses", GraduationCap], ["insights", "Key Insights", Lightbulb]] as const).filter(([t]) => t !== "insights" || !hasNoInsights).map(([t, label, Icon]) => (
                <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)} data-testid={`tab-uni-${t}`}
                  className={`inline-flex h-11 items-center gap-2 rounded-t-xl px-5 text-sm font-semibold transition-colors ${tab === t ? "bg-[hsl(var(--sic-paper))] text-[hsl(var(--sic-navy))]" : "text-white/80 hover:bg-white/10"}`}>
                  <Icon className="h-4 w-4" aria-hidden /> {label}
                </button>
              ))}
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-8">
        {optionsError && !options ? (
          <div role="alert" className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-8 text-center">
            <p className="sic-display text-xl">This university could not be loaded</p>
            <button type="button" onClick={() => setOptionsAttempt((n) => n + 1)} className="mt-4 h-10 rounded-lg bg-[hsl(var(--sic-navy))] px-5 text-sm font-semibold text-white" data-testid="button-retry-university">Try again</button>
          </div>
        ) : notFound ? (
          <div className="rounded-2xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-10 text-center" data-testid="state-university-not-found">
            <p className="sic-display text-2xl">We could not find that university</p>
            <p className="mt-2 text-sm text-[hsl(var(--sic-muted))]">It may have been removed or the link is incorrect.</p>
            <Link href="/toolkit?mode=universities" className="mt-4 inline-flex h-10 items-center rounded-lg border border-[hsl(var(--sic-navy))] px-5 text-sm font-semibold" data-testid="link-browse-universities">Browse universities</Link>
          </div>
        ) : tab === "insights" ? (
          id != null && <UniversityInsights universityId={id} universityName={name || "University"} />
        ) : (
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
            <section className="min-w-0" aria-labelledby="uni-courses-heading">
              <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h2 id="uni-courses-heading" className="sic-display text-2xl">Courses</h2>
                  <p className="text-sm text-[hsl(var(--sic-muted))]" aria-live="polite" data-testid="text-uni-course-count">
                    {loading && !data ? "Loading courses" : data ? `${data.total.toLocaleString()} approved course${data.total === 1 ? "" : "s"}` : ""}
                  </p>
                </div>
                <form role="search" onSubmit={(e) => { e.preventDefault(); setQ(qDraft); setPage(1); }} className="flex w-full sm:w-80 items-center gap-2 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3">
                  <Search className="h-4 w-4 text-[hsl(var(--sic-muted))]" aria-hidden />
                  <label htmlFor="uni-course-q" className="sr-only">Search courses at this university</label>
                  <input id="uni-course-q" value={qDraft} onChange={(e) => setQDraft(e.target.value)} placeholder="Search within this university"
                    className="h-10 min-w-0 flex-1 bg-transparent text-sm outline-none" data-testid="input-uni-course-search" />
                  {qDraft && <button type="button" aria-label="Clear search" onClick={() => setQDraft("")} className="p-1 text-[hsl(var(--sic-muted))]"><X className="h-4 w-4" aria-hidden /></button>}
                </form>
              </div>
              {error ? (
                <div role="alert" className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-8 text-center">
                  <p className="sic-display text-xl">Courses could not be loaded</p>
                  <button type="button" onClick={() => setReload((k) => k + 1)} className="mt-4 h-10 rounded-lg bg-[hsl(var(--sic-navy))] px-5 text-sm font-semibold text-white" data-testid="button-retry-uni-courses">Try again</button>
                </div>
              ) : loading && !data ? (
                <div className="grid gap-4 sm:grid-cols-2" aria-busy="true">{Array.from({ length: 4 }).map((_, i) => <CourseCardSkeleton key={i} view="grid" />)}</div>
              ) : results.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-10 text-center" data-testid="state-uni-courses-empty">
                  <p className="sic-display text-2xl">{q ? "No courses match that search" : "No approved courses yet"}</p>
                  {q && <button type="button" onClick={() => { setQDraft(""); setQ(""); }} className="mt-4 h-10 rounded-lg border border-[hsl(var(--sic-navy))] px-5 text-sm font-semibold">Clear search</button>}
                </div>
              ) : (
                <div className={`grid gap-4 sm:grid-cols-2 transition-opacity ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
                  {results.map((c, i) => (
                    <CourseCard key={c.id} index={i} course={c} view="grid" selected={selected?.id === c.id}
                      shortlisted={shortlist.has(c.id)} onSelect={() => setSelected(c)} onToggleShortlist={() => shortlist.toggle(c)} />
                  ))}
                </div>
              )}
              {data && data.total > data.limit && !error && (
                <nav className="mt-8 flex items-center justify-center gap-3" aria-label="Pagination">
                  <button type="button" disabled={page <= 1 || loading} onClick={() => setPage((p) => p - 1)} data-testid="button-prev-page"
                    className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm disabled:opacity-40"><ChevronLeft className="h-4 w-4" aria-hidden /> Previous</button>
                  <span className="text-sm tabular-nums" data-testid="text-page">Page {page} of {totalPages}</span>
                  <button type="button" disabled={page >= totalPages || loading} onClick={() => setPage((p) => p + 1)} data-testid="button-next-page"
                    className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm disabled:opacity-40">Next <ChevronRight className="h-4 w-4" aria-hidden /></button>
                </nav>
              )}
            </section>
            <aside className="hidden xl:block self-start sticky top-6" aria-label="Selected course">
              <DetailPanel course={selected} shortlisted={!!selected && shortlist.has(selected.id)} onToggleShortlist={() => selected && shortlist.toggle(selected)} onClose={() => setSelected(null)} />
            </aside>
          </div>
        )}
      </main>

      {selected && tab === "courses" && (
        <div className="xl:hidden fixed inset-0 z-40 flex items-end sm:items-center justify-center bg-[hsl(213_60%_12%/0.45)] sm:p-6" role="dialog" aria-modal="true" aria-label="Course details"
          onKeyDown={(e) => e.key === "Escape" && setSelected(null)}>
          <div className="w-full max-w-lg max-h-[90dvh] overflow-y-auto">
            <DetailPanel course={selected} shortlisted={shortlist.has(selected.id)} onToggleShortlist={() => shortlist.toggle(selected)} onClose={() => setSelected(null)} />
          </div>
        </div>
      )}
      <ShortlistDrawer open={shortlistOpen} items={shortlist.items} compareIds={shortlist.compareIds}
        onClose={() => setShortlistOpen(false)} onRemove={shortlist.remove} onClear={shortlist.clear}
        onToggleCompare={shortlist.toggleCompare} onOpenCourse={(cid) => { setShortlistOpen(false); const f = results.find((c) => c.id === cid); if (f) setSelected(f); else setComparing([cid]); }}
        onCompare={() => { setComparing(shortlist.compareIds); setShortlistOpen(false); }} />
      {comparing && <CompareView ids={comparing} onClose={() => setComparing(null)} />}
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "wouter";
import { ChevronDown, ChevronLeft, ChevronRight, MapPin, RotateCcw, Search, X } from "lucide-react";
import type { OptionsResponse } from "./lib";
import { SearchableSelect } from "./searchable-select";

type Uni = OptionsResponse["universities"][number];
const PAGE_SIZE = 24;

/** Trimmed location value, or null for blank / placeholder values. */
export function cleanPlace(v: string | null | undefined): string | null {
  const t = (v ?? "").trim();
  return t && !/^(unknown|n\/?a|none|null|-+|not published)$/i.test(t) ? t : null;
}
const same = (a: string | null, b: string) => !!a && a.toLowerCase() === b.toLowerCase();
const sortAlpha = (xs: Iterable<string>) => Array.from(xs).sort((a, b) => a.localeCompare(b));

export function filterUniversities(list: Uni[], q: string, country: string, city: string): Uni[] {
  const term = q.trim().toLowerCase();
  return list.filter((u) =>
    (!term || u.name.toLowerCase().includes(term)) &&
    (!country || same(cleanPlace(u.country), country)) &&
    (!city || same(cleanPlace(u.city), city)));
}

function initials(name: string) {
  return name.split(/\s+/).filter((w) => /^[A-Z]/.test(w)).slice(0, 2).map((w) => w[0]).join("") || name[0];
}

type Props = {
  options: OptionsResponse | null; optionsError: boolean; onRetry: () => void;
  q: string; country: string; city: string;
  page: number; onPage: (page: number) => void;
  onQ: (v: string) => void; onCountry: (v: string) => void; onCity: (v: string) => void;
};

export function UniversityDirectory({ options, optionsError, onRetry, q, country, city, page, onPage, onQ, onCountry, onCity }: Props) {
  const resultsTop = useRef<HTMLElement>(null);
  const all = useMemo(() => options?.universities ?? [], [options]);
  const countries = useMemo(() => {
    const m = new Map<string, string>();
    for (const u of all) { const c = cleanPlace(u.country); if (c && !m.has(c.toLowerCase())) m.set(c.toLowerCase(), c); }
    return sortAlpha(m.values());
  }, [all]);
  const cities = useMemo(() => {
    const m = new Map<string, string>();
    for (const u of all) {
      const c = cleanPlace(u.city);
      if (c && (!country || same(cleanPlace(u.country), country)) && !m.has(c.toLowerCase())) m.set(c.toLowerCase(), c);
    }
    return sortAlpha(m.values());
  }, [all, country]);
  const matches = useMemo(() => filterUniversities(all, q, country, city), [all, q, country, city]);
  const totalPages = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const start = (currentPage - 1) * PAGE_SIZE;
  const visible = matches.slice(start, start + PAGE_SIZE);
  useEffect(() => {
    if (options && !optionsError && page > totalPages) onPage(totalPages);
  }, [options, optionsError, page, totalPages, onPage]);
  const goToPage = (next: number) => {
    onPage(next);
    resultsTop.current?.scrollIntoView?.({ block: "start" });
  };
  const active = [q.trim() && { k: "q", label: `"${q.trim()}"`, clear: () => onQ("") }, country && { k: "country", label: country, clear: () => onCountry("") }, city && { k: "city", label: city, clear: () => onCity("") }]
    .filter(Boolean) as { k: string; label: string; clear: () => void }[];
  const reset = () => { onQ(""); onCountry(""); onCity(""); };
  const loading = !options && !optionsError;
  const [placeOpen, setPlaceOpen] = useState(() => !!(country || city));
  const placeCount = (country ? 1 : 0) + (city ? 1 : 0);

  return (
    <main className="mx-auto max-w-[1400px] px-4 sm:px-6 py-5 sm:py-8 grid gap-5 sm:gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
      <aside className="self-start lg:sticky lg:top-6" aria-label="University filters">
        <div className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-4 sm:p-5">
          <div className="flex items-center justify-between">
            <h2 className="sic-display text-lg">Where to study</h2>
            {active.length > 0 && (
              <button type="button" onClick={reset} className="inline-flex min-h-10 items-center gap-1 px-1 text-xs font-semibold text-primary hover:underline" data-testid="button-reset-university-filters">
                <RotateCcw className="h-3.5 w-3.5" aria-hidden /> Reset
              </button>
            )}
          </div>
          <form role="search" aria-label="University search" autoComplete="off" className="sic-university-search mt-3 sm:mt-4" onSubmit={(e) => e.preventDefault()}>
            <label htmlFor="uni-q" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">University name</label>
            <div className="flex items-center gap-2 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 focus-within:border-[hsl(var(--sic-navy))] focus-within:ring-1 focus-within:ring-[hsl(var(--sic-navy))]">
              <Search className="h-4 w-4 shrink-0 text-[hsl(var(--sic-muted))]" aria-hidden />
              <input id="uni-q" type="search" autoComplete="off" value={q} onChange={(e) => onQ(e.target.value)} placeholder="University name"
                className="h-11 sm:h-10 w-full min-w-0 bg-transparent text-base sm:text-sm text-[hsl(var(--sic-ink))] outline-none placeholder:text-[hsl(var(--sic-muted))] [&::-webkit-search-cancel-button]:appearance-none" data-testid="input-university-search" />
              {q && <button type="button" aria-label="Clear university search" onClick={() => onQ("")} className="-mr-1.5 grid h-9 w-9 shrink-0 place-items-center text-[hsl(var(--sic-muted))] hover:text-primary" data-testid="button-clear-university-search"><X className="h-4 w-4" aria-hidden /></button>}
            </div>
          </form>
          <button type="button" onClick={() => setPlaceOpen((o) => !o)} aria-expanded={placeOpen} aria-controls="uni-place-filters" data-testid="button-toggle-university-place"
            className="lg:hidden mt-3 flex h-11 w-full items-center justify-between rounded-lg border border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] px-3 text-sm font-semibold text-[hsl(var(--sic-navy))]">
            <span>Country and city{placeCount ? ` (${placeCount})` : ""}</span>
            <ChevronDown className={`h-4 w-4 transition-transform ${placeOpen ? "rotate-180" : ""}`} aria-hidden />
          </button>
          <div id="uni-place-filters" className={`mt-3 lg:mt-4 gap-4 sm:grid-cols-2 lg:grid lg:grid-cols-1 ${placeOpen ? "grid" : "hidden"}`}>
            <div>
              <p className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">Country</p>
<SearchableSelect label="Country" testId="select-university-country" anyLabel="All countries" value={country} disabled={!options}
                options={countries.map((c) => ({ value: c, label: c }))} searchPlaceholder="Type a country" emptyText="No countries found"
                onChange={(c) => { onCountry(c); if (city && c && !all.some((u) => same(cleanPlace(u.country), c) && same(cleanPlace(u.city), city))) onCity(""); }} />
            </div>
            <div>
              <p className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">City</p>
<SearchableSelect label="City" testId="select-university-city" anyLabel={country ? `All cities in ${country}` : "All cities"} value={city}
                disabled={!options || cities.length === 0} options={cities.map((c) => ({ value: c, label: c }))}
                searchPlaceholder="Type a city" emptyText="No cities found" onChange={onCity} />
            </div>
          </div>
          {countries.length === 0 && options && <p className="mt-3 text-xs text-[hsl(var(--sic-muted))]">Locations have not been published for these universities yet.</p>}
        </div>
      </aside>

      <section aria-labelledby="uni-heading" className="min-w-0" ref={resultsTop}>
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div className="min-w-0">
            <h2 id="uni-heading" className="sic-display text-2xl">Universities</h2>
            <p className="text-sm text-[hsl(var(--sic-muted))]" aria-live="polite" data-testid="text-university-count">
              {loading ? "Loading universities" : options ? `${matches.length.toLocaleString()} ${matches.length === all.length ? "" : `of ${all.length.toLocaleString()} `}universit${(active.length ? all.length : matches.length) === 1 ? "y" : "ies"}` : ""}
            </p>
          </div>
          {active.length > 0 && (
            <ul className="flex flex-wrap gap-2" aria-label="Active filters">
              {active.map((a) => (
                <li key={a.k}>
                  <button type="button" onClick={a.clear} aria-label={`Remove ${a.label}`} data-testid={`chip-university-${a.k}`}
                    className="inline-flex h-8 items-center gap-1.5 rounded-full bg-[hsl(var(--sic-navy))] px-3 text-xs font-semibold text-white hover:bg-primary">
                    {a.label} <X className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {optionsError ? (
          <div role="alert" className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-8 text-center">
            <p className="sic-display text-xl">Universities could not be loaded</p>
            <button type="button" onClick={onRetry} className="mt-4 h-10 rounded-lg bg-[hsl(var(--sic-navy))] px-5 text-sm font-semibold text-white" data-testid="button-retry-universities">Try again</button>
          </div>
        ) : loading ? (
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true">
            {Array.from({ length: 9 }).map((_, i) => <div key={i} className="h-[88px] animate-pulse rounded-xl bg-[hsl(var(--sic-mist))]" />)}
          </div>
        ) : matches.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-10 text-center" data-testid="state-university-empty">
            <p className="sic-display text-2xl">No universities match</p>
            <p className="text-sm text-[hsl(var(--sic-muted))] mt-2">Check the spelling, or widen the country or city.</p>
            {active.length > 0 && <button type="button" onClick={reset} className="mt-4 h-10 rounded-lg border border-[hsl(var(--sic-navy))] px-5 text-sm font-semibold" data-testid="button-university-empty-clear">Clear all</button>}
          </div>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {visible.map((u) => {
              const place = [cleanPlace(u.city), cleanPlace(u.country)].filter(Boolean).join(", ");
              return (
                <li key={u.id}>
                  <Link href={`/toolkit/universities/${u.id}`} data-testid={`card-university-${u.id}`}
                    className="group flex h-full w-full items-center gap-3 rounded-xl border border-[hsl(var(--sic-line))] bg-white p-4 text-left transition-[border-color,transform] hover:-translate-y-0.5 hover:border-[hsl(var(--sic-navy))]">
                    <span className="grid h-11 w-11 shrink-0 place-items-center rounded-lg bg-[hsl(var(--sic-navy))] text-sm font-semibold text-white group-hover:bg-primary">{initials(u.name)}</span>
                    <span className="min-w-0 flex-1">
                      <span className="block font-semibold leading-snug break-words">{u.name}</span>
                      {place && <span className="mt-0.5 flex items-center gap-1 text-xs text-[hsl(var(--sic-muted))]" data-testid={`text-university-location-${u.id}`}><MapPin className="h-3 w-3 shrink-0" aria-hidden />{place}</span>}
                    </span>
                    <ChevronRight className="h-4 w-4 shrink-0 text-[hsl(var(--sic-muted))] transition-transform group-hover:translate-x-0.5" aria-hidden />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
        {options && !optionsError && matches.length > 0 && (
          <div className="mt-6 flex flex-col items-center justify-between gap-3 border-t border-[hsl(var(--sic-line))] pt-5 sm:flex-row">
            <p className="text-sm text-[hsl(var(--sic-muted))]" aria-live="polite" data-testid="text-university-range">
              Showing {start + 1}–{Math.min(start + PAGE_SIZE, matches.length)} of {matches.length.toLocaleString()} {matches.length === 1 ? "university" : "universities"}
            </p>
            {totalPages > 1 && (
              <nav aria-label="University pagination" className="flex flex-wrap items-center justify-center gap-2 sm:gap-3">
                <button type="button" disabled={currentPage <= 1} onClick={() => goToPage(currentPage - 1)}
                  data-testid="button-prev-university-page" className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm font-semibold hover:border-[hsl(var(--sic-navy))] disabled:cursor-not-allowed disabled:opacity-40">
                  <ChevronLeft className="h-4 w-4" aria-hidden /> Previous
                </button>
                <span className="whitespace-nowrap text-sm tabular-nums" data-testid="text-university-page">Page {currentPage} of {totalPages}</span>
                <button type="button" disabled={currentPage >= totalPages} onClick={() => goToPage(currentPage + 1)}
                  data-testid="button-next-university-page" className="inline-flex h-10 items-center gap-1 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 text-sm font-semibold hover:border-[hsl(var(--sic-navy))] disabled:cursor-not-allowed disabled:opacity-40">
                  Next <ChevronRight className="h-4 w-4" aria-hidden />
                </button>
              </nav>
            )}
          </div>
        )}
      </section>
    </main>
  );
}

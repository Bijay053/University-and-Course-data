import { X } from "lucide-react";
import { SearchableSelect } from "./searchable-select";
import { type DegreeFilters, type OptionsResponse } from "./lib";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const selectCls = "w-full h-10 rounded-lg border border-[hsl(var(--sic-line))] bg-white/90 px-3 text-sm text-[hsl(var(--sic-ink))]";

type Props = {
  searchValue: string;
  onSearchChange: (value: string) => void;
  onSearchSubmit: () => void;
  filters: DegreeFilters;
  options: OptionsResponse | null;
  optionsError: boolean;
  onRetryOptions: () => void;
  onChange: (patch: Partial<DegreeFilters>) => void;
  onClear: () => void;
  activeCount: number;
};

function Section({ title, id, children }: { title: string; id: string; children: React.ReactNode }) {
  return (
    <fieldset className="border-t border-[hsl(var(--sic-line))] pt-4">
      <legend id={id} className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[hsl(var(--sic-muted))] mb-2 float-left w-full">{title}</legend>
      <div className="clear-both space-y-2">{children}</div>
    </fieldset>
  );
}

export function FilterPanel({ searchValue, onSearchChange, onSearchSubmit, filters, options, optionsError, onRetryOptions, onChange, onClear, activeCount }: Props) {
  const strOpts = (xs: string[] | undefined) => (xs ?? []).map((x) => ({ value: x, label: x }));
  const resetFeeSort = filters.sort === "fee_asc" || filters.sort === "fee_desc" ? "relevance" : filters.sort;
  const months = options?.intake_months?.length ? options.intake_months : MONTHS;
  const currencies = options?.currencies ?? [];
  const budgetIncomplete = !!filters.budgetMax.trim() && !filters.budgetCurrency;
  return (
    <div className="space-y-4" data-testid="panel-filters">
      <div className="flex items-center justify-between">
        <h2 className="sic-display text-lg">Refine</h2>
        <button type="button" onClick={onClear} disabled={activeCount === 0} data-testid="button-clear-filters"
          className="text-sm font-medium text-[hsl(var(--sic-teal))] disabled:text-[hsl(var(--sic-muted))]/60 hover:underline">
          Clear all{activeCount ? ` (${activeCount})` : ""}
        </button>
      </div>
      <form role="search" aria-label="Course search" onSubmit={(event) => { event.preventDefault(); onSearchSubmit(); }} className="space-y-2">
        <label className="block text-xs font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">
          Search courses
          <input type="search" value={searchValue} onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Course name or subject" data-testid="input-search"
            className={`${selectCls} mt-2 font-normal normal-case tracking-normal`} />
        </label>
        <button type="submit" data-testid="button-search" className="h-10 w-full rounded-lg bg-primary text-sm font-semibold text-primary-foreground hover:opacity-90">Search</button>
      </form>
      {optionsError && (
        <div role="alert" className="rounded-lg bg-[hsl(var(--sic-amber))]/10 p-3 text-sm">
          Filter lists could not be loaded.{" "}
          <button type="button" className="font-semibold underline" onClick={onRetryOptions} data-testid="button-retry-options">Retry</button>
        </div>
      )}
      <Section title="Destination country" id="f-country">
        <SearchableSelect label="Destination country" testId="select-country" anyLabel="Any country" value={filters.country}
          options={strOpts(options?.countries)} onChange={(v) => onChange({ country: v })} searchPlaceholder="Type a country" emptyText="No countries found" />
      </Section>
      <Section title="University" id="f-uni">
        <SearchableSelect label="University" testId="select-university" anyLabel="Any university" value={filters.universityId}
          options={(options?.universities ?? []).map((u) => ({ value: String(u.id), label: u.name }))}
          onChange={(v) => onChange({ universityId: v })} searchPlaceholder="Type a university" emptyText={options ? "No universities found" : "University list is not available yet"} />
      </Section>
      <Section title="Degree level" id="f-level">
        <SearchableSelect label="Degree level" testId="select-degree-level" anyLabel="Any level" value={filters.degreeLevel}
          options={strOpts(options?.degree_levels)} onChange={(v) => onChange({ degreeLevel: v })} searchPlaceholder="Type a level" emptyText="No levels found" />
      </Section>
      <Section title="Intake month" id="f-intake">
        <div className="grid grid-cols-3 gap-1.5" role="group" aria-labelledby="f-intake">
          {months.map((m) => {
            const on = filters.intakes.includes(m);
            return (
              <button key={m} type="button" aria-pressed={on} data-testid={`toggle-intake-${m}`}
                onClick={() => onChange({ intakes: on ? filters.intakes.filter((x) => x !== m) : [...filters.intakes, m] })}
                className={`h-8 rounded-md text-xs font-medium border transition-colors ${on ? "bg-[hsl(var(--sic-navy))] text-white border-transparent" : "bg-white/70 border-[hsl(var(--sic-line))] hover:border-[hsl(var(--sic-teal))]"}`}>
                {m.slice(0, 3)}
              </button>
            );
          })}
        </div>
      </Section>
      <Section title="Fee currency and budget" id="f-budget">
        <p className="text-xs text-[hsl(var(--sic-muted))]">Pick a currency to see only fees published in it. Nothing is converted. Add an amount to cap the budget.</p>
        <div className="grid grid-cols-[96px_1fr] gap-2">
<SearchableSelect label="Budget currency" testId="select-budget-currency" anyLabel={currencies.length ? "Any" : "None"} value={filters.budgetCurrency}
            options={strOpts(currencies)} disabled={!currencies.length} searchPlaceholder="Code" emptyText="None"
            onChange={(v) => onChange(v ? { budgetCurrency: v } : { budgetCurrency: "", sort: resetFeeSort })} />
          <label className="sr-only" htmlFor="budget-max">Maximum fee amount</label>
          <input id="budget-max" type="number" inputMode="decimal" min={0} placeholder="Max fee amount"
            className={selectCls} value={filters.budgetMax} data-testid="input-budget-max"
            onChange={(e) => onChange({ budgetMax: e.target.value })} />
        </div>
        {budgetIncomplete && <p className="text-xs text-[hsl(var(--sic-amber))]" role="status">Choose a currency to apply this amount.</p>}
        {(filters.budgetMax || filters.budgetCurrency) && (
          <button type="button" className="inline-flex items-center gap-1 text-xs text-[hsl(var(--sic-muted))] hover:text-[hsl(var(--sic-ink))]"
            onClick={() => onChange({ budgetMax: "", budgetCurrency: "", sort: resetFeeSort })} data-testid="button-clear-budget">
            <X className="h-3 w-3" aria-hidden /> Remove currency and budget
          </button>
        )}
      </Section>
    </div>
  );
}

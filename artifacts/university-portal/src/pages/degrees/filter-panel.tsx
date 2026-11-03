import { X } from "lucide-react";
import { useState } from "react";
import { type DegreeFilters, type OptionsResponse } from "./lib";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const selectCls = "w-full h-10 rounded-lg border border-[hsl(var(--sic-line))] bg-white/90 px-3 text-sm text-[hsl(var(--sic-ink))]";

type Props = {
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

export function FilterPanel({ filters, options, optionsError, onRetryOptions, onChange, onClear, activeCount }: Props) {
  const [universityQuery, setUniversityQuery] = useState("");
  const matchingUniversities = (options?.universities ?? []).filter((u) =>
    u.name.toLocaleLowerCase().includes(universityQuery.trim().toLocaleLowerCase()),
  );
  const months = options?.intake_months?.length ? options.intake_months : MONTHS;
  const currencies = options?.currencies ?? [];
  const budgetIncomplete = !!filters.budgetMax.trim() && !filters.budgetCurrency;
  return (
    <div className="space-y-4" data-testid="panel-filters">
      <div className="flex items-center justify-between">
        <h2 className="sic-display text-lg">Refine</h2>
        <button type="button" onClick={() => { setUniversityQuery(""); onClear(); }} disabled={activeCount === 0 && !universityQuery} data-testid="button-clear-filters"
          className="text-sm font-medium text-[hsl(var(--sic-teal))] disabled:text-[hsl(var(--sic-muted))]/60 hover:underline">
          Clear all{activeCount ? ` (${activeCount})` : ""}
        </button>
      </div>
      {optionsError && (
        <div role="alert" className="rounded-lg bg-[hsl(var(--sic-amber))]/10 p-3 text-sm">
          Filter lists could not be loaded.{" "}
          <button type="button" className="font-semibold underline" onClick={onRetryOptions} data-testid="button-retry-options">Retry</button>
        </div>
      )}
      <Section title="Destination country" id="f-country">
        <select aria-labelledby="f-country" className={selectCls} value={filters.country} data-testid="select-country"
          onChange={(e) => onChange({ country: e.target.value })}>
          <option value="">Any country</option>
          {(options?.countries ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </Section>
      <Section title="University" id="f-uni">
        <input type="search" aria-label="Search universities" placeholder="Search universities…"
          className={selectCls} value={universityQuery} data-testid="input-university-search"
          onChange={(e) => setUniversityQuery(e.target.value)} />
        {universityQuery.trim() && (
          <div className="max-h-48 overflow-y-auto rounded-lg border border-[hsl(var(--sic-line))] bg-white">
            {matchingUniversities.map((u) => (
              <button key={u.id} type="button"
                className="block w-full px-3 py-2 text-left text-sm hover:bg-[hsl(var(--sic-teal-soft))]"
                onClick={() => { onChange({ universityId: String(u.id) }); setUniversityQuery(""); }}>
                {u.name}
              </button>
            ))}
            {!matchingUniversities.length && (
              <p role="status" className="px-3 py-2 text-sm text-[hsl(var(--sic-muted))]">
                {options ? "No universities found." : "University list is not available yet."}
              </p>
            )}
          </div>
        )}
        <select aria-labelledby="f-uni" className={selectCls} value={filters.universityId} data-testid="select-university"
          onChange={(e) => onChange({ universityId: e.target.value })}>
          <option value="">Any university</option>
          {(options?.universities ?? []).map((u) => <option key={u.id} value={String(u.id)}>{u.name}</option>)}
        </select>
      </Section>
      <Section title="Degree level" id="f-level">
        <select aria-labelledby="f-level" className={selectCls} value={filters.degreeLevel} data-testid="select-degree-level"
          onChange={(e) => onChange({ degreeLevel: e.target.value })}>
          <option value="">Any level</option>
          {(options?.degree_levels ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
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
          <label className="sr-only" htmlFor="budget-currency">Budget currency</label>
          <select id="budget-currency" className={selectCls} value={filters.budgetCurrency} data-testid="select-budget-currency"
            onChange={(e) => onChange(e.target.value ? { budgetCurrency: e.target.value } : { budgetCurrency: "", sort: filters.sort === "fee_asc" || filters.sort === "fee_desc" ? "relevance" : filters.sort })} disabled={!currencies.length}>
            <option value="">{currencies.length ? "Any" : "None"}</option>
            {currencies.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
          <label className="sr-only" htmlFor="budget-max">Maximum fee amount</label>
          <input id="budget-max" type="number" inputMode="decimal" min={0} placeholder="Max fee amount"
            className={selectCls} value={filters.budgetMax} data-testid="input-budget-max"
            onChange={(e) => onChange({ budgetMax: e.target.value })} />
        </div>
        {budgetIncomplete && <p className="text-xs text-[hsl(var(--sic-amber))]" role="status">Choose a currency to apply this amount.</p>}
        {(filters.budgetMax || filters.budgetCurrency) && (
          <button type="button" className="inline-flex items-center gap-1 text-xs text-[hsl(var(--sic-muted))] hover:text-[hsl(var(--sic-ink))]"
            onClick={() => onChange({ budgetMax: "", budgetCurrency: "", sort: filters.sort === "fee_asc" || filters.sort === "fee_desc" ? "relevance" : filters.sort })} data-testid="button-clear-budget">
            <X className="h-3 w-3" aria-hidden /> Remove currency and budget
          </button>
        )}
      </Section>
    </div>
  );
}

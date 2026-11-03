import type { ReactNode } from "react";
import { X } from "lucide-react";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { SearchableSelect } from "./searchable-select";
import { RangeFilter } from "./range-filter";
import { IntakeSelect } from "./intake-select";
import {
  DURATION_DEFAULT, DURATION_LIMIT, ENGLISH_BANDS, ENGLISH_EXAMS_FALLBACK, FEE_INPUT_MAX, FEE_SLIDER_MAX, GRADING_SCHEMES,
  type DegreeFilters, type OptionsResponse,
} from "./lib";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const inputCls = "w-full h-9 rounded-md border border-[hsl(var(--sic-line))] bg-white/90 px-3 text-sm text-[hsl(var(--sic-ink))] disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary";
const subLabel = "mb-1 block text-xs text-[hsl(var(--sic-muted))]";
const SECTIONS = ["country", "qualification", "grading", "english", "other", "intakes", "duration", "fee"];
const BLANK_BANDS = { eOverall: "", eReading: "", eWriting: "", eListening: "", eSpeaking: "" };

type Props = {
  filters: DegreeFilters; options: OptionsResponse | null; optionsError: boolean;
  onRetryOptions: () => void; onChange: (patch: Partial<DegreeFilters>) => void; onClear: () => void; activeCount: number;
  onSearch: () => void; onClose?: () => void;
};

function Item({ value, title, children }: { value: string; title: ReactNode; children: ReactNode }) {
  return (
    <AccordionItem value={value} className="border-[hsl(var(--sic-line))]" data-testid={`section-filter-${value}`}>
      <AccordionTrigger className="py-3 text-sm font-semibold text-[hsl(var(--sic-ink))] hover:no-underline">{title}</AccordionTrigger>
      <AccordionContent className="space-y-2 pt-1">{children}</AccordionContent>
    </AccordionItem>
  );
}

export function FilterPanel({ filters: f, options, optionsError, onRetryOptions, onChange, onClear, activeCount, onSearch, onClose }: Props) {
  const strOpts = (xs: string[] | undefined) => (xs ?? []).map((x) => ({ value: x, label: x }));
  const resetFeeSort = f.sort === "fee_asc" || f.sort === "fee_desc" ? "relevance" : f.sort;
  const months = options?.intakes?.length ? options.intakes : options?.intake_months?.length ? options.intake_months : MONTHS;
  const currencies = options?.currencies ?? [];
  const outs = options?.grading_schemes?.find((s) => s.scheme === "GPA")?.out_of ?? [];
  const exams = options?.english_exams?.length ? options.english_exams : ENGLISH_EXAMS_FALLBACK;
  const cur = f.budgetCurrency;
  const fee: [number, number] = [Number(f.budgetMin) || 0, f.budgetMax.trim() ? Number(f.budgetMax) || 0 : FEE_SLIDER_MAX];
  const setFee = ([lo, hi]: [number, number]) => onChange({ budgetMin: lo > 0 ? String(lo) : "", budgetMax: hi !== FEE_SLIDER_MAX ? String(hi) : "" });
  const budgetIncomplete = !cur && !!(f.budgetMin.trim() || f.budgetMax.trim());
  const k = (n: number) => `${cur} ${n / 1000}k`;

  return (
    <div className="space-y-3" data-testid="panel-filters">
      <div className={`flex items-center justify-between ${onClose ? "sticky top-0 z-20 -mx-5 bg-[hsl(var(--sic-paper))] px-5 py-3" : ""}`}>
        <h2 className="sic-display text-lg">Refine</h2>
        <div className="flex items-center gap-2">
        <button type="button" onClick={onClear} disabled={activeCount === 0} data-testid="button-clear-filters"
          className="text-sm font-medium text-primary disabled:text-[hsl(var(--sic-muted))]/60 hover:underline">
          Reset{activeCount ? ` (${activeCount})` : ""}
        </button>
          {onClose && <button type="button" onClick={onClose} aria-label="Close filters" title="Close filters" data-testid="button-close-filters"
            className="grid h-11 w-11 shrink-0 place-items-center rounded-lg border border-[hsl(var(--sic-line))] bg-white text-[hsl(var(--sic-navy))] hover:bg-[hsl(var(--sic-mist))]">
            <X className="h-5 w-5" aria-hidden />
          </button>}
        </div>
      </div>
      {optionsError && (
        <div role="alert" className="rounded-lg bg-[hsl(var(--sic-amber))]/10 p-3 text-sm">
          Filter lists could not be loaded.{" "}
          <button type="button" className="font-semibold underline" onClick={onRetryOptions} data-testid="button-retry-options">Retry</button>
        </div>
      )}
      <Accordion type="multiple" defaultValue={SECTIONS} className="w-full border-t border-[hsl(var(--sic-line))]">
        <Item value="country" title={<>Study Destination Country <span className="ml-0.5 text-primary">*</span></>}>
          <SearchableSelect label="Study Destination Country" testId="select-country" anyLabel="Any" value={f.country}
            options={strOpts(options?.countries)} onChange={(v) => onChange({ country: v })} searchPlaceholder="Search countries" emptyText="No countries found" />
        </Item>

        <Item value="qualification" title={<>Highest Qualification Studied <span className="ml-0.5 text-primary">*</span></>}>
          <SearchableSelect label="Highest Qualification Studied" testId="select-qualification" anyLabel="Any qualification" value={f.qualification}
            options={strOpts(options?.qualifications)} searchPlaceholder="Search qualifications" emptyText="No qualifications found"
            onChange={(v) => onChange({ qualification: v, scheme: "", outOf: "", gradingScore: "" })} />
        </Item>

        <Item value="grading" title={f.qualification ? `${f.qualification} Grading Scheme` : "Grading Scheme"}>
          <SearchableSelect label="Grading Scheme" testId="select-grading-scheme" anyLabel={f.qualification ? "Any" : "Pick qualification first"} value={f.scheme}
            disabled={!f.qualification} options={GRADING_SCHEMES.map((s) => ({ value: s, label: s }))}
            onChange={(v) => onChange({ scheme: v, outOf: "", gradingScore: "" })} />
          <p className="text-[11px] text-[hsl(var(--sic-muted))]">
            {f.qualification ? "Courses with unpublished academic requirements remain included." : "Select a qualification above to enter your grade."}
          </p>
          {f.qualification && f.scheme === "GPA" && (
            <div className="grid grid-cols-2 gap-2">
              <div>
                <span className={subLabel}>Out of</span>
                <SearchableSelect label="Out of" testId="select-grading-out-of" anyLabel="Any" value={f.outOf}
                  options={(outs.length ? outs : ["4", "5", "10"]).map((o) => ({ value: o, label: `Out of ${o}` }))}
                  onChange={(v) => onChange({ outOf: v, gradingScore: "" })} />
              </div>
              <label className={subLabel}>{f.qualification} GPA
                <input type="number" step="0.01" min="0" max={f.outOf ? Number(f.outOf) : undefined} disabled={!f.outOf}
                  value={f.gradingScore} onChange={(e) => onChange({ gradingScore: e.target.value })} placeholder="e.g. 3.5"
                  className={`${inputCls} mt-1`} data-testid="input-grading-score" />
              </label>
            </div>
          )}
          {f.qualification && f.scheme === "Percentage" && (
            <label className={subLabel}>{f.qualification} Percentage (%)
              <input type="number" step="0.01" min="0" max="100" value={f.gradingScore} placeholder="e.g. 75"
                onChange={(e) => onChange({ gradingScore: e.target.value })} className={`${inputCls} mt-1`} data-testid="input-grading-score" />
            </label>
          )}
        </Item>

        <Item value="english" title="English Proficiency Exam">
          <SearchableSelect label="English Proficiency Exam" testId="select-english-exam" anyLabel="Any" value={f.englishExam}
            options={strOpts(exams)} onChange={(v) => onChange({ englishExam: v, ...BLANK_BANDS })} searchPlaceholder="Search exams" />
          {f.englishExam && (
            <div className="grid grid-cols-2 gap-2 pt-1">
              {ENGLISH_BANDS.map(([key, label], i) => (
                <label key={key} className={`${subLabel} ${i === ENGLISH_BANDS.length - 1 ? "col-span-2" : ""}`}>{label}
                  <input type="number" step="0.5" min="0" placeholder="Score" value={f[key]} data-testid={`input-english-${label.toLowerCase()}`}
                    onChange={(e) => onChange({ [key]: e.target.value } as Partial<DegreeFilters>)} className={`${inputCls} mt-1`} />
                </label>
              ))}
            </div>
          )}
        </Item>

        <Item value="other" title="Other Exam (GMAT/GRE)">
          <label className="sr-only" htmlFor="other-exam">Other exam</label>
          <input id="other-exam" placeholder="e.g. GMAT, GRE" value={f.otherExam} onChange={(e) => onChange({ otherExam: e.target.value })}
            className={inputCls} data-testid="input-other-exam" />
        </Item>

        <Item value="intakes" title="Intakes">
          <IntakeSelect options={months} value={f.intakes} onChange={(intakes) => onChange({ intakes })} />
        </Item>

        <Item value="duration" title="Duration (years)">
          <RangeFilter idPrefix="duration" unit="" value={[f.durationMin, f.durationMax]} sliderMax={DURATION_DEFAULT[1]} inputMax={DURATION_LIMIT} step={0.5}
            presets={[{ label: "\u2264 1 yr", range: [0, 1] }, { label: "1\u20132 yrs", range: [1, 2] }, { label: "3 yrs", range: [3, 3] }, { label: "4+ yrs", range: [4, 6] }]}
            onChange={([lo, hi]) => onChange({ durationMin: lo, durationMax: hi })} />
        </Item>

        <Item value="fee" title={`Tuition Fee${cur ? ` (${cur})` : ""}`}>
          <span className={subLabel}>Fee currency</span>
          <SearchableSelect label="Fee currency" testId="select-budget-currency" anyLabel={currencies.length ? "Any" : "None"} value={cur}
            options={strOpts(currencies)} disabled={!currencies.length} searchPlaceholder="Code" emptyText="None"
            onChange={(v) => onChange(v ? { budgetCurrency: v } : { budgetCurrency: "", sort: resetFeeSort })} />
          <p className="text-xs text-[hsl(var(--sic-muted))]">{cur ? "Only fees published in this currency are compared. Nothing is converted." : "Choose a currency to set a fee range. Fees are never converted between currencies."}</p>
          <RangeFilter idPrefix="fee" unit={cur} value={fee} sliderMax={FEE_SLIDER_MAX} inputMax={FEE_INPUT_MAX} step={1000} disabled={!cur}
            presets={[{ label: `\u2264 ${k(20000)}`, range: [0, 20000] }, { label: `${k(20000)}\u2013${40}k`, range: [20000, 40000] }, { label: `${k(40000)}\u2013${60}k`, range: [40000, 60000] }, { label: `${k(60000)}+`, range: [60000, FEE_SLIDER_MAX] }]}
            onChange={setFee} />
          {budgetIncomplete && <p className="text-xs text-[hsl(var(--sic-amber))]" role="status">Choose a currency to apply this amount.</p>}
          {(f.budgetMin || f.budgetMax || cur) && (
            <button type="button" className="inline-flex items-center gap-1 text-xs text-[hsl(var(--sic-muted))] hover:text-[hsl(var(--sic-ink))]"
              onClick={() => onChange({ budgetMin: "", budgetMax: "", budgetCurrency: "", sort: resetFeeSort })} data-testid="button-clear-budget">
              <X className="h-3 w-3" aria-hidden /> Remove currency and fee range
            </button>
          )}
        </Item>
      </Accordion>
      <button type="button" onClick={onSearch} data-testid="button-search-filters"
        className="flex h-11 w-full items-center justify-center rounded-lg bg-primary px-4 text-sm font-semibold text-primary-foreground hover:opacity-90">
        Search courses
      </button>
    </div>
  );
}

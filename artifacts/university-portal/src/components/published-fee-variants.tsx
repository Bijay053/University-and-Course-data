/** Persisted scraper authority, not an inferred scalar fee. */
export type FeeVariantCarrier = {
  feeVariants?: unknown;
  fee_variants?: unknown;
  extractionMethod?: unknown;
  extraction_method?: unknown;
};

type FeeOption = {
  amount: number;
  currency: string;
  campus: string;
  study_variant: string;
  year: number;
  period: string | null;
  source_url: string;
  snippet: string;
};

const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;

export function feeVariantAuthority(course: FeeVariantCarrier) {
  return record(course.feeVariants) ?? record(course.fee_variants)
    ?? record(record(course.extractionMethod)?.fee_variants)
    ?? record(record(course.extraction_method)?.fee_variants);
}

function validOption(value: unknown): value is FeeOption {
  const o = record(value);
  return !!o && typeof o.amount === "number" && Number.isFinite(o.amount) && o.amount > 0
    && o.currency === "GBP" && typeof o.campus === "string"
    && typeof o.study_variant === "string" && typeof o.year === "number"
    && (o.period === null || typeof o.period === "string")
    && typeof o.source_url === "string" && typeof o.snippet === "string";
}

export function feeVariantNeedsReview(course: FeeVariantCarrier) {
  const authority = feeVariantAuthority(course);
  return !!authority && (authority.status !== "uniform" || !feeVariantSummary(course));
}

const money = (amount: number) => `£${amount.toLocaleString("en-GB", { maximumFractionDigits: 2 })}`;
const periodLabel = (period: string | null) =>
  period === "Annual" ? "Annual (per year)" : period === "Full Course" ? "Full Course" : period || "Period not stated";

/** Never combine years or billing periods into a misleading single range. */
export function feeVariantSummary(course: FeeVariantCarrier): string | null {
  const authority = feeVariantAuthority(course);
  if (!authority || !["range", "uniform"].includes(String(authority.status))) return null;
  const selected = Array.isArray(authority.selected) ? authority.selected : [];
  if (!selected.length || !selected.every(validOption)) return null;
  const groups = new Map<string, FeeOption[]>();
  for (const option of selected) {
    const key = `${option.year} · ${periodLabel(option.period)}`;
    groups.set(key, [...(groups.get(key) ?? []), option]);
  }
  return [...groups].map(([label, options]) => {
    const amounts = options.map(o => o.amount);
    const min = Math.min(...amounts), max = Math.max(...amounts);
    return `${money(min)}${max !== min ? `–${money(max)}` : ""} GBP · ${label}`;
  }).join("; ");
}

function safeSource(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? value : undefined;
  } catch { return undefined; }
}

export function PublishedFeeVariants({ course, id }: { course: FeeVariantCarrier; id: number | string }) {
  const authority = feeVariantAuthority(course);
  if (!authority) return null;
  const summary = feeVariantSummary(course);
  const selected = Array.isArray(authority.selected) ? authority.selected.filter(validOption) : [];
  const options = Array.isArray(authority.options) ? authority.options.filter(validOption) : [];
  const needsReview = feeVariantNeedsReview(course);
  const renderOption = (option: FeeOption, index: number, section: string) => (
    <li key={`${section}-${index}`} className="border-t pt-2 mt-2" data-testid={`fee-option-${id}-${section}-${index}`}>
      <div className="font-medium">{money(option.amount)} GBP · {option.year} · {periodLabel(option.period)}</div>
      <div>International · {option.campus} · {option.study_variant}</div>
      <p className="text-slate-500 whitespace-normal">{option.snippet}</p>
      {safeSource(option.source_url) ? (
        <a href={safeSource(option.source_url)} target="_blank" rel="noopener noreferrer"
          className="text-blue-600 underline break-all" data-testid={`fee-source-${id}-${section}-${index}`}>
          {option.source_url}
        </a>
      ) : <span className="text-amber-700">Source URL unavailable</span>}
    </li>
  );
  return (
    <div className="text-left whitespace-normal min-w-[200px] max-w-md" data-testid={`published-fees-${id}`}>
      <div className="text-green-700 font-medium" data-testid={`fee-summary-${id}`}>
        {summary ?? "Published fee unresolved"}
      </div>
      {(needsReview || !summary) && (
        <div className="text-xs text-amber-700" data-testid={`fee-review-${id}`}>
          {summary ? "Known alternatives — variant review required" : "No applicable fee confirmed — review required"}
        </div>
      )}
      <details className="text-xs mt-1">
        <summary className="cursor-pointer text-blue-700" data-testid={`expand-fees-${id}`}>Fee options &amp; sources</summary>
        <div className="p-2 border rounded bg-slate-50 mt-1">
          <div className="font-semibold">Applicable published options</div>
          {selected.length ? <ul>{selected.map((o, i) => renderOption(o, i, "selected"))}</ul>
            : <p>No applicable option confirmed.</p>}
          {options.length > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer" data-testid={`expand-all-fees-${id}`}>All published options (including other years / variants)</summary>
              <ul>{options.map((o, i) => renderOption(o, i, "all"))}</ul>
            </details>
          )}
        </div>
      </details>
    </div>
  );
}
import { useEffect, useState } from "react";

export type FeeSelection = {
  snapshotToken: string;
  options: Array<{
    optionId: string;
    amount: number;
    currency: string;
    year: number;
    period: string | null;
    campus: string;
    studyVariant: string;
    sourceUrl: string;
    snippet: string;
  }>;
  selectedOptionId: string | null;
};

/** Persisted scraper authority, not an inferred scalar fee. */
export type FeeVariantCarrier = {
  feeVariants?: unknown;
  fee_variants?: unknown;
  extractionMethod?: unknown;
  extraction_method?: unknown;
  feeSelection?: FeeSelection | null;
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

/** Matching campus prices may be grouped automatically; other fee choices may not. */
function autoCampusRange(authority: Record<string, unknown> | null): boolean {
  if (authority?.status !== "range" || !Array.isArray(authority.selected)
    || authority.selected.length < 2 || !authority.selected.every(validOption)) return false;
  const selected: FeeOption[] = authority.selected;
  const [first] = selected;
  const byCampus = new Map<string, number>();
  for (const option of selected) {
    const campus = option.campus.trim().toLowerCase();
    if (!campus || !option.study_variant.trim()
      || option.year !== first.year || option.period !== first.period
      || option.study_variant.trim().toLowerCase() !== first.study_variant.trim().toLowerCase()
      || option.currency !== first.currency) return false;
    if (byCampus.has(campus) && byCampus.get(campus) !== option.amount) return false;
    byCampus.set(campus, option.amount);
  }
  return byCampus.size > 1 && new Set(selected.map(option => option.amount)).size > 1;
}

export function feeVariantNeedsReview(course: FeeVariantCarrier) {
  const authority = feeVariantAuthority(course);
  if (!authority) return false;
  if (authority.status === "uniform" && !!feeVariantSummary(course)) return false;
  return !course.feeSelection?.selectedOptionId
    || !course.feeSelection.options.some(o => o.optionId === course.feeSelection?.selectedOptionId);
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

export function PublishedFeeVariants({ course, id, readOnly = false, onCourseUpdated, onRefresh }: {
  course: FeeVariantCarrier; id: number | string; readOnly?: boolean;
  onCourseUpdated?: (course: FeeVariantCarrier & { id: number }) => void;
  onRefresh?: () => void | Promise<unknown>;
}) {
  const selection = course.feeSelection;
  const [chosenId, setChosenId] = useState<string | null>(selection?.selectedOptionId ?? null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    setChosenId(selection?.selectedOptionId ?? null);
    setError(null);
    setConfirmed(false);
  }, [selection?.snapshotToken, selection?.selectedOptionId]);
  const save = async () => {
    if (!selection || !chosenId || saving || chosenId === selection.selectedOptionId) return;
    setSaving(true);
    setError(null);
    setConfirmed(false);
    try {
      const res = await fetch(`/api/scrape/staged/${id}/fee-selection`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snapshotToken: selection.snapshotToken, optionId: chosenId }),
      });
      if (!res.ok) {
        let message = `Unable to save fee choice (${res.status}).`;
        try {
          const body = await res.json();
          message = typeof body.detail === "string" ? body.detail : message;
        } catch { /* keep the HTTP error */ }
        if (res.status === 409) {
          message += " Fee options changed. Refresh and choose again.";
          await onRefresh?.();
        }
        setError(message);
        return;
      }
      const body = await res.json() as { success?: boolean; course?: FeeVariantCarrier & { id: number } };
      if (!body.success || !body.course) throw new Error("Server did not confirm the saved fee choice.");
      onCourseUpdated?.(body.course);
      setConfirmed(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save fee choice.");
    } finally {
      setSaving(false);
    }
  };
  const authority = feeVariantAuthority(course);
  if (!authority) return null;
  const summary = feeVariantSummary(course);
  const selected = Array.isArray(authority.selected) ? authority.selected.filter(validOption) : [];
  const options = Array.isArray(authority.options) ? authority.options.filter(validOption) : [];
  const needsReview = feeVariantNeedsReview(course);
  const automaticCampusFees = autoCampusRange(authority);
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
          {automaticCampusFees
            ? "Campus prices differ. Approval checks course-owned campuses and groups verified fees automatically; unverified locations stay pending for review."
            : summary ? "Known alternatives — variant review required" : "No applicable fee confirmed — review required"}
        </div>
      )}
      {!automaticCampusFees && selection?.selectedOptionId && (
        <div className="text-xs text-green-700" data-testid={`fee-saved-${id}`}>
          Saved choice: {selection.options.find(o => o.optionId === selection.selectedOptionId)?.campus ?? "Selected option"}
          {" · "}{selection.options.find(o => o.optionId === selection.selectedOptionId)?.year}
          {" · "}{selection.options.find(o => o.optionId === selection.selectedOptionId)?.period}
        </div>
      )}
      {!readOnly && !automaticCampusFees && selection && selection.options.length > 0 && (
        <div className="mt-2 text-xs" data-testid={`fee-selection-${id}`}>
          <div className="font-semibold">Choose a published fee before approval</div>
          <div role="radiogroup" aria-label="Published fee options">
            {selection.options.map((o) => (
              <label key={o.optionId} className="flex items-start gap-1 py-1">
                <input type="radio" name={`fee-choice-${id}`} value={o.optionId}
                  data-testid={`fee-choice-${id}-${o.optionId}`}
                  checked={chosenId === o.optionId} disabled={saving}
                  onChange={() => { setChosenId(o.optionId); setConfirmed(false); }} />
                <span>{money(o.amount)} {o.currency} · {o.campus} · {o.studyVariant} · {o.year} · {periodLabel(o.period)}
                  <span className="block text-slate-500">{o.snippet}</span>
                  {safeSource(o.sourceUrl) && <a href={o.sourceUrl} target="_blank" rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()} data-testid={`fee-choice-source-${id}-${o.optionId}`}
                    className="text-blue-600 underline break-all">{o.sourceUrl}</a>}
                </span>
              </label>
            ))}
          </div>
          <button type="button" onClick={save} disabled={saving || !chosenId || chosenId === selection.selectedOptionId}
            data-testid={`save-fee-choice-${id}`} className="border rounded px-2 py-1 bg-blue-600 text-white disabled:opacity-50">
            {saving ? "Saving…" : "Save fee choice"}
          </button>
          {confirmed && <div className="text-green-700" data-testid={`fee-confirmation-${id}`}>Fee choice saved.</div>}
          {error && <div role="alert" className="text-red-700" data-testid={`fee-error-${id}`}>
            {error} <button type="button" onClick={() => void onRefresh?.()} data-testid={`refresh-fee-${id}`}
              className="underline">Refresh fee options</button>
          </div>}
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
import { useState } from "react";
import { AlertTriangle, ChevronRight, ExternalLink } from "lucide-react";
import { safeSourceUrl } from "./assistant-safe-url";

export type ComparisonStatus = "published" | "pending" | "recorded" | "missing";
export type CatalogueComparison = {
  complete: boolean;
  note: string;
  observed: number;
  counts: { published: number; pending: number; recorded: number; missing: number };
  sources: { url: string; status: string; note?: string }[];
  courses: { url: string; title: string; status: ComparisonStatus; courseIds: number[]; reviewJobIds: string[] }[];
};

const STATUS_META: Record<ComparisonStatus, { label: string; chip: string; dot: string }> = {
  published: { label: "Approved active", chip: "bg-teal-100 text-teal-900", dot: "bg-teal-700" },
  pending: { label: "Pending review", chip: "bg-amber-100 text-amber-900", dot: "bg-amber-500" },
  recorded: { label: "Recorded, not published", chip: "bg-sky-100 text-sky-900", dot: "bg-sky-600" },
  missing: { label: "Not recorded", chip: "bg-red-100 text-red-800", dot: "bg-red-600" },
};
const ORDER: ComparisonStatus[] = ["missing", "pending", "recorded", "published"];
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const arr = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);

function CourseRow({ c, idx, onReviewJob }: { c: CatalogueComparison["courses"][number]; idx: number; onReviewJob?: (jobId: string) => void }) {
  const [open, setOpen] = useState(false);
  const meta = STATUS_META[c.status] ?? STATUS_META.missing;
  const href = safeSourceUrl(c.url);
  const ids = arr<number>(c.courseIds);
  const jobs = arr<string>(c.reviewJobIds).filter(j => typeof j === "string" && j);
  return (
    <li className="border-t border-slate-100 first:border-t-0" data-testid={`comparison-row-${idx}`} data-status={c.status}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12.5px] hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600"
        data-testid={`button-comparison-row-${idx}`}>
        <ChevronRight className={`h-3 w-3 shrink-0 text-slate-400 motion-safe:transition-transform ${open ? "rotate-90" : ""}`} aria-hidden />
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.dot}`} aria-hidden />
        <span className="min-w-0 flex-1 truncate text-slate-800">{String(c.title || c.url || "Untitled course")}</span>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-medium ${meta.chip}`} data-testid={`status-comparison-row-${idx}`}>{meta.label}</span>
      </button>
      {open && (
        <div className="space-y-1.5 px-3 pb-2.5 pl-10 text-[12px] text-slate-600">
          {href
            ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex max-w-full items-center gap-1 font-mono text-[11px] text-teal-800 underline underline-offset-2" data-testid={`link-comparison-row-${idx}`}><span className="truncate">{href}</span><ExternalLink className="h-3 w-3 shrink-0" aria-hidden /></a>
            : <span className="text-[11px] text-slate-500">(link unavailable)</span>}
          <p>{ids.length ? `Recorded as course ${ids.map(i => `#${i}`).join(", ")}` : "No recorded course matches this catalogue entry."}</p>
          {jobs.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span>In review:</span>
              {jobs.map(j => onReviewJob
                ? <button key={j} type="button" onClick={() => onReviewJob(j)} className="rounded-md border border-slate-200 bg-white px-2 py-0.5 font-mono text-[11px] text-teal-800 hover:border-teal-300" data-testid={`button-comparison-review-${idx}-${j}`}>Open review {j}</button>
                : <span key={j} className="font-mono text-[11px]">{j}</span>)}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function CatalogueComparisonView({ comparison, onReviewJob, testId = "assistant-comparison" }: {
  comparison: CatalogueComparison; onReviewJob?: (jobId: string) => void; testId?: string;
}) {
  const counts = comparison.counts ?? ({} as CatalogueComparison["counts"]);
  const courses = arr<CatalogueComparison["courses"][number]>(comparison.courses)
    .filter(c => c && typeof c === "object")
    .map((c, i) => ({ c, i }))
    .sort((a, b) => ORDER.indexOf(a.c.status) - ORDER.indexOf(b.c.status) || a.i - b.i);
  const sources = arr<CatalogueComparison["sources"][number]>(comparison.sources);
  const complete = comparison.complete === true;
  return (
    <div className="mt-3 overflow-hidden rounded-xl border border-slate-200 bg-white" data-testid={testId}>
      <div className="border-b border-slate-100 px-3 py-2.5">
        <p className="text-[12.5px] font-semibold text-slate-900">Catalogue comparison</p>
        <p className="text-[11.5px] text-slate-500" data-testid="text-comparison-observed">
          {num(comparison.observed)} official course link{num(comparison.observed) === 1 ? "" : "s"} observed in {complete ? "the official catalogue" : "the parts of the catalogue that could be read"}
        </p>
      </div>
      {!complete && (
        <div role="note" className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-3 py-2 text-[12px] text-amber-900" data-testid="comparison-partial">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span><strong className="font-semibold">Partial comparison.</strong> Not every catalogue page could be checked, so these counts are not a complete inventory of the catalogue.{comparison.note ? ` ${comparison.note}` : ""}</span>
        </div>
      )}
      {complete && comparison.note && <p className="border-b border-slate-100 px-3 py-2 text-[12px] text-slate-600">{comparison.note}</p>}
      <p className="border-b border-slate-100 px-3 py-1.5 text-[11px] text-slate-500" data-testid="text-comparison-approved-note">
        Approved active means an approved, active course record exists. It does not prove the course appears in public search.
      </p>
      <dl className="grid grid-cols-2 gap-px bg-slate-100 sm:grid-cols-4" data-testid="comparison-counts">
        {(["published", "pending", "recorded", "missing"] as ComparisonStatus[]).map(k => (
          <div key={k} className="bg-white px-3 py-2">
            <dt className="text-[10.5px] uppercase tracking-wider text-slate-500">{STATUS_META[k].label}</dt>
            <dd className="font-mono text-[18px] tabular-nums text-slate-900" data-testid={`count-comparison-${k}`}>{num(counts[k])}</dd>
          </div>
        ))}
      </dl>
      {courses.length > 0 && (
        <ul className="max-h-72 overflow-y-auto border-t border-slate-100" aria-label="Catalogue courses">
          {courses.map(({ c, i }) => <CourseRow key={`${c.url}-${i}`} c={c} idx={i} onReviewJob={onReviewJob} />)}
        </ul>
      )}
      {sources.length > 0 && (
        <div className="space-y-1 border-t border-slate-100 px-3 py-2">
          <p className="text-[10.5px] uppercase tracking-wider text-slate-500">Catalogue sources checked</p>
          {sources.map((s, si) => {
            const href = safeSourceUrl(s.url);
            return (
              <div key={si} className="flex flex-wrap items-baseline gap-2 text-[11.5px]" data-testid={`comparison-source-${si}`}>
                {href
                  ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="min-w-0 truncate font-mono text-teal-800 underline underline-offset-2" data-testid={`link-comparison-source-${si}`}>{href}</a>
                  : <span className="text-slate-500">(link unavailable)</span>}
                <span className="font-mono text-slate-500">{String(s.status ?? "")}</span>
                {s.note && <span className="text-slate-600">{s.note}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

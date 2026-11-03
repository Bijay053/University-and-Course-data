import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Loader2, X } from "lucide-react";
import { BASE, type CompareCourse, safeHttpUrl } from "./lib";
import { comparisonRows } from "./comparison-data";

type Props = { ids: number[]; onClose: () => void };

export function CompareView({ ids, onClose }: Props) {
  const [courses, setCourses] = useState<CompareCourse[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const downloadInFlight = useRef(false);
  async function downloadPdf() {
    if (!courses?.length || downloadInFlight.current) return;
    downloadInFlight.current = true;
    setDownloading(true);
    setDownloadError(null);
    try {
      const { createComparisonPdf } = await import("./compare-pdf");
      const now = new Date();
      const pdf = await createComparisonPdf(courses, ids.length, now);
      await pdf.save(`study-info-centre-course-comparison-${now.toISOString().slice(0, 10)}.pdf`, { returnPromise: true });
    } catch {
      setDownloadError("The PDF could not be downloaded. Please try again.");
    } finally {
      downloadInFlight.current = false;
      setDownloading(false);
    }
  }
  const load = useCallback(async () => {
    setError(null); setCourses(null);
    try {
      const res = await fetch(`${BASE}/api/search/compare?ids=${ids.join(",")}`);
      if (!res.ok) throw new Error("Comparison is unavailable right now.");
      const json = await res.json() as { courses: CompareCourse[] };
      setCourses(json.courses ?? []);
    } catch (e) { setError(e instanceof Error ? e.message : "Comparison failed."); }
  }, [ids]);
  useEffect(() => { void load(); }, [load]);

  const rows = comparisonRows(courses ?? []);

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-[hsl(213_60%_12%/0.5)] p-3 sm:p-8" role="dialog" aria-modal="true" aria-labelledby="compare-title" aria-describedby="compare-desc"
      onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="sic-rise mx-auto max-w-6xl rounded-2xl bg-[hsl(var(--sic-paper))] shadow-2xl" data-testid="dialog-compare">
        <header className="flex flex-wrap items-center justify-between gap-3 p-5 border-b border-[hsl(var(--sic-line))]">
          <div className="min-w-0 flex-1 basis-full sm:basis-0">
            <h2 id="compare-title" className="sic-display text-2xl">Side by side</h2>
            <p id="compare-desc" className="text-xs text-[hsl(var(--sic-muted))]">Fees appear in their published currency. They are not converted or ranked against each other.</p>
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <button type="button" onClick={downloadPdf} disabled={!courses?.length || downloading || !!error}
              data-testid="button-download-compare" className="inline-flex h-10 items-center gap-2 rounded-lg bg-primary px-3 text-sm font-semibold text-primary-foreground hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40">
              {downloading ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <Download className="h-4 w-4" aria-hidden />}
              {downloading ? "Preparing PDF…" : "Download PDF"}
            </button>
            <button type="button" autoFocus onClick={onClose} aria-label="Close comparison" data-testid="button-close-compare" className="grid h-9 w-9 place-items-center rounded-full hover:bg-[hsl(var(--sic-teal-soft))]"><X className="h-4 w-4" aria-hidden /></button>
          </div>
        </header>
        <div className="p-5">
          {downloadError && <p role="alert" className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700" data-testid="error-download-compare">{downloadError}</p>}
          {error ? (
            <div role="alert" className="rounded-xl bg-white p-6 text-sm">{error} <button type="button" onClick={load} className="ml-2 font-semibold text-[hsl(var(--sic-teal))] underline" data-testid="button-retry-compare">Retry</button></div>
          ) : !courses ? (
            <div className="grid gap-3" aria-busy="true">{[0, 1, 2, 3].map((i) => <div key={i} className="sic-skel h-10" />)}</div>
          ) : courses.length === 0 ? (
            <p className="text-sm">These courses are no longer listed.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-separate border-spacing-0 text-sm">
                <thead>
                  <tr>
                    <th className="w-36 sticky left-0 bg-[hsl(var(--sic-paper))]" scope="col"><span className="sr-only">Attribute</span></th>
                    {courses.map((c) => <th key={c.id} scope="col" className="p-3 text-left align-bottom sic-display text-base font-semibold">{c.course_name}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.label}>
                      <th scope="row" className="sticky left-0 bg-[hsl(var(--sic-paper))] p-3 text-left align-top text-xs uppercase tracking-[0.1em] text-[hsl(var(--sic-muted))] border-t border-[hsl(var(--sic-line))]">{r.label}</th>
                      {r.values.map((value, i) => <td key={courses[i].id} className="whitespace-pre-line p-3 align-top border-t border-[hsl(var(--sic-line))]">{r.label === "Official page" && safeHttpUrl(value) ? <a className="text-[hsl(var(--sic-teal))] underline" href={value} target="_blank" rel="noopener noreferrer">Visit</a> : value}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
              {courses.length < ids.length && <p className="mt-3 text-xs text-[hsl(var(--sic-muted))]">{ids.length - courses.length} shortlisted course(s) are no longer listed and were left out.</p>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

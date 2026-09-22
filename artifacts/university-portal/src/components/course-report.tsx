import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { Loader2 } from "lucide-react";
import { Form } from "@/components/ui/form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { readResponseJson } from "@/lib/readResponseJson";

type Report = {
  job_id: string; report_id?: string; original_job_id?: string;
  status: string; found: number; staged: number; skipped: number; errors: number;
  error?: string; processed?: number; exclusions: Record<string, unknown>;
  request: {
    kind: string; eligibility_review?: boolean; expected_count?: number; description?: string; fields?: string[];
    course_urls?: string[]; source_url?: string; catalogue_url?: string;
  };
  verification?: { capped?: boolean };
  recovery?: { phase?: string; reason?: string; next_action?: string; exhausted?: boolean };
  continuation?: {
    available: boolean; remaining_urls: string[]; remaining_count: number; completed_count: number;
    selected_count: number; reason?: string; run_count: number;
  };
  retry?: { available: boolean; remaining_urls: string[]; remaining_count: number };
  programme_urls?: Array<{
    url: string; status: "queued" | "processing" | "staged" | "skipped" | "error";
    origin: "submitted" | "related" | "discovered";
  }>;
  children?: Array<{
    job_id: string; status: string; staged: number; processed?: number; found?: number;
    skipped?: number; errors?: number;
  }>;
};
type Values = {
  kind: "missing" | "incorrect"; eligibilityReview: boolean; urls: string; catalogue: string;
  expected: string; fields: string[]; description: string; source: string;
};
const defaults: Values = { kind: "missing", eligibilityReview: false, urls: "", catalogue: "", expected: "", fields: [], description: "", source: "" };
const activeStatuses = new Set(["queued", "running", "recovering", "verification_queued", "verifying"]);

export function Exclusions({ counts }: { counts: Record<string, unknown> }) {
  const staging = counts.staging_rejections as { reasons?: Record<string, number> } | undefined;
  const entries = Object.entries({ ...counts, ...staging?.reasons }).filter(([, value]) => typeof value === "number" && value > 0);
  return <div className="text-xs text-muted-foreground" data-testid="report-exclusions">
    <strong>Exclusion / skip breakdown: </strong>
    {entries.length ? entries.map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ")
      : "No per-reason counts recorded. This does not prove that no courses were excluded."}
  </div>;
}

export function CourseReport({ jobId, onReview, onStarted, openRequest = 0 }: {
  jobId: string; onReview: (id: string) => void; onStarted?: () => void; openRequest?: number;
}) {
  const [open, setOpen] = useState(false);
  const [reports, setReports] = useState<Report[]>([]);
  const [exclusions, setExclusions] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [continuationReviewed, setContinuationReviewed] = useState<Record<string, boolean>>({});
  const [continuationBusy, setContinuationBusy] = useState<string | null>(null);
  const [continuationErrors, setContinuationErrors] = useState<Record<string, string>>({});
  const [retryBusy, setRetryBusy] = useState<string | null>(null);
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({});
  const form = useForm<Values>({ defaultValues: defaults });
  const kind = form.watch("kind");
  useEffect(() => {
    if (!openRequest) return;
    form.reset(defaults);
    setError("");
    setOpen(true);
  }, [form, openRequest]);
  useEffect(() => {
    let disposed = false;
    const load = async () => {
      try {
        const response = await fetch(`/api/scrape/jobs/${encodeURIComponent(jobId)}/course-reports`, { credentials: "include" });
        const data = await readResponseJson<{ reports: Report[]; source_exclusions: Record<string, unknown>; detail?: string }>(response);
        if (!response.ok || !data || !Array.isArray(data.reports)) throw new Error(data?.detail || "Could not load report history");
        if (!disposed) { setReports(data.reports); setExclusions(data.source_exclusions ?? {}); setHistoryError(""); }
      } catch (e) {
        if (!disposed) setHistoryError(e instanceof Error ? e.message : "Could not load report history");
      } finally { if (!disposed) setLoading(false); }
    };
    setReports([]); setLoading(true);
    void load();
    const interval = setInterval(load, 15000);
    return () => { disposed = true; clearInterval(interval); };
  }, [jobId, refresh]);

  useEffect(() => {
    setContinuationReviewed({});
    setContinuationBusy(null);
    setContinuationErrors({});
    setRetryBusy(null);
    setRetryErrors({});
  }, [jobId]);

  const retryReport = async (report: Report) => {
    if (retryBusy) return;
    setRetryBusy(report.job_id);
    setRetryErrors(previous => ({ ...previous, [report.job_id]: "" }));
    try {
      const response = await fetch(
        `/api/scrape/jobs/${encodeURIComponent(jobId)}/course-reports/${encodeURIComponent(report.job_id)}/retry`,
        { method: "POST", credentials: "include" },
      );
      const data = await readResponseJson<Report & { detail?: unknown }>(response);
      if (!response.ok || !data) {
        const detail = data && "detail" in data ? data.detail : undefined;
        throw new Error(typeof detail === "string" ? detail : "Could not retry recovery");
      }
      setReports(previous => [data, ...previous]);
      setRefresh(value => value + 1);
      onStarted?.();
    } catch (e) {
      setRetryErrors(previous => ({
        ...previous,
        [report.job_id]: e instanceof Error ? e.message : "Could not retry recovery",
      }));
    } finally {
      setRetryBusy(null);
    }
  };

  const continueReport = async (report: Report) => {
    if (!continuationReviewed[report.job_id] || continuationBusy) return;
    setContinuationBusy(report.job_id);
    setContinuationErrors(previous => ({ ...previous, [report.job_id]: "" }));
    try {
      const response = await fetch(
        `/api/scrape/jobs/${encodeURIComponent(jobId)}/course-reports/${encodeURIComponent(report.job_id)}/continue`,
        {
          method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reviewed: true }),
        },
      );
      const data = await readResponseJson<Report & { detail?: unknown }>(response);
      if (!response.ok || !data) {
        const detail = data && "detail" in data ? data.detail : undefined;
        throw new Error(typeof detail === "string" ? detail : "Could not continue recovery");
      }
      if (data.job_id) {
        setReports(previous => previous.map(item =>
          (report.report_id && item.report_id === report.report_id) || item.job_id === report.job_id ? data : item
        ));
      }
      setContinuationReviewed(previous => ({ ...previous, [report.job_id]: false }));
      setRefresh(value => value + 1);
      onStarted?.();
    } catch (e) {
      setContinuationErrors(previous => ({
        ...previous,
        [report.job_id]: e instanceof Error ? e.message : "Could not continue recovery",
      }));
    } finally {
      setContinuationBusy(null);
    }
  };

  const submit = form.handleSubmit(async values => {
    setError("");
    const urls = values.urls.split(/\s+/).map(u => u.trim()).filter(Boolean);
    if (kind === "incorrect" && (!urls.length || !values.fields.length || !values.description.trim())) {
      setError("Provide course URLs, affected fields and a description."); return;
    }
    if (!urls.length && !values.catalogue.trim()) {
      setError("Provide official course URLs or a catalogue link."); return;
    }
    if (urls.length && values.catalogue.trim()) {
      setError("Use course URLs or a catalogue link, not both."); return;
    }
    try {
      const response = await fetch(`/api/scrape/jobs/${encodeURIComponent(jobId)}/course-reports`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: values.kind, course_urls: urls, catalogue_url: values.catalogue.trim() || null,
          eligibility_review: values.eligibilityReview,
          expected_count: values.expected ? Number(values.expected) : null,
          fields: values.fields, description: values.description.trim(), source_url: values.source.trim() || null,
        }),
      });
      const data = await readResponseJson<Report & { detail?: unknown }>(response);
      if (!response.ok || !data) throw new Error(typeof data?.detail === "string" ? data.detail : "Report could not be started. Check the official URLs and form values.");
      setReports(previous => [data, ...previous]); setOpen(false); form.reset(defaults);
      setRefresh(n => n + 1);
      onStarted?.();
    } catch (e) { setError(e instanceof Error ? e.message : "Report could not be started"); }
  });

  return <section className="m-4 space-y-3 rounded-lg border bg-muted/20 p-4" aria-label="Course recovery">
    <Button variant="outline" size="sm" data-testid="button-report-courses" onClick={() => {
      form.reset(defaults); setError(""); setOpen(!open);
    }}>Report missing or incorrect courses</Button>
    <p className="text-xs text-muted-foreground">
      Field completeness measures the courses already found, not catalogue coverage.
      Reports run a fresh, bounded recovery (up to 50 courses / 10 minutes), keep existing pending rows,
      preserve eligibility filters and stage results for review — never publish automatically.
      Reported expectations are not proof of missing eligible courses.
    </p>
    <Exclusions counts={exclusions} />
    {historyError && <p role="status" className="text-xs text-destructive">{historyError}</p>}
    {error && <p role={open ? "alert" : "status"} className="text-sm text-destructive" data-testid="report-error">{error}</p>}
    {open && <Form {...form}><form onSubmit={submit} className="space-y-3">
      <label className="block text-sm">Problem
        <select {...form.register("kind", { onChange: () => {
          form.setValue("catalogue", ""); form.setValue("expected", ""); form.setValue("fields", []);
        } })} className="ml-2 rounded border p-2" data-testid="select-report-kind">
          <option value="missing">Missing courses</option><option value="incorrect">Incorrect fields</option>
        </select>
      </label>
        <label className="block text-sm">Official course URLs (one per line; processed in bounded batches)
        <Textarea {...form.register("urls")} rows={3} data-testid="input-report-urls" />
      </label>
      {kind === "missing" && <>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" {...form.register("eligibilityReview")} data-testid="checkbox-report-eligibility" />
          <span>Request eligibility review for a foundation or pathway programme</span>
        </label>
        <label className="block text-sm">Or official catalogue link
          <Input type="url" {...form.register("catalogue")} data-testid="input-report-catalogue" />
        </label>
        <label className="block text-sm">Expected eligible course count (optional)
          <Input type="number" min={1} max={100000} {...form.register("expected")} data-testid="input-report-expected" />
        </label>
      </>}
      {kind === "incorrect" && <fieldset className="flex flex-wrap gap-3 text-sm">
        <legend>Affected fields</legend>
        {["fee", "english", "intake", "duration", "campus", "other"].map(field => <label key={field}>
          <input type="checkbox" value={field} {...form.register("fields")} data-testid={`checkbox-report-${field}`} /> {field}
        </label>)}
      </fieldset>}
      <label className="block text-sm">Describe what is missing or incorrect {kind === "incorrect" ? "(required)" : "(optional)"}
        <Textarea maxLength={4000} {...form.register("description")} data-testid="input-report-description" />
      </label>
      <label className="block text-sm">Official supporting source URL (optional)
        <Input type="url" {...form.register("source")} data-testid="input-report-source" />
      </label>
      <p className="text-xs text-muted-foreground">Recovery re-extracts official evidence, not your asserted values. Eligibility review only considers direct official programme pages; category pages and global international safeguards remain rejected.</p>
      <Button type="submit" disabled={form.formState.isSubmitting} data-testid="button-submit-report">
        {form.formState.isSubmitting ? "Starting recovery…" : "Start recovery"}
      </Button>
    </form></Form>}
    {loading && <p className="text-xs">Loading report history…</p>}
    {reports.map(report => {
      const active = activeStatuses.has(report.status);
      const processed = report.processed ?? report.continuation?.completed_count ?? 0;
      const total = Math.max(report.continuation?.selected_count ?? report.found ?? 0, processed);
      const progress = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
      return <article key={report.report_id ?? report.job_id} className="space-y-2 rounded border bg-background p-3 text-sm" data-testid={`report-${report.job_id}`}>
      <p className="flex items-center gap-2">
        {active && <Loader2 className="h-4 w-4 animate-spin text-primary" aria-hidden="true" />}
        <strong>{report.request.kind === "missing" ? "Missing course recovery" : "Incorrect field recovery"}</strong> — {report.status}
      </p>
      {active && <div
        className="space-y-1 rounded border border-blue-200 bg-blue-50 p-2 text-blue-950"
        role="status"
        aria-live="polite"
        data-testid={`report-progress-${report.job_id}`}
      >
        <p className="flex items-center gap-2 text-xs font-medium">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          {total > 0
            ? `Processing reported pages… ${processed} of ${total} complete`
            : "Starting recovery… waiting for the first page"}
        </p>
        <div
          className="h-2 overflow-hidden rounded-full bg-blue-100"
          role="progressbar"
          aria-label="Recovery progress"
          aria-valuemin={0}
          aria-valuemax={total || undefined}
          aria-valuenow={total > 0 ? processed : undefined}
        >
          <div
            className={`h-full rounded-full bg-blue-600 transition-[width] duration-500 ${total > 0 ? "" : "w-1/3 animate-pulse"}`}
            style={total > 0 ? { width: `${progress}%` } : undefined}
          />
        </div>
        <p className="text-[11px] text-blue-800">This updates automatically about every 15 seconds.</p>
      </div>}
      {report.request.eligibility_review && <p className="text-xs">Eligibility review requested; only page-owned foundation/pathway evidence can recover this page.</p>}
      {report.request.description && <p>{report.request.description}</p>}
      {report.request.fields?.length ? <p>Reported fields: {report.request.fields.join(", ")}</p> : null}
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3" aria-label="Recovery counts">
        <div className="rounded border p-2" data-testid={`report-processed-${report.job_id}`}>
          <dt className="text-xs text-muted-foreground">Processed</dt>
          <dd className="font-semibold">{report.continuation?.completed_count ?? report.processed ?? 0}</dd>
        </div>
        <div className="rounded border p-2" data-testid={`report-staged-${report.job_id}`}>
          <dt className="text-xs text-muted-foreground">Staged for review</dt>
          <dd className="font-semibold">{report.staged}</dd>
        </div>
        <div className="rounded border p-2" data-testid={`report-remaining-${report.job_id}`}>
          <dt className="text-xs text-muted-foreground">Remaining</dt>
          <dd className="font-semibold">{report.continuation?.remaining_count ?? 0}</dd>
        </div>
      </dl>
      <p className="text-xs text-muted-foreground">
        {report.found} found · {report.skipped} skipped · {report.errors} errors
        {report.continuation ? ` · ${report.continuation.selected_count} selected · ${report.continuation.run_count} bounded ${report.continuation.run_count === 1 ? "run" : "runs"}` : ""}
      </p>
      {report.programme_urls?.length ? <details className="rounded border p-2 text-xs">
        <summary className="cursor-pointer font-medium" data-testid={`button-programme-urls-${report.job_id}`}>
          Programme pages checked ({report.programme_urls.length})
        </summary>
        <ul className="mt-2 space-y-2">
          {report.programme_urls.map((item, index) => <li
            key={item.url}
            className="flex flex-col gap-1 rounded border p-2 sm:flex-row sm:items-center sm:justify-between"
            data-testid={`programme-url-${report.job_id}-${index}`}
          >
            <a className="break-all underline" href={item.url} target="_blank" rel="noreferrer">{item.url}</a>
            <span className="flex shrink-0 gap-2">
              <span className={item.origin === "submitted" ? "font-medium" : "text-muted-foreground"}>
                {item.origin === "submitted" ? "Submitted" : item.origin === "related" ? "Related" : "Discovered"}
              </span>
              <span className={
                item.status === "error" ? "font-medium text-destructive"
                  : item.status === "staged" ? "font-medium text-green-700"
                    : "font-medium"
              }>{item.status}</span>
            </span>
          </li>)}
        </ul>
      </details> : null}
      <details className="text-xs">
        <summary className="cursor-pointer" data-testid={`button-report-sources-${report.job_id}`}>Reported official sources</summary>
        {[...(report.request.course_urls ?? []), report.request.catalogue_url, report.request.source_url]
          .filter((url): url is string => Boolean(url)).map((url, index) => <a
            key={`${url}-${index}`} className="block break-all underline" href={url} target="_blank" rel="noreferrer"
            data-testid={`link-report-source-${report.job_id}-${index}`}>{url}</a>)}
      </details>
      {report.request.expected_count && <p>Reported expected count: {report.request.expected_count} (not a verified catalogue total)</p>}
      <p className="text-xs">Catalogue coverage: not verified{report.verification?.capped ? " — recovery limit reached" : ""}. Staged results still need review; a finished run does not confirm every reported issue is fixed.</p>
      <Exclusions counts={report.exclusions} />
      {report.error && <p className="text-destructive">{report.error}</p>}
      {report.recovery?.reason && <p className="text-xs">{report.recovery.reason}</p>}
      {report.recovery?.exhausted && <p className="text-xs text-destructive">
        Automatic delivery retries exhausted. Check the connection and submit a new report to retry.
      </p>}
      {report.continuation && report.continuation.remaining_count > 0 && <div
        className="space-y-2 rounded border border-amber-200 bg-amber-50 p-3 text-amber-950"
        data-testid={`continuation-${report.job_id}`}
      >
        <p className="font-medium">More reported URLs remain</p>
        <p className="text-xs">
          Continuing starts another bounded run of up to 50 courses, 600 seconds, and $2.
          It does not verify the full catalogue, and any newly staged courses will still require review.
        </p>
        <details className="text-xs">
          <summary className="cursor-pointer" data-testid={`button-remaining-urls-${report.job_id}`}>
            Review {report.continuation.remaining_count} remaining {report.continuation.remaining_count === 1 ? "URL" : "URLs"}
          </summary>
          {report.continuation.remaining_urls.map((url, index) => <a
            key={`${url}-${index}`} className="block break-all underline" href={url} target="_blank" rel="noreferrer"
            data-testid={`link-remaining-url-${report.job_id}-${index}`}
          >{url}</a>)}
        </details>
        {report.continuation.reason && <p className="text-xs">{report.continuation.reason}</p>}
        {report.continuation.available ? <>
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={Boolean(continuationReviewed[report.job_id])}
              disabled={Boolean(continuationBusy)}
              onChange={event => setContinuationReviewed(previous => ({
                ...previous, [report.job_id]: event.target.checked,
              }))}
              data-testid={`checkbox-continuation-review-${report.job_id}`}
            />
            <span>
              I reviewed the remaining URL list and understand the next run is limited to 50 courses,
              600 seconds, and $2.
            </span>
          </label>
          <Button
            size="sm"
            type="button"
            disabled={!continuationReviewed[report.job_id] || Boolean(continuationBusy)}
            onClick={() => void continueReport(report)}
            data-testid={`button-continue-report-${report.job_id}`}
          >
            {continuationBusy === report.job_id ? "Starting next run…" : "Continue bounded recovery"}
          </Button>
        </> : <p className="text-xs" role="status">
          Continuation is not available for this report{report.continuation.reason ? `: ${report.continuation.reason}` : "."}
        </p>}
        {continuationErrors[report.job_id] && <p
          className="text-xs text-destructive" role="alert" data-testid={`continuation-error-${report.job_id}`}
        >{continuationErrors[report.job_id]}</p>}
      </div>}
      {report.children?.some(child => child.job_id !== report.job_id) && <div className="space-y-2" data-testid={`report-run-history-${report.report_id ?? report.job_id}`}>
        <p className="text-xs font-medium">Prior bounded runs</p>
        {report.children.filter(child => child.job_id !== report.job_id).map((child, index) => <div
          key={child.job_id} className="flex flex-wrap items-center justify-between gap-2 rounded border p-2 text-xs"
          data-testid={`report-child-${child.job_id}`}
        >
          <span>
            Run {index + 1} — {child.status} · {child.processed ?? child.found ?? 0} processed · {child.staged} staged
          </span>
          {child.staged > 0 && <Button
            size="sm" variant="outline" onClick={() => onReview(child.job_id)}
            data-testid={`button-review-report-${child.job_id}`}
          >
            Review {child.staged} staged {child.staged === 1 ? "course" : "courses"}
          </Button>}
        </div>)}
      </div>}
      {report.staged > 0 ? (
        <Button size="sm" variant="outline" onClick={() => onReview(report.job_id)} data-testid={`button-review-report-${report.job_id}`}>
          Review {report.staged} staged {report.staged === 1 ? "course" : "courses"}
        </Button>
      ) : ["completed", "completed_with_errors", "stopped", "failed", "failed_degraded"].includes(report.status) ? (
        <div className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900" role="status">
          <p>
            No recovered courses are available to review.
          </p>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">Review will become available if this recovery stages an eligible course.</p>
      )}
      {report.retry?.available && <div
        className="space-y-2 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900"
        data-testid={`retry-${report.job_id}`}
      >
        <p>
          {report.retry.remaining_count} directly reported {report.retry.remaining_count === 1 ? "URL has" : "URLs have"} not
          produced a staged course. Retry revalidates only {report.retry.remaining_count === 1 ? "that source" : "those sources"}.
        </p>
        <Button
          size="sm"
          variant="outline"
          type="button"
          disabled={Boolean(retryBusy)}
          onClick={() => void retryReport(report)}
          data-testid={`button-retry-report-${report.job_id}`}
        >
          {retryBusy === report.job_id ? "Retrying recovery…" : "Retry recovery"}
        </Button>
        {retryErrors[report.job_id] && <p
          className="text-destructive" role="alert" data-testid={`retry-error-${report.job_id}`}
        >{retryErrors[report.job_id]}</p>}
      </div>}
    </article>;
    })}
  </section>;
}

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { Form } from "@/components/ui/form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { readResponseJson } from "@/lib/readResponseJson";

type Report = {
  job_id: string; status: string; found: number; staged: number; skipped: number; errors: number;
  error?: string; processed?: number; exclusions: Record<string, unknown>;
  request: {
    kind: string; eligibility_review?: boolean; expected_count?: number; description?: string; fields?: string[];
    course_urls?: string[]; source_url?: string; catalogue_url?: string;
  };
  verification?: { capped?: boolean };
  recovery?: { phase?: string; reason?: string; next_action?: string; exhausted?: boolean };
};
type Values = {
  kind: "missing" | "incorrect"; eligibilityReview: boolean; urls: string; catalogue: string;
  expected: string; fields: string[]; description: string; source: string;
};
const defaults: Values = { kind: "missing", eligibilityReview: false, urls: "", catalogue: "", expected: "", fields: [], description: "", source: "" };

export function Exclusions({ counts }: { counts: Record<string, unknown> }) {
  const staging = counts.staging_rejections as { reasons?: Record<string, number> } | undefined;
  const entries = Object.entries({ ...counts, ...staging?.reasons }).filter(([, value]) => typeof value === "number" && value > 0);
  return <div className="text-xs text-muted-foreground" data-testid="report-exclusions">
    <strong>Exclusion / skip breakdown: </strong>
    {entries.length ? entries.map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ")
      : "No per-reason counts recorded. This does not prove that no courses were excluded."}
  </div>;
}

export function CourseReport({ jobId, onReview, onStarted }: {
  jobId: string; onReview: (id: string) => void; onStarted?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [reports, setReports] = useState<Report[]>([]);
  const [exclusions, setExclusions] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const form = useForm<Values>({ defaultValues: defaults });
  const kind = form.watch("kind");
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
      <label className="block text-sm">Official course URLs (one per line, maximum 50)
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
    {reports.map(report => <article key={report.job_id} className="space-y-2 rounded border bg-background p-3 text-sm" data-testid={`report-${report.job_id}`}>
      <p><strong>{report.request.kind === "missing" ? "Missing course recovery" : "Incorrect field recovery"}</strong> — {report.status}</p>
      {report.request.eligibility_review && <p className="text-xs">Eligibility review requested; only page-owned foundation/pathway evidence can recover this page.</p>}
      {report.request.description && <p>{report.request.description}</p>}
      {report.request.fields?.length ? <p>Reported fields: {report.request.fields.join(", ")}</p> : null}
      <p>{report.found} found · {report.processed ?? 0} processed · {report.staged} staged · {report.skipped} skipped · {report.errors} errors</p>
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
      {report.staged > 0 ? (
        <Button size="sm" variant="outline" onClick={() => onReview(report.job_id)} data-testid={`button-review-report-${report.job_id}`}>
          Review {report.staged} staged {report.staged === 1 ? "course" : "courses"}
        </Button>
      ) : ["completed", "completed_with_errors", "stopped", "failed", "failed_degraded"].includes(report.status) ? (
        <p className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900" role="status">
          No recovered courses are available to review. This run skipped every reported page; use the exclusion reason above,
          then report a direct eligible course page or an official catalogue source.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">Review will become available if this recovery stages an eligible course.</p>
      )}
    </article>)}
  </section>;
}
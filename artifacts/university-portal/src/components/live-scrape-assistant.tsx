import { useEffect, useState } from "react";
import { Activity, AlertTriangle, ChevronDown, Loader2, RefreshCw } from "lucide-react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { ScrapePromptAssistant } from "@/components/scrape-prompt-assistant";

export const LIVE_DIAGNOSTICS_POLL_MS = 5000;

export type LiveIssue = { code: string; title: string; detail: string };
export type LiveDiagnostics = {
  jobId: string;
  status: string | null;
  processed: number | null;
  total: number | null;
  elapsedSeconds: number | null;
  coursesPerMinute: number | null;
  etaSeconds: number | null;
  timeouts: number | null;
  retries: number | null;
  duplicateRequests: number | null;
  aiEnrichments: number | null;
  heartbeatAgeSeconds: number | null;
  issues: LiveIssue[] | null;
  capabilities: { runtimeChanges: boolean } | null;
};

export const liveDiagnosticsUrl = (jobId: string) =>
  `/api/scrape/jobs/${encodeURIComponent(jobId)}/live-diagnostics`;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function formatSeconds(s: number | null): string {
  if (s === null) return "Unavailable";
  const t = Math.max(0, Math.round(s));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  if (h) return `${h}h ${m}m`;
  return m ? `${m}m ${sec}s` : `${sec}s`;
}
const show = (v: number | null, digits = 0) => (v === null ? "Unavailable" : v.toFixed(digits));

type State = { jobId: string; data: LiveDiagnostics | null; error: string | null; loading: boolean };

/** Polls live diagnostics for exactly one job. Responses for any other job are discarded. */
export function useLiveDiagnostics(jobId: string, active: boolean) {
  const [state, setState] = useState<State>({ jobId, data: null, error: null, loading: true });
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setState({ jobId, data: null, error: null, loading: true });
    if (!active) { setState({ jobId, data: null, error: null, loading: false }); return; }
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const res = await fetch(liveDiagnosticsUrl(jobId), { credentials: "include", cache: "no-store", signal: ctrl.signal });
        if (!res.ok) throw new Error(await getFetchErrorMessage(res));
        const body = await readResponseJson<LiveDiagnostics>(res);
        if (ctrl.signal.aborted) return;
        if (!body || (body.jobId && body.jobId !== jobId)) throw new Error("Diagnostics response did not match this job.");
        setState({ jobId, data: body, error: null, loading: false });
      } catch (err) {
        if (ctrl.signal.aborted) return;
        setState(s => ({ jobId, data: s.jobId === jobId ? s.data : null, error: err instanceof Error ? err.message : String(err), loading: false }));
      }
      if (!ctrl.signal.aborted) timer = setTimeout(tick, LIVE_DIAGNOSTICS_POLL_MS);
    };
    void tick();
    return () => { ctrl.abort(); if (timer) clearTimeout(timer); };
  }, [jobId, active, retry]);
  const visible = state.jobId === jobId ? state : { jobId, data: null, error: null, loading: true };
  return { ...visible, retry: () => setRetry(r => r + 1) };
}

function Metric({ label, value, testId, warn }: { label: string; value: string; testId: string; warn?: boolean }) {
  const na = value === "Unavailable";
  return (
    <div className="min-w-0 rounded-md border border-slate-200 bg-white px-2 py-1.5">
      <div className="truncate text-[9.5px] font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div data-testid={testId}
        className={`truncate font-mono text-[12px] tabular-nums ${na ? "italic text-slate-400" : warn ? "text-amber-700" : "text-slate-900"}`}>
        {value}
      </div>
    </div>
  );
}

export function LiveDiagnosticsPanel({ jobId, active = true }: { jobId: string; active?: boolean }) {
  const { data, error, loading, retry } = useLiveDiagnostics(jobId, active);
  const [open, setOpen] = useState(false);
  const processed = num(data?.processed), total = num(data?.total);
  const rate = num(data?.coursesPerMinute), eta = num(data?.etaSeconds);
  const hb = num(data?.heartbeatAgeSeconds);
  const issues = Array.isArray(data?.issues) ? data!.issues!.filter(i => i && typeof i.title === "string") : null;
  const stale = hb !== null && hb > 60;

  let headline: string;
  let tone = "text-slate-700";
  if (!data) headline = loading ? "Reading live diagnostics" : "Diagnostics unavailable";
  else if (issues === null) headline = "Diagnosis unavailable";
  else if (issues.length) { headline = issues[0].title + (issues.length > 1 ? ` (+${issues.length - 1} more)` : ""); tone = "text-amber-800"; }
  else if (stale) { headline = `No heartbeat for ${formatSeconds(hb)}`; tone = "text-amber-800"; }
  else if (hb === null) headline = "No issues reported; heartbeat unavailable";
  else { headline = "No issues reported"; tone = "text-teal-800"; }

  return (
    <section className="rounded-lg border border-teal-200/70 bg-[#f6f4ee] p-2.5 space-y-2" aria-label="Live diagnosis"
      data-testid={`live-diagnostics-${jobId}`}>
      <div className="flex items-start gap-2">
        <Activity className="mt-0.5 h-3.5 w-3.5 shrink-0 text-teal-700" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] font-semibold text-slate-900">Live diagnosis</span>
            {data?.status && <span className="rounded bg-teal-100 px-1.5 font-mono text-[10px] text-teal-900" data-testid="status-live-job">{data.status}</span>}
            {loading && <Loader2 className="h-3 w-3 animate-spin text-teal-700" aria-label="Loading" />}
          </div>
          <p className={`break-words text-[11.5px] ${tone}`} data-testid="text-live-headline">{headline}</p>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex items-start gap-1.5 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-800" data-testid="live-diagnostics-error">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          <span className="min-w-0 flex-1 break-words">{data ? `Showing last reading. ${error}` : error}</span>
          <button type="button" onClick={retry} className="inline-flex shrink-0 items-center gap-1 font-medium underline" data-testid="button-live-retry">
            <RefreshCw className="h-3 w-3" aria-hidden />Retry
          </button>
        </div>
      )}

      {loading && !data && (
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4" aria-busy="true" data-testid="live-diagnostics-loading">
          {[0, 1, 2, 3].map(i => <div key={i} className="h-9 rounded-md bg-slate-200/70 animate-pulse" />)}
        </div>
      )}

      {data && (
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
          <Metric label="Processed" testId="text-live-processed"
            value={processed === null ? "Unavailable" : total === null ? String(processed) : `${processed}/${total}`} />
          <Metric label="Courses/min" testId="text-live-rate" value={show(rate, 1)} />
          <Metric label="ETA" testId="text-live-eta" value={formatSeconds(eta)} />
          <Metric label="Heartbeat" testId="text-live-heartbeat" value={hb === null ? "Unavailable" : `${formatSeconds(hb)} ago`} warn={stale} />
        </div>
      )}

      {data && (
        <div>
          <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
            className="flex items-center gap-1 text-[11px] font-medium text-teal-900 hover:text-teal-950" data-testid="button-live-details">
            <ChevronDown className={`h-3 w-3 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
            {open ? "Hide details" : "Details"}
          </button>
          {open && (
            <div className="mt-1.5 space-y-2" data-testid="live-diagnostics-details">
              <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                <Metric label="Elapsed" testId="text-live-elapsed" value={formatSeconds(num(data.elapsedSeconds))} />
                <Metric label="Timeouts" testId="text-live-timeouts" value={show(num(data.timeouts))} warn={(num(data.timeouts) ?? 0) > 0} />
                <Metric label="Retries" testId="text-live-retries" value={show(num(data.retries))} />
                <Metric label="Duplicate requests" testId="text-live-duplicates" value={show(num(data.duplicateRequests))} warn={(num(data.duplicateRequests) ?? 0) > 0} />
                <Metric label="AI enrichments" testId="text-live-ai" value={show(num(data.aiEnrichments))} />
              </div>
              {issues === null ? (
                <p className="text-[11px] italic text-slate-500" data-testid="text-live-issues-unavailable">Issue list unavailable.</p>
              ) : issues.length > 0 ? (
                <ul className="space-y-1.5" data-testid="list-live-issues">
                  {issues.map((i, idx) => (
                    <li key={`${i.code}-${idx}`} className="rounded border-l-2 border-amber-400 bg-amber-50/70 px-2 py-1" data-testid={`live-issue-${i.code}`}>
                      <div className="text-[11px] font-semibold text-amber-950 break-words">{i.title}</div>
                      {i.detail && <div className="text-[10.5px] text-amber-900 break-words">{i.detail}</div>}
                      <div className="font-mono text-[9.5px] text-amber-700">{i.code}</div>
                    </li>
                  ))}
                </ul>
              ) : null}
              <p className="text-[10.5px] text-slate-500" data-testid="text-live-capabilities">
                {data.capabilities == null
                  ? "Runtime change support unavailable."
                  : data.capabilities.runtimeChanges
                    ? "Runtime changes for this job can be requested through the assistant chat."
                    : "This job does not accept runtime changes; the assistant can still diagnose."}
              </p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export type LiveScrapeAssistantProps = {
  jobId: string;
  onChanged?: () => void;
  onReviewJob?: (jobId: string) => void;
  disabled?: boolean;
};

/** Live diagnosis + chat for one running job. Parent must key this by jobId. */
export function LiveScrapeAssistant({ jobId, onChanged, onReviewJob, disabled }: LiveScrapeAssistantProps) {
  const [chatOpen, setChatOpen] = useState(false);
  return (
    <div className="space-y-2 min-w-0" data-testid={`live-scrape-assistant-${jobId}`}>
      <LiveDiagnosticsPanel jobId={jobId} />
      <button type="button" onClick={() => setChatOpen(o => !o)} aria-expanded={chatOpen}
        className="flex w-full items-center justify-between rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11.5px] font-medium text-slate-800 hover:bg-slate-50"
        data-testid="button-toggle-live-assistant">
        <span>{chatOpen ? "Hide scrape assistant" : "Ask the scrape assistant about this run"}</span>
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${chatOpen ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {chatOpen && (
        <ScrapePromptAssistant jobId={jobId} onChanged={onChanged} onReviewJob={onReviewJob} disabled={disabled} />
      )}
    </div>
  );
}

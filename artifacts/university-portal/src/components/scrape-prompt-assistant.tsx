import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { AlertCircle, ExternalLink, Loader2, MessageSquareText, Send, ShieldAlert, Wrench } from "lucide-react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { useAuth } from "@/context/auth";

export type AssistantSource = { url: string; title: string; excerpt: string };
export type AssistantAction = {
  id: string;
  kind: "fix_fields" | "repair_catalogue";
  label: string;
  courseCount: number;
  fields: string[];
  state: "proposed" | "starting" | "running" | "completed" | "error";
  jobId?: string;
  error?: string;
  progress?: { current: number; total: number; status: string };
};
export type AssistantMessage = {
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  sources?: AssistantSource[];
  actions?: AssistantAction[];
};
export type AssistantThread = {
  threadId: string | null;
  status: "idle" | "queued" | "researching" | "ready" | "error";
  phase: string;
  error: string | null;
  messages: AssistantMessage[];
};

export type ScrapePromptAssistantProps = {
  jobId: string;
  onChanged?: () => void;
  disabled?: boolean;
  /** Open the course review for a job (e.g. a catalogue-repair verification child). Parent must guard unsaved edits. */
  onReviewJob?: (jobId: string) => void;
};

export const ASSISTANT_POLL_MS = 2500;
const MIN_LEN = 5;
const MAX_LEN = 2000;
export const AUTO_CORRECTABLE_FIELDS = ["international_fee", "ielts_overall", "duration", "course_location"] as const;
const FIELD_LABELS: Record<string, string> = {
  international_fee: "International fee",
  ielts_overall: "IELTS overall and bands",
  duration: "Duration",
  course_location: "Course location",
};
const fieldLabel = (f: string) => FIELD_LABELS[f] ?? f.replace(/_/g, " ");
const EXAMPLES = [
  "Which courses from the official catalogue appear to be missing from this scrape?",
  "Why are international fees blank for some postgraduate courses?",
  "Check IELTS overall and band scores against the official English requirements page.",
];

export function safeSourceUrl(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

function isActiveThread(t: AssistantThread | null): boolean {
  if (!t) return false;
  if (t.status === "queued" || t.status === "researching") return true;
  return t.messages.some(m => m.actions?.some(a => a.state === "starting" || a.state === "running"));
}

/** Terminal actions that may have persisted data: completed, or errored after a batch started (jobId set). */
function completedActionIds(t: AssistantThread | null): Set<string> {
  const ids = new Set<string>();
  t?.messages.forEach(m => m.actions?.forEach(a => {
    if (a.state === "completed" || (a.state === "error" && a.jobId)) ids.add(`${a.id}:${a.state}`);
  }));
  return ids;
}

async function errorFor(res: Response): Promise<string> {
  if (res.status === 401 || res.status === 403) {
    return "You do not have permission to use the scrape assistant for this job.";
  }
  return getFetchErrorMessage(res);
}

export function ScrapePromptAssistant({ jobId, onChanged, disabled, onReviewJob }: ScrapePromptAssistantProps) {
  const { user } = useAuth();
  // History is private per staff member: every cache is keyed by identity + job.
  const identity = user ? `${user.id ?? ""}:${user.email}` : null;
  const viewKey = identity ? `${identity}\u0000${jobId}` : null;
  const [stored, setStored] = useState<{ key: string; thread: AssistantThread } | null>(null);
  // Render-time guard: a thread from another identity/job is never shown, even for one frame.
  const thread = stored && stored.key === viewKey ? stored.thread : null;
  const [loading, setLoading] = useState(true);
  const [loadErrorState, setLoadError] = useState<{ key: string | null; msg: string } | null>(null);
  const loadError = loadErrorState && loadErrorState.key === viewKey ? loadErrorState.msg : null;
  const [promptState, setPromptState] = useState<{ key: string | null; text: string }>({ key: null, text: "" });
  const prompt = promptState.key === viewKey ? promptState.text : "";
  const setPrompt = (text: string) => setPromptState({ key: viewKeyRef.current, text });
  const [submitting, setSubmitting] = useState(false);
  const [submitErrorState, setSubmitErrorState] = useState<{ key: string | null; msg: string } | null>(null);
  const submitError = submitErrorState && submitErrorState.key === viewKey ? submitErrorState.msg : null;
  const setSubmitError = (msg: string | null) => setSubmitErrorState(msg === null ? null : { key: viewKeyRef.current, msg });
  const [confirmingState, setConfirmingState] = useState<{ key: string | null; action: AssistantAction } | null>(null);
  const confirming = confirmingState && confirmingState.key === viewKey ? confirmingState.action : null;
  const setConfirming = (action: AssistantAction | null) => setConfirmingState(action ? { key: viewKeyRef.current, action } : null);
  const [actionBusy, setActionBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;
  const seenCompleted = useRef<Set<string> | null>(null);
  const mutateAbort = useRef<AbortController | null>(null);

  const base = `/api/scrape/jobs/${encodeURIComponent(jobId)}/assistant`;

  const accept = useCallback((forKey: string | null, t: AssistantThread) => {
    if (!forKey || viewKeyRef.current !== forKey) return;
    const done = completedActionIds(t);
    if (seenCompleted.current) {
      let changed = false;
      done.forEach(id => { if (!seenCompleted.current!.has(id)) changed = true; });
      if (changed) onChangedRef.current?.();
    }
    seenCompleted.current = done;
    setStored({ key: forKey, thread: t });
  }, []);

  // Reset on job or identity change and load history.
  useEffect(() => {
    setStored(null);
    setLoadError(null);
    setSubmitErrorState(null);
    setConfirmingState(null);
    setPromptState({ key: null, text: "" });
    setActionBusy(false);
    setSubmitting(false);
    seenCompleted.current = null;
    mutateAbort.current?.abort();
    if (!viewKey) { setLoading(false); return; }
    setLoading(true);
    const forKey = viewKey;
    const ctrl = new AbortController();
    fetch(base, { credentials: "include", signal: ctrl.signal })
      .then(async res => {
        if (!res.ok) throw new Error(await errorFor(res));
        const t = await readResponseJson<AssistantThread>(res);
        if (!t) throw new Error("Assistant returned an empty response.");
        accept(forKey, t);
      })
      .catch(err => {
        if (ctrl.signal.aborted) return;
        setLoadError({ key: forKey, msg: err instanceof Error ? err.message : String(err) });
      })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    return () => ctrl.abort();
  }, [base, viewKey, reloadKey, accept]);

  const active = isActiveThread(thread);

  // Poll while active.
  useEffect(() => {
    if (!active || !viewKey) return;
    const forKey = viewKey;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const res = await fetch(base, { credentials: "include", signal: ctrl.signal });
        if (!res.ok) throw new Error(await errorFor(res));
        const t = await readResponseJson<AssistantThread>(res);
        if (t && !ctrl.signal.aborted) accept(forKey, t);
      } catch (err) {
        if (ctrl.signal.aborted) return;
        setLoadError({ key: forKey, msg: err instanceof Error ? err.message : String(err) });
        return;
      }
      if (!ctrl.signal.aborted) timer = setTimeout(tick, ASSISTANT_POLL_MS);
    };
    timer = setTimeout(tick, ASSISTANT_POLL_MS);
    return () => { ctrl.abort(); if (timer) clearTimeout(timer); };
  }, [active, base, viewKey, accept]);

  useEffect(() => () => mutateAbort.current?.abort(), []);

  const trimmed = prompt.trim();
  const lengthOk = trimmed.length >= MIN_LEN && trimmed.length <= MAX_LEN;
  const busy = submitting || active;

  const submit = async () => {
    if (!lengthOk || busy || disabled || !viewKey) return;
    setSubmitting(true);
    setSubmitError(null);
    const ctrl = new AbortController();
    mutateAbort.current = ctrl;
    const forKey = viewKey;
    try {
      const res = await fetch(base, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: trimmed }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(await errorFor(res));
      const t = await readResponseJson<AssistantThread>(res);
      if (!t) throw new Error("Assistant returned an empty response.");
      accept(forKey, t);
      if (viewKeyRef.current === forKey) setPrompt("");
    } catch (err) {
      if (!ctrl.signal.aborted) setSubmitError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!ctrl.signal.aborted) setSubmitting(false);
    }
  };

  const runAction = async (action: AssistantAction) => {
    if (!viewKey) return;
    setActionBusy(true);
    setSubmitError(null);
    const ctrl = new AbortController();
    mutateAbort.current = ctrl;
    const forKey = viewKey;
    try {
      const res = await fetch(`${base}/actions/${encodeURIComponent(action.id)}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(await errorFor(res));
      const t = await readResponseJson<AssistantThread>(res);
      if (!t) throw new Error("Assistant returned an empty response.");
      accept(forKey, t);
    } catch (err) {
      if (!ctrl.signal.aborted) setSubmitError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!ctrl.signal.aborted) { setActionBusy(false); setConfirming(null); }
    }
  };

  return (
    <section
      className="rounded-lg border border-teal-200 bg-teal-50/40 overflow-hidden"
      aria-label="Scrape prompt assistant"
      data-testid="scrape-prompt-assistant"
    >
      <header className="flex items-center gap-2 px-3 py-2 bg-teal-50 border-b border-teal-200">
        <MessageSquareText className="w-3.5 h-3.5 text-teal-700" aria-hidden />
        <span className="text-xs font-semibold text-teal-900">Ask about this scrape</span>
        {active && (
          <span className="ml-auto flex items-center gap-1 text-[11px] text-teal-800" role="status" data-testid="status-assistant-active">
            <Loader2 className="w-3 h-3 animate-spin" aria-hidden />
            {thread?.phase || thread?.status || "Working"}
          </span>
        )}
      </header>
      <div className="p-3 space-y-3">
        <p className="text-[11px] text-gray-600">
          Investigates official university pages for this job only. Automatic corrections are limited to international fee, IELTS overall and bands, duration and course location; other topics (for example intakes) can be investigated but not auto-applied. Findings may be incomplete or uncertain; verify sources before relying on them. Nothing is applied or published without your confirmation.
        </p>

        {loading && (
          <div className="space-y-2" aria-busy="true" data-testid="assistant-loading">
            <div className="h-3 w-2/3 rounded bg-teal-100 animate-pulse" />
            <div className="h-3 w-1/2 rounded bg-teal-100 animate-pulse" />
          </div>
        )}

        {loadError && (
          <div role="alert" className="flex items-start gap-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700" data-testid="assistant-load-error">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden />
            <span className="flex-1">{loadError}</span>
            <Button size="sm" variant="outline" className="h-6 text-[11px]" onClick={() => setReloadKey(k => k + 1)} data-testid="button-assistant-retry">
              Retry
            </Button>
          </div>
        )}

        {thread?.status === "error" && thread.error && (
          <div role="alert" className="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700" data-testid="assistant-thread-error">
            Investigation failed: {thread.error}
          </div>
        )}

        {thread && thread.messages.length > 0 && (
          <ol className="space-y-2 max-h-96 overflow-y-auto" aria-live="polite" data-testid="assistant-history">
            {thread.messages.map((m, i) => (
              <li
                key={`${m.createdAt}-${i}`}
                className={`rounded-md p-2 text-xs ${m.role === "user" ? "bg-white border border-gray-200" : "bg-white border border-teal-200"}`}
                data-testid={`assistant-message-${i}`}
              >
                <div className="flex justify-between text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                  <span>{m.role === "user" ? "You" : "Assistant"}</span>
                  <time dateTime={m.createdAt}>{new Date(m.createdAt).toLocaleString()}</time>
                </div>
                {/* Rendered as a React text node: model output is never parsed as HTML. */}
                <p className="whitespace-pre-wrap break-words text-gray-800">{String(m.content ?? "")}</p>
                {!!m.sources?.length && (
                  <div className="mt-2 space-y-1.5">
                    <p className="text-[10px] font-semibold text-gray-600">Evidence</p>
                    {m.sources.map((s, si) => {
                      const href = safeSourceUrl(s.url);
                      return (
                        <blockquote key={si} className="border-l-2 border-teal-300 pl-2">
                          {href ? (
                            <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-teal-800 underline" data-testid={`link-source-${i}-${si}`}>
                              {s.title || href} <ExternalLink className="w-3 h-3" aria-hidden />
                            </a>
                          ) : (
                            <span className="text-gray-700">{s.title} (link unavailable)</span>
                          )}
                          {s.excerpt && <p className="text-gray-600 italic mt-0.5">"{s.excerpt}"</p>}
                        </blockquote>
                      );
                    })}
                  </div>
                )}
                {!!m.actions?.length && (
                  <div className="mt-2 space-y-1.5">
                    {m.actions.map(a => (
                      <div key={a.id} className="rounded border border-amber-200 bg-amber-50 p-2" data-testid={`assistant-action-${a.id}`}>
                        <div className="flex items-center gap-2">
                          <Wrench className="w-3 h-3 text-amber-700" aria-hidden />
                          <span className="font-medium text-amber-900 flex-1">{a.label}</span>
                          <span className="text-[10px] text-amber-800" data-testid={`status-action-${a.id}`}>{a.state}</span>
                        </div>
                        <p className="text-[11px] text-amber-900 mt-0.5">
                          {a.courseCount} course{a.courseCount === 1 ? "" : "s"}{a.fields.length ? ` · ${a.fields.map(fieldLabel).join(", ")}` : ""}
                        </p>
                        {a.progress && (a.state === "starting" || a.state === "running") && (
                          <div className="mt-1">
                            <div className="h-1.5 rounded bg-amber-100 overflow-hidden" role="progressbar" aria-valuemin={0} aria-valuemax={a.progress.total} aria-valuenow={a.progress.current}>
                              <div className="h-full bg-amber-500 origin-left transition-transform" style={{ transform: `scaleX(${a.progress.total ? a.progress.current / a.progress.total : 0})` }} />
                            </div>
                            <p className="text-[10px] text-amber-800 mt-0.5">{a.progress.current}/{a.progress.total} · {a.progress.status}</p>
                          </div>
                        )}
                        {a.state === "error" && (
                          <p role="alert" className="text-[11px] text-red-700 mt-1">
                            {a.error || "Action failed."}{a.jobId ? " Some courses may already have been updated; review data was refreshed." : ""}
                          </p>
                        )}
                        {a.state === "completed" && <p className="text-[11px] text-green-700 mt-1">Finished. Review data was refreshed; check values before approving.</p>}
                        {(a.state === "completed" || a.state === "error") && a.jobId && onReviewJob && (
                          <Button size="sm" variant="outline" className="mt-1.5 h-7 text-[11px]"
                            onClick={() => onReviewJob(a.jobId!)} data-testid={`button-open-review-${a.id}`}>
                            <ExternalLink className="w-3 h-3 mr-1" aria-hidden />
                            {a.kind === "repair_catalogue" ? "Open verification review" : "Open course review"}
                          </Button>
                        )}
                        {a.state === "proposed" && (
                          <Button size="sm" variant="outline" className="mt-1.5 h-7 text-[11px]" disabled={disabled || actionBusy}
                            onClick={() => setConfirming(a)} data-testid={`button-run-action-${a.id}`}>
                            Review and run
                          </Button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}

        {thread && thread.messages.length === 0 && !active && (
          <div className="space-y-1.5" data-testid="assistant-empty">
            <p className="text-[11px] text-gray-600">No questions yet. Try one of these:</p>
            <div className="flex flex-col gap-1">
              {EXAMPLES.map((ex, i) => (
                <button key={i} type="button" disabled={disabled}
                  onClick={() => setPrompt(ex)}
                  className="text-left text-[11px] rounded border border-teal-200 bg-white px-2 py-1 hover:bg-teal-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600 disabled:opacity-50"
                  data-testid={`button-example-${i}`}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {thread && (
          <form onSubmit={e => { e.preventDefault(); void submit(); }} className="space-y-1.5">
            <label htmlFor={`assistant-prompt-${jobId}`} className="sr-only">Question about this scrape</label>
            <Textarea
              id={`assistant-prompt-${jobId}`}
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void submit(); } }}
              placeholder="e.g. Why is IELTS missing for nursing courses?"
              maxLength={MAX_LEN}
              rows={3}
              disabled={disabled || busy}
              className="text-xs bg-white"
              data-testid="input-assistant-prompt"
            />
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-gray-500">{trimmed.length}/{MAX_LEN}{trimmed.length > 0 && trimmed.length < MIN_LEN ? " · at least 5 characters" : ""}</span>
              <Button type="submit" size="sm" className="ml-auto h-7 text-[11px] bg-teal-700 hover:bg-teal-800"
                disabled={disabled || busy || !lengthOk} data-testid="button-assistant-submit">
                {submitting || active ? <Loader2 className="w-3 h-3 mr-1 animate-spin" aria-hidden /> : <Send className="w-3 h-3 mr-1" aria-hidden />}
                {active ? "Investigating" : submitting ? "Sending" : "Ask"}
              </Button>
            </div>
          </form>
        )}

        {submitError && (
          <p role="alert" className="text-xs text-red-700" data-testid="assistant-submit-error">{submitError}</p>
        )}
      </div>

      <AlertDialog open={!!confirming} onOpenChange={open => { if (!open && !actionBusy) setConfirming(null); }}>
        <AlertDialogContent data-testid="dialog-confirm-action">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <ShieldAlert className="w-4 h-4 text-amber-600" aria-hidden /> Run "{confirming?.label}"?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm">
                <p>
                  {confirming?.kind === "repair_catalogue"
                    ? "This starts a bounded scraper repair for this job's catalogue."
                    : "This re-extracts the listed fields for staged review courses."}
                  {" "}It affects {confirming?.courseCount} course{confirming?.courseCount === 1 ? "" : "s"} in review data only.
                </p>
                {!!confirming?.fields.length && <p>Fields: {confirming.fields.map(fieldLabel).join(", ")}</p>}
                <p>It does not approve or publish any course. Results are not guaranteed to be accurate; review every changed value and its source afterwards.</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={actionBusy} data-testid="button-cancel-action">Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={actionBusy}
              onClick={e => { e.preventDefault(); if (confirming) void runAction(confirming); }}
              data-testid="button-confirm-action"
            >
              {actionBusy && <Loader2 className="w-3 h-3 mr-1 animate-spin" aria-hidden />}
              Confirm and start
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

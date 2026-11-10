import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { AssistantImportStatus } from "./assistant-import-status";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { AlertCircle, ArrowDown, ArrowUp, ExternalLink, FileText, Loader2, Paperclip, ShieldAlert, Sparkles, Wrench, X } from "lucide-react";
import { DocumentReviewView, type DocumentReview } from "./assistant-document-review";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { useAuth } from "@/context/auth";
import { safeSourceUrl } from "./assistant-safe-url";
import { AssistantMarkdown, CodeBlock } from "./assistant-markdown";
import { CatalogueComparisonView, type CatalogueComparison } from "./assistant-catalogue-comparison";

export { safeSourceUrl };

export type AssistantSource = { url: string; title: string; excerpt: string; page?: number; quoteId?: string };
export type AssistantAction = {
  id: string;
  kind: "runtime_tuning" | "fix_fields" | "repair_catalogue" | "review_courses" | "recover_catalogue" | "scan_catalogue" | "exclude_domestic" | "undo_exclusion" | "approve_course" | "staff_location" | "undo_staff_location" | "staff_edit" | "retry_failed" | "document_update";
  requiresConfirmation?: boolean;
  label: string;
  courseCount: number;
  publishedCount?: number;
  fields: string[];
  state: "proposed" | "starting" | "running" | "completed" | "error" | "superseded";
  resultSummary?: string;
  courseResults?: { url: string; status: string; message: string }[];
  jobId?: string;
  error?: string;
  progress?: { current: number; total: number; status: string };
  /** recover_catalogue only: server-owned not-recorded candidates (eligibility unverified). */
  courses?: { url: string; title: string }[];
};
export const RECOVER_MAX = 20;
export type AssistantActivity = {
  id: string;
  at: string;
  label: string;
  detail?: string;
  url?: string;
  status: "running" | "completed" | "error" | "info";
  durationMs?: number;
};
export type AssistantMessage = {
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  turnId?: string;
  sources?: AssistantSource[];
  actions?: AssistantAction[];
  /** Archived real tool events for the turn this reply concluded. */
  activity?: AssistantActivity[];
  comparison?: CatalogueComparison;
  documentReview?: DocumentReview;
};
export type AssistantRelatedReview = { jobId: string; courseName: string; status: string };
export type AssistantThread = {
  threadId: string | null;
  status: "idle" | "queued" | "researching" | "ready" | "error";
  phase: string;
  error: string | null;
  messages: AssistantMessage[];
  /** Real events of the current (in-flight or most recent) turn. */
  activity?: AssistantActivity[];
  turnId?: string | null;
  queuedAt?: string | null;
  startedAt?: string | null;
  updatedAt?: string | null;
  relatedReviews?: AssistantRelatedReview[];
  reviewCounts?: { providerPendingRows: number; thisJobPendingRows: number; pendingByJob: {jobId: string; pendingRows: number}[] };
};

export type ScrapePromptAssistantProps = {
  jobId: string;
  onChanged?: () => void;
  disabled?: boolean;
  /** Open the course review for a job (e.g. a catalogue-repair verification child). Parent must guard unsaved edits. */
  onReviewJob?: (jobId: string) => void;
};

export const ASSISTANT_POLL_MS = 1000;
export const MAX_DOCUMENT_BYTES = 2_000_000;
/** Kinds that must never run without an explicit confirmation dialog. */
export function actionNeedsConfirmation(a: AssistantAction): boolean {
  return !!a.requiresConfirmation || ["runtime_tuning", "staff_edit", "staff_location", "undo_staff_location", "document_update"].includes(a.kind);
}
export function validateDocument(file: File): string | null {
  const isPdf = file.type === "application/pdf" && /\.pdf$/i.test(file.name);
  if (!isPdf) return "Only PDF documents can be attached.";
  if (file.size <= 0) return "The selected PDF is empty.";
  if (file.size > MAX_DOCUMENT_BYTES) return "PDF is larger than the 2 MB (2,000,000 bytes) limit.";
  return null;
}
type Attachment = { key: string; seq: number; name: string; size: number; status: "uploading" | "ready" | "error"; token: string | null; error: string | null };
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
const NEAR_BOTTOM_PX = 96;

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${(ms / 1000).toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
function parseTime(v?: string | null): number | null {
  if (!v) return null;
  const n = Date.parse(v);
  return Number.isFinite(n) ? n : null;
}
const validEvents = (list: unknown): AssistantActivity[] =>
  Array.isArray(list) ? list.filter((a): a is AssistantActivity => !!a && typeof a.label === "string") : [];

/** Valid related reviews, excluding the current job and duplicate job IDs. */
export function relatedReviewsFor(t: AssistantThread | null, currentJobId: string): AssistantRelatedReview[] {
  const list = t && Array.isArray(t.relatedReviews) ? t.relatedReviews : [];
  const seen = new Set<string>([currentJobId]);
  const out: AssistantRelatedReview[] = [];
  for (const r of list) {
    if (!r || typeof r.jobId !== "string" || !r.jobId || seen.has(r.jobId)) continue;
    seen.add(r.jobId);
    out.push(r);
  }
  return out;
}

const PUBLIC_ACTION_KEYS = ["kind", "state", "label", "fields", "courseCount", "courses", "jobId", "error", "progress"] as const;
/** JSON of the public action fields actually supplied by the server; underscore/private keys are never included. */
export function operationDetailsJson(a: AssistantAction): string {
  const out: Record<string, unknown> = {};
  const src = a as unknown as Record<string, unknown>;
  for (const k of PUBLIC_ACTION_KEYS) if (src[k] !== undefined && src[k] !== null) out[k] = src[k];
  return JSON.stringify(out, null, 2);
}

/** Current-turn events not already archived onto a message (deduped by turn and event id). */
export function currentTurnEvents(t: AssistantThread | null): AssistantActivity[] {
  if (!t) return [];
  const events = validEvents(t.activity);
  if (!events.length) return [];
  if (t.turnId && t.messages.some(m => m.role === "assistant" && m.turnId === t.turnId && validEvents(m.activity).length)) return [];
  const archived = new Set<string>();
  t.messages.forEach(m => validEvents(m.activity).forEach(a => a.id && archived.add(a.id)));
  return events.filter(a => !a.id || !archived.has(a.id));
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

const STATUS_TEXT: Record<AssistantActivity["status"], string> = {
  running: "In progress", completed: "Done", error: "Failed", info: "Note",
};

function EventRow({ a, idx }: { a: AssistantActivity; idx: number }) {
  const key = a.id || String(idx);
  const href = a.url ? safeSourceUrl(a.url) : null;
  const detail = a.detail ? String(a.detail) : "";
  const tone = a.status === "error" ? "text-red-700" : a.status === "running" ? "text-teal-900" : "text-slate-700";
  return (
    <li className="relative flex gap-2.5 pl-0.5" data-testid={`activity-item-${key}`} data-status={a.status}>
      <span className="relative z-10 mt-[5px] flex h-3 w-3 shrink-0 items-center justify-center rounded-full bg-[#f6f4ee]" aria-hidden>
        {a.status === "running"
          ? <Loader2 className="h-3 w-3 text-teal-700 motion-safe:animate-spin" />
          : <span className={`h-1.5 w-1.5 rounded-full ${a.status === "error" ? "bg-red-600" : a.status === "completed" ? "bg-teal-700" : "bg-slate-400"}`} />}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex items-baseline gap-2 text-[12.5px]">
          <span className={`min-w-0 flex-1 break-words ${tone}`}>
            <span className="sr-only">{STATUS_TEXT[a.status] ?? "Note"}: </span>{a.label}
          </span>
          {a.status === "error" && <span className="shrink-0 text-[11px] font-medium text-red-700">Failed</span>}
          {typeof a.durationMs === "number" && (
            <span className="shrink-0 font-mono text-[11px] tabular-nums text-slate-500" data-testid={`text-activity-duration-${key}`}>{formatDuration(a.durationMs)}</span>
          )}
        </div>
        {a.url && (href ? (
          <a href={href} target="_blank" rel="noopener noreferrer nofollow"
            className="inline-flex max-w-full items-center gap-1 font-mono text-[11px] text-teal-800 underline decoration-teal-300 underline-offset-2 hover:text-teal-950"
            data-testid={`link-activity-${key}`}>
            <span className="truncate">{href}</span><ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
          </a>
        ) : <span className="text-[11px] text-slate-500">(link unavailable)</span>)}
        {detail && <p className={`whitespace-pre-wrap break-words text-[12px] ${a.status === "error" ? "text-red-700" : "text-slate-600"}`}>{detail}</p>}
      </div>
    </li>
  );
}

function EventList({ events, testId }: { events: AssistantActivity[]; testId: string }) {
  if (!events.length) return null;
  return (
    <ol className="relative space-y-2 before:absolute before:bottom-2 before:left-[7px] before:top-2 before:w-px before:bg-teal-200" aria-label="Assistant tool activity" data-testid={testId}>
      {events.map((a, i) => <EventRow key={a.id || i} a={a} idx={i} />)}
    </ol>
  );
}

export function ScrapePromptAssistant({ jobId, onChanged, disabled, onReviewJob }: ScrapePromptAssistantProps) {
  const { user } = useAuth();
  // History is private per staff member: every cache is keyed by identity + job.
  const identity = user ? `${user.id ?? ""}:${user.email}` : null;
  const viewKey = identity ? `${identity}\u0000${jobId}` : null;
  const [importState, setImportState] = useState<{key: string | null; paused: boolean | null} | null>(null);
  const updateImportStatus = useCallback((paused: boolean | null) => setImportState({key: viewKey, paused}), [viewKey]);
  const importsBlocked = importState?.key !== viewKey || importState?.paused !== false;
  const isImport = (a: AssistantAction) => ["scan_catalogue", "recover_catalogue", "review_courses", "repair_catalogue", "retry_failed"].includes(a.kind);
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
  const [selectedUrls, setSelectedUrls] = useState<string[]>([]);
  const recoverCandidates = (a: AssistantAction | null) =>
    a?.kind === "recover_catalogue" && Array.isArray(a.courses) ? a.courses.filter(c => c && typeof c.url === "string" && c.url) : [];
  const openConfirm = (a: AssistantAction) => {
    setSelectedUrls(recoverCandidates(a).slice(0, RECOVER_MAX).map(c => c.url));
    setConfirming(a);
  };
  const toggleUrl = (url: string) => setSelectedUrls(cur =>
    cur.includes(url) ? cur.filter(u => u !== url) : cur.length >= RECOVER_MAX ? cur : [...cur, url]);
  const [reloadKey, setReloadKey] = useState(0);

  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;
  const seenCompleted = useRef<Set<string> | null>(null);
  const mutateAbort = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const nearBottom = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const uploadSeq = useRef(0);
  const [attachState, setAttachState] = useState<Attachment | null>(null);
  const attachment = attachState && attachState.key === viewKey ? attachState : null;

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
    setShowJump(false);
    setAttachState(null);
    uploadSeq.current++;
    nearBottom.current = true;
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
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);

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

  const liveEvents = currentTurnEvents(thread);

  // Follow new content only when the reader is already near the bottom.
  const contentSig = thread
    ? `${thread.messages.length}|${thread.status}|${validEvents(thread.activity).map(a => `${a.id}:${a.status}`).join(",")}`
    : "";
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !contentSig) return;
    if (nearBottom.current) { el.scrollTop = el.scrollHeight; setShowJump(false); }
    else setShowJump(true);
  }, [contentSig]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
    if (nearBottom.current) setShowJump(false);
  };
  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (!el) return;
    const reduce = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight, behavior: reduce ? "auto" : "smooth" });
    else el.scrollTop = el.scrollHeight;
    nearBottom.current = true;
    setShowJump(false);
  };

  // Auto-grow composer.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [prompt]);

  const clearAttachment = () => { uploadSeq.current++; setAttachState(null); if (fileRef.current) fileRef.current.value = ""; };
  const attach = async (file: File) => {
    if (!viewKey) return;
    const forKey = viewKey;
    const seq = ++uploadSeq.current;
    const baseAtt = { key: forKey, seq, name: file.name, size: file.size, token: null };
    const invalid = validateDocument(file);
    if (invalid) { setAttachState({ ...baseAtt, status: "error", error: invalid }); return; }
    setAttachState({ ...baseAtt, status: "uploading", error: null });
    const current = () => uploadSeq.current === seq && viewKeyRef.current === forKey;
    try {
      const res = await fetch(`${base}/documents/upload-url`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size, contentType: "application/pdf" }),
      });
      if (!res.ok) throw new Error(await errorFor(res));
      const grant = await readResponseJson<{ transport?: string; uploadUrl?: string; fields?: Record<string, string>; documentToken: string }>(res);
      if (!grant?.documentToken || (grant.transport !== "portal" && !grant.uploadUrl)) throw new Error("Upload could not be prepared.");
      if (!current()) return;
      let up: Response;
      if (grant.transport === "portal") {
        up = await fetch(`${base}/documents/content`, {
          method: "POST", credentials: "include", body: file,
          headers: { "Content-Type": "application/pdf", "X-Document-Token": grant.documentToken },
        });
      } else {
        const form = new FormData();
        Object.entries(grant.fields ?? {}).forEach(([k, v]) => form.append(k, String(v)));
        form.append("file", file);
        up = await fetch(grant.uploadUrl!, { method: "POST", body: form });
      }
      if (!up.ok) throw new Error(`Document upload failed (${up.status}).`);
      if (!current()) return;
      setAttachState({ ...baseAtt, status: "ready", token: grant.documentToken, error: null });
    } catch (err) {
      if (current()) setAttachState({ ...baseAtt, status: "error", error: err instanceof Error ? err.message : String(err) });
    }
  };

  const trimmed = prompt.trim();
  const lengthOk = trimmed.length > 0;
  const busy = submitting || active;
  const attachBlocks = !!attachment && attachment.status !== "ready";

  const submit = async () => {
    if (!lengthOk || busy || disabled || !viewKey || attachBlocks) return;
    const sentSeq = attachment?.seq;
    const documentToken = attachment?.token ?? undefined;
    setSubmitting(true);
    setSubmitError(null);
    nearBottom.current = true;
    const ctrl = new AbortController();
    mutateAbort.current = ctrl;
    const forKey = viewKey;
    try {
      const res = await fetch(base, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(documentToken ? { prompt: trimmed, documentToken } : { prompt: trimmed }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(await errorFor(res));
      const t = await readResponseJson<AssistantThread>(res);
      if (!t) throw new Error("Assistant returned an empty response.");
      accept(forKey, t);
      if (viewKeyRef.current === forKey) {
        setPrompt("");
        if (sentSeq !== undefined && uploadSeq.current === sentSeq) clearAttachment();
      }
    } catch (err) {
      // Draft is kept so the user can retry.
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
        body: JSON.stringify(action.kind === "recover_catalogue" ? { confirm: true, courseUrls: recoverCandidates(action).slice(0, RECOVER_MAX).map(c => c.url) } : { confirm: true }),
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

  const queued = parseTime(thread?.queuedAt);
  const started = parseTime(thread?.startedAt);
  let elapsed = "";
  if (active && thread?.status === "queued" && queued !== null) elapsed = `Waiting ${formatDuration(Math.max(0, now - queued))}`;
  else if (active && started !== null) elapsed = `Researching ${formatDuration(Math.max(0, now - started))}`;
  const runningEvent = [...liveEvents].reverse().find(a => a.status === "running");
  const statusLine = runningEvent ? runningEvent.label
    : thread?.status === "queued" ? "Queued, waiting for a research slot"
    : (thread?.phase || "Working");
  const related = onReviewJob ? relatedReviewsFor(thread, jobId) : [];
  const hasConversation = !!thread && (thread.messages.length > 0 || liveEvents.length > 0 || active);

  const renderActions = (m: AssistantMessage) => !!m.actions?.length && (
    <div className="mt-3 space-y-2">
      {m.actions.map(a => (
        <div key={a.id} className="rounded-xl border border-amber-200 bg-amber-50/80 p-3" data-testid={`assistant-action-${a.id}`}>
          <div className="flex items-center gap-2 text-[13px]">
            <Wrench className="h-3.5 w-3.5 text-amber-700" aria-hidden />
            <span className="flex-1 font-medium text-amber-950">{a.label}</span>
            <span className="rounded-full bg-amber-100 px-2 py-0.5 font-mono text-[10.5px] text-amber-900" data-testid={`status-action-${a.id}`}>{a.state}</span>
          </div>
          <p className="mt-1 text-[12px] text-amber-900">
            {a.courseCount} course{a.courseCount === 1 ? "" : "s"}{a.fields.length ? ` · ${a.fields.map(fieldLabel).join(", ")}` : ""}
          </p>
          {a.progress && (a.state === "starting" || a.state === "running") && (
            <div className="mt-2">
              <div className="h-1.5 overflow-hidden rounded-full bg-amber-100" role="progressbar" aria-valuemin={0} aria-valuemax={a.progress.total} aria-valuenow={a.progress.current}>
                <div className="h-full origin-left bg-amber-500 motion-safe:transition-transform" style={{ transform: `scaleX(${a.progress.total ? a.progress.current / a.progress.total : 0})` }} />
              </div>
              <p className="mt-1 font-mono text-[11px] text-amber-800">{a.progress.current}/{a.progress.total} · {a.progress.status}</p>
            </div>
          )}
          {a.state === "error" && (
            <p role="alert" className="mt-1.5 text-[12px] text-red-700">
              {a.error || "Action failed."}{a.jobId && !a.courseResults && (a.kind !== "repair_catalogue" || a.jobId !== jobId) ? " Some courses may already have been updated; review data was refreshed." : ""}
            </p>
          )}
          {a.state === "completed" && <p className="mt-1.5 text-[12px] text-green-800">{a.resultSummary || "Job finished. Updated pending data was refreshed. Nothing was published."}</p>}
          {!!a.courseResults?.length && (
            <ul className="mt-2 space-y-2 text-[12px] text-amber-950" data-testid={`course-results-${a.id}`}>
              {a.courseResults.map(result => (
                <li key={result.url}>
                  <span className="block break-all font-medium">{result.url}</span>
                  <span>{result.message}</span>
                </li>
              ))}
            </ul>
          )}
          {(a.state === "completed" || a.state === "error") && a.jobId && onReviewJob && (a.kind !== "repair_catalogue" || a.jobId !== jobId) && (
            <Button size="sm" variant="outline" className="mt-2 h-8 text-[12px]"
              onClick={() => onReviewJob(a.jobId!)} data-testid={`button-open-review-${a.id}`}>
              <ExternalLink className="mr-1 h-3 w-3" aria-hidden />
              {a.kind === "repair_catalogue" ? "Open verification review" : "Open course review"}
            </Button>
          )}
          <details className="group mt-2" data-testid={`details-operation-${a.id}`}>
            <summary className="cursor-pointer select-none text-[11.5px] font-medium text-amber-900 hover:text-amber-950 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600">Operation details</summary>
            <div className="mt-1.5"><CodeBlock code={operationDetailsJson(a)} lang="json" testId={`code-operation-${a.id}`} /></div>
          </details>
          {a.state === "proposed" && (
            <Button size="sm" variant="outline" className="mt-2 h-8 border-amber-300 text-[12px]" disabled={disabled || actionBusy || (isImport(a) && importsBlocked)}
              onClick={() => actionNeedsConfirmation(a) ? openConfirm(a) : void runAction(a)} data-testid={`button-run-action-${a.id}`}>
              {isImport(a) && importsBlocked ? "Imports paused or status unavailable" : a.kind === "undo_staff_location" ? "Undo locations" : "Run now"}
            </Button>
          )}
        </div>
      ))}
    </div>
  );

  return (
    <section
      className="flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-[#f6f4ee] shadow-[0_1px_0_rgba(15,23,42,0.04),0_8px_24px_-12px_rgba(15,60,60,0.18)]"
      aria-label="Scrape assistant"
      data-testid="scrape-prompt-assistant"
    >
      <header className="flex items-center gap-2.5 border-b border-slate-200/80 bg-[#fbfaf6] px-4 py-3">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-teal-800 text-teal-50" aria-hidden>
          <Sparkles className="h-3.5 w-3.5" />
        </span>
        <div className="min-w-0">
          <h3 className="text-[14px] font-semibold leading-tight text-slate-900">Scrape assistant</h3>
          <p className="truncate text-[11.5px] text-slate-500">Runs research and pending-record repairs automatically. Reports results here. Never publishes automatically.</p>
        </div>
        {active && (
          <span className="ml-auto flex shrink-0 items-center gap-1.5 rounded-full bg-teal-100 px-2.5 py-1 text-[11.5px] font-medium text-teal-900" data-testid="status-assistant-active">
            <Loader2 className="h-3 w-3 motion-safe:animate-spin" aria-hidden />
            {thread?.status === "queued" ? "Queued" : "Working"}
          </span>
        )}
      </header>
      {viewKey && <AssistantImportStatus key={viewKey} base={base} onStatus={updateImportStatus} />}

      <div className="relative">
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="max-h-[min(70vh,640px)] min-h-[200px] overflow-y-auto px-4 py-5 sm:px-6"
          data-testid="assistant-scroll"
        >
          {loading && (
            <div className="space-y-3" aria-busy="true" data-testid="assistant-loading">
              <div className="ml-auto h-9 w-1/2 rounded-2xl bg-slate-200/70 motion-safe:animate-pulse" />
              <div className="h-3 w-2/3 rounded bg-slate-200/70 motion-safe:animate-pulse" />
              <div className="h-3 w-1/2 rounded bg-slate-200/70 motion-safe:animate-pulse" />
            </div>
          )}

          {loadError && (
            <div role="alert" className="mb-4 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3 text-[13px] text-red-800" data-testid="assistant-load-error">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span className="flex-1">{loadError}</span>
              <Button size="sm" variant="outline" className="h-7 text-[12px]" onClick={() => setReloadKey(k => k + 1)} data-testid="button-assistant-retry">
                Retry
              </Button>
            </div>
          )}

          {thread?.reviewCounts && (
            <div className="mb-3 text-xs text-muted-foreground" data-testid="assistant-review-counts">
              Provider total: {thread.reviewCounts.providerPendingRows} pending rows across all reviews.
              {" "}This job: {thread.reviewCounts.thisJobPendingRows} pending rows.
            </div>
          )}
          {thread && !hasConversation && (
            <div className="mx-auto max-w-lg py-4 text-center" data-testid="assistant-empty">
              <p className="text-[15px] font-semibold text-slate-900">What should I look into?</p>
              <p className="mt-1 text-[12.5px] text-slate-600">
                Name an existing course to edit its fields without creating another course. Location values you supply are saved as staff-provided, not as verified website facts. “Add this in all courses” repeats the last location edit across this provider without discovery. Published records and changes over 10 rows require confirmation.
              </p>
              <div className="mt-4 flex flex-col gap-2 text-left">
                {EXAMPLES.map((ex, i) => (
                  <button key={i} type="button" disabled={disabled}
                    onClick={() => { setPrompt(ex); inputRef.current?.focus(); }}
                    className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12.5px] text-slate-700 hover:border-teal-300 hover:bg-teal-50/60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600 disabled:opacity-50 motion-safe:transition-colors"
                    data-testid={`button-example-${i}`}>
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          )}

          {thread && hasConversation && (
            <ol className="space-y-5" data-testid="assistant-history">
              {thread.messages.map((m, i) => {
                const archived = m.role === "assistant" ? validEvents(m.activity) : [];
                return (
                  <Fragment key={`${m.createdAt}-${i}`}>
                    {archived.length > 0 && (
                      <li className="pl-1" data-testid={`assistant-turn-activity-${i}`}>
                        <EventList events={archived} testId={`list-activity-${i}`} />
                      </li>
                    )}
                    {m.role === "user" ? (
                      <li className="flex justify-end" data-testid={`assistant-message-${i}`} data-role="user">
                        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-teal-900 px-3.5 py-2.5 text-[13.5px] leading-relaxed text-teal-50">
                          <span className="sr-only">You said: </span>
                          <p className="whitespace-pre-wrap break-words">{String(m.content ?? "")}</p>
                          <time dateTime={m.createdAt} className="mt-1 block text-right text-[10.5px] text-teal-200/80">{new Date(m.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>
                        </div>
                      </li>
                    ) : (
                      <li className="flex gap-3" data-testid={`assistant-message-${i}`} data-role="assistant">
                        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-teal-800 text-teal-50" aria-hidden>
                          <Sparkles className="h-3 w-3" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="mb-1 flex items-baseline gap-2 text-[11px] text-slate-500">
                            <span className="font-medium text-slate-700">Assistant</span>
                            <time dateTime={m.createdAt}>{new Date(m.createdAt).toLocaleString()}</time>
                          </div>
                          {/* Safe markdown: rendered as React text nodes, never as HTML. */}
                          <AssistantMarkdown content={String(m.content ?? "")} />
                          {!!m.sources?.length && (
                            <div className="mt-3 space-y-2">
                              <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">Evidence</p>
                              {m.sources.map((s, si) => {
                                const sourceHref = safeSourceUrl(s.url);
                                const page = typeof s.page === "number" && Number.isSafeInteger(s.page) && s.page > 0 ? s.page : null;
                                const href = sourceHref && page ? `${sourceHref.split("#")[0]}#page=${page}` : sourceHref;
                                return (
                                  <blockquote key={si} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-[12.5px]">
                                    {href ? (
                                      <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 font-medium text-teal-800 underline underline-offset-2" data-testid={`link-source-${i}-${si}`}>
                                        {s.title || sourceHref}{page && <span> · Page {page}</span>} <ExternalLink className="h-3 w-3" aria-hidden />
                                      </a>
                                    ) : (
                                      <span className="text-slate-700">{s.title}{page && ` · Page ${page}`} (link unavailable)</span>
                                    )}
                                    {s.excerpt && <p className={page
                                      ? "mt-1 overflow-x-auto whitespace-pre font-mono text-slate-600"
                                      : "mt-1 italic text-slate-600"}>{page ? s.excerpt : `"${s.excerpt}"`}</p>}
                                  </blockquote>
                                );
                              })}
                            </div>
                          )}
                          {m.comparison && typeof m.comparison === "object" && (
                            <CatalogueComparisonView comparison={m.comparison} onReviewJob={onReviewJob} testId={`assistant-comparison-${i}`} />
                          )}
                          {m.documentReview && typeof m.documentReview === "object" && (
                            <DocumentReviewView review={m.documentReview} testId={`assistant-document-review-${i}`} />
                          )}
                          {renderActions(m)}
                        </div>
                      </li>
                    )}
                  </Fragment>
                );
              })}

              {liveEvents.length > 0 && (
                <li className="pl-1" data-testid="assistant-turn-activity-current">
                  <EventList events={liveEvents} testId="list-activity" />
                </li>
              )}

              {active && (
                <li className="flex items-center gap-2 pl-1 text-[12.5px] text-teal-900" data-testid="assistant-turn-working">
                  <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" aria-hidden />
                  <span role="status" aria-live="polite" data-testid="text-activity-current">{statusLine}</span>
                  {elapsed && <span className="ml-auto font-mono text-[11.5px] tabular-nums text-slate-500" data-testid="text-activity-elapsed">{elapsed}</span>}
                </li>
              )}

              {thread.status === "error" && thread.error && (
                <li role="alert" className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 p-3 text-[13px] text-red-800" data-testid="assistant-thread-error">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                  <span>Investigation failed: {thread.error}</span>
                </li>
              )}
            </ol>
          )}
        </div>

        {showJump && (
          <button type="button" onClick={jumpToLatest}
            className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-medium text-slate-700 shadow-md hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600"
            data-testid="button-jump-latest">
            <ArrowDown className="h-3.5 w-3.5" aria-hidden /> Jump to latest
          </button>
        )}
      </div>

      {related.length > 0 && (
        <div className="mx-4 mb-3 space-y-2 rounded-xl border border-teal-200 bg-white p-3 sm:mx-6" data-testid="assistant-related-reviews">
          <p className="text-[12.5px] font-semibold text-teal-900">Available course review</p>
          <p className="text-[11.5px] text-slate-600">A pending review in another job of this university includes a course you asked about. Opening it does not change earlier results here.</p>
          {related.map(r => (
            <div key={r.jobId} className="flex items-center gap-2 text-[12.5px]" data-testid={`related-review-${r.jobId}`}>
              <span className="min-w-0 flex-1 break-words text-slate-800">{String(r.courseName || "Course")}</span>
              <span className="font-mono text-[11px] text-slate-500">{String(r.status ?? "")}</span>
              <Button size="sm" variant="outline" className="h-8 text-[12px]"
                onClick={() => onReviewJob!(r.jobId)} data-testid={`button-open-related-review-${r.jobId}`}>
                <ExternalLink className="mr-1 h-3 w-3" aria-hidden /> Open review
              </Button>
            </div>
          ))}
        </div>
      )}

      {thread && (
        <form onSubmit={e => { e.preventDefault(); void submit(); }} className="border-t border-slate-200/80 bg-[#fbfaf6] px-4 py-3 sm:px-6">
          <label htmlFor={`assistant-prompt-${jobId}`} className="sr-only">Message the scrape assistant</label>
          {attachment && (
            <div className={`mb-2 flex items-center gap-2 rounded-xl border px-3 py-1.5 text-[12px] ${attachment.status === "error" ? "border-red-200 bg-red-50 text-red-800" : "border-teal-200 bg-teal-50/60 text-teal-950"}`} data-testid="assistant-attachment" data-status={attachment.status}>
              {attachment.status === "uploading" ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" aria-hidden /> : <FileText className="h-3.5 w-3.5" aria-hidden />}
              <span className="min-w-0 flex-1 truncate">{attachment.name}</span>
              <span className="shrink-0 font-mono text-[11px]" role="status" data-testid="text-attachment-status">
                {attachment.status === "uploading" ? "Uploading" : attachment.status === "ready" ? `${(attachment.size / 1_000_000).toFixed(2)} MB · Ready` : attachment.error}
              </span>
              <button type="button" onClick={clearAttachment} aria-label="Remove attachment" disabled={submitting}
                className="rounded p-0.5 hover:bg-black/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600" data-testid="button-remove-attachment">
                <X className="h-3.5 w-3.5" aria-hidden />
              </button>
            </div>
          )}
          <input ref={fileRef} type="file" accept="application/pdf,.pdf" className="hidden" data-testid="input-assistant-document"
            onChange={e => { const f = e.target.files?.[0]; if (f) void attach(f); e.target.value = ""; }} />
          <div className="flex items-end gap-2 rounded-2xl border border-slate-300 bg-white px-3 py-2 focus-within:border-teal-600 focus-within:ring-2 focus-within:ring-teal-600/20 motion-safe:transition-shadow">
            <textarea
              ref={inputRef}
              id={`assistant-prompt-${jobId}`}
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => {
                if (e.key !== "Enter" || e.shiftKey) return;
                if (e.nativeEvent.isComposing || e.keyCode === 229) return;
                e.preventDefault();
                void submit();
              }}
              placeholder={active ? "The assistant is working. You can draft your next message." : "Ask about missing courses, fees, IELTS, durations..."}
              rows={1}
              disabled={disabled}
              aria-describedby={`assistant-hint-${jobId}`}
              className="max-h-60 min-h-[24px] flex-1 resize-none bg-transparent py-1 text-[13.5px] leading-relaxed text-slate-900 placeholder:text-slate-400 focus:outline-none disabled:opacity-60"
              data-testid="input-assistant-prompt"
            />
            <Button type="button" size="icon" variant="ghost" aria-label="Attach PDF (max 2 MB)"
              className="h-8 w-8 shrink-0 rounded-full text-teal-800 hover:bg-teal-50"
              disabled={disabled || submitting} onClick={() => fileRef.current?.click()} data-testid="button-attach-document">
              <Paperclip className="h-4 w-4" aria-hidden />
            </Button>
            <Button type="submit" size="icon" aria-label={active ? "Assistant is working" : submitting ? "Sending" : "Send message"}
              className="h-8 w-8 shrink-0 rounded-full bg-teal-800 text-teal-50 hover:bg-teal-900 disabled:bg-slate-300"
              disabled={disabled || busy || !lengthOk || attachBlocks} data-testid="button-assistant-submit">
              {busy ? <Loader2 className="h-4 w-4 motion-safe:animate-spin" aria-hidden /> : <ArrowUp className="h-4 w-4" aria-hidden />}
            </Button>
          </div>
          <p id={`assistant-hint-${jobId}`} className="mt-1.5 text-[11px] text-slate-500">Enter to send, Shift+Enter for a new line. Attach one PDF up to 2 MB.</p>
          {submitError && (
            <p role="alert" className="mt-1 text-[12px] text-red-700" data-testid="assistant-submit-error">{submitError}</p>
          )}
        </form>
      )}

      <AlertDialog open={!!confirming} onOpenChange={open => { if (!open && !actionBusy) setConfirming(null); }}>
        <AlertDialogContent data-testid="dialog-confirm-action">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-amber-600" aria-hidden /> Run "{confirming?.label}"?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm">
                <p>
                  {confirming?.kind === "runtime_tuning"
                    ? "This requests the displayed old/new settings for this job only. The owning worker must acknowledge before this is reported as applied. Active requests finish normally. Provider throttles and fleet limits remain in place. Nothing is restarted, discarded or published."
                    : confirming?.kind === "undo_staff_location"
                    ? "This restores the prior audited locations and provenance. It refuses to overwrite later edits. No page fetch or discovery runs."
                    : confirming?.kind === "staff_location" || confirming?.kind === "staff_edit"
                    ? "This saves the supplied values on the exact records listed in the chat, with staff-provided provenance and an audit log. It does not independently verify them, fetch pages or run catalogue discovery."
                    : confirming?.kind === "repair_catalogue"
                    ? "This starts a bounded scraper repair for this job's catalogue."
                    : confirming?.kind === "recover_catalogue"
                    ? "This checks the selected not-recorded catalogue links and scrapes any that pass the usual eligibility checks into review data. Some may turn out not to be eligible courses."
                    : confirming?.kind === "scan_catalogue"
                    ? "This runs the existing configured full catalogue discovery for this university, bypassing cached pages, as a review-only run. It does not delete existing review rows. It may take considerably longer than the quick check. It cannot guarantee that every course on the website is discoverable, and it does not approve, delete or publish anything."
                    : confirming?.kind === "review_courses"
                    ? "This fetches the requested official course URLs into a fresh pending review. It does not rename the course in the current review or duplicate a published course."
                    : confirming?.kind === "document_update"
                    ? "This applies the document-sourced before/after values listed in the chat to the matched records. Each value cites the quoted PDF page."
                    : confirming?.kind === "retry_failed"
                      ? "This retries only the selected, settled failures. Attempt limits and backoff are listed in the chat. Successful URLs are not replayed."
                    : "This re-extracts the listed fields for staged review courses."}
                  {confirming?.kind === "runtime_tuning" ? ""
                    : confirming?.kind === "staff_location" || confirming?.kind === "undo_staff_location" || confirming?.kind === "staff_edit" || confirming?.kind === "document_update" ? ` It changes ${confirming.courseCount} listed records, including ${confirming.publishedCount ?? 0} published records.`
                    : confirming?.kind === "scan_catalogue" ? " Results land in review data only."
                    : confirming?.kind === "recover_catalogue" ? ` It checks ${selectedUrls.length} selected link${selectedUrls.length === 1 ? "" : "s"}; results land in review data only.`
                    : ` It affects ${confirming?.courseCount} course${confirming?.courseCount === 1 ? "" : "s"} in review data only.`}
                </p>
                {confirming?.kind === "recover_catalogue" && (
                  <fieldset className="space-y-1.5" data-testid="fieldset-recover-courses">
                    <legend className="text-[12.5px] font-medium text-slate-800">
                      Not recorded candidates: select up to {RECOVER_MAX} <span className="font-mono" data-testid="text-recover-count">({selectedUrls.length}/{Math.min(RECOVER_MAX, recoverCandidates(confirming).length)} selected)</span>
                    </legend>
                    {recoverCandidates(confirming).length === 0 && <p className="text-[12px] text-red-700">No candidate links were supplied.</p>}
                    <div className="max-h-56 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
                      {recoverCandidates(confirming).map((c, ci) => {
                        const checked = selectedUrls.includes(c.url);
                        const href = safeSourceUrl(c.url);
                        return (
                          <label key={c.url} className="flex items-start gap-2 text-[12.5px]">
                            <input type="checkbox" className="mt-0.5 accent-teal-700" checked={checked}
                              disabled={actionBusy || (!checked && selectedUrls.length >= RECOVER_MAX)}
                              onChange={() => toggleUrl(c.url)} data-testid={`checkbox-recover-${ci}`} />
                            <span className="min-w-0">
                              <span className="block text-slate-800">{String(c.title || c.url)}</span>
                              <span className="block truncate font-mono text-[11px] text-slate-500">{href ?? "(link unavailable)"}</span>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                    {selectedUrls.length === 0 && recoverCandidates(confirming).length > 0 && <p className="text-[12px] text-red-700" data-testid="text-recover-empty">Select at least one course.</p>}
                  </fieldset>
                )}
                {!!confirming?.fields.length && <p>Fields: {confirming.fields.map(fieldLabel).join(", ")}</p>}
                <p>It does not approve or publish any course. Results are not guaranteed to be accurate; review every changed value and its source afterwards.</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={actionBusy} data-testid="button-cancel-action">Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={actionBusy || (confirming?.kind === "recover_catalogue" && (selectedUrls.length < 1 || selectedUrls.length > RECOVER_MAX))}
              onClick={e => { e.preventDefault(); if (confirming) void runAction(confirming); }}
              data-testid="button-confirm-action"
            >
              {actionBusy && <Loader2 className="mr-1 h-3 w-3 motion-safe:animate-spin" aria-hidden />}
              {confirming?.kind === "recover_catalogue" ? "Check selected courses" : "Confirm and start"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

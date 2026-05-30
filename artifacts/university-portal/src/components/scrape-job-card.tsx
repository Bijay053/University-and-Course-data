import { useState, useRef, useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Play, StopCircle, Loader2, Globe, CheckCircle2, AlertCircle,
  ChevronsUpDown, Search, Eye, RefreshCw, ChevronDown, X, Zap, TrendingUp,
} from "lucide-react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { CountrySelect } from "@/components/country-select";

// ── Types ────────────────────────────────────────────────────────────────────
type UniOption = { id: number; name: string; scrapeUrl?: string | null; feePageUrl?: string | null; requirementsPageUrl?: string | null };
type ScrapeLog = { event: string; message?: string; current?: number; total?: number; phase?: string; totalFound?: number; imported?: number; skipped?: number; errors?: number };

type QualityAction = {
  action_type: string;
  target_fields: string[];
  reason: string;
  executed: boolean;
  skipped_reason: string;
  result: string;
  courses_improved: number;
};
type QualityData = {
  job_id: string;
  current_avg_completeness: number;
  last_run: {
    timestamp: string;
    job_id: string;
    overall_before: number;
    overall_after: number;
    inline_improved: number;
    celery_dispatched: string[];
    actions: QualityAction[];
  } | null;
  performance: {
    jobs_in_gap: number;
    jobs_above_threshold: number;
    pushed_above_threshold: boolean;
    completeness_gain_pct: number;
  };
};

const ACTION_LABELS: Record<string, string> = {
  pdf_extraction: "PDF Backfill",
  repair_extractor: "Repair Extractor",
  browser_retry: "Browser Retry",
  manual_review: "Manual Review",
  api_promotion: "API Promotion",
};

export type ScrapeJobCardProps = {
  slotIndex: number;
  universities: UniOption[];
  onReviewReady: (jobId: string, uniName: string) => void;
  onRemove?: () => void;
  canRemove?: boolean;
  /** Incremented by the parent's "Cancel All" action to force-reset this card. */
  forceResetKey?: number;
};

const MAX_LOGS = 5000;
const POLL_BASE = 1500;
const POLL_MAX = 10000;
const ALL = "__new__";

// ── Small helpers ─────────────────────────────────────────────────────────────
function fmt(ms: number) {
  const s = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function logColor(event: string, phase?: string) {
  if (event === "error") return "text-red-400";
  if (event === "done") return "text-green-400 font-semibold";
  if (event === "warn") return "text-amber-400";
  if (event === "progress") return "text-blue-400";
  if (phase === "extract") return "text-emerald-300";
  if (phase === "discover" || phase === "fetch") return "text-cyan-400";
  if (phase === "classify") return "text-violet-400";
  if (phase === "stage") return "text-yellow-400";
  return "text-gray-400";
}

// ── Mini university combobox ──────────────────────────────────────────────────
function UniPicker({ value, onChange, universities, disabled }: {
  value: string; onChange: (v: string) => void; universities: UniOption[]; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const filtered = universities.filter((u) => u.name.toLowerCase().includes(search.toLowerCase())).slice(0, 40);
  const label = value === ALL ? "+ Create new" : (universities.find((u) => String(u.id) === value)?.name ?? "Select university…");

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="flex w-full items-center justify-between rounded-md border border-input bg-white px-3 py-2 text-sm h-9 disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50 truncate"
        >
          <span className="truncate">{label}</span>
          <ChevronsUpDown className="ml-2 h-3.5 w-3.5 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-2 z-50" align="start">
        <div className="flex items-center gap-1.5 border rounded px-2 py-1 mb-1.5 bg-white">
          <Search className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
          <input
            autoFocus
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="flex-1 text-sm outline-none bg-transparent"
          />
        </div>
        <div className="max-h-52 overflow-y-auto space-y-0.5">
          <button type="button" onClick={() => { onChange(ALL); setOpen(false); }}
            className="flex w-full items-center rounded px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground">
            <span className="text-blue-600 font-medium">+ Create New University</span>
          </button>
          {filtered.map((u) => (
            <button key={u.id} type="button" onClick={() => { onChange(String(u.id)); setOpen(false); setSearch(""); }}
              className="flex w-full items-center rounded px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground">
              <span className="truncate">{u.name}</span>
              {u.scrapeUrl && <span className="ml-2 text-green-600 text-xs shrink-0">(saved)</span>}
            </button>
          ))}
          {filtered.length === 0 && <div className="py-4 text-center text-xs text-muted-foreground">No match</div>}
        </div>
      </PopoverContent>
    </Popover>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export function ScrapeJobCard({ slotIndex, universities, onReviewReady, onRemove, canRemove, forceResetKey }: ScrapeJobCardProps) {
  const slotKey = `scrape_slot_${slotIndex}_jobId`;
  const startTimeKey = `scrape_slot_${slotIndex}_startTime`;
  const [selectedUni, setSelectedUni] = useState("");
  const [scrapeUrl, setScrapeUrl] = useState("");
  const [newUniName, setNewUniName] = useState("");
  const [newUniCountry, setNewUniCountry] = useState("");
  const [newUniCity, setNewUniCity] = useState("");
  const [feePageUrl, setFeePageUrl] = useState("");
  const [requirementsPageUrl, setRequirementsPageUrl] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [fastMode, setFastMode] = useState(false);

  const [phase, setPhase] = useState<"idle" | "running" | "done" | "error">("idle");
  const [jobStatus, setJobStatus] = useState<"queued" | "running" | "awaiting_approval" | null>(null);
  const [scraping, setScraping] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [logs, setLogs] = useState<ScrapeLog[]>([]);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [uniName, setUniName] = useState("");
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null);
  const [startTime, setStartTime] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [resultSummary, setResultSummary] = useState<{ imported: number; skipped: number; errors: number } | null>(null);
  const [completedJobId, setCompletedJobId] = useState<string | null>(null);
  const [qualityData, setQualityData] = useState<QualityData | null>(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualityError, setQualityError] = useState<string | null>(null);
  // Optimizer run lifecycle: idle → queued → polling → done
  const [qualityStatus, setQualityStatus] = useState<"idle" | "queued" | "polling" | "done">("idle");
  const [showQualityPanel, setShowQualityPanel] = useState(true);
  const qualityPollRef = useRef<number | null>(null);
  const qualityTriggerTimeRef = useRef<number>(0);

  const pollRef = useRef<number | null>(null);
  const logIndexRef = useRef(0);
  const pollInFlightRef = useRef(false);
  const pollFailRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const submittingRef = useRef(false);

  // Restore any in-progress job after navigation
  useEffect(() => {
    const savedJobId = sessionStorage.getItem(slotKey);
    if (savedJobId) {
      setScraping(true);
      setPhase("running");
      setActiveJobId(savedJobId);
      // Restore elapsed timer — use saved start time if available
      const savedT0 = sessionStorage.getItem(startTimeKey);
      if (savedT0) setStartTime(parseInt(savedT0, 10));
      pollJobStatus(savedJobId);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Tick the clock every second while running
  useEffect(() => {
    if (!scraping) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [scraping]);

  // Scroll logs to bottom — use direct container scrollTop to avoid
  // scrollIntoView pulling the whole page up when the user is reading below.
  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs]);

  const resetToIdle = useCallback(() => {
    if (pollRef.current) clearTimeout(pollRef.current);
    pollRef.current = null;
    pollInFlightRef.current = false;
    pollFailRef.current = 0;
    logIndexRef.current = 0;
    sessionStorage.removeItem(slotKey);
    sessionStorage.removeItem(startTimeKey);
    setScraping(false);
    setStopping(false);
    setProgress(null);
    setStartTime(null);
    setActiveJobId(null);
    setPhase("idle");
    setJobStatus(null);
    setLogs([]);
    setResultSummary(null);
    setCompletedJobId(null);
    setQualityData(null);
    setQualityError(null);
    setQualityStatus("idle");
    if (qualityPollRef.current) { clearTimeout(qualityPollRef.current); qualityPollRef.current = null; }
    setUniName("");
  }, [slotKey, startTimeKey]);

  const fetchQualityData = useCallback(async (jobId: string) => {
    setQualityLoading(true);
    setQualityError(null);
    try {
      const res = await fetch(`/api/scrape/jobs/${jobId}/quality-actions`, {
        cache: "no-store", headers: { "Cache-Control": "no-cache" },
      });
      if (!res.ok) {
        const msg = await getFetchErrorMessage(res);
        setQualityError(msg || `Error ${res.status}`);
        return;
      }
      const data = await readResponseJson<QualityData>(res);
      if (data) setQualityData(data);
    } catch (e) {
      setQualityError(String(e));
    } finally {
      setQualityLoading(false);
    }
  }, []);

  const handleRunOptimizer = useCallback(async () => {
    if (!completedJobId || qualityStatus === "queued" || qualityStatus === "polling") return;
    setQualityStatus("queued");
    setQualityError(null);
    qualityTriggerTimeRef.current = Date.now();

    try {
      const res = await fetch(`/api/scrape/jobs/${completedJobId}/run-quality-optimizer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        const msg = await getFetchErrorMessage(res);
        setQualityError(msg || `Error ${res.status}`);
        setQualityStatus("idle");
        return;
      }
    } catch (e) {
      setQualityError(String(e));
      setQualityStatus("idle");
      return;
    }

    // ── Polling loop: check every 5s for up to 2 minutes ─────────────────
    setQualityStatus("polling");
    const triggerTime = qualityTriggerTimeRef.current;
    const POLL_INTERVAL = 5000;
    const MAX_WAIT_MS = 2 * 60 * 1000;
    let elapsed = 0;

    const poll = async () => {
      if (elapsed >= MAX_WAIT_MS) {
        setQualityStatus("done");
        setQualityError("Optimizer timed out — check back shortly or click Refresh.");
        return;
      }
      try {
        const r = await fetch(`/api/scrape/jobs/${completedJobId}/quality-actions`, {
          cache: "no-store", headers: { "Cache-Control": "no-cache" },
        });
        if (r.ok) {
          const data = await readResponseJson<QualityData>(r);
          if (data) {
            setQualityData(data);
            // Stop polling once a last_run with a timestamp newer than trigger appears.
            const ts = data.last_run?.timestamp;
            if (ts && new Date(ts).getTime() >= triggerTime - 5000) {
              setQualityStatus("done");
              return;
            }
          }
        }
      } catch { /* network blip — keep polling */ }

      elapsed += POLL_INTERVAL;
      qualityPollRef.current = window.setTimeout(poll, POLL_INTERVAL);
    };

    qualityPollRef.current = window.setTimeout(poll, POLL_INTERVAL);
  }, [completedJobId, qualityStatus, fetchQualityData]);

  // Auto-fetch quality data when job completes
  useEffect(() => {
    if (!completedJobId) return;
    // Small delay so the orchestrator has time to write _p7_last_run
    const t = setTimeout(() => fetchQualityData(completedJobId), 3000);
    return () => clearTimeout(t);
  }, [completedJobId, fetchQualityData]);

  const pollJobStatus = useCallback((jobId: string) => {
    if (pollRef.current) clearTimeout(pollRef.current);

    const schedule = (ms: number) => {
      pollRef.current = window.setTimeout(poll, ms);
    };

    const poll = async () => {
      if (pollInFlightRef.current) { schedule(POLL_BASE); return; }
      pollInFlightRef.current = true;
      try {
        const res = await fetch(`/api/scrape/status/${jobId}?since=${logIndexRef.current}`, {
          cache: "no-store", headers: { "Cache-Control": "no-cache" },
        });
        if (res.status === 304) { schedule(POLL_BASE); return; }
        if (!res.ok) {
          if (res.status === 404) { sessionStorage.removeItem(slotKey); setScraping(false); setPhase("error"); return; }
          pollFailRef.current += 1;
          schedule(Math.min(POLL_BASE * (pollFailRef.current + 1), POLL_MAX));
          return;
        }
        pollFailRef.current = 0;
        const data = await readResponseJson<{
          universityName?: string; url?: string; logs?: ScrapeLog[]; logIndex?: number;
          status?: string; imported?: number;
        }>(res);
        if (!data) { schedule(POLL_BASE); return; }

        if (data.status === "queued" || data.status === "running" || data.status === "awaiting_approval") {
          setJobStatus(data.status as "queued" | "running" | "awaiting_approval");
        }
        if (data.universityName) setUniName(data.universityName);
        if (data.logs && data.logs.length > 0) {
          setLogs((prev) => [...prev, ...data.logs!].slice(-MAX_LOGS));
          if (data.logIndex !== undefined) logIndexRef.current = data.logIndex;

          const progressLog = [...data.logs].reverse().find((l) => l.event === "progress" && l.total);
          if (progressLog) setProgress({ current: progressLog.current ?? 0, total: progressLog.total! });

          const doneLog = data.logs.find((l) => l.event === "done");
          if (doneLog) {
            setResultSummary({
              imported: doneLog.imported ?? 0,
              skipped: doneLog.skipped ?? 0,
              errors: doneLog.errors ?? 0,
            });
          }
        }

        // Auto-approve the "awaiting_approval" gate so bulk fetch proceeds without manual confirmation
        if (data.status === "awaiting_approval") {
          fetch(`/api/scrape/approve/${jobId}`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proceed: true }),
          }).catch(() => {});
        }

        const terminal = data.status && !["queued", "running", "awaiting_approval"].includes(data.status);
        if (terminal) {
          setScraping(false);
          setStopping(false);
          setCompletedJobId(jobId);
          setPhase(data.status === "completed" || data.status === "completed_with_errors" ? "done" : "error");
          if (pollRef.current) clearTimeout(pollRef.current);
          return;
        }
      } finally {
        pollInFlightRef.current = false;
      }
      schedule(POLL_BASE);
    };

    logIndexRef.current = 0;
    pollFailRef.current = 0;
    void poll();
  }, [slotKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleStart = useCallback(async () => {
    if (submittingRef.current || scraping) return;
    submittingRef.current = true;

    const url = scrapeUrl.trim();
    if (!url) { submittingRef.current = false; return; }

    const body: Record<string, unknown> = { url };
    if (selectedUni && selectedUni !== ALL) {
      body.universityId = parseInt(selectedUni);
    } else {
      if (!newUniName.trim()) {
        setLogs([{ event: "error", message: "University Name is required." }]);
        setPhase("error"); submittingRef.current = false; return;
      }
      if (!newUniCountry.trim()) {
        setLogs([{ event: "error", message: "Country is required." }]);
        setPhase("error"); submittingRef.current = false; return;
      }
      if (!newUniCity.trim()) {
        setLogs([{ event: "error", message: "City is required." }]);
        setPhase("error"); submittingRef.current = false; return;
      }
      // Create uni first
      try {
        const cr = await fetch("/api/universities", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: newUniName.trim(), website: url, country: newUniCountry.trim(), city: newUniCity.trim() }),
        });
        if (cr.status === 409) {
          const d = await cr.json() as { detail?: { id?: number } };
          if (d?.detail?.id) body.universityId = d.detail.id;
        } else if (cr.ok) {
          const d = await cr.json() as { id?: number };
          if (d?.id) body.universityId = d.id;
        }
      } catch {}
    }
    if (feePageUrl.trim()) body.feePageUrl = feePageUrl.trim();
    if (requirementsPageUrl.trim()) body.requirementsPageUrl = requirementsPageUrl.trim();
    if (fastMode) body.fastMode = true;

    setScraping(true);
    setPhase("running");
    setLogs([]);
    setProgress(null);
    setResultSummary(null);
    const t0 = Date.now();
    setStartTime(t0);
    sessionStorage.setItem(startTimeKey, String(t0));
    submittingRef.current = false;

    try {
      const resp = await fetch("/api/scrape/start", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const msg = await getFetchErrorMessage(resp);
        setLogs([{ event: "error", message: msg }]); setScraping(false); setPhase("error"); return;
      }
      const data = await readResponseJson<{ jobId: string }>(resp);
      if (!data?.jobId) {
        setLogs([{ event: "error", message: "Server did not return a job ID." }]); setScraping(false); setPhase("error"); return;
      }
      setActiveJobId(data.jobId);
      sessionStorage.setItem(slotKey, data.jobId);
      pollJobStatus(data.jobId);
    } catch (e) {
      setLogs([{ event: "error", message: String(e) }]); setScraping(false); setPhase("error");
    }
  }, [scraping, scrapeUrl, selectedUni, newUniName, newUniCountry, newUniCity, feePageUrl, requirementsPageUrl, fastMode, pollJobStatus, slotKey]);

  const handleStop = useCallback(async () => {
    if (!activeJobId) return;
    setStopping(true);
    // Cancel the poll FIRST so it cannot race and override the idle reset
    // with a terminal "stopped" status (which would set phase="error").
    if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    pollInFlightRef.current = false;
    pollFailRef.current = 0;
    logIndexRef.current = 0;
    try { await fetch(`/api/scrape/stop/${activeJobId}`, { method: "POST" }); } catch {}
    sessionStorage.removeItem(slotKey);
    sessionStorage.removeItem(startTimeKey);
    setScraping(false);
    setStopping(false);
    setActiveJobId(null);
    setPhase("idle");
    setLogs([]);
    setProgress(null);
    setJobStatus(null);
    setUniName("");
    setStartTime(null);
  }, [activeJobId, slotKey, startTimeKey]);

  // Force-reset when the parent's "Cancel All" fires (forceResetKey increments)
  useEffect(() => {
    if (!forceResetKey) return;
    resetToIdle();
  }, [forceResetKey, resetToIdle]);

  // Auto-fill URL when university is selected
  useEffect(() => {
    if (!selectedUni || selectedUni === ALL) return;
    const uni = universities.find((u) => String(u.id) === selectedUni);
    if (uni) {
      if (uni.scrapeUrl) setScrapeUrl(uni.scrapeUrl);
      if (uni.feePageUrl) { setFeePageUrl(uni.feePageUrl); setShowAdvanced(true); }
      if (uni.requirementsPageUrl) { setRequirementsPageUrl(uni.requirementsPageUrl); setShowAdvanced(true); }
    }
  }, [selectedUni, universities]);

  // When done, notify parent
  useEffect(() => {
    if (phase === "done" && completedJobId) {
      onReviewReady(completedJobId, uniName);
    }
  }, [phase, completedJobId, uniName, onReviewReady]);

  const progressLog = logs.slice().reverse().find((l) => l.event === "progress" && l.total);
  const elapsed = startTime ? fmt(now - startTime) : null;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className={`relative flex flex-col rounded-xl border bg-white shadow-sm overflow-hidden ${
      phase === "running" && jobStatus === "queued" ? "border-amber-300 shadow-amber-50" :
      phase === "running" ? "border-blue-300 shadow-blue-100" :
      phase === "done"    ? "border-green-300 shadow-green-50" :
      phase === "error"   ? "border-red-200"  : "border-gray-200"
    }`}>
      {/* Header */}
      <div className={`flex items-center justify-between px-4 py-2.5 border-b text-sm font-medium ${
        phase === "running" && jobStatus === "queued" ? "bg-amber-50 border-amber-200 text-amber-800" :
        phase === "running" ? "bg-blue-50 border-blue-200 text-blue-800" :
        phase === "done"    ? "bg-green-50 border-green-200 text-green-800" :
        phase === "error"   ? "bg-red-50 border-red-200 text-red-700" : "bg-gray-50 border-gray-200 text-gray-700"
      }`}>
        <div className="flex items-center gap-2">
          {phase === "running" && jobStatus === "queued" && <span className="text-base leading-none">⏳</span>}
          {phase === "running" && jobStatus !== "queued" && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          {phase === "done"    && <CheckCircle2 className="w-3.5 h-3.5" />}
          {phase === "error"   && <AlertCircle className="w-3.5 h-3.5" />}
          <span>
            {phase === "idle"    && `Slot ${slotIndex + 1}`}
            {phase === "running" && jobStatus === "queued" && (uniName ? `${uniName} — Queued` : `Slot ${slotIndex + 1} — Queued`)}
            {phase === "running" && jobStatus !== "queued" && (uniName || `Slot ${slotIndex + 1} — Running`)}
            {phase === "done"    && (uniName || `Slot ${slotIndex + 1} — Done`)}
            {phase === "error"   && (uniName || `Slot ${slotIndex + 1} — Error`)}
          </span>
          {elapsed && phase === "running" && (
            <span className={`text-xs font-normal tabular-nums ${jobStatus === "queued" ? "text-amber-500" : "text-blue-500"}`}>({elapsed})</span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {phase === "idle" && canRemove && (
            <button onClick={onRemove} className="p-1 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600">
              <X className="w-3.5 h-3.5" />
            </button>
          )}
          {(phase === "done" || phase === "error") && (
            <button onClick={resetToIdle} className="p-1 rounded hover:bg-gray-200 text-gray-500 hover:text-gray-700" title="New scrape">
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-col flex-1 p-4 gap-3">

        {/* ── IDLE: Configuration form ─────────────────────────────── */}
        {phase === "idle" && (
          <>
            <div>
              <label className="text-xs font-medium text-gray-500 mb-1 block">University</label>
              <UniPicker value={selectedUni} onChange={setSelectedUni} universities={universities} disabled={scraping} />
            </div>

            {/* New university fields */}
            {selectedUni === ALL && (
              <div className="grid grid-cols-1 gap-2">
                <Input placeholder="University Name" value={newUniName} onChange={(e) => setNewUniName(e.target.value)} className="h-8 text-sm" />
                <div className="grid grid-cols-2 gap-2">
                  <CountrySelect value={newUniCountry} onChange={setNewUniCountry} />
                  <Input placeholder="City" value={newUniCity} onChange={(e) => setNewUniCity(e.target.value)} className="h-8 text-sm" />
                </div>
              </div>
            )}

            <div>
              <label className="text-xs font-medium text-gray-500 mb-1 block">Scrape URL</label>
              <Input
                placeholder="https://university.edu/courses"
                value={scrapeUrl}
                onChange={(e) => setScrapeUrl(e.target.value)}
                className="h-8 text-sm bg-white"
              />
            </div>

            <div className="flex items-center gap-3">
              <label className="flex items-center gap-1.5 text-xs text-amber-800 cursor-pointer select-none">
                <input type="checkbox" checked={fastMode} onChange={(e) => setFastMode(e.target.checked)} className="accent-amber-600" />
                Fast mode
              </label>
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700"
              >
                <ChevronDown className={`w-3 h-3 transition-transform ${showAdvanced ? "rotate-180" : ""}`} />
                Advanced
              </button>
            </div>

            {showAdvanced && (
              <div className="grid grid-cols-2 gap-2 pt-1 border-t border-gray-100">
                <div>
                  <label className="text-xs text-gray-500 mb-1 block">Fee Page URL</label>
                  <Input placeholder="https://…/fees" value={feePageUrl} onChange={(e) => setFeePageUrl(e.target.value)} className="h-8 text-xs" />
                </div>
                <div>
                  <label className="text-xs text-gray-500 mb-1 block">Requirements URL</label>
                  <Input placeholder="https://…/requirements" value={requirementsPageUrl} onChange={(e) => setRequirementsPageUrl(e.target.value)} className="h-8 text-xs" />
                </div>
              </div>
            )}

            <Button onClick={handleStart} disabled={!scrapeUrl.trim()} className="h-9 bg-blue-600 hover:bg-blue-700 mt-1">
              <Play className="w-4 h-4 mr-2" />Start Scrape
            </Button>
          </>
        )}

        {/* ── RUNNING / ERROR: Log view ─────────────────────────────── */}
        {(phase === "running" || phase === "error") && (
          <>
            {/* Progress bar */}
            {progressLog && progressLog.total ? (() => {
              const pct = ((progressLog.current ?? 0) / progressLog.total!) * 100;
              const remaining = startTime && (progressLog.current ?? 0) > 0
                ? fmt(((now - startTime) / (progressLog.current ?? 1)) * ((progressLog.total ?? 1) - (progressLog.current ?? 0)))
                : null;
              return (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>Scraping courses…</span>
                    <span className="tabular-nums">
                      {progressLog.current}/{progressLog.total}
                      {remaining && <span className="ml-2 text-blue-500 font-medium">~{remaining} left</span>}
                    </span>
                  </div>
                  <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full bg-blue-500 rounded-full transition-all" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })() : null}

            {/* Compact log stream */}
            <div ref={logContainerRef} className="flex-1 min-h-[160px] max-h-[420px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed">
              {logs.length === 0 ? (
                jobStatus === "queued" ? (
                  <div className="flex flex-col gap-1.5 pt-2">
                    <span className="text-amber-400 font-medium">⏳ Queued — waiting for a worker to pick up this job</span>
                    <span className="text-gray-500">This job is in the queue and will start automatically once a worker slot is available.</span>
                  </div>
                ) : (
                  <span className="text-gray-500">Starting…</span>
                )
              ) : logs.map((l, i) => (
                <div key={i} className={`${logColor(l.event, l.phase)} break-words`}>
                  {l.message || l.event}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>

            <div className="flex gap-2">
              {phase === "running" && (
                <Button
                  onClick={handleStop}
                  disabled={stopping}
                  variant="outline"
                  size="sm"
                  className="flex-1 border-red-300 text-red-700 hover:bg-red-50"
                >
                  {stopping ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <StopCircle className="w-3.5 h-3.5 mr-1.5" />}
                  {stopping ? "Stopping…" : "Stop"}
                </Button>
              )}
              {phase === "error" && (
                <Button onClick={resetToIdle} variant="outline" size="sm" className="flex-1">
                  <RefreshCw className="w-3.5 h-3.5 mr-1.5" />New Scrape
                </Button>
              )}
            </div>
          </>
        )}

        {/* ── DONE: Result summary ──────────────────────────────────── */}
        {phase === "done" && (
          <>
            {resultSummary && (
              <div className="grid grid-cols-3 gap-2 text-center">
                <div className="bg-green-50 rounded-lg p-2">
                  <div className="text-lg font-bold text-green-700">{resultSummary.imported}</div>
                  <div className="text-xs text-green-600">Staged</div>
                </div>
                <div className="bg-amber-50 rounded-lg p-2">
                  <div className="text-lg font-bold text-amber-700">{resultSummary.skipped}</div>
                  <div className="text-xs text-amber-600">Skipped</div>
                </div>
                <div className="bg-red-50 rounded-lg p-2">
                  <div className="text-lg font-bold text-red-700">{resultSummary.errors}</div>
                  <div className="text-xs text-red-600">Errors</div>
                </div>
              </div>
            )}

            {/* ── Quality Optimizer panel ─────────────────────────── */}
            {completedJobId && (
              <div className="border border-violet-200 rounded-lg overflow-hidden">
                <button
                  type="button"
                  className="w-full flex items-center justify-between px-3 py-2 bg-violet-50 hover:bg-violet-100 transition-colors"
                  onClick={() => setShowQualityPanel((v) => !v)}
                >
                  <div className="flex items-center gap-1.5">
                    <Zap className="w-3.5 h-3.5 text-violet-600" />
                    <span className="text-xs font-semibold text-violet-800">Quality Optimizer</span>
                    {qualityLoading && <Loader2 className="w-3 h-3 animate-spin text-violet-400" />}
                    {qualityData?.performance.pushed_above_threshold && qualityStatus === "idle" && (
                      <span className="text-[10px] bg-green-100 text-green-700 px-1.5 py-0.5 rounded-full font-medium">
                        ↑ Pushed to 85%+
                      </span>
                    )}
                    {qualityStatus === "queued" && (
                      <span className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded-full font-medium flex items-center gap-1">
                        <Loader2 className="w-2.5 h-2.5 animate-spin" /> Queuing…
                      </span>
                    )}
                    {qualityStatus === "polling" && (
                      <span className="text-[10px] bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full font-medium flex items-center gap-1">
                        <Loader2 className="w-2.5 h-2.5 animate-spin" /> Running…
                      </span>
                    )}
                    {qualityStatus === "done" && !qualityError && (
                      <span className="text-[10px] bg-green-100 text-green-700 px-1.5 py-0.5 rounded-full font-medium">
                        ✓ Complete
                      </span>
                    )}
                  </div>
                  <ChevronDown className={`w-3 h-3 text-violet-400 transition-transform ${showQualityPanel ? "rotate-180" : ""}`} />
                </button>

                {showQualityPanel && (
                  <div className="p-3 space-y-2.5">
                    {qualityError && (
                      <div className="text-[10px] text-red-500 bg-red-50 rounded px-2 py-1">{qualityError}</div>
                    )}

                    {qualityData && !qualityLoading && (
                      <>
                        {/* Completeness bar */}
                        <div className="flex items-center gap-3 text-[11px] flex-wrap">
                          <div>
                            <span className="text-gray-500">Completeness:</span>
                            <span className={`font-mono font-semibold ml-1 ${qualityData.current_avg_completeness >= 0.85 ? "text-green-700" : "text-amber-700"}`}>
                              {(qualityData.current_avg_completeness * 100).toFixed(1)}%
                            </span>
                          </div>
                          {qualityData.last_run && qualityData.last_run.overall_before !== qualityData.last_run.overall_after && (
                            <>
                              <span className="text-gray-300">·</span>
                              <div className="flex items-center gap-1">
                                <span className="text-gray-400 font-mono">{(qualityData.last_run.overall_before * 100).toFixed(1)}%</span>
                                <span className="text-gray-300">→</span>
                                <span className={`font-mono font-semibold ${qualityData.last_run.overall_after >= 0.85 ? "text-green-700" : "text-amber-700"}`}>
                                  {(qualityData.last_run.overall_after * 100).toFixed(1)}%
                                </span>
                                {qualityData.performance.completeness_gain_pct > 0 && (
                                  <span className="text-green-600 text-[10px] font-medium">+{qualityData.performance.completeness_gain_pct}%</span>
                                )}
                              </div>
                            </>
                          )}
                          {qualityData.last_run && qualityData.last_run.inline_improved > 0 && (
                            <span className="text-green-600 text-[10px]">
                              {qualityData.last_run.inline_improved} courses improved inline
                            </span>
                          )}
                        </div>

                        {/* Action list */}
                        {qualityData.last_run?.actions && qualityData.last_run.actions.length > 0 ? (
                          <div className="space-y-1">
                            {qualityData.last_run.actions.map((action, i) => (
                              <div key={i} className="flex items-start gap-2 text-[10px] px-2 py-1.5 rounded bg-gray-50 border border-gray-100">
                                <div className={`shrink-0 mt-1 w-1.5 h-1.5 rounded-full ${
                                  action.executed ? "bg-green-500" : action.skipped_reason ? "bg-gray-300" : "bg-red-400"
                                }`} />
                                <div className="flex-1 min-w-0 space-y-0.5">
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    <span className="font-semibold text-gray-700">
                                      {ACTION_LABELS[action.action_type] ?? action.action_type}
                                    </span>
                                    <span className="text-gray-400">→</span>
                                    <span className="text-violet-600 truncate max-w-[140px]">
                                      {action.target_fields.join(", ")}
                                    </span>
                                    {action.courses_improved > 0 && (
                                      <span className="bg-green-100 text-green-700 px-1 py-0.5 rounded font-medium shrink-0">
                                        +{action.courses_improved} courses
                                      </span>
                                    )}
                                    <span className={`px-1 py-0.5 rounded shrink-0 ${
                                      action.executed
                                        ? "bg-green-100 text-green-700"
                                        : action.skipped_reason
                                        ? "bg-gray-100 text-gray-500"
                                        : "bg-red-50 text-red-600"
                                    }`}>
                                      {action.executed ? "✓ done" : action.skipped_reason ? "↷ skipped" : "✗ failed"}
                                    </span>
                                  </div>
                                  <div className="text-gray-400 truncate">
                                    {action.skipped_reason || action.reason}
                                  </div>
                                  {action.result && !action.skipped_reason && (
                                    <div className="text-gray-500 truncate">{action.result}</div>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="text-[10px] text-gray-400 italic">
                            {qualityData.last_run
                              ? "No actions taken — all fields already meet quality thresholds."
                              : qualityData.current_avg_completeness >= 0.85
                              ? "Completeness already ≥85% — optimizer not required. You can still run it manually."
                              : qualityStatus === "polling"
                              ? "Optimizer running — results will appear here when complete."
                              : "No quality actions recorded yet. Run the optimizer below."}
                          </div>
                        )}

                        {/* Celery tasks dispatched */}
                        {qualityData.last_run?.celery_dispatched && qualityData.last_run.celery_dispatched.length > 0 && (
                          <div className="flex items-center gap-1.5 flex-wrap text-[10px] text-gray-500">
                            <span>Background tasks queued:</span>
                            {qualityData.last_run.celery_dispatched.map((t) => (
                              <span key={t} className="bg-violet-100 text-violet-700 px-1.5 py-0.5 rounded">
                                {ACTION_LABELS[t] ?? t}
                              </span>
                            ))}
                          </div>
                        )}

                        {/* Performance stats */}
                        {(qualityData.performance.jobs_in_gap > 0 || qualityData.performance.jobs_above_threshold > 0) && (
                          <div className="flex items-center gap-1 text-[10px] text-gray-400 border-t border-gray-100 pt-2">
                            <TrendingUp className="w-3 h-3 text-violet-400 shrink-0" />
                            <span>
                              {qualityData.performance.jobs_above_threshold} scrape run{qualityData.performance.jobs_above_threshold !== 1 ? "s" : ""} crossed 85%
                              {qualityData.performance.jobs_in_gap > 0 && ` · ${qualityData.performance.jobs_in_gap} in the 70–84% gap`}
                            </span>
                          </div>
                        )}
                      </>
                    )}

                    {/* Actions row */}
                    <div className="flex items-center gap-2 pt-1 border-t border-gray-100 flex-wrap">
                      <Button
                        onClick={handleRunOptimizer}
                        disabled={qualityStatus === "queued" || qualityStatus === "polling" || qualityLoading}
                        size="sm"
                        variant="outline"
                        className={`h-7 text-xs ${
                          qualityData && qualityData.current_avg_completeness >= 0.85
                            ? "border-gray-300 text-gray-500 hover:bg-gray-50"
                            : "border-violet-300 text-violet-700 hover:bg-violet-50"
                        }`}
                        title={qualityData && qualityData.current_avg_completeness >= 0.85
                          ? "Completeness already ≥85% — optimizer not required, but you can still run it manually"
                          : undefined}
                      >
                        {qualityStatus === "queued"
                          ? <><Loader2 className="w-3 h-3 animate-spin mr-1" />Queuing…</>
                          : qualityStatus === "polling"
                          ? <><Loader2 className="w-3 h-3 animate-spin mr-1" />Running…</>
                          : qualityData && qualityData.current_avg_completeness >= 0.85
                          ? <><Zap className="w-3 h-3 mr-1" />Run Anyway</>
                          : <><Zap className="w-3 h-3 mr-1" />Run Quality Optimizer</>}
                      </Button>
                      {qualityData && qualityData.current_avg_completeness >= 0.85 && qualityStatus === "idle" && (
                        <span className="text-[10px] text-gray-400 italic">
                          Optimizer not required — completeness already ≥85%
                        </span>
                      )}
                      <button
                        type="button"
                        onClick={() => completedJobId && fetchQualityData(completedJobId)}
                        disabled={qualityLoading}
                        className="text-[10px] text-gray-400 hover:text-gray-600 disabled:opacity-50 ml-auto"
                      >
                        ↻ Refresh
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Full log (scrollable) */}
            <div className="max-h-[400px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed">
              {logs.map((l, i) => (
                <div key={i} className={`${logColor(l.event, l.phase)} break-words`}>{l.message || l.event}</div>
              ))}
            </div>

            <div className="flex gap-2">
              {completedJobId && resultSummary && resultSummary.imported > 0 && (
                <Button
                  onClick={() => completedJobId && onReviewReady(completedJobId, uniName)}
                  className="flex-1 bg-green-600 hover:bg-green-700 h-9"
                  size="sm"
                >
                  <Eye className="w-3.5 h-3.5 mr-1.5" />Review {resultSummary.imported} Courses
                </Button>
              )}
              <Button onClick={resetToIdle} variant="outline" size="sm" className="h-9">
                <RefreshCw className="w-3.5 h-3.5 mr-1.5" />New
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

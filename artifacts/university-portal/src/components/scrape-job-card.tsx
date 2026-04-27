import { useState, useRef, useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Play, StopCircle, Loader2, Globe, CheckCircle2, AlertCircle,
  ChevronsUpDown, Search, Eye, RefreshCw, ChevronDown, X,
} from "lucide-react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";

// ── Types ────────────────────────────────────────────────────────────────────
type UniOption = { id: number; name: string; scrapeUrl?: string | null; feePageUrl?: string | null; requirementsPageUrl?: string | null };
type ScrapeLog = { event: string; message?: string; current?: number; total?: number; phase?: string; totalFound?: number; imported?: number; skipped?: number; errors?: number };

export type ScrapeJobCardProps = {
  slotIndex: number;
  universities: UniOption[];
  onReviewReady: (jobId: string, uniName: string) => void;
  onRemove?: () => void;
  canRemove?: boolean;
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

function logColor(event: string) {
  if (event === "error") return "text-red-500";
  if (event === "done") return "text-green-600";
  if (event === "warn") return "text-amber-500";
  return "text-gray-500";
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
export function ScrapeJobCard({ slotIndex, universities, onReviewReady, onRemove, canRemove }: ScrapeJobCardProps) {
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

  const pollRef = useRef<number | null>(null);
  const logIndexRef = useRef(0);
  const pollInFlightRef = useRef(false);
  const pollFailRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);
  const submittingRef = useRef(false);

  // Tick the clock every second while running
  useEffect(() => {
    if (!scraping) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [scraping]);

  // Scroll logs to bottom
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [logs]);

  const resetToIdle = useCallback(() => {
    if (pollRef.current) clearTimeout(pollRef.current);
    pollRef.current = null;
    pollInFlightRef.current = false;
    pollFailRef.current = 0;
    logIndexRef.current = 0;
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
    setUniName("");
  }, []);

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
          if (res.status === 404) { setScraping(false); setPhase("error"); return; }
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
  }, []);

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
    setStartTime(Date.now());
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
      pollJobStatus(data.jobId);
    } catch (e) {
      setLogs([{ event: "error", message: String(e) }]); setScraping(false); setPhase("error");
    }
  }, [scraping, scrapeUrl, selectedUni, newUniName, newUniCountry, newUniCity, feePageUrl, requirementsPageUrl, fastMode, pollJobStatus]);

  const handleStop = useCallback(async () => {
    if (!activeJobId) return;
    setStopping(true);
    try { await fetch(`/api/scrape/stop/${activeJobId}`, { method: "POST" }); } catch {}
    setScraping(false); setStopping(false); setActiveJobId(null); setPhase("idle");
  }, [activeJobId]);

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
                  <Input placeholder="Country" value={newUniCountry} onChange={(e) => setNewUniCountry(e.target.value)} className="h-8 text-sm" />
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
            <div className="flex-1 min-h-[160px] max-h-[420px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed">
              {logs.length === 0 ? (
                jobStatus === "queued" ? (
                  <div className="flex flex-col gap-1.5 pt-2">
                    <span className="text-amber-400 font-medium">⏳ Queued — waiting for a worker slot</span>
                    <span className="text-gray-500">The Celery worker pool is full. This job will start automatically once a slot frees up.</span>
                  </div>
                ) : (
                  <span className="text-gray-500">Starting…</span>
                )
              ) : logs.map((l, i) => (
                <div key={i} className={`${logColor(l.event)} break-words`}>
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

            {/* Full log (scrollable) */}
            <div className="max-h-[400px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed">
              {logs.map((l, i) => (
                <div key={i} className={`${logColor(l.event)} break-words`}>{l.message || l.event}</div>
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

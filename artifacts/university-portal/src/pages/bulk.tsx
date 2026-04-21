import { useState, useRef, useEffect, useCallback } from "react";
import { useListUniversities } from "@workspace/api-client-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
  Upload, FileSpreadsheet, CheckCircle2, AlertCircle, Loader2, X,
  Play, Square, Download, RefreshCw, Globe, ChevronDown, ChevronRight,
  FileJson, FileText, CheckCheck, WifiOff, RotateCcw,
} from "lucide-react";
import { readResponseJson } from "@/lib/readResponseJson";

// ─── Types ────────────────────────────────────────────────────────────────────
type ImportResult = {
  universityName: string;
  totalRows: number;
  imported: number;
  skipped: number;
  errors: string[];
};

type University = {
  id: number;
  name: string;
  country: string;
  city: string;
  scrape_url?: string | null;
  website?: string | null;
};

type LastRun = {
  university_id: number;
  university_name: string;
  status: string;
  imported: number;
  total_found: number;
  runtime_job_id: string;
};

type BulkUniStatus = "pending" | "running" | "done" | "error" | "skipped" | "stopped";

type BulkUniEntry = {
  uniId: number;
  name: string;
  jobId: string | null;
  status: BulkUniStatus;
  imported: number;
  found: number;
  staged: number;
  error?: string;
};

type BulkSessionData = {
  sessionId: string;
  status: "running" | "stopped" | "completed";
  currentIndex: number;
  total: number;
  startedAt: string;
  updatedAt: string;
  unis: BulkUniEntry[];
};

// ─── Constants ────────────────────────────────────────────────────────────────
const STORAGE_KEY = "bulkScrapeSessionId";
const POLL_INTERVAL = 3000;

// ─── Helpers ─────────────────────────────────────────────────────────────────
function downloadFile(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function StatusBadge({ status }: { status?: BulkUniStatus }) {
  switch (status) {
    case "running":  return <Badge className="bg-amber-100 text-amber-700 border-amber-200"><Loader2 className="w-3 h-3 mr-1 animate-spin" />Scraping</Badge>;
    case "done":     return <Badge className="bg-green-100 text-green-700 border-green-200"><CheckCircle2 className="w-3 h-3 mr-1" />Done</Badge>;
    case "error":    return <Badge className="bg-red-100 text-red-700 border-red-200"><AlertCircle className="w-3 h-3 mr-1" />Error</Badge>;
    case "stopped":  return <Badge variant="outline" className="text-gray-500">Stopped</Badge>;
    case "skipped":  return <Badge variant="outline" className="text-gray-400">Skipped</Badge>;
    case "pending":  return <Badge variant="outline" className="text-gray-400">Queued</Badge>;
    default:         return <Badge variant="outline" className="text-gray-400">Queued</Badge>;
  }
}

// ─── Component ────────────────────────────────────────────────────────────────
export default function Bulk() {
  const [activeTab, setActiveTab] = useState<"scrape" | "import">("scrape");

  // ── Server-side session state ──────────────────────────────────────────────
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionData, setSessionData] = useState<BulkSessionData | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [sessionNotFound, setSessionNotFound] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollActiveRef = useRef(false);

  // ── Selection state (used when no session is active) ──────────────────────
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // ── Excel Import state ────────────────────────────────────────────────────
  const [file, setFile] = useState<File | null>(null);
  const [uniMode, setUniMode] = useState<"existing" | "new">("existing");
  const [universityId, setUniversityId] = useState<string>("");
  const [newUniName, setNewUniName] = useState("");
  const [newUniCountry, setNewUniCountry] = useState("Australia");
  const [newUniCity, setNewUniCity] = useState("");
  const [importLoading, setImportLoading] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: uniData } = useListUniversities({ limit: 200 });
  const universities = (uniData?.data ?? []) as University[];
  const scrapeable = universities.filter((u) => u.scrape_url);

  const [lastRuns, setLastRuns] = useState<Record<number, LastRun>>({});
  useEffect(() => {
    fetch("/api/scrape/last-runs")
      .then(r => r.json())
      .then((rows: LastRun[]) => {
        const map: Record<number, LastRun> = {};
        rows.forEach(r => { map[r.university_id] = r; });
        setLastRuns(map);
      })
      .catch(() => {});
  }, []);

  // Initialise selectedIds when universities load
  useEffect(() => {
    if (scrapeable.length && selectedIds.size === 0 && !sessionId) {
      setSelectedIds(new Set(scrapeable.map((u) => u.id)));
    }
  }, [scrapeable.length]);

  // ── Polling ───────────────────────────────────────────────────────────────
  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
    pollActiveRef.current = false;
  }, []);

  const pollStatus = useCallback(async (sid: string) => {
    try {
      const res = await fetch(`/api/scrape/bulk/status/${sid}`);
      if (res.status === 404) {
        setSessionNotFound(true);
        stopPolling();
        return;
      }
      if (!res.ok) return;
      const data = await readResponseJson<BulkSessionData>(res);
      if (!data) return;
      setSessionData(data);
      setSessionNotFound(false);
      if (data.status !== "running") {
        stopPolling();
        if (data.status === "completed" || data.status === "stopped") {
          localStorage.removeItem(STORAGE_KEY);
        }
      }
    } catch { /* network hiccup, keep polling */ }
  }, [stopPolling]);

  const startPolling = useCallback((sid: string) => {
    stopPolling();
    pollActiveRef.current = true;
    const loop = async () => {
      if (!pollActiveRef.current) return;
      await pollStatus(sid);
      if (pollActiveRef.current) {
        pollRef.current = setTimeout(loop, POLL_INTERVAL);
      }
    };
    pollRef.current = setTimeout(loop, 0);
  }, [pollStatus, stopPolling]);

  // ── On mount: check localStorage for a running session ───────────────────
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return;
    setReconnecting(true);
    fetch(`/api/scrape/bulk/status/${saved}`)
      .then(async (res) => {
        if (!res.ok) { localStorage.removeItem(STORAGE_KEY); setReconnecting(false); return; }
        const data = await readResponseJson<BulkSessionData>(res);
        if (!data) { setReconnecting(false); return; }
        setSessionId(saved);
        setSessionData(data);
        setReconnecting(false);
        if (data.status === "running") startPolling(saved);
      })
      .catch(() => { localStorage.removeItem(STORAGE_KEY); setReconnecting(false); });

    return stopPolling;
  }, []);

  // ── Start bulk session ────────────────────────────────────────────────────
  const startQueue = useCallback(async () => {
    const selected = scrapeable.filter((u) => selectedIds.has(u.id));
    if (selected.length === 0) return;

    const res = await fetch("/api/scrape/bulk/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        unis: selected.map((u) => ({ id: u.id, name: u.name, scrapeUrl: u.scrapeUrl })),
      }),
    });
    if (!res.ok) return;
    const data = await readResponseJson<{ sessionId: string }>(res);
    if (!data?.sessionId) return;

    localStorage.setItem(STORAGE_KEY, data.sessionId);
    setSessionId(data.sessionId);
    setSessionData(null);
    setSessionNotFound(false);
    startPolling(data.sessionId);
  }, [scrapeable, selectedIds, startPolling]);

  // ── Stop bulk session ─────────────────────────────────────────────────────
  const stopQueue = useCallback(async () => {
    if (!sessionId) return;
    await fetch(`/api/scrape/bulk/stop/${sessionId}`, { method: "POST" });
    stopPolling();
    localStorage.removeItem(STORAGE_KEY);
  }, [sessionId, stopPolling]);

  // ── Reset (clear session, go back to selection) ───────────────────────────
  const resetQueue = useCallback(() => {
    stopPolling();
    setSessionId(null);
    setSessionData(null);
    setSessionNotFound(false);
    localStorage.removeItem(STORAGE_KEY);
  }, [stopPolling]);

  // ── Derived state ─────────────────────────────────────────────────────────
  const isRunning = sessionData?.status === "running";
  const isComplete = sessionData?.status === "completed";
  const isStopped = sessionData?.status === "stopped";
  const doneCount = sessionData?.unis.filter((u) => u.status === "done").length ?? 0;
  const errorCount = sessionData?.unis.filter((u) => u.status === "error").length ?? 0;
  const total = sessionData?.total ?? 0;

  const downloadRaw = (uniId: number, format: "json" | "csv") => {
    const url = `/api/scrape/export?universityId=${uniId}&format=${format}`;
    const uni = scrapeable.find((u) => u.id === uniId);
    const name = uni?.name.toLowerCase().replace(/\s+/g, "_") ?? `uni${uniId}`;
    const ts = new Date().toISOString().slice(0, 10);
    downloadFile(url, `courses_${name}_${ts}.${format}`);
  };

  const downloadAll = (format: "json" | "csv") => {
    const url = `/api/scrape/export?format=${format}`;
    const ts = new Date().toISOString().slice(0, 10);
    downloadFile(url, `courses_all_${ts}.${format}`);
  };

  // ── Excel Import handlers ─────────────────────────────────────────────────
  const handleFile = (f: File | null) => { setFile(f); setImportResult(null); setImportError(null); };
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const f = e.dataTransfer.files[0];
    if (f && (f.name.endsWith(".xlsx") || f.name.endsWith(".xls"))) handleFile(f);
  };

  const handleImport = async () => {
    if (!file) { setImportError("Please select a file."); return; }
    if (uniMode === "existing" && !universityId) { setImportError("Please select a university."); return; }
    if (uniMode === "new" && !newUniName.trim()) { setImportError("Please enter a university name."); return; }
    setImportLoading(true);
    setImportError(null);
    setImportResult(null);
    const formData = new FormData();
    formData.append("file", file);
    if (uniMode === "existing") formData.append("universityId", universityId);
    else {
      formData.append("universityName", newUniName.trim());
      formData.append("universityCountry", newUniCountry.trim());
      formData.append("universityCity", newUniCity.trim());
    }
    try {
      const res = await fetch("/api/import/excel", { method: "POST", body: formData });
      const data = await readResponseJson<ImportResult & { error?: string }>(res);
      if (!res.ok) { setImportError(data?.error ?? "Import failed"); return; }
      if (!data) { setImportError("Import failed (empty response)"); return; }
      setImportResult(data as ImportResult);
    } catch (err) {
      setImportError("Network error: " + (err as Error).message);
    } finally {
      setImportLoading(false);
    }
  };

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-5 max-w-4xl">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Bulk Operations</h1>
        <p className="text-muted-foreground text-sm">Scrape all universities sequentially or import course data from Excel.</p>
      </div>

      {/* Tabs */}
      <div className="flex border-b gap-1">
        {(["scrape", "import"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === tab
                ? "border-blue-600 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {tab === "scrape" ? "Bulk Scrape" : "Excel Import"}
          </button>
        ))}
      </div>

      {/* ── Bulk Scrape Tab ─────────────────────────────────────────────────── */}
      {activeTab === "scrape" && (
        <div className="space-y-4">

          {/* Reconnect notice */}
          {reconnecting && (
            <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700">
              <Loader2 className="w-4 h-4 animate-spin shrink-0" />
              Reconnecting to previous bulk scrape session…
            </div>
          )}

          {/* Session not found notice */}
          {sessionNotFound && (
            <div className="flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-700">
              <WifiOff className="w-4 h-4 shrink-0" />
              Previous session has expired (server was restarted). Start a new session to continue.
              <button className="ml-auto underline hover:no-underline" onClick={resetQueue}>Dismiss</button>
            </div>
          )}

          {/* Active / completed session banner */}
          {sessionData && !sessionNotFound && (
            <div className={`flex items-center gap-3 rounded-lg border p-3 text-sm ${
              isRunning ? "border-blue-200 bg-blue-50 text-blue-700" :
              isComplete ? "border-green-200 bg-green-50 text-green-700" :
              "border-gray-200 bg-gray-50 text-gray-600"
            }`}>
              {isRunning ? (
                <><Loader2 className="w-4 h-4 animate-spin shrink-0" />
                Running in the background — safe to close this tab and come back</>
              ) : isComplete ? (
                <><CheckCheck className="w-4 h-4 shrink-0" />
                All universities scraped successfully</>
              ) : (
                <><Square className="w-4 h-4 shrink-0" />Scrape queue stopped</>
              )}
            </div>
          )}

          {/* Controls */}
          <div className="flex flex-wrap items-center gap-2">
            {!sessionId ? (
              <Button
                onClick={startQueue}
                disabled={selectedIds.size === 0 || reconnecting}
                className="bg-blue-600 hover:bg-blue-700"
              >
                <Play className="w-4 h-4 mr-2" />
                Start Queue ({selectedIds.size} universities)
              </Button>
            ) : isRunning ? (
              <Button onClick={stopQueue} variant="destructive">
                <Square className="w-4 h-4 mr-2" />
                Stop Queue
              </Button>
            ) : (
              <Button onClick={resetQueue} variant="outline">
                <RotateCcw className="w-4 h-4 mr-2" />
                Start New Queue
              </Button>
            )}

            {!isRunning && !sessionId && (
              <Button onClick={resetQueue} variant="outline" size="sm" disabled={reconnecting}>
                <RefreshCw className="w-4 h-4 mr-1" />
                Refresh List
              </Button>
            )}

            {doneCount > 0 && !isRunning && (
              <div className="flex gap-1 ml-auto">
                <Button variant="outline" size="sm" onClick={() => downloadAll("json")}>
                  <FileJson className="w-4 h-4 mr-1" />
                  Download All JSON
                </Button>
                <Button variant="outline" size="sm" onClick={() => downloadAll("csv")}>
                  <FileText className="w-4 h-4 mr-1" />
                  Download All CSV
                </Button>
              </div>
            )}
          </div>

          {/* Global progress (while session is active) */}
          {sessionData && (
            <div className="rounded-xl border bg-gray-50 p-4 space-y-2">
              <div className="flex justify-between text-sm font-medium">
                <span className="text-gray-700">
                  {isRunning ? "Running…" : isComplete ? "Complete" : "Stopped"}
                  {" "}&nbsp;
                  <span className="text-blue-600 font-bold">{doneCount}</span>
                  <span className="text-gray-400"> / {total} universities done</span>
                </span>
                {errorCount > 0 && (
                  <span className="text-red-500">{errorCount} error{errorCount > 1 ? "s" : ""}</span>
                )}
              </div>
              <Progress value={total > 0 ? (doneCount / total) * 100 : 0} className="h-2" />
            </div>
          )}

          {/* Session university list */}
          {sessionData ? (
            <div className="space-y-2">
              {sessionData.unis.map((entry, idx) => {
                const isActive = sessionData.currentIndex === idx;
                const isDone = entry.status === "done";
                const isError = entry.status === "error";
                const isExpanded = expandedId === entry.uniId;
                const uni = scrapeable.find((u) => u.id === entry.uniId);

                return (
                  <div
                    key={entry.uniId}
                    className={`rounded-xl border transition-all ${
                      isActive ? "border-blue-300 bg-blue-50 shadow-sm" :
                      isDone ? "border-green-200 bg-green-50/40" :
                      isError ? "border-red-200 bg-red-50/30" :
                      "border-gray-200 bg-white"
                    }`}
                  >
                    <div className="flex items-center gap-3 p-3 pr-4">
                      {/* Status icon */}
                      <div className="shrink-0 w-7 h-7 flex items-center justify-center">
                        {isDone ? (
                          <CheckCircle2 className="w-5 h-5 text-green-500" />
                        ) : isError ? (
                          <AlertCircle className="w-5 h-5 text-red-400" />
                        ) : isActive ? (
                          <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
                        ) : entry.status === "stopped" ? (
                          <Square className="w-4 h-4 text-gray-400" />
                        ) : (
                          <div className="w-4 h-4 rounded-full border-2 border-gray-300" />
                        )}
                      </div>

                      {/* Info */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-sm text-gray-900 truncate">{entry.name}</span>
                          <StatusBadge status={entry.status} />
                        </div>
                        {uni?.scrapeUrl && (
                          <p className="text-xs text-gray-400 truncate mt-0.5">{uni.scrapeUrl}</p>
                        )}
                        {isDone && entry.staged > 0 && (
                          <p className="text-xs text-green-600 mt-0.5">{entry.staged} courses staged for review</p>
                        )}
                        {isError && entry.error && (
                          <p className="text-xs text-red-500 mt-0.5 truncate">{entry.error}</p>
                        )}
                        {isActive && (
                          <p className="text-xs text-blue-500 mt-0.5">Scraping in progress…</p>
                        )}
                      </div>

                      {/* Actions */}
                      {isDone && (
                        <div className="flex items-center gap-1 shrink-0">
                          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => downloadRaw(entry.uniId, "json")}>
                            <FileJson className="w-3.5 h-3.5 mr-1" />JSON
                          </Button>
                          <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => downloadRaw(entry.uniId, "csv")}>
                            <FileText className="w-3.5 h-3.5 mr-1" />CSV
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : !reconnecting && (
            /* ── Selection list (no active session) ── */
            <>
              {scrapeable.length === 0 ? (
                <div className="rounded-xl border-2 border-dashed border-gray-200 p-12 text-center text-gray-400">
                  <Globe className="w-10 h-10 mx-auto mb-3 opacity-40" />
                  <p className="font-medium">No universities with a scrape URL configured</p>
                  <p className="text-sm mt-1">Add a Scrape URL to a university profile first.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="flex gap-3 items-center text-sm text-gray-500 px-1">
                    <button className="hover:text-blue-600 transition-colors" onClick={() => setSelectedIds(new Set(scrapeable.map((u) => u.id)))}>
                      Select all
                    </button>
                    <span>·</span>
                    <button className="hover:text-blue-600 transition-colors" onClick={() => setSelectedIds(new Set())}>
                      Deselect all
                    </button>
                    <span className="ml-auto">{selectedIds.size} selected</span>
                  </div>

                  {scrapeable.map((uni) => {
                    const lr = lastRuns[uni.id];
                    return (
                    <div key={uni.id} className="rounded-xl border border-gray-200 bg-white hover:border-gray-300 transition-all">
                      <div className="flex items-center gap-3 p-3 pr-4">
                        <input
                          type="checkbox"
                          checked={selectedIds.has(uni.id)}
                          onChange={(e) => {
                            const next = new Set(selectedIds);
                            if (e.target.checked) next.add(uni.id); else next.delete(uni.id);
                            setSelectedIds(next);
                          }}
                          className="w-4 h-4 accent-blue-600 shrink-0"
                        />
                        <div className="flex-1 min-w-0">
                          <span className="font-medium text-sm text-gray-900">{uni.name}</span>
                          {uni.scrape_url && (
                            <p className="text-xs text-gray-400 truncate mt-0.5">{uni.scrape_url}</p>
                          )}
                          {lr && (
                            <p className="text-xs mt-0.5 flex items-center gap-2">
                              <span className={lr.status === "completed" ? "text-green-600 font-medium" : "text-amber-600 font-medium"}>
                                {lr.status === "completed" ? "✓ Last scraped" : "⚠ Last stopped"}
                              </span>
                              <span className="text-gray-400">·</span>
                              <span className="text-gray-500">{lr.imported} imported / {lr.total_found} found</span>
                            </p>
                          )}
                        </div>
                        {lr ? (
                          <Badge variant="outline" className={lr.status === "completed" ? "text-green-600 border-green-200 bg-green-50 shrink-0" : "text-amber-600 border-amber-200 bg-amber-50 shrink-0"}>
                            {lr.status === "completed" ? "Done" : "Stopped"}
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="text-gray-400 shrink-0">Never scraped</Badge>
                        )}
                      </div>
                    </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {/* Legend */}
          <div className="text-xs text-gray-400 flex flex-wrap gap-4 pt-2">
            <span className="flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5 text-green-500" /> Done</span>
            <span className="flex items-center gap-1"><Loader2 className="w-3.5 h-3.5 text-blue-500" /> Running</span>
            <span className="flex items-center gap-1"><AlertCircle className="w-3.5 h-3.5 text-red-400" /> Error</span>
            <span className="flex items-center gap-1"><div className="w-3.5 h-3.5 rounded-full border-2 border-gray-300" /> Queued</span>
          </div>
        </div>
      )}

      {/* ── Excel Import Tab ─────────────────────────────────────────────────── */}
      {activeTab === "import" && (
        <div className="space-y-6 max-w-2xl">
          <div
            className={`border-2 border-dashed rounded-xl p-10 text-center transition-colors cursor-pointer ${
              file ? "border-blue-400 bg-blue-50" : "border-gray-300 hover:border-blue-300 hover:bg-gray-50"
            }`}
            onClick={() => fileRef.current?.click()}
            onDrop={handleDrop}
            onDragOver={(e) => e.preventDefault()}
          >
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xls"
              className="hidden"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
            />
            {file ? (
              <div className="flex items-center justify-center gap-3">
                <FileSpreadsheet className="w-8 h-8 text-blue-500" />
                <div className="text-left">
                  <p className="font-medium text-blue-700">{file.name}</p>
                  <p className="text-sm text-blue-500">{(file.size / 1024).toFixed(0)} KB</p>
                </div>
                <button
                  className="ml-2 text-gray-400 hover:text-red-500"
                  onClick={(e) => { e.stopPropagation(); handleFile(null); if (fileRef.current) fileRef.current.value = ""; }}
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
            ) : (
              <div className="text-gray-500">
                <Upload className="w-10 h-10 mx-auto mb-3 text-gray-400" />
                <p className="font-medium">Drop your Excel file here or click to browse</p>
                <p className="text-sm mt-1">Supports .xlsx and .xls — Scrapy pipeline output format</p>
              </div>
            )}
          </div>

          <div className="space-y-4 border rounded-xl p-5">
            <h2 className="font-semibold text-gray-800">University</h2>
            <div className="flex gap-3">
              <Button variant={uniMode === "existing" ? "default" : "outline"} size="sm" onClick={() => setUniMode("existing")}>Existing University</Button>
              <Button variant={uniMode === "new" ? "default" : "outline"} size="sm" onClick={() => setUniMode("new")}>New University</Button>
            </div>
            {uniMode === "existing" ? (
              <div>
                <Label>Select University</Label>
                <Select value={universityId} onValueChange={setUniversityId}>
                  <SelectTrigger className="mt-1">
                    <SelectValue placeholder="Choose a university..." />
                  </SelectTrigger>
                  <SelectContent>
                    {universities.map((u) => (
                      <SelectItem key={u.id} value={String(u.id)}>
                        {u.name} — {u.city}, {u.country}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-4">
                <div className="col-span-2">
                  <Label>University Name *</Label>
                  <Input className="mt-1" placeholder="e.g. University of Hull" value={newUniName} onChange={(e) => setNewUniName(e.target.value)} />
                </div>
                <div>
                  <Label>Country</Label>
                  <Input className="mt-1" placeholder="e.g. United Kingdom" value={newUniCountry} onChange={(e) => setNewUniCountry(e.target.value)} />
                </div>
                <div>
                  <Label>City</Label>
                  <Input className="mt-1" placeholder="e.g. Hull" value={newUniCity} onChange={(e) => setNewUniCity(e.target.value)} />
                </div>
              </div>
            )}
          </div>

          {importError && (
            <div className="flex items-start gap-2 rounded-lg bg-red-50 border border-red-200 p-4 text-red-700">
              <AlertCircle className="w-5 h-5 mt-0.5 shrink-0" />
              <p className="text-sm">{importError}</p>
            </div>
          )}

          {importResult && (
            <div className="rounded-xl border border-green-200 bg-green-50 p-5 space-y-3">
              <div className="flex items-center gap-2 text-green-700 font-semibold">
                <CheckCircle2 className="w-5 h-5" />
                Import Complete — {importResult.universityName}
              </div>
              <div className="grid grid-cols-3 gap-4 text-center">
                <div className="bg-white rounded-lg p-3 border border-green-200">
                  <div className="text-2xl font-bold text-gray-800">{importResult.totalRows}</div>
                  <div className="text-xs text-gray-500 mt-0.5">Total Rows</div>
                </div>
                <div className="bg-white rounded-lg p-3 border border-green-200">
                  <div className="text-2xl font-bold text-green-600">{importResult.imported}</div>
                  <div className="text-xs text-gray-500 mt-0.5">Imported</div>
                </div>
                <div className="bg-white rounded-lg p-3 border border-green-200">
                  <div className="text-2xl font-bold text-amber-500">{importResult.skipped}</div>
                  <div className="text-xs text-gray-500 mt-0.5">Skipped</div>
                </div>
              </div>
              {importResult.errors.length > 0 && (
                <div className="space-y-1">
                  <p className="text-sm font-medium text-red-600">Errors ({importResult.errors.length}):</p>
                  {importResult.errors.map((e, i) => (
                    <p key={i} className="text-xs text-red-500 bg-white rounded p-1 border border-red-100">{e}</p>
                  ))}
                </div>
              )}
            </div>
          )}

          <Button className="w-full" size="lg" onClick={handleImport} disabled={importLoading || !file}>
            {importLoading ? (
              <><Loader2 className="w-4 h-4 mr-2 animate-spin" />Importing...</>
            ) : (
              <><Upload className="w-4 h-4 mr-2" />Import Excel File</>
            )}
          </Button>

          <div className="rounded-xl border bg-gray-50 p-5 text-sm text-gray-600 space-y-2">
            <p className="font-medium text-gray-700">Expected Column Format</p>
            <p>The Excel file should use the Scrapy pipeline output format with these key columns:</p>
            <div className="flex flex-wrap gap-1 mt-2">
              {[
                "Course Name", "Category", "Sub Category", "Degree Level", "Duration", "Duration Term",
                "Study Mode", "Study Load", "Intake Month", "International Fee", "Fee Term", "Currency",
                "IELTS Overall", "IELTS Listening", "PTE Overall", "TOEFL Overall",
                "Academic Level", "Academic Country", "Scholarship", "Course Website",
              ].map((col) => (
                <Badge key={col} variant="secondary" className="text-xs">{col}</Badge>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
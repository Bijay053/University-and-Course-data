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
  SkipForward, FileJson, FileText, CheckCheck,
} from "lucide-react";
import { readResponseJson } from "@/lib/readResponseJson";

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
  scrapeUrl?: string | null;
  website?: string | null;
};

type ScrapeLog = {
  event: string;
  message?: string;
  current?: number;
  total?: number;
  totalFound?: number;
  imported?: number;
  phase?: string;
};

type JobState = {
  jobId: string;
  status: "starting" | "running" | "done" | "error" | "stopped";
  logs: ScrapeLog[];
  courseCount: number;
  error?: string;
  imported?: number;
};

type UniQueueItem = {
  uni: University;
  state: JobState | null;
  skipped?: boolean;
};

const POLL_INTERVAL = 2000;

function downloadFile(url: string, filename: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function getLatestProgress(logs: ScrapeLog[]): { current: number; total: number } | null {
  for (let i = logs.length - 1; i >= 0; i--) {
    const l = logs[i];
    if (l.event === "progress" && l.total) return { current: l.current ?? 0, total: l.total };
  }
  return null;
}

function getLatestMessage(logs: ScrapeLog[]): string {
  for (let i = logs.length - 1; i >= 0; i--) {
    const l = logs[i];
    if (l.message) return l.message;
  }
  return "";
}

function StatusBadge({ state }: { state: JobState | null; skipped?: boolean }) {
  if (!state) return <Badge variant="outline" className="text-gray-400">Queued</Badge>;
  switch (state.status) {
    case "starting": return <Badge className="bg-blue-100 text-blue-700 border-blue-200"><Loader2 className="w-3 h-3 mr-1 animate-spin" />Starting</Badge>;
    case "running": return <Badge className="bg-amber-100 text-amber-700 border-amber-200"><Loader2 className="w-3 h-3 mr-1 animate-spin" />Scraping</Badge>;
    case "done": return <Badge className="bg-green-100 text-green-700 border-green-200"><CheckCircle2 className="w-3 h-3 mr-1" />Done</Badge>;
    case "error": return <Badge className="bg-red-100 text-red-700 border-red-200"><AlertCircle className="w-3 h-3 mr-1" />Error</Badge>;
    case "stopped": return <Badge variant="outline" className="text-gray-500">Stopped</Badge>;
    default: return null;
  }
}

export default function Bulk() {
  const [activeTab, setActiveTab] = useState<"scrape" | "import">("scrape");

  // ── Bulk Scrape state ──────────────────────────────────────────────────────
  const [queue, setQueue] = useState<UniQueueItem[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [isRunning, setIsRunning] = useState(false);
  const [currentIdx, setCurrentIdx] = useState<number>(-1);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const stopRef = useRef(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const logIndexRef = useRef<Record<number, number>>({});

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
  const scrapeable = universities.filter((u) => u.scrapeUrl);

  useEffect(() => {
    if (scrapeable.length && queue.length === 0) {
      setQueue(scrapeable.map((u) => ({ uni: u, state: null })));
      setSelectedIds(new Set(scrapeable.map((u) => u.id)));
    }
  }, [scrapeable.length]);

  const updateQueueItem = useCallback((uniId: number, updater: (prev: UniQueueItem) => UniQueueItem) => {
    setQueue((q) => q.map((item) => (item.uni.id === uniId ? updater(item) : item)));
  }, []);

  const pollJob = useCallback(async (uniId: number, jobId: string): Promise<boolean> => {
    const idx = logIndexRef.current[uniId] ?? 0;
    try {
      const res = await fetch(`/api/scrape/status/${jobId}?since=${idx}`);
      if (!res.ok) return false;
      const data = await res.json() as { status?: string; logs?: ScrapeLog[]; logIndex?: number; imported?: number };
      const newLogs = data.logs ?? [];
      logIndexRef.current[uniId] = data.logIndex ?? idx;

      updateQueueItem(uniId, (prev) => ({
        ...prev,
        state: prev.state
          ? {
              ...prev.state,
              status: data.status === "done" || data.status === "complete" ? "done"
                : data.status === "error" ? "error"
                : "running",
              logs: [...(prev.state.logs ?? []), ...newLogs],
              imported: data.imported ?? prev.state.imported,
              courseCount: data.imported ?? prev.state.courseCount,
            }
          : null,
      }));

      const finished = data.status === "done" || data.status === "complete" || data.status === "error";
      return finished;
    } catch {
      return false;
    }
  }, [updateQueueItem]);

  const runUniversity = useCallback(async (item: UniQueueItem, idx: number) => {
    if (stopRef.current) return false;
    const { uni } = item;
    logIndexRef.current[uni.id] = 0;

    updateQueueItem(uni.id, (prev) => ({
      ...prev,
      state: { jobId: "", status: "starting", logs: [], courseCount: 0 },
    }));
    setCurrentIdx(idx);
    setExpandedId(uni.id);

    try {
      const res = await fetch("/api/scrape/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: uni.scrapeUrl, universityId: uni.id, bulkMode: true }),
      });
      const data = await readResponseJson<{ jobId?: string; error?: string }>(res);
      if (!res.ok || !data?.jobId) {
        updateQueueItem(uni.id, (prev) => ({
          ...prev,
          state: { jobId: "", status: "error", logs: [], courseCount: 0, error: data?.error ?? "Failed to start" },
        }));
        return true;
      }

      const jobId = data.jobId;
      updateQueueItem(uni.id, (prev) => ({
        ...prev,
        state: prev.state ? { ...prev.state, jobId, status: "running" } : { jobId, status: "running", logs: [], courseCount: 0 },
      }));

      await new Promise<void>((resolve) => {
        const poll = async () => {
          if (stopRef.current) {
            updateQueueItem(uni.id, (prev) => ({
              ...prev,
              state: prev.state ? { ...prev.state, status: "stopped" } : null,
            }));
            resolve();
            return;
          }
          const done = await pollJob(uni.id, jobId);
          if (done) { resolve(); return; }
          pollRef.current = setTimeout(poll, POLL_INTERVAL);
        };
        pollRef.current = setTimeout(poll, POLL_INTERVAL);
      });
    } catch (err) {
      updateQueueItem(uni.id, (prev) => ({
        ...prev,
        state: { jobId: "", status: "error", logs: [], courseCount: 0, error: (err as Error).message },
      }));
    }
    return true;
  }, [pollJob, updateQueueItem]);

  const startQueue = useCallback(async () => {
    stopRef.current = false;
    setIsRunning(true);

    const selected = queue.filter((item) => selectedIds.has(item.uni.id));
    for (let i = 0; i < selected.length; i++) {
      if (stopRef.current) break;
      const globalIdx = queue.findIndex((q) => q.uni.id === selected[i].uni.id);
      await runUniversity(selected[i], globalIdx);
      await new Promise((r) => setTimeout(r, 500));
    }

    setIsRunning(false);
    setCurrentIdx(-1);
  }, [queue, selectedIds, runUniversity]);

  const stopQueue = useCallback(() => {
    stopRef.current = true;
    if (pollRef.current) clearTimeout(pollRef.current);
    setIsRunning(false);
    setCurrentIdx(-1);
  }, []);

  const resetQueue = useCallback(() => {
    stopQueue();
    setQueue(scrapeable.map((u) => ({ uni: u, state: null })));
    setSelectedIds(new Set(scrapeable.map((u) => u.id)));
    logIndexRef.current = {};
  }, [scrapeable, stopQueue]);

  const skipCurrent = useCallback(() => {
    if (currentIdx >= 0) {
      const item = queue[currentIdx];
      if (item) {
        updateQueueItem(item.uni.id, (prev) => ({
          ...prev,
          state: prev.state ? { ...prev.state, status: "stopped" } : null,
          skipped: true,
        }));
      }
      stopRef.current = true;
    }
  }, [currentIdx, queue, updateQueueItem]);

  const doneCount = queue.filter((i) => i.state?.status === "done").length;
  const errorCount = queue.filter((i) => i.state?.status === "error").length;
  const selectedCount = selectedIds.size;
  const total = selectedCount;

  const downloadRaw = (uniId: number, format: "json" | "csv") => {
    const url = `/api/scrape/export?universityId=${uniId}&format=${format}`;
    const uni = queue.find((q) => q.uni.id === uniId)?.uni;
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
          {/* Controls */}
          <div className="flex flex-wrap items-center gap-2">
            {!isRunning ? (
              <Button
                onClick={startQueue}
                disabled={selectedCount === 0 || isRunning}
                className="bg-blue-600 hover:bg-blue-700"
              >
                <Play className="w-4 h-4 mr-2" />
                Start Queue ({selectedCount} universities)
              </Button>
            ) : (
              <Button onClick={stopQueue} variant="destructive">
                <Square className="w-4 h-4 mr-2" />
                Stop Queue
              </Button>
            )}

            {isRunning && (
              <Button onClick={skipCurrent} variant="outline" size="sm">
                <SkipForward className="w-4 h-4 mr-1" />
                Skip Current
              </Button>
            )}

            <Button onClick={resetQueue} variant="outline" size="sm" disabled={isRunning}>
              <RefreshCw className="w-4 h-4 mr-1" />
              Reset
            </Button>

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

          {/* Global progress */}
          {(isRunning || doneCount > 0) && (
            <div className="rounded-xl border bg-gray-50 p-4 space-y-2">
              <div className="flex justify-between text-sm font-medium">
                <span className="text-gray-700">
                  {isRunning ? "Running…" : "Complete"}
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

          {/* University queue list */}
          {queue.length === 0 ? (
            <div className="rounded-xl border-2 border-dashed border-gray-200 p-12 text-center text-gray-400">
              <Globe className="w-10 h-10 mx-auto mb-3 opacity-40" />
              <p className="font-medium">No universities with a scrape URL configured</p>
              <p className="text-sm mt-1">Add a Scrape URL to a university profile first.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {/* Select all / deselect all */}
              {!isRunning && (
                <div className="flex gap-3 items-center text-sm text-gray-500 px-1">
                  <button className="hover:text-blue-600 transition-colors" onClick={() => setSelectedIds(new Set(scrapeable.map((u) => u.id)))}>
                    Select all
                  </button>
                  <span>·</span>
                  <button className="hover:text-blue-600 transition-colors" onClick={() => setSelectedIds(new Set())}>
                    Deselect all
                  </button>
                  <span className="ml-auto">{selectedCount} selected</span>
                </div>
              )}

              {queue.map((item, idx) => {
                const { uni, state } = item;
                const isActive = currentIdx === idx;
                const prog = state ? getLatestProgress(state.logs) : null;
                const msg = state ? getLatestMessage(state.logs) : "";
                const isExpanded = expandedId === uni.id;
                const isDone = state?.status === "done";
                const isError = state?.status === "error";

                return (
                  <div
                    key={uni.id}
                    className={`rounded-xl border transition-all ${
                      isActive ? "border-blue-300 bg-blue-50 shadow-sm" :
                      isDone ? "border-green-200 bg-green-50/40" :
                      isError ? "border-red-200 bg-red-50/30" :
                      "border-gray-200 bg-white hover:border-gray-300"
                    }`}
                  >
                    <div className="flex items-center gap-3 p-3 pr-4">
                      {/* Checkbox */}
                      {!isRunning && (
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
                      )}

                      {/* Status icon */}
                      <div className="shrink-0 w-7 h-7 flex items-center justify-center">
                        {isDone ? (
                          <CheckCircle2 className="w-5 h-5 text-green-500" />
                        ) : isError ? (
                          <AlertCircle className="w-5 h-5 text-red-400" />
                        ) : isActive ? (
                          <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
                        ) : state?.status === "stopped" ? (
                          <Square className="w-4 h-4 text-gray-400" />
                        ) : (
                          <div className="w-4 h-4 rounded-full border-2 border-gray-300" />
                        )}
                      </div>

                      {/* Info */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-medium text-sm text-gray-900 truncate">{uni.name}</span>
                          <StatusBadge state={state} />
                        </div>
                        {uni.scrapeUrl && (
                          <p className="text-xs text-gray-400 truncate mt-0.5">{uni.scrapeUrl}</p>
                        )}
                        {isActive && prog && (
                          <div className="mt-1.5 space-y-1">
                            <Progress value={(prog.current / prog.total) * 100} className="h-1.5" />
                            <p className="text-xs text-blue-600">{prog.current} / {prog.total} courses scraped</p>
                          </div>
                        )}
                        {isActive && !prog && msg && (
                          <p className="text-xs text-blue-500 mt-1 truncate">{msg}</p>
                        )}
                        {isDone && state?.courseCount !== undefined && state.courseCount > 0 && (
                          <p className="text-xs text-green-600 mt-0.5">{state.courseCount} courses found</p>
                        )}
                        {isError && state?.error && (
                          <p className="text-xs text-red-500 mt-0.5 truncate">{state.error}</p>
                        )}
                      </div>

                      {/* Actions */}
                      <div className="flex items-center gap-1 shrink-0">
                        {isDone && (
                          <>
                            <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => downloadRaw(uni.id, "json")}>
                              <FileJson className="w-3.5 h-3.5 mr-1" />JSON
                            </Button>
                            <Button size="sm" variant="outline" className="h-7 px-2 text-xs" onClick={() => downloadRaw(uni.id, "csv")}>
                              <FileText className="w-3.5 h-3.5 mr-1" />CSV
                            </Button>
                          </>
                        )}
                        {state && state.logs.length > 0 && (
                          <button
                            className="text-gray-400 hover:text-gray-600 ml-1"
                            onClick={() => setExpandedId(isExpanded ? null : uni.id)}
                          >
                            {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                          </button>
                        )}
                      </div>
                    </div>

                    {/* Log expansion */}
                    {isExpanded && state && state.logs.length > 0 && (
                      <div className="border-t border-gray-100 bg-gray-900 rounded-b-xl p-3 max-h-40 overflow-y-auto">
                        <div className="space-y-0.5 font-mono text-xs">
                          {state.logs.filter((l) => l.message).slice(-30).map((log, i) => (
                            <div key={i} className={`${
                              log.event === "error" ? "text-red-400" :
                              log.event === "status" ? "text-blue-300" :
                              log.event === "progress" ? "text-green-400" :
                              "text-gray-300"
                            }`}>
                              [{log.event}] {log.message}
                              {log.current !== undefined && log.total ? ` (${log.current}/${log.total})` : ""}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
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

import { useEffect, useState, useRef, useCallback } from "react";
import { useListUniversities } from "@workspace/api-client-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  FileSpreadsheet, CheckCircle2, Clock, AlertCircle, RefreshCw,
  Globe, Zap, Loader2, X, ExternalLink, Bot, ArrowRight,
} from "lucide-react";
import { Link } from "wouter";

type ImportJob = {
  id: number;
  universityName: string;
  fileName: string;
  status: string;
  totalRows: number | null;
  importedRows: number | null;
  skippedRows: number | null;
  errorMessage: string | null;
  createdAt: string;
  completedAt: string | null;
};

type UniStat = {
  id: number;
  name: string;
  country: string;
  city: string;
  courseCount: number;
};

type ScrapeLog = {
  event: string;
  message?: string;
  name?: string;
  status?: string;
  current?: number;
  total?: number;
  totalFound?: number;
  imported?: number;
  skipped?: number;
  errors?: number;
  phase?: string;
};

const ALL = "__new__";

function statusBadge(status: string) {
  if (status === "completed") return <Badge className="bg-green-100 text-green-700 border-green-200">Completed</Badge>;
  if (status === "completed_with_errors") return <Badge className="bg-amber-100 text-amber-700 border-amber-200">Completed (Errors)</Badge>;
  if (status === "running") return <Badge className="bg-blue-100 text-blue-700 border-blue-200">Running</Badge>;
  return <Badge variant="secondary">{status}</Badge>;
}

function fmtDate(s: string) {
  return new Date(s).toLocaleString("en-AU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function Scraping() {
  const [jobs, setJobs] = useState<ImportJob[]>([]);
  const [uniStats, setUniStats] = useState<UniStat[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);

  const [scrapeUrl, setScrapeUrl] = useState("");
  const [selectedUni, setSelectedUni] = useState("");
  const [newUniName, setNewUniName] = useState("");
  const [newUniCountry, setNewUniCountry] = useState("");
  const [newUniCity, setNewUniCity] = useState("");
  const [scraping, setScraping] = useState(false);
  const [scrapeLogs, setScrapeLogs] = useState<ScrapeLog[]>([]);
  const [scrapeResult, setScrapeResult] = useState<ScrapeLog | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const logIndexRef = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const { data: uniData } = useListUniversities({ limit: 100 });

  const fetchJobs = async () => {
    setLoadingJobs(true);
    try {
      const res = await fetch("/api/import/history");
      if (res.ok) setJobs(await res.json());
    } finally {
      setLoadingJobs(false);
    }
  };

  useEffect(() => { fetchJobs(); }, []);

  useEffect(() => {
    if (!uniData?.data) return;
    Promise.all(
      uniData.data.map(async (u) => {
        const res = await fetch(`/api/courses?universityId=${u.id}&limit=1`);
        const d = await res.json();
        return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: d.total ?? 0 };
      })
    ).then(setUniStats);
  }, [uniData]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [scrapeLogs]);

  const pollJobStatus = useCallback((jobId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    logIndexRef.current = 0;
    
    const poll = async () => {
      try {
        const res = await fetch(`/api/scrape/status/${jobId}?since=${logIndexRef.current}`);
        if (!res.ok) return;
        const data = await res.json();

        if (data.logs && data.logs.length > 0) {
          setScrapeLogs((prev) => [...prev, ...data.logs]);
          logIndexRef.current = data.logIndex;

          const doneLog = data.logs.find((l: ScrapeLog) => l.event === "done");
          if (doneLog) setScrapeResult(doneLog);
        }

        if (data.status !== "running") {
          setScraping(false);
          setActiveJobId(null);
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        }
      } catch {}
    };

    poll();
    pollRef.current = setInterval(poll, 1500);
  }, []);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  useEffect(() => {
    const savedJobId = sessionStorage.getItem("activeScrapeJob");
    if (savedJobId) {
      setActiveJobId(savedJobId);
      setScraping(true);
      setScrapeLogs([]);
      setScrapeResult(null);
      pollJobStatus(savedJobId);
    }
  }, [pollJobStatus]);

  const startScraping = useCallback(async () => {
    if (!scrapeUrl) return;
    setScraping(true);
    setScrapeLogs([]);
    setScrapeResult(null);

    const body: Record<string, unknown> = { url: scrapeUrl };
    if (selectedUni && selectedUni !== ALL) {
      body.universityId = parseInt(selectedUni);
    } else {
      body.universityName = newUniName;
      body.universityCountry = newUniCountry;
      body.universityCity = newUniCity;
    }

    try {
      const resp = await fetch("/api/scrape/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (!resp.ok) {
        const err = await resp.json();
        setScrapeLogs([{ event: "error", message: err.error || "Failed to start scraping" }]);
        setScraping(false);
        return;
      }

      const data = await resp.json();
      const jobId = data.jobId;
      setActiveJobId(jobId);
      sessionStorage.setItem("activeScrapeJob", jobId);
      setScrapeLogs([{ event: "status", message: "Scraping started in background..." }]);
      pollJobStatus(jobId);
    } catch (err) {
      setScrapeLogs([{ event: "error", message: (err as Error).message }]);
      setScraping(false);
    }
  }, [scrapeUrl, selectedUni, newUniName, newUniCountry, newUniCity, pollJobStatus]);

  useEffect(() => {
    if (!scraping && activeJobId) {
      sessionStorage.removeItem("activeScrapeJob");
    }
  }, [scraping, activeJobId]);

  const progressLog = scrapeLogs.findLast((l) => l.event === "progress");
  const courseLogs = scrapeLogs.filter((l) => l.event === "course");

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Scraping & Import</h1>
          <p className="text-muted-foreground">Scrape university websites with AI or import from Excel files.</p>
        </div>
        <Link href="/bulk">
          <Button variant="outline">
            <FileSpreadsheet className="w-4 h-4 mr-2" />
            Upload Excel File
          </Button>
        </Link>
      </div>

      <Card className="border-2 border-blue-100 bg-gradient-to-br from-blue-50/50 to-purple-50/30">
        <CardHeader className="pb-4">
          <CardTitle className="flex items-center gap-2 text-lg">
            <div className="w-8 h-8 bg-blue-500 rounded-lg flex items-center justify-center">
              <Bot className="w-5 h-5 text-white" />
            </div>
            AI-Powered Web Scraper
            {scraping && (
              <Badge className="ml-2 bg-blue-100 text-blue-700 border-blue-200 animate-pulse">
                Running in Background
              </Badge>
            )}
          </CardTitle>
          <p className="text-sm text-muted-foreground">
            Paste a university course listing URL and AI will automatically extract all course data — international fees, IELTS/PTE/TOEFL requirements, intakes, and more. Scraping continues even if you navigate away.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <div className="flex-1 relative">
              <Globe className="absolute left-3 top-3 w-4 h-4 text-muted-foreground" />
              <Input
                placeholder="https://www.university.edu/courses"
                value={scrapeUrl}
                onChange={(e) => setScrapeUrl(e.target.value)}
                className="pl-9 h-11 bg-white"
                disabled={scraping}
              />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="sm:col-span-2 lg:col-span-1">
              <label className="text-xs font-medium text-gray-500 mb-1 block">University</label>
              <Select value={selectedUni} onValueChange={setSelectedUni} disabled={scraping}>
                <SelectTrigger className="bg-white h-9">
                  <SelectValue placeholder="Select university..." />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>+ Create New University</SelectItem>
                  {uniData?.data?.map((u) => (
                    <SelectItem key={u.id} value={String(u.id)}>{u.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {(!selectedUni || selectedUni === ALL) && (
              <>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">University Name</label>
                  <Input placeholder="University of Example" value={newUniName} onChange={(e) => setNewUniName(e.target.value)} className="bg-white h-9" disabled={scraping} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">Country</label>
                  <Input placeholder="United Kingdom" value={newUniCountry} onChange={(e) => setNewUniCountry(e.target.value)} className="bg-white h-9" disabled={scraping} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">City</label>
                  <Input placeholder="London" value={newUniCity} onChange={(e) => setNewUniCity(e.target.value)} className="bg-white h-9" disabled={scraping} />
                </div>
              </>
            )}
          </div>

          <Button
            onClick={startScraping}
            disabled={scraping || !scrapeUrl || (!selectedUni && !newUniName)}
            className="h-11 px-6 bg-blue-600 hover:bg-blue-700"
            size="lg"
          >
            {scraping ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                Scraping in Background...
              </>
            ) : (
              <>
                <Zap className="w-4 h-4 mr-2" />
                Start AI Scraping
                <ArrowRight className="w-4 h-4 ml-2" />
              </>
            )}
          </Button>

          {scrapeLogs.length > 0 && (
            <div className="space-y-3">
              {progressLog && progressLog.total && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>Scraping courses...</span>
                    <span>{progressLog.current}/{progressLog.total}</span>
                  </div>
                  <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded-full transition-all duration-300"
                      style={{ width: `${((progressLog.current ?? 0) / progressLog.total) * 100}%` }}
                    />
                  </div>
                </div>
              )}

              <div ref={logRef} className="bg-gray-900 rounded-lg p-4 max-h-60 overflow-auto font-mono text-xs space-y-1">
                {scrapeLogs.map((log, i) => (
                  <div key={i} className={
                    log.event === "error" ? "text-red-400" :
                    log.event === "course" && log.status === "imported" ? "text-green-400" :
                    log.event === "course" && log.status === "skipped" ? "text-yellow-400" :
                    log.event === "done" ? "text-cyan-400 font-bold" :
                    "text-gray-300"
                  }>
                    {log.event === "status" && <span>[INFO] {log.message}</span>}
                    {log.event === "progress" && <span>[{log.current}/{log.total}] {log.message}</span>}
                    {log.event === "course" && <span>[{log.status?.toUpperCase()}] {log.name}</span>}
                    {log.event === "error" && <span>[ERROR] {log.message}</span>}
                    {log.event === "done" && (
                      <span>
                        === COMPLETE === Found: {log.totalFound} | Imported: {log.imported} | Skipped: {log.skipped} | Errors: {log.errors}
                      </span>
                    )}
                  </div>
                ))}
                {scraping && (
                  <div className="text-blue-400 animate-pulse">
                    <Loader2 className="inline w-3 h-3 animate-spin mr-1" />
                    Processing in background...
                  </div>
                )}
              </div>

              {scrapeResult && (
                <div className="grid grid-cols-4 gap-3">
                  <div className="bg-white border rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-gray-800">{scrapeResult.totalFound}</div>
                    <div className="text-xs text-gray-400">Found</div>
                  </div>
                  <div className="bg-green-50 border border-green-200 rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-green-600">{scrapeResult.imported}</div>
                    <div className="text-xs text-green-500">Imported</div>
                  </div>
                  <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-amber-600">{scrapeResult.skipped}</div>
                    <div className="text-xs text-amber-500">Skipped</div>
                  </div>
                  <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-red-600">{scrapeResult.errors}</div>
                    <div className="text-xs text-red-500">Errors</div>
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <div>
        <h2 className="text-lg font-semibold mb-3">University Coverage</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {uniStats.map((u) => (
            <Link key={u.id} href={`/universities/${u.id}`}>
              <div className="border rounded-xl p-4 hover:shadow-md transition-shadow cursor-pointer bg-white">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="font-semibold text-gray-800 truncate">{u.name}</p>
                    <p className="text-sm text-gray-500">{u.city}, {u.country}</p>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="text-xl font-bold text-blue-600">{u.courseCount}</div>
                    <div className="text-xs text-gray-400">courses</div>
                  </div>
                </div>
                <div className="mt-3 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-blue-500 rounded-full transition-all"
                    style={{ width: `${Math.min((u.courseCount / 400) * 100, 100)}%` }}
                  />
                </div>
              </div>
            </Link>
          ))}
        </div>
      </div>

      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Import History</h2>
          <Button variant="outline" size="sm" onClick={fetchJobs} disabled={loadingJobs}>
            <RefreshCw className={`w-4 h-4 mr-1 ${loadingJobs ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>

        {jobs.length === 0 ? (
          <div className="border rounded-xl p-10 text-center text-gray-400">
            <Clock className="w-8 h-8 mx-auto mb-2 opacity-40" />
            <p>No import jobs yet.</p>
            <p className="text-sm mt-1">Use <Link href="/bulk" className="text-blue-500 underline">Bulk Upload</Link> or the AI Scraper above.</p>
          </div>
        ) : (
          <div className="border rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b">
                <tr>
                  <th className="text-left p-3 font-medium text-gray-600">University</th>
                  <th className="text-left p-3 font-medium text-gray-600">File</th>
                  <th className="text-center p-3 font-medium text-gray-600">Status</th>
                  <th className="text-center p-3 font-medium text-gray-600">Imported</th>
                  <th className="text-center p-3 font-medium text-gray-600">Skipped</th>
                  <th className="text-left p-3 font-medium text-gray-600">Date</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {jobs.map((job) => (
                  <tr key={job.id} className="hover:bg-gray-50">
                    <td className="p-3 font-medium text-gray-800">{job.universityName}</td>
                    <td className="p-3 text-gray-500 text-xs max-w-[180px] truncate">{job.fileName}</td>
                    <td className="p-3 text-center">{statusBadge(job.status)}</td>
                    <td className="p-3 text-center">
                      {job.importedRows != null ? (
                        <span className="font-semibold text-green-600">{job.importedRows}</span>
                      ) : "—"}
                    </td>
                    <td className="p-3 text-center">
                      {job.skippedRows != null ? (
                        <span className="text-amber-600">{job.skippedRows}</span>
                      ) : "—"}
                    </td>
                    <td className="p-3 text-gray-400 text-xs whitespace-nowrap">{fmtDate(job.createdAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

import { useEffect, useState, useRef, useCallback } from "react";
import { useListUniversities } from "@workspace/api-client-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  FileSpreadsheet, CheckCircle2, Clock, AlertCircle, RefreshCw,
  Globe, Zap, Loader2, X, ExternalLink, Bot, ArrowRight,
  Eye, Pencil, Trash2, Check, XCircle, CheckCheck, Save,
  Square, StopCircle, Play, ShieldCheck, Info, PlusCircle, ChevronDown, AlertTriangle,
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

type ApprovalSummary = {
  totalCourses: number;
  validSamples: number;
  rejectedSamples: number;
  sampleTotal: number;
  validExamples: string[];
  rejectedExamples: string[];
  estimatedMinutes: number;
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
  sampleResult?: "valid" | "rejected";
  // approval_required fields
  totalCourses?: number;
  validSamples?: number;
  rejectedSamples?: number;
  sampleTotal?: number;
  validExamples?: string[];
  rejectedExamples?: string[];
  estimatedMinutes?: number;
};

type StagedCourse = {
  id: number;
  scrapeJobId: string;
  universityId: number;
  courseName: string;
  category: string | null;
  subCategory: string | null;
  courseWebsite: string | null;
  duration: number | null;
  durationTerm: string | null;
  studyMode: string | null;
  degreeLevel: string | null;
  studyLoad: string | null;
  language: string | null;
  description: string | null;
  otherRequirement: string | null;
  internationalFee: number | null;
  feeTerm: string | null;
  feeYear: number | null;
  currency: string | null;
  ieltsOverall: number | null;
  ieltsListening: number | null;
  ieltsSpeaking: number | null;
  ieltsWriting: number | null;
  ieltsReading: number | null;
  pteOverall: number | null;
  pteListening: number | null;
  pteSpeaking: number | null;
  pteWriting: number | null;
  pteReading: number | null;
  toeflOverall: number | null;
  toeflListening: number | null;
  toeflSpeaking: number | null;
  toeflWriting: number | null;
  toeflReading: number | null;
  cambridgeOverall: number | null;
  duolingoOverall: number | null;
  intakeMonths: string[] | null;
  academicLevel: string | null;
  academicScore: number | null;
  scoreType: string | null;
  academicCountry: string | null;
  scholarship: string | null;
  status: string;
  completeness: number | null;
  notes: string | null;
  createdAt: string;
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

  const [scrapeUrls, setScrapeUrls] = useState<string[]>([""]);
  const [selectedUni, setSelectedUni] = useState("");
  const [newUniName, setNewUniName] = useState("");
  const [newUniCountry, setNewUniCountry] = useState("");
  const [newUniCity, setNewUniCity] = useState("");
  const [scraping, setScraping] = useState(false);
  const [scrapeLogs, setScrapeLogs] = useState<ScrapeLog[]>([]);
  const [scrapeResult, setScrapeResult] = useState<ScrapeLog | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [urlQueueProgress, setUrlQueueProgress] = useState<{ current: number; total: number } | null>(null);
  const urlQueueRef = useRef<string[]>([]);
  const uniBodyRef = useRef<Record<string, unknown>>({});
  const logIndexRef = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const [stagedCourses, setStagedCourses] = useState<StagedCourse[]>([]);
  const [showReview, setShowReview] = useState(false);
  const [reviewJobId, setReviewJobId] = useState<string | null>(null);
  const [editingCourse, setEditingCourse] = useState<StagedCourse | null>(null);
  const [approving, setApproving] = useState(false);
  const [approvingId, setApprovingId] = useState<number | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [scrapeUniName, setScrapeUniName] = useState("");
  const [scrapeTargetUrl, setScrapeTargetUrl] = useState("");
  const [stopping, setStopping] = useState(false);
  const [awaitingApproval, setAwaitingApproval] = useState<ApprovalSummary | null>(null);
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [feePageUrl, setFeePageUrl] = useState("");
  const [requirementsPageUrl, setRequirementsPageUrl] = useState("");

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

  const loadStagedCourses = useCallback(async (jobId: string) => {
    try {
      const res = await fetch(`/api/scrape/staged/${jobId}`);
      if (res.ok) {
        const data = await res.json();
        setStagedCourses(data.filter((c: StagedCourse) => c.status === "pending"));
        setReviewJobId(jobId);
        setShowReview(true);
        setSelectedIds(new Set(data.filter((c: StagedCourse) => c.status === "pending").map((c: StagedCourse) => c.id)));
      }
    } catch {}
  }, []);

  const startSingleJob = useCallback(async (url: string): Promise<boolean> => {
    const body: Record<string, unknown> = { url, ...uniBodyRef.current };
    const resp = await fetch("/api/scrape/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json();
      setScrapeLogs((prev) => [...prev, { event: "error", message: err.error || "Failed to start scraping" }]);
      return false;
    }
    const data = await resp.json();
    setActiveJobId(data.jobId);
    setScrapeTargetUrl(url);
    sessionStorage.setItem("activeScrapeJob", data.jobId);
    setScrapeLogs((prev) => [...prev, { event: "status", message: `Scraping ${url}...` }]);
    return data.jobId;
  }, []);

  const pollJobStatus = useCallback((jobId: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    logIndexRef.current = 0;

    const poll = async () => {
      try {
        const res = await fetch(`/api/scrape/status/${jobId}?since=${logIndexRef.current}`);
        if (!res.ok) {
          if (res.status === 404) {
            setScraping(false);
            setActiveJobId(null);
            setUrlQueueProgress(null);
            sessionStorage.removeItem("activeScrapeJob");
            if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          }
          return;
        }
        const data = await res.json();

        if (data.universityName) setScrapeUniName(data.universityName);
        if (data.url) setScrapeTargetUrl(data.url);

        if (data.logs && data.logs.length > 0) {
          setScrapeLogs((prev) => [...prev, ...data.logs]);
          logIndexRef.current = data.logIndex;

          const doneLog = data.logs.find((l: ScrapeLog) => l.event === "done");
          if (doneLog) setScrapeResult(doneLog);

          const approvalLog = data.logs.find((l: ScrapeLog) => l.event === "approval_required");
          if (approvalLog) {
            setAwaitingApproval({
              totalCourses: approvalLog.totalCourses ?? 0,
              validSamples: approvalLog.validSamples ?? 0,
              rejectedSamples: approvalLog.rejectedSamples ?? 0,
              sampleTotal: approvalLog.sampleTotal ?? 0,
              validExamples: approvalLog.validExamples ?? [],
              rejectedExamples: approvalLog.rejectedExamples ?? [],
              estimatedMinutes: approvalLog.estimatedMinutes ?? 1,
            });
          }
        }

        // Also sync approval state from status response directly
        if (data.awaitingApproval && !awaitingApproval) {
          setAwaitingApproval(data.awaitingApproval);
        }

        if (data.status !== "running" && data.status !== "awaiting_approval") {
          setStopping(false);
          setAwaitingApproval(null);
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          if ((data.status === "completed" || data.status === "stopped") && data.imported > 0) {
            loadStagedCourses(jobId);
          }

          // Process next URL in queue
          const nextUrl = urlQueueRef.current.shift();
          if (nextUrl) {
            setUrlQueueProgress((prev) => prev ? { ...prev, current: prev.current + 1 } : null);
            setScrapeLogs((prev) => [...prev, { event: "status", message: `── Starting next URL (${nextUrl}) ──` }]);
            const nextJobId = await startSingleJob(nextUrl);
            if (nextJobId) {
              pollJobStatus(nextJobId as string);
            } else {
              setScraping(false);
              setUrlQueueProgress(null);
            }
          } else {
            setScraping(false);
            setUrlQueueProgress(null);
          }
        }
      } catch {}
    };

    poll();
    pollRef.current = setInterval(poll, 1500);
  }, [loadStagedCourses, startSingleJob]);

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

  const stopScraping = useCallback(async () => {
    if (!activeJobId) return;
    setStopping(true);
    setAwaitingApproval(null);
    try {
      await fetch(`/api/scrape/stop/${activeJobId}`, { method: "POST" });
    } catch {}
  }, [activeJobId]);

  const handleApproval = useCallback(async (proceed: boolean) => {
    if (!activeJobId) return;
    setApprovalLoading(true);
    try {
      await fetch(`/api/scrape/approve/${activeJobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proceed }),
      });
      if (!proceed) {
        setScraping(false);
        setStopping(false);
      }
      setAwaitingApproval(null);
    } catch {}
    setApprovalLoading(false);
  }, [activeJobId]);

  const startScraping = useCallback(async () => {
    const validUrls = scrapeUrls.map((u) => u.trim()).filter(Boolean);
    if (validUrls.length === 0) return;

    setScraping(true);
    setScrapeLogs([]);
    setScrapeResult(null);
    setShowReview(false);
    setStagedCourses([]);
    setStopping(false);
    setAwaitingApproval(null);

    const uniBody: Record<string, unknown> = {};
    if (selectedUni && selectedUni !== ALL) {
      uniBody.universityId = parseInt(selectedUni);
      const uni = uniData?.data?.find((u) => String(u.id) === selectedUni);
      if (uni) setScrapeUniName(uni.name);
    } else {
      uniBody.universityName = newUniName;
      uniBody.universityCountry = newUniCountry;
      uniBody.universityCity = newUniCity;
      setScrapeUniName(newUniName);
    }
    if (feePageUrl.trim()) uniBody.feePage = feePageUrl.trim();
    if (requirementsPageUrl.trim()) uniBody.requirementsPage = requirementsPageUrl.trim();
    uniBodyRef.current = uniBody;

    // Queue remaining URLs (all except the first)
    urlQueueRef.current = validUrls.slice(1);
    if (validUrls.length > 1) {
      setUrlQueueProgress({ current: 1, total: validUrls.length });
    } else {
      setUrlQueueProgress(null);
    }

    try {
      setScrapeLogs([{ event: "status", message: "Scraping started in background..." }]);
      const jobId = await startSingleJob(validUrls[0]);
      if (jobId) {
        pollJobStatus(jobId as string);
      } else {
        setScraping(false);
      }
    } catch (err) {
      setScrapeLogs([{ event: "error", message: (err as Error).message }]);
      setScraping(false);
    }
  }, [scrapeUrls, feePageUrl, requirementsPageUrl, selectedUni, newUniName, newUniCountry, newUniCity, startSingleJob, pollJobStatus, uniData]);

  useEffect(() => {
    if (!scraping && activeJobId) {
      sessionStorage.removeItem("activeScrapeJob");
    }
  }, [scraping, activeJobId]);

  const handleApproveSelected = async () => {
    if (!reviewJobId || selectedIds.size === 0) return;
    setApproving(true);
    const succeededIds = new Set<number>();
    const failedIds = new Set<number>();

    for (const id of selectedIds) {
      try {
        const res = await fetch(`/api/scrape/staged/${id}/approve`, { method: "POST" });
        if (res.ok) succeededIds.add(id); else failedIds.add(id);
      } catch { failedIds.add(id); }
    }

    setStagedCourses((prev) => prev.filter((c) => !succeededIds.has(c.id)));
    setSelectedIds(failedIds);
    setApproving(false);
    fetchJobs();
    if (uniData?.data) {
      Promise.all(
        uniData.data.map(async (u) => {
          const res = await fetch(`/api/courses?universityId=${u.id}&limit=1`);
          const d = await res.json();
          return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: d.total ?? 0 };
        })
      ).then(setUniStats);
    }
  };

  const handleRejectSelected = async () => {
    if (selectedIds.size === 0) return;
    const succeededIds = new Set<number>();
    for (const id of selectedIds) {
      try {
        const res = await fetch(`/api/scrape/staged/${id}`, { method: "DELETE" });
        if (res.ok) succeededIds.add(id);
      } catch {}
    }
    setStagedCourses((prev) => prev.filter((c) => succeededIds.has(c.id) ? false : true));
    setSelectedIds((prev) => { const n = new Set<number>(); for (const id of prev) { if (!succeededIds.has(id)) n.add(id); } return n; });
  };

  const handleApproveSingle = async (id: number) => {
    setApprovingId(id);
    try {
      const res = await fetch(`/api/scrape/staged/${id}/approve`, { method: "POST" });
      if (res.ok) {
        setStagedCourses((prev) => prev.filter((c) => c.id !== id));
        setSelectedIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      }
    } catch {}
    setApprovingId(null);
  };

  const handleRejectSingle = async (id: number) => {
    try {
      await fetch(`/api/scrape/staged/${id}`, { method: "DELETE" });
      setStagedCourses((prev) => prev.filter((c) => c.id !== id));
      setSelectedIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
    } catch {}
  };

  const handleSaveEdit = async () => {
    if (!editingCourse) return;
    try {
      await fetch(`/api/scrape/staged/${editingCourse.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editingCourse),
      });
      setStagedCourses((prev) => prev.map((c) => c.id === editingCourse.id ? editingCourse : c));
      setEditingCourse(null);
    } catch {}
  };

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };

  const toggleAll = () => {
    if (selectedIds.size === stagedCourses.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(stagedCourses.map((c) => c.id)));
    }
  };

  const progressLog = scrapeLogs.findLast((l) => l.event === "progress");

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
            Paste a university course listing URL and AI will automatically extract all course data. Scraped courses go to a staging area for your review before saving.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            {scrapeUrls.map((url, idx) => (
              <div key={idx} className="flex gap-2 items-center">
                <div className="flex-1 relative">
                  <Globe className="absolute left-3 top-3 w-4 h-4 text-muted-foreground" />
                  <Input
                    placeholder="https://www.university.edu/courses"
                    value={url}
                    onChange={(e) => {
                      const next = [...scrapeUrls];
                      next[idx] = e.target.value;
                      setScrapeUrls(next);
                    }}
                    className="pl-9 h-11 bg-white"
                    disabled={scraping}
                  />
                </div>
                {scrapeUrls.length > 1 && (
                  <button
                    type="button"
                    onClick={() => setScrapeUrls(scrapeUrls.filter((_, i) => i !== idx))}
                    disabled={scraping}
                    className="text-gray-400 hover:text-red-500 disabled:opacity-40 transition-colors"
                    title="Remove URL"
                  >
                    <X className="w-4 h-4" />
                  </button>
                )}
              </div>
            ))}
            <button
              type="button"
              onClick={() => setScrapeUrls([...scrapeUrls, ""])}
              disabled={scraping}
              className="flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-800 disabled:opacity-40 transition-colors"
            >
              <PlusCircle className="w-4 h-4" />
              Add another URL
            </button>

            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              disabled={scraping}
              className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-700 disabled:opacity-40 transition-colors"
            >
              <ChevronDown className={`w-4 h-4 transition-transform ${showAdvanced ? "rotate-180" : ""}`} />
              {showAdvanced ? "Hide advanced options" : "Advanced: specify fee & requirements pages"}
            </button>

            {showAdvanced && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-gray-100">
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">
                    Fee Schedule Page URL
                    <span className="ml-1 text-gray-400 font-normal">(optional — overrides auto-discovery)</span>
                  </label>
                  <Input
                    placeholder="https://university.edu/fees"
                    value={feePageUrl}
                    onChange={(e) => setFeePageUrl(e.target.value)}
                    className="bg-white h-9 text-sm"
                    disabled={scraping}
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">
                    Entry Requirements Page URL
                    <span className="ml-1 text-gray-400 font-normal">(optional — overrides auto-discovery)</span>
                  </label>
                  <Input
                    placeholder="https://university.edu/entry-requirements"
                    value={requirementsPageUrl}
                    onChange={(e) => setRequirementsPageUrl(e.target.value)}
                    className="bg-white h-9 text-sm"
                    disabled={scraping}
                  />
                </div>
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="sm:col-span-2 lg:col-span-1">
              <label className="text-xs font-medium text-gray-500 mb-1 block">University</label>
              <Select value={selectedUni} onValueChange={(val) => {
                setSelectedUni(val);
                if (val && val !== ALL) {
                  const uni = uniData?.data?.find((u) => String(u.id) === val);
                  if (uni?.scrapeUrl) setScrapeUrls([uni.scrapeUrl]);
                }
              }} disabled={scraping}>
                <SelectTrigger className="bg-white h-9">
                  <SelectValue placeholder="Select university..." />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>+ Create New University</SelectItem>
                  {uniData?.data?.map((u) => (
                    <SelectItem key={u.id} value={String(u.id)}>
                      {u.name}
                      {u.scrapeUrl && <span className="ml-1 text-green-500 text-xs">(saved)</span>}
                    </SelectItem>
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

          <div className="flex gap-2 items-center flex-wrap">
            <Button
              onClick={startScraping}
              disabled={scraping || scrapeUrls.every((u) => !u.trim()) || (!selectedUni && !newUniName)}
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
                  {scrapeUrls.filter((u) => u.trim()).length > 1
                    ? `Start AI Scraping (${scrapeUrls.filter((u) => u.trim()).length} URLs)`
                    : "Start AI Scraping"}
                  <ArrowRight className="w-4 h-4 ml-2" />
                </>
              )}
            </Button>
            {urlQueueProgress && urlQueueProgress.total > 1 && (
              <span className="text-sm text-gray-500 font-medium">
                URL {urlQueueProgress.current} of {urlQueueProgress.total}
              </span>
            )}
            {selectedUni && selectedUni !== ALL && uniData?.data?.find((u) => String(u.id) === selectedUni)?.scrapeConfig && (
              <Button
                onClick={async () => {
                  setScraping(true);
                  setScrapeLogs([]);
                  setScrapeResult(null);
                  setShowReview(false);
                  setStagedCourses([]);
                  setStopping(false);
                  const uni = uniData?.data?.find((u) => String(u.id) === selectedUni);
                  if (uni) setScrapeUniName(uni.name);
                  setScrapeTargetUrl(uni?.scrapeUrl || "");
                  try {
                    const resp = await fetch("/api/scrape/rescrape", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ universityId: parseInt(selectedUni) }),
                    });
                    if (!resp.ok) {
                      const err = await resp.json();
                      setScrapeLogs([{ event: "error", message: err.error || "Failed to start re-scraping" }]);
                      setScraping(false);
                      return;
                    }
                    const data = await resp.json();
                    setActiveJobId(data.jobId);
                    sessionStorage.setItem("activeScrapeJob", data.jobId);
                    setScrapeLogs([{ event: "status", message: "Re-scraping started (no AI, zero cost)..." }]);
                    pollJobStatus(data.jobId);
                  } catch (err) {
                    setScrapeLogs([{ event: "error", message: (err as Error).message }]);
                    setScraping(false);
                  }
                }}
                disabled={scraping}
                variant="outline"
                className="h-11 px-4 border-green-300 text-green-700 hover:bg-green-50"
                size="lg"
              >
                <RefreshCw className="w-4 h-4 mr-2" />
                Re-scrape (No AI)
              </Button>
            )}
            {scraping && (
              <Button
                onClick={stopScraping}
                disabled={stopping}
                variant="destructive"
                className="h-11 px-6"
                size="lg"
              >
                {stopping ? (
                  <>
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                    Stopping...
                  </>
                ) : (
                  <>
                    <Square className="w-4 h-4 mr-2" />
                    Stop Scraping
                  </>
                )}
              </Button>
            )}
          </div>

          {(scraping || scrapeLogs.length > 0) && (
            <div className="space-y-3">
              {(scrapeUniName || scrapeTargetUrl) && (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 space-y-1.5">
                  {scrapeUniName && (
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium text-blue-800">University:</span>
                      <span className="text-blue-700">{scrapeUniName}</span>
                    </div>
                  )}
                  {scrapeTargetUrl && (
                    <div className="flex items-center gap-2 text-sm">
                      <Globe className="w-3.5 h-3.5 text-blue-500 flex-shrink-0" />
                      <a href={scrapeTargetUrl} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline truncate">
                        {scrapeTargetUrl}
                      </a>
                      <ExternalLink className="w-3 h-3 text-blue-400 flex-shrink-0" />
                    </div>
                  )}
                </div>
              )}

              {progressLog && progressLog.total && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>{progressLog.message || "Scraping courses..."}</span>
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

              {scraping && !progressLog && !awaitingApproval && (
                <div className="flex items-center gap-2 text-sm text-blue-600">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span>{scrapeLogs.length > 0 ? (scrapeLogs[scrapeLogs.length - 1]?.message || "Processing...") : "Starting scraper..."}</span>
                </div>
              )}

              {awaitingApproval && (
                <div className="border-2 border-amber-300 bg-amber-50 rounded-xl p-5 space-y-4">
                  <div className="flex items-center gap-2">
                    <ShieldCheck className="w-5 h-5 text-amber-600" />
                    <span className="font-semibold text-amber-900">Research Complete — Confirm Bulk Fetch</span>
                  </div>

                  <div className="grid grid-cols-3 gap-3 text-center">
                    <div className="bg-white border border-amber-200 rounded-lg p-3">
                      <div className="text-2xl font-bold text-gray-800">{awaitingApproval.totalCourses}</div>
                      <div className="text-xs text-gray-500 mt-0.5">Courses Found</div>
                    </div>
                    <div className="bg-white border border-green-200 rounded-lg p-3">
                      <div className="text-2xl font-bold text-green-700">{awaitingApproval.validSamples}</div>
                      <div className="text-xs text-gray-500 mt-0.5">Samples Valid</div>
                    </div>
                    <div className="bg-white border border-red-200 rounded-lg p-3">
                      <div className="text-2xl font-bold text-red-600">{awaitingApproval.rejectedSamples}</div>
                      <div className="text-xs text-gray-500 mt-0.5">Samples Rejected</div>
                    </div>
                  </div>

                  {awaitingApproval.validExamples.length > 0 && (
                    <div className="space-y-1.5">
                      <div className="text-xs font-medium text-gray-600 uppercase tracking-wide">Valid Course Samples</div>
                      <div className="space-y-1">
                        {awaitingApproval.validExamples.map((name, i) => (
                          <div key={i} className="flex items-center gap-1.5 text-sm text-green-700">
                            <CheckCircle2 className="w-3.5 h-3.5 flex-shrink-0" />
                            <span className="truncate">{name}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {awaitingApproval.rejectedExamples.length > 0 && (
                    <div className="space-y-1.5">
                      <div className="text-xs font-medium text-gray-600 uppercase tracking-wide">Rejected Samples</div>
                      <div className="space-y-1">
                        {awaitingApproval.rejectedExamples.map((name, i) => (
                          <div key={i} className="flex items-center gap-1.5 text-sm text-red-600">
                            <XCircle className="w-3.5 h-3.5 flex-shrink-0" />
                            <span className="truncate">{name}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="flex items-center gap-1.5 text-xs text-amber-700 bg-amber-100 rounded-lg px-3 py-2">
                    <Info className="w-3.5 h-3.5 flex-shrink-0" />
                    <span>Estimated time: ~{awaitingApproval.estimatedMinutes} minute{awaitingApproval.estimatedMinutes !== 1 ? "s" : ""} to fetch all {awaitingApproval.totalCourses} course pages in parallel.</span>
                  </div>

                  <div className="flex gap-3">
                    <Button
                      onClick={() => handleApproval(true)}
                      disabled={approvalLoading}
                      className="flex-1 bg-green-600 hover:bg-green-700 h-11"
                      size="lg"
                    >
                      {approvalLoading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Play className="w-4 h-4 mr-2" />}
                      Proceed — Fetch {awaitingApproval.totalCourses} Courses
                    </Button>
                    <Button
                      onClick={() => handleApproval(false)}
                      disabled={approvalLoading}
                      variant="outline"
                      className="border-red-300 text-red-600 hover:bg-red-50 h-11 px-6"
                      size="lg"
                    >
                      <X className="w-4 h-4 mr-2" />
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              <div ref={logRef} className="bg-gray-900 rounded-lg p-4 max-h-60 overflow-auto font-mono text-xs space-y-1">
                {scrapeLogs.map((log, i) => (
                  <div key={i} className={
                    log.event === "error" ? "text-red-400" :
                    log.event === "approval_required" ? "text-amber-400 font-semibold" :
                    log.event === "course" && (log.status === "staged" || log.status === "staged (cheerio only)") ? "text-green-400" :
                    log.event === "course" && log.status === "skipped" ? "text-yellow-400" :
                    log.event === "done" ? "text-cyan-400 font-bold" :
                    log.event === "status" && log.sampleResult === "valid" ? "text-green-300" :
                    log.event === "status" && log.sampleResult === "rejected" ? "text-red-300" :
                    "text-gray-300"
                  }>
                    {log.event === "status" && <span>[INFO] {log.message}</span>}
                    {log.event === "approval_required" && <span>[WAITING] {log.message}</span>}
                    {log.event === "progress" && <span>[{log.current}/{log.total}] {log.message}</span>}
                    {log.event === "course" && <span>[{log.status?.toUpperCase()}] {log.name}</span>}
                    {log.event === "error" && <span>[ERROR] {log.message}</span>}
                    {log.event === "done" && (
                      <span>
                        === COMPLETE === Found: {log.totalFound} | Staged: {log.imported} | Skipped: {log.skipped} | Errors: {log.errors}
                      </span>
                    )}
                  </div>
                ))}
                {scraping && !awaitingApproval && (
                  <div className="text-blue-400 animate-pulse">
                    <Loader2 className="inline w-3 h-3 animate-spin mr-1" />
                    Processing in background...
                  </div>
                )}
                {awaitingApproval && (
                  <div className="text-amber-400 animate-pulse">
                    Waiting for your confirmation above...
                  </div>
                )}
              </div>

              {scrapeResult && (
                <div className="grid grid-cols-4 gap-3">
                  <div className="bg-white border rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-gray-800">{scrapeResult.totalFound}</div>
                    <div className="text-xs text-gray-400">Found</div>
                  </div>
                  <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-center">
                    <div className="text-2xl font-bold text-blue-600">{scrapeResult.imported}</div>
                    <div className="text-xs text-blue-500">Staged for Review</div>
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

              {scrapeResult && !showReview && activeJobId && (
                <Button onClick={() => loadStagedCourses(activeJobId)} className="w-full bg-green-600 hover:bg-green-700">
                  <Eye className="w-4 h-4 mr-2" />
                  Review Scraped Courses
                </Button>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {showReview && stagedCourses.length > 0 && (
        <Card className="border-2 border-green-100">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="flex items-center gap-2 text-lg">
                <Eye className="w-5 h-5 text-green-600" />
                Review Scraped Courses
                <Badge className="bg-blue-100 text-blue-700">{stagedCourses.length} pending</Badge>
              </CardTitle>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="text-red-600 border-red-200 hover:bg-red-50"
                  onClick={handleRejectSelected}
                  disabled={selectedIds.size === 0 || approving}
                >
                  <XCircle className="w-4 h-4 mr-1" />
                  Reject ({selectedIds.size})
                </Button>
                <Button
                  size="sm"
                  className="bg-green-600 hover:bg-green-700"
                  onClick={handleApproveSelected}
                  disabled={selectedIds.size === 0 || approving}
                >
                  {approving ? <Loader2 className="w-4 h-4 mr-1 animate-spin" /> : <CheckCheck className="w-4 h-4 mr-1" />}
                  Approve ({selectedIds.size})
                </Button>
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              Review each course below. Edit any details, then approve to save to the database or reject to discard. Existing courses with the same name will be updated.
            </p>
          </CardHeader>
          <CardContent>
            <div className="border rounded-lg overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b">
                    <tr>
                      <th className="p-2 w-10">
                        <input
                          type="checkbox"
                          checked={selectedIds.size === stagedCourses.length && stagedCourses.length > 0}
                          onChange={toggleAll}
                          className="rounded border-gray-300"
                        />
                      </th>
                      <th className="text-left p-2 font-medium text-gray-600 min-w-[200px]">Course Name</th>
                      <th className="text-center p-2 font-medium text-gray-600 w-16">Score</th>
                      <th className="text-left p-2 font-medium text-gray-600">Level</th>
                      <th className="text-left p-2 font-medium text-gray-600">Duration</th>
                      <th className="text-right p-2 font-medium text-gray-600">Intl. Fee</th>
                      <th className="text-center p-2 font-medium text-purple-600">IELTS</th>
                      <th className="text-center p-2 font-medium text-orange-600">PTE</th>
                      <th className="text-center p-2 font-medium text-rose-600">TOEFL</th>
                      <th className="text-center p-2 font-medium text-teal-600">CAE</th>
                      <th className="text-center p-2 font-medium text-emerald-600">DET</th>
                      <th className="text-left p-2 font-medium text-gray-600">Intakes</th>
                      <th className="text-left p-2 font-medium text-gray-600">Mode</th>
                      <th className="text-center p-2 font-medium text-gray-600 w-[120px]">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {stagedCourses.map((course) => (
                      <tr key={course.id} className={`hover:bg-gray-50 ${selectedIds.has(course.id) ? "bg-blue-50/50" : ""}`}>
                        <td className="p-2">
                          <input
                            type="checkbox"
                            checked={selectedIds.has(course.id)}
                            onChange={() => toggleSelect(course.id)}
                            className="rounded border-gray-300"
                          />
                        </td>
                        <td className="p-2">
                          <div className="flex items-center gap-1 max-w-[260px]">
                            <span className="font-medium text-gray-800 truncate" title={course.courseName}>
                              {course.courseName}
                            </span>
                            {course.courseWebsite && (
                              <a
                                href={course.courseWebsite}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={`Verify: ${course.courseWebsite}`}
                                className="flex-shrink-0 text-blue-400 hover:text-blue-600 transition-colors"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ExternalLink className="w-3.5 h-3.5" />
                              </a>
                            )}
                          </div>
                          {course.category && (
                            <div className="text-xs text-gray-400 truncate">{course.category}</div>
                          )}
                          {course.notes && (
                            <div className="text-xs text-amber-600 truncate mt-0.5" title={course.notes}>⚠ {course.notes}</div>
                          )}
                        </td>
                        <td className="p-2 text-center">
                          {course.completeness != null ? (
                            <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-semibold ${
                              course.completeness >= 80 ? "bg-green-100 text-green-700" :
                              course.completeness >= 50 ? "bg-yellow-100 text-yellow-700" :
                              "bg-red-100 text-red-700"
                            }`}>{course.completeness}%</span>
                          ) : <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2">
                          {course.degreeLevel ? (
                            <Badge variant="outline" className="text-xs">{course.degreeLevel}</Badge>
                          ) : <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2 text-gray-600 whitespace-nowrap">
                          {course.duration ? `${course.duration} ${course.durationTerm || ""}` : <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2 text-right font-medium whitespace-nowrap">
                          {course.internationalFee ? (
                            <span className="text-green-700">
                              {course.currency === "GBP" ? "\u00A3" : course.currency === "USD" ? "$" : "A$"}
                              {course.internationalFee.toLocaleString()}
                              <span className="text-xs text-gray-400 ml-1">/{course.feeTerm || "yr"}</span>
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-0.5 text-amber-600 text-xs font-medium" title="Missing international fee">
                              <AlertTriangle className="w-3 h-3" />
                            </span>
                          )}
                        </td>
                        <td className="p-2 text-center">
                          {course.ieltsOverall ? (
                            <span className="text-purple-700 font-medium">{course.ieltsOverall}</span>
                          ) : (
                            <span className="inline-flex items-center gap-0.5 text-amber-600 text-xs font-medium" title="Missing IELTS Overall">
                              <AlertTriangle className="w-3 h-3" />
                            </span>
                          )}
                        </td>
                        <td className="p-2 text-center">
                          {course.pteOverall ? (
                            <span className="text-orange-600 font-medium">{course.pteOverall}</span>
                          ) : <span className="text-gray-300 text-xs">-</span>}
                        </td>
                        <td className="p-2 text-center">
                          {course.toeflOverall ? (
                            <span className="text-rose-600 font-medium">{course.toeflOverall}</span>
                          ) : <span className="text-gray-300 text-xs">-</span>}
                        </td>
                        <td className="p-2 text-center">
                          {course.cambridgeOverall ? (
                            <span className="text-teal-600 font-medium">{course.cambridgeOverall}</span>
                          ) : <span className="text-gray-300 text-xs">-</span>}
                        </td>
                        <td className="p-2 text-center">
                          {course.duolingoOverall ? (
                            <span className="text-emerald-600 font-medium">{course.duolingoOverall}</span>
                          ) : <span className="text-gray-300 text-xs">-</span>}
                        </td>
                        <td className="p-2 text-xs text-gray-600">
                          {course.intakeMonths?.length ? (
                            course.intakeMonths.map(m => m.slice(0, 3)).join(", ")
                          ) : (
                            <span className="inline-flex items-center gap-0.5 text-amber-600 text-xs font-medium" title="Missing intake months">
                              <AlertTriangle className="w-3 h-3" />
                            </span>
                          )}
                        </td>
                        <td className="p-2 text-xs text-gray-600">
                          {course.studyMode || <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2">
                          <div className="flex gap-1 justify-center">
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-blue-600 hover:bg-blue-50"
                              onClick={() => setEditingCourse({ ...course })}
                              title="Edit"
                            >
                              <Pencil className="w-3.5 h-3.5" />
                            </Button>
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-green-600 hover:bg-green-50"
                              onClick={() => handleApproveSingle(course.id)}
                              disabled={approvingId === course.id}
                              title="Approve"
                            >
                              {approvingId === course.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                            </Button>
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-red-600 hover:bg-red-50"
                              onClick={() => handleRejectSingle(course.id)}
                              title="Reject"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {showReview && stagedCourses.length === 0 && (
        <Card className="border-2 border-green-100">
          <CardContent className="p-10 text-center">
            <CheckCircle2 className="w-10 h-10 text-green-500 mx-auto mb-3" />
            <h3 className="font-semibold text-lg">All courses reviewed</h3>
            <p className="text-muted-foreground text-sm mt-1">All scraped courses have been approved or rejected.</p>
          </CardContent>
        </Card>
      )}

      <Dialog open={!!editingCourse} onOpenChange={(o) => { if (!o) setEditingCourse(null); }}>
        <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Edit Scraped Course</DialogTitle>
          </DialogHeader>
          {editingCourse && (
            <div className="grid grid-cols-2 gap-4">
              <div className="col-span-2">
                <label className="text-xs font-medium text-gray-500 mb-1 block">Course Name</label>
                <Input value={editingCourse.courseName} onChange={(e) => setEditingCourse({ ...editingCourse, courseName: e.target.value })} />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Category</label>
                <Input value={editingCourse.category || ""} onChange={(e) => setEditingCourse({ ...editingCourse, category: e.target.value || null })} />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Sub Category</label>
                <Input value={editingCourse.subCategory || ""} onChange={(e) => setEditingCourse({ ...editingCourse, subCategory: e.target.value || null })} />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Degree Level</label>
                <Select value={editingCourse.degreeLevel || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, degreeLevel: v || null })}>
                  <SelectTrigger><SelectValue placeholder="Select..." /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="Bachelor">Bachelor</SelectItem>
                    <SelectItem value="Master">Master</SelectItem>
                    <SelectItem value="PhD">PhD</SelectItem>
                    <SelectItem value="Certificate & Diploma">Certificate & Diploma</SelectItem>
                    <SelectItem value="Graduate Certificate & Diploma">Graduate Certificate & Diploma</SelectItem>
                    <SelectItem value="Associate Degree">Associate Degree</SelectItem>
                    <SelectItem value="Equivalent">Equivalent</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Study Mode</label>
                <Select value={editingCourse.studyMode || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, studyMode: v || null })}>
                  <SelectTrigger><SelectValue placeholder="Select..." /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="On Campus">On Campus</SelectItem>
                    <SelectItem value="Online">Online</SelectItem>
                    <SelectItem value="Blended">Blended</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Duration</label>
                <div className="flex gap-2">
                  <Input type="number" value={editingCourse.duration ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, duration: e.target.value ? parseFloat(e.target.value) : null })} className="w-24" />
                  <Select value={editingCourse.durationTerm || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, durationTerm: v || null })}>
                    <SelectTrigger className="w-28"><SelectValue placeholder="Term" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="Year">Year</SelectItem>
                      <SelectItem value="Month">Month</SelectItem>
                      <SelectItem value="Week">Week</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Study Load</label>
                <Select value={editingCourse.studyLoad || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, studyLoad: v || null })}>
                  <SelectTrigger><SelectValue placeholder="Select..." /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="Full Time">Full Time</SelectItem>
                    <SelectItem value="Part Time">Part Time</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-gray-700 mb-2">International Fees</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Fee Amount</label>
                <Input type="number" value={editingCourse.internationalFee ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, internationalFee: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div className="flex gap-2">
                <div className="flex-1">
                  <label className="text-xs font-medium text-gray-500 mb-1 block">Currency</label>
                  <Select value={editingCourse.currency || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, currency: v || null })}>
                    <SelectTrigger><SelectValue placeholder="Currency" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="AUD">AUD</SelectItem>
                      <SelectItem value="GBP">GBP</SelectItem>
                      <SelectItem value="USD">USD</SelectItem>
                      <SelectItem value="EUR">EUR</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex-1">
                  <label className="text-xs font-medium text-gray-500 mb-1 block">Fee Term</label>
                  <Select value={editingCourse.feeTerm || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, feeTerm: v || null })}>
                    <SelectTrigger><SelectValue placeholder="Term" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="Annual">Annual</SelectItem>
                      <SelectItem value="Total">Total</SelectItem>
                      <SelectItem value="Semester">Semester</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-gray-700 mb-2">English Requirements</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">IELTS Overall</label>
                <Input type="number" step="0.5" value={editingCourse.ieltsOverall ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, ieltsOverall: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div className="grid grid-cols-4 gap-2">
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">L</label>
                  <Input type="number" step="0.5" value={editingCourse.ieltsListening ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, ieltsListening: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">R</label>
                  <Input type="number" step="0.5" value={editingCourse.ieltsReading ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, ieltsReading: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">W</label>
                  <Input type="number" step="0.5" value={editingCourse.ieltsWriting ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, ieltsWriting: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">S</label>
                  <Input type="number" step="0.5" value={editingCourse.ieltsSpeaking ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, ieltsSpeaking: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-orange-600 mb-2">PTE Academic</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">PTE Overall</label>
                <Input type="number" value={editingCourse.pteOverall ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, pteOverall: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div className="grid grid-cols-4 gap-2">
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">L</label>
                  <Input type="number" value={editingCourse.pteListening ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, pteListening: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">R</label>
                  <Input type="number" value={editingCourse.pteReading ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, pteReading: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">W</label>
                  <Input type="number" value={editingCourse.pteWriting ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, pteWriting: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">S</label>
                  <Input type="number" value={editingCourse.pteSpeaking ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, pteSpeaking: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-rose-600 mb-2">TOEFL iBT</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">TOEFL Overall</label>
                <Input type="number" value={editingCourse.toeflOverall ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, toeflOverall: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div className="grid grid-cols-4 gap-2">
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">L</label>
                  <Input type="number" value={editingCourse.toeflListening ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, toeflListening: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">R</label>
                  <Input type="number" value={editingCourse.toeflReading ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, toeflReading: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">W</label>
                  <Input type="number" value={editingCourse.toeflWriting ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, toeflWriting: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">S</label>
                  <Input type="number" value={editingCourse.toeflSpeaking ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, toeflSpeaking: e.target.value ? parseFloat(e.target.value) : null })} />
                </div>
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-teal-600 mb-2">Cambridge & Duolingo</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Cambridge CAE Overall</label>
                <Input type="number" value={editingCourse.cambridgeOverall ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, cambridgeOverall: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Duolingo Overall</label>
                <Input type="number" value={editingCourse.duolingoOverall ?? ""} onChange={(e) => setEditingCourse({ ...editingCourse, duolingoOverall: e.target.value ? parseFloat(e.target.value) : null })} />
              </div>
              <div className="col-span-2 border-t pt-3">
                <h4 className="text-sm font-semibold text-gray-700 mb-2">Other</h4>
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Intake Months (comma-separated)</label>
                <Input
                  value={editingCourse.intakeMonths?.join(", ") || ""}
                  onChange={(e) => setEditingCourse({ ...editingCourse, intakeMonths: e.target.value ? e.target.value.split(",").map(s => s.trim()).filter(Boolean) : null })}
                  placeholder="January, March, July"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Course Website</label>
                <Input value={editingCourse.courseWebsite || ""} onChange={(e) => setEditingCourse({ ...editingCourse, courseWebsite: e.target.value || null })} />
              </div>
              <div className="col-span-2">
                <label className="text-xs font-medium text-gray-500 mb-1 block">Description</label>
                <Textarea rows={3} value={editingCourse.description || ""} onChange={(e) => setEditingCourse({ ...editingCourse, description: e.target.value || null })} />
              </div>
              <div className="col-span-2">
                <label className="text-xs font-medium text-gray-500 mb-1 block">Other Requirements</label>
                <Textarea rows={2} value={editingCourse.otherRequirement || ""} onChange={(e) => setEditingCourse({ ...editingCourse, otherRequirement: e.target.value || null })} />
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingCourse(null)}>Cancel</Button>
            <Button onClick={handleSaveEdit} className="bg-blue-600 hover:bg-blue-700">
              <Save className="w-4 h-4 mr-1" />
              Save Changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
                      ) : "\u2014"}
                    </td>
                    <td className="p-3 text-center">
                      {job.skippedRows != null ? (
                        <span className="text-amber-600">{job.skippedRows}</span>
                      ) : "\u2014"}
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

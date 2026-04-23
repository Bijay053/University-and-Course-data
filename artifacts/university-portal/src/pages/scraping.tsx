import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import { useListUniversities } from "@workspace/api-client-react";
import { useToast } from "@/hooks/use-toast";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ChevronsUpDown, Search } from "lucide-react";
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
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";

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

type ScrapeStatusResponse = {
  universityName?: string;
  url?: string;
  logs?: ScrapeLog[];
  logIndex?: number;
  status?: string;
  imported?: number;
  awaitingApproval?: ApprovalSummary;
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
  courseLocation: string | null;
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
  studentMarket: string | null;
  deliveryMode: string | null;
  internationalEligible: boolean | null;
  onCampusAvailable: boolean | null;
  eligibilityStatus: string | null;
  eligibilityReason: string | null;
  eligibilityConfidence: number | null;
  autoPublishStatus: string | null;
  decisionScore: number | null;
  status: string;
  completeness: number | null;
  notes: string | null;
  createdAt: string;
};

type ReviewEvidence = {
  id: number;
  fieldKey: string;
  candidateValue: string | null;
  sourceUrl: string | null;
  pageType: string | null;
  extractionMethod: string | null;
  snippet: string | null;
  confidence: number | null;
  decisionStatus: string;
  selected: boolean;
};

type ReviewConflict = {
  id: number;
  fieldKey: string;
  valueA: string | null;
  valueB: string | null;
  reason: string | null;
  status: string;
};

type CourseReviewPayload = {
  course: StagedCourse;
  evidence: ReviewEvidence[];
  conflicts: ReviewConflict[];
};

const ALL = "__new__";

type UniLite = { id: number; name: string; scrapeUrl?: string | null };

function UniversityCombobox({
  value,
  onChange,
  universities,
  disabled,
}: {
  value: string;
  onChange: (val: string) => void;
  universities: UniLite[];
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const selected = value && value !== ALL ? universities.find((u) => String(u.id) === value) : null;
  const label = value === ALL ? "+ Create New University" : selected ? selected.name : "Select university...";

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return universities;
    return universities.filter((u) => u.name.toLowerCase().includes(q));
  }, [universities, search]);

  useEffect(() => {
    if (open) {
      setSearch("");
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          disabled={disabled}
          className="bg-white h-9 w-full justify-between font-normal"
        >
          <span className={`truncate ${!selected && value !== ALL ? "text-muted-foreground" : ""}`}>{label}</span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="p-0 w-[--radix-popover-trigger-width] min-w-[280px]" align="start">
        <div className="flex flex-col">
          <div className="flex items-center border-b px-3 py-2">
            <Search className="mr-2 h-4 w-4 shrink-0 opacity-50" />
            <input
              ref={inputRef}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search universities..."
              className="flex h-7 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
          </div>
          <div className="max-h-[300px] overflow-y-auto p-1">
            <button
              type="button"
              onClick={() => { onChange(ALL); setOpen(false); }}
              className="flex w-full items-center rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
            >
              <span className="text-blue-600 font-medium">+ Create New University</span>
            </button>
            {filtered.length === 0 && search.trim() && (
              <div className="py-6 text-center text-sm text-muted-foreground">No university found.</div>
            )}
            {filtered.map((u) => (
              <button
                key={u.id}
                type="button"
                onClick={() => { onChange(String(u.id)); setOpen(false); }}
                className="flex w-full items-center rounded-sm px-2 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground"
              >
                <span className="truncate">{u.name}</span>
                {u.scrapeUrl && <span className="ml-2 text-green-600 text-xs">(saved)</span>}
              </button>
            ))}
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
const MAX_SCRAPE_LOG_LINES = 800;
const SCRAPE_POLL_BASE_DELAY_MS = 1500;
const SCRAPE_POLL_MAX_DELAY_MS = 10000;
const SCRAPE_POLL_TIMEOUT_MS = 360000;
const SCRAPE_POLL_WARNING_AFTER_FAILURES = 4;
const SCRAPE_POLL_WARNING_AFTER_IDLE_MS = 120000;

function statusBadge(status: string) {
  if (status === "completed") return <Badge className="bg-green-100 text-green-700 border-green-200">Completed</Badge>;
  if (status === "completed_with_errors") return <Badge className="bg-amber-100 text-amber-700 border-amber-200">Completed (Errors)</Badge>;
  if (status === "queued") return <Badge className="bg-slate-100 text-slate-700 border-slate-200">Queued</Badge>;
  if (status === "running") return <Badge className="bg-blue-100 text-blue-700 border-blue-200">Running</Badge>;
  return <Badge variant="secondary">{status}</Badge>;
}

function fmtDate(s: string) {
  return new Date(s).toLocaleString("en-AU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function Scraping() {
  const { toast } = useToast();
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
  const [scrapeStartTime, setScrapeStartTime] = useState<number | null>(null);
  // `now` ticks every second while a scrape is running so the "(Xs elapsed)"
  // counter updates live instead of only when the status poll fires
  // (which is every 5s, and gets throttled to >1min on background tabs).
  const [now, setNow] = useState<number>(() => Date.now());
  const urlQueueRef = useRef<string[]>([]);
  const uniBodyRef = useRef<Record<string, unknown>>({});
  const logIndexRef = useRef(0);
  const logRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollFailureCountRef = useRef(0);
  const pollInFlightRef = useRef(false);
  const pollWarningShownRef = useRef(false);
  const pollLastSuccessAtRef = useRef(Date.now());
  const pollRequestTimeoutRef = useRef<number | null>(null);

  const [stagedCourses, setStagedCourses] = useState<StagedCourse[]>([]);
  const [lastScrapeInfo, setLastScrapeInfo] = useState<{ jobId: string; startedAt: string | null; completedAt: string | null; durationMs: number | null; totalFound: number; staged: number; skipped: number; errors: number } | null>(null);
  const [showReview, setShowReview] = useState(false);
  const [reviewJobId, setReviewJobId] = useState<string | null>(null);
  const [editingCourse, setEditingCourse] = useState<StagedCourse | null>(null);
  const [reviewDetail, setReviewDetail] = useState<CourseReviewPayload | null>(null);
  const [rejectingIds, setRejectingIds] = useState<number[] | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [rejectFieldKey, setRejectFieldKey] = useState("general");
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
  const [scholarshipPageUrl, setScholarshipPageUrl] = useState("");
  const [academicRequirementsPageUrl, setAcademicRequirementsPageUrl] = useState("");
  const [fastMode, setFastMode] = useState(false);

  // ── Scrape History (persistent, browseable after a run completes) ────────
  type HistoryRun = {
    runtimeJobId: string;
    universityId: number | null;
    universityName: string | null;
    url: string | null;
    status: string;
    totalFound: number | null;
    imported: number | null;
    skipped: number | null;
    errors: number | null;
    startedAt: string | null;
    completedAt: string | null;
    errorMessage: string | null;
    durationMs: number | null;
    stagedCount: number;
    approvedCount: number;
    rejectedCount: number;
  };
  type HistoryLogEntry = { sequence: number; event: string; createdAt: string; message?: string; phase?: string; [k: string]: unknown };
  type HistoryStagedCourse = {
    id: number; courseName: string | null; status: string | null; autoPublishStatus: string | null;
    eligibilityStatus: string | null; ieltsOverall: string | null; pteOverall: string | null;
    toeflOverall: string | null; internationalFee: string | null; duration: string | null;
    durationTerm: string | null; category: string | null; degreeLevel: string | null;
  };
  const [historyRuns, setHistoryRuns] = useState<HistoryRun[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [expandedHistoryId, setExpandedHistoryId] = useState<string | null>(null);
  const [historyDetailLoading, setHistoryDetailLoading] = useState(false);
  const [historyDetail, setHistoryDetail] = useState<{ logs: HistoryLogEntry[]; stagedCourses: HistoryStagedCourse[] } | null>(null);
  const [historyView, setHistoryView] = useState<"logs" | "courses">("logs");
  const [historyLogFilter, setHistoryLogFilter] = useState("");

  const fetchHistory = useCallback(async () => {
    setLoadingHistory(true);
    try {
      const res = await fetch("/api/scrape/history?limit=50");
      const data = await readResponseJson<{ runs: HistoryRun[] }>(res);
      setHistoryRuns(data?.runs ?? []);
    } catch {
      // Non-fatal — empty state will render.
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  const openHistoryDetail = useCallback(async (runtimeJobId: string, view: "logs" | "courses") => {
    if (expandedHistoryId === runtimeJobId && historyView === view) {
      setExpandedHistoryId(null);
      return;
    }
    setExpandedHistoryId(runtimeJobId);
    setHistoryView(view);
    setHistoryLogFilter("");
    setHistoryDetailLoading(true);
    setHistoryDetail(null);
    try {
      const res = await fetch(`/api/scrape/history/${runtimeJobId}`);
      const data = await readResponseJson<{ logs: HistoryLogEntry[]; stagedCourses: HistoryStagedCourse[] }>(res);
      setHistoryDetail({ logs: data?.logs ?? [], stagedCourses: data?.stagedCourses ?? [] });
    } catch {
      setHistoryDetail({ logs: [], stagedCourses: [] });
    } finally {
      setHistoryDetailLoading(false);
    }
  }, [expandedHistoryId, historyView]);

  useEffect(() => {
    void fetchHistory();
  }, [fetchHistory]);

  const formatHistoryDuration = (ms: number | null): string => {
    if (!ms || ms < 0) return "—";
    const totalSec = Math.floor(ms / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    if (m === 0) return `${s}s`;
    return `${m}m ${s}s`;
  };

  const formatHistoryDate = (iso: string | null): string => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };

  const historyStatusBadge = (status: string) => {
    const map: Record<string, { label: string; cls: string }> = {
      completed: { label: "✓", cls: "bg-green-100 text-green-700" },
      completed_with_errors: { label: "⚠", cls: "bg-amber-100 text-amber-700" },
      failed: { label: "✗", cls: "bg-red-100 text-red-700" },
      stopped: { label: "■", cls: "bg-gray-200 text-gray-700" },
      running: { label: "●", cls: "bg-blue-100 text-blue-700" },
      queued: { label: "…", cls: "bg-gray-100 text-gray-600" },
      awaiting_approval: { label: "?", cls: "bg-yellow-100 text-yellow-700" },
    };
    const s = map[status] ?? { label: status, cls: "bg-gray-100 text-gray-600" };
    return <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-mono ${s.cls}`}>{s.label}</span>;
  };

  const { data: uniData } = useListUniversities({ limit: 100 });

  const fetchJobs = async () => {
    setLoadingJobs(true);
    try {
      const res = await fetch("/api/import/history");
      if (res.ok) {
        const rows = await readResponseJson<ImportJob[]>(res);
        if (rows) setJobs(rows);
      }
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
        if (!res.ok) {
          return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: 0 };
        }
        const d = await readResponseJson<{ total?: number }>(res);
        return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: d?.total ?? 0 };
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
        const payload = await readResponseJson<unknown>(res);
        if (!payload) return;
        // Backward-compat: old endpoint returned Array<StagedCourse>; new endpoint returns { courses, lastScrape }
        const data: StagedCourse[] = Array.isArray(payload)
          ? (payload as StagedCourse[])
          : ((payload as { courses?: StagedCourse[] }).courses ?? []);
        const lastScrape = Array.isArray(payload)
          ? null
          : ((payload as { lastScrape?: typeof lastScrapeInfo }).lastScrape ?? null);
        if (lastScrape) setLastScrapeInfo(lastScrape);
        setStagedCourses(data.filter((c: StagedCourse) => c.status === "pending"));
        setReviewJobId(jobId);
        setShowReview(true);
        setSelectedIds(new Set(data.filter((c: StagedCourse) => c.status === "pending").map((c: StagedCourse) => c.id)));
      }
    } catch {}
  }, []);

  const resetActiveScrapeState = useCallback((message?: string) => {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
    if (pollRequestTimeoutRef.current !== null) {
      window.clearTimeout(pollRequestTimeoutRef.current);
      pollRequestTimeoutRef.current = null;
    }
    pollFailureCountRef.current = 0;
    pollLastSuccessAtRef.current = Date.now();
    setScraping(false);
    setStopping(false);
    setAwaitingApproval(null);
    setActiveJobId(null);
    setUrlQueueProgress(null);
    sessionStorage.removeItem("activeScrapeJob");
    if (message) {
      setScrapeLogs((prev) => [...prev, { event: "error", message }].slice(-MAX_SCRAPE_LOG_LINES));
    }
  }, []);

  const startSingleJob = useCallback(async (url: string): Promise<string | false> => {
    const body: Record<string, unknown> = { url, ...uniBodyRef.current };
    const resp = await fetch("/api/scrape/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const msg = await getFetchErrorMessage(resp);
      setScrapeLogs((prev) => [...prev, { event: "error", message: msg }].slice(-MAX_SCRAPE_LOG_LINES));
      return false;
    }
    const data = await readResponseJson<{ jobId: string }>(resp);
    if (!data?.jobId) {
      setScrapeLogs((prev) => [...prev, { event: "error", message: "Invalid response from server" }].slice(-MAX_SCRAPE_LOG_LINES));
      return false;
    }
    setActiveJobId(data.jobId);
    setScrapeTargetUrl(url);
    sessionStorage.setItem("activeScrapeJob", data.jobId);
    setScrapeLogs((prev) => [...prev, { event: "status", message: `Scraping ${url}...` }].slice(-MAX_SCRAPE_LOG_LINES));
    return data.jobId;
  }, []);

  const pollJobStatus = useCallback((jobId: string) => {
    if (pollRef.current) clearTimeout(pollRef.current);
    if (pollRequestTimeoutRef.current !== null) {
      window.clearTimeout(pollRequestTimeoutRef.current);
      pollRequestTimeoutRef.current = null;
    }
    logIndexRef.current = 0;
    pollFailureCountRef.current = 0;
    pollInFlightRef.current = false;
    pollWarningShownRef.current = false;
    pollLastSuccessAtRef.current = Date.now();

    const scheduleNextPoll = (delayMs: number) => {
      if (pollRef.current) clearTimeout(pollRef.current);
      pollRef.current = window.setTimeout(() => {
        void poll();
      }, delayMs);
    };

    const maybeReportPollingDelay = () => {
      const idleMs = Date.now() - pollLastSuccessAtRef.current;
      if (
        pollFailureCountRef.current >= SCRAPE_POLL_WARNING_AFTER_FAILURES &&
        idleMs >= SCRAPE_POLL_WARNING_AFTER_IDLE_MS &&
        !pollWarningShownRef.current
      ) {
        pollWarningShownRef.current = true;
        setScrapeLogs((prev) => [...prev, {
          event: "status",
          message: "Local scrape is still running. Status refresh is delayed, but it will keep retrying automatically.",
        }].slice(-MAX_SCRAPE_LOG_LINES));
      }
    };

    const poll = async () => {
      if (pollInFlightRef.current) return;
      pollInFlightRef.current = true;
      let continuePolling = true;
      let nextDelayMs = SCRAPE_POLL_BASE_DELAY_MS;
      try {
        const controller = new AbortController();
        pollRequestTimeoutRef.current = window.setTimeout(() => controller.abort(), SCRAPE_POLL_TIMEOUT_MS);
        const res = await fetch(`/api/scrape/status/${jobId}?since=${logIndexRef.current}`, {
          signal: controller.signal,
          cache: "no-store",
          headers: { "Cache-Control": "no-cache" },
        });
        if (pollRequestTimeoutRef.current !== null) {
          window.clearTimeout(pollRequestTimeoutRef.current);
          pollRequestTimeoutRef.current = null;
        }
        if (res.status === 304) {
          pollFailureCountRef.current = 0;
          pollWarningShownRef.current = false;
          pollLastSuccessAtRef.current = Date.now();
          return;
        }
        if (!res.ok) {
          if (res.status === 404) {
            resetActiveScrapeState("The previous scrape job is no longer available locally.");
            continuePolling = false;
            return;
          }
          pollFailureCountRef.current += 1;
          nextDelayMs = Math.min(SCRAPE_POLL_BASE_DELAY_MS * (pollFailureCountRef.current + 1), SCRAPE_POLL_MAX_DELAY_MS);
          maybeReportPollingDelay();
          return;
        }
        pollFailureCountRef.current = 0;
        pollWarningShownRef.current = false;
        pollLastSuccessAtRef.current = Date.now();
        const data = await readResponseJson<ScrapeStatusResponse>(res);
        if (!data) return;

        if (data.universityName) setScrapeUniName(data.universityName);
        if (data.url) setScrapeTargetUrl(data.url);

        let nextAwaitingApproval: ApprovalSummary | null = null;
        const logs = data.logs;
        if (logs && logs.length > 0) {
          setScrapeLogs((prev) => [...prev, ...logs].slice(-MAX_SCRAPE_LOG_LINES));
          if (data.logIndex !== undefined) logIndexRef.current = data.logIndex;

          const doneLog = logs.find((l: ScrapeLog) => l.event === "done");
          if (doneLog) setScrapeResult(doneLog);

          const approvalLog = logs.find((l: ScrapeLog) => l.event === "approval_required");
          if (approvalLog) {
            nextAwaitingApproval = {
              totalCourses: approvalLog.totalCourses ?? 0,
              validSamples: approvalLog.validSamples ?? 0,
              rejectedSamples: approvalLog.rejectedSamples ?? 0,
              sampleTotal: approvalLog.sampleTotal ?? 0,
              validExamples: approvalLog.validExamples ?? [],
              rejectedExamples: approvalLog.rejectedExamples ?? [],
              estimatedMinutes: approvalLog.estimatedMinutes ?? 1,
            };
          }
        }

        const fetchAlreadyStarted =
          (data.current ?? 0) > 0 ||
          !!logs?.some((log) =>
            log.event === "progress" ||
            (log.event === "status" && (
              String(log.message || "").includes("User confirmed") ||
              String(log.message || "").includes("Fetching") && log.phase === "extract"
            ))
          );

        if (data.status === "awaiting_approval" && !fetchAlreadyStarted) {
          setAwaitingApproval(nextAwaitingApproval ?? (data.awaitingApproval as ApprovalSummary | null) ?? null);
        } else {
          setAwaitingApproval(null);
        }

        if (data.status !== "queued" && data.status !== "running" && data.status !== "awaiting_approval") {
          setStopping(false);
          setAwaitingApproval(null);
          if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; }
          if ((data.status === "completed" || data.status === "completed_with_errors" || data.status === "stopped") && (data.imported ?? 0) > 0) {
            loadStagedCourses(jobId);
          }
          // ETA tracking: clear start time when this URL finishes (next URL will reset it).
          if (urlQueueRef.current.length === 0) setScrapeStartTime(null);

          // Process next URL in queue
          const nextUrl = urlQueueRef.current.shift();
          if (nextUrl) {
            continuePolling = false;
            setUrlQueueProgress((prev) => prev ? { ...prev, current: prev.current + 1 } : null);
            setScrapeLogs((prev) => [...prev, { event: "status", message: `── Starting next URL (${nextUrl}) ──` }].slice(-MAX_SCRAPE_LOG_LINES));
            setScrapeStartTime(Date.now());
            const nextJobId = await startSingleJob(nextUrl);
            if (nextJobId) {
              pollJobStatus(nextJobId);
            } else {
              setScraping(false);
              setUrlQueueProgress(null);
            }
          } else {
            setScraping(false);
            setUrlQueueProgress(null);
            continuePolling = false;
          }
        }
      } catch (error) {
        if (pollRequestTimeoutRef.current !== null) {
          window.clearTimeout(pollRequestTimeoutRef.current);
          pollRequestTimeoutRef.current = null;
        }
        pollFailureCountRef.current += 1;
        const aborted =
          (error instanceof DOMException && error.name === "AbortError") ||
          (error instanceof Error && /abort|timeout/i.test(error.message));
        nextDelayMs = Math.min(
          SCRAPE_POLL_BASE_DELAY_MS * (aborted ? pollFailureCountRef.current + 2 : pollFailureCountRef.current + 1),
          SCRAPE_POLL_MAX_DELAY_MS
        );
        maybeReportPollingDelay();
      } finally {
        pollInFlightRef.current = false;
        if (continuePolling) {
          scheduleNextPoll(nextDelayMs);
        }
      }
    };

    void poll();
  }, [loadStagedCourses, resetActiveScrapeState, startSingleJob]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current);
      if (pollRequestTimeoutRef.current !== null) {
        window.clearTimeout(pollRequestTimeoutRef.current);
      }
    };
  }, []);

  // ── Live elapsed-timer tick ─────────────────────────────────────────────
  // Re-render once per second while a scrape is running so the "(Xs elapsed)"
  // label increments smoothly. We gate on `scrapeStartTime` so the timer is
  // off when no scrape is active. setInterval is throttled on background
  // tabs (~1Hz max), but that is fine — we just want monotonic ticks.
  useEffect(() => {
    if (!scrapeStartTime) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [scrapeStartTime]);

  // ── Snap timer back to truth when the tab regains focus ────────────────
  // Background-tab throttling can leave `now` lagging by tens of seconds.
  // visibilitychange fires the moment the user returns, so we force one
  // refresh — and trigger a status poll if a job is active so the progress
  // numbers (current/total) catch up too.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      setNow(Date.now());
      if (activeJobId) void pollJobStatus(activeJobId);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [activeJobId, pollJobStatus]);

  useEffect(() => {
    let cancelled = false;
    const savedJobId = sessionStorage.getItem("activeScrapeJob");

    // Helper: recover startedAt from /api/scrape/active for a known job id so
    // the live elapsed-timer can resume after in-tab navigation. Without this,
    // navigating away from /scraping and back leaves scrapeStartTime=null
    // (sessionStorage doesn't persist it), which silently kills the timer.
    const restoreStartTimeFor = (jobId: string) => {
      fetch("/api/scrape/active")
        .then((r) => (r.ok ? r.json() : null))
        .then((data: { activeJobs?: Array<{ id?: string; runtimeJobId?: string; universityName?: string | null; startedAt?: string | null }> } | null) => {
          if (cancelled) return;
          const match = data?.activeJobs?.find((j) => (j.runtimeJobId ?? j.id) === jobId);
          if (!match) return;
          if (match.universityName) setScrapeUniName(match.universityName);
          if (match.startedAt) {
            const t = new Date(match.startedAt).getTime();
            if (!Number.isNaN(t)) setScrapeStartTime(t);
          }
        })
        .catch(() => {});
    };

    if (savedJobId) {
      setActiveJobId(savedJobId);
      setScraping(true);
      setScrapeLogs([]);
      setScrapeResult(null);
      pollJobStatus(savedJobId);
      restoreStartTimeFor(savedJobId);
      return () => { cancelled = true; };
    }
    // Cross-tab sync: no job in sessionStorage, but maybe another browser
    // tab (or the API server itself, after a restart) has a scrape running.
    // Pick it up so every tab on /scraping shows the live progress.
    fetch("/api/scrape/active")
      .then((r) => (r.ok ? r.json() : null))
      .then((data: { activeJobs?: Array<{ id?: string; runtimeJobId?: string; universityName?: string | null; status?: string; startedAt?: string | null }> } | null) => {
        if (cancelled || !data?.activeJobs?.length) return;
        // Backend orders running > awaiting_approval > queued by recency,
        // so [0] is the right job. Accept either `id` or `runtimeJobId`.
        const job = data.activeJobs[0];
        const jobId = job?.runtimeJobId ?? job?.id;
        if (!jobId) return;
        setActiveJobId(jobId);
        if (job.universityName) setScrapeUniName(job.universityName);
        setScraping(true);
        // Restore the elapsed-timer baseline from the server's startedAt so
        // navigating back to /scraping (or opening it in a fresh tab) shows
        // the correct "(Xs elapsed)" instead of starting from 0 or blank.
        if (job.startedAt) {
          const t = new Date(job.startedAt).getTime();
          if (!Number.isNaN(t)) setScrapeStartTime(t);
        }
        setScrapeLogs([{ event: "status", message: `Resumed in-progress scrape (${job.universityName ?? "unknown"}) from another tab/session.` }]);
        setScrapeResult(null);
        sessionStorage.setItem("activeScrapeJob", jobId);
        pollJobStatus(jobId);
      })
      .catch(() => {});
    return () => { cancelled = true; };
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
      const res = await fetch(`/api/scrape/approve/${activeJobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proceed }),
      });
      if (!res.ok) return;
      if (!proceed) {
        setScraping(false);
        setStopping(false);
      }
      setAwaitingApproval(null);
    } catch {}
    setApprovalLoading(false);
  }, [activeJobId]);

  // Auto-proceed: as soon as the backend reports research complete,
  // approve immediately so the bulk fetch starts without manual confirmation.
  // Track per-job so a failed POST never causes an infinite retry loop.
  const autoApprovedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!awaitingApproval || !activeJobId) return;
    if (autoApprovedRef.current.has(activeJobId)) return;
    autoApprovedRef.current.add(activeJobId);
    handleApproval(true);
  }, [awaitingApproval, activeJobId, handleApproval]);

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
    if (scholarshipPageUrl.trim()) uniBody.scholarshipPage = scholarshipPageUrl.trim();
    if (academicRequirementsPageUrl.trim()) uniBody.academicRequirementsPage = academicRequirementsPageUrl.trim();
    if (fastMode) uniBody.fastMode = true;
    uniBodyRef.current = uniBody;

    // Queue remaining URLs (all except the first)
    urlQueueRef.current = validUrls.slice(1);
    if (validUrls.length > 1) {
      setUrlQueueProgress({ current: 1, total: validUrls.length });
    } else {
      setUrlQueueProgress(null);
    }

    try {
      setScrapeLogs([{ event: "status", message: "Scraping started in background..." }].slice(-MAX_SCRAPE_LOG_LINES));
      setScrapeStartTime(Date.now());
      const jobId = await startSingleJob(validUrls[0]);
      if (jobId) {
        pollJobStatus(jobId);
      } else {
        setScraping(false);
      }
    } catch (err) {
      setScrapeLogs([{ event: "error", message: (err as Error).message }].slice(-MAX_SCRAPE_LOG_LINES));
      setScraping(false);
    }
  }, [scrapeUrls, feePageUrl, requirementsPageUrl, fastMode, selectedUni, newUniName, newUniCountry, newUniCity, startSingleJob, pollJobStatus, uniData]);

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
    const failedMessages: string[] = [];

    for (const id of selectedIds) {
      try {
        const res = await fetch(`/api/scrape/staged/${id}/approve`, { method: "POST" });
        if (res.ok) {
          succeededIds.add(id);
        } else {
          failedIds.add(id);
          failedMessages.push(await getFetchErrorMessage(res));
        }
      } catch {
        failedIds.add(id);
      }
    }

    setStagedCourses((prev) => prev.filter((c) => !succeededIds.has(c.id)));
    setSelectedIds(failedIds);
    setApproving(false);
    fetchJobs();
    if (uniData?.data) {
      Promise.all(
        uniData.data.map(async (u) => {
          const res = await fetch(`/api/courses?universityId=${u.id}&limit=1`);
          if (!res.ok) {
            return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: 0 };
          }
          const d = await readResponseJson<{ total?: number }>(res);
          return { id: u.id, name: u.name, country: u.country, city: u.city, courseCount: d?.total ?? 0 };
        })
      ).then(setUniStats);
    }
    if (failedMessages.length > 0) {
      toast({
        title: `${failedIds.size} course(s) could not be published`,
        description: failedMessages.slice(0, 3).join(" · "),
        variant: "destructive",
      });
    }
  };

  const handleRejectSelected = async () => {
    if (selectedIds.size === 0) return;
    setRejectingIds(Array.from(selectedIds));
  };

  const handleApproveSingle = async (id: number) => {
    setApprovingId(id);
    try {
      const res = await fetch(`/api/scrape/staged/${id}/approve`, { method: "POST" });
      if (res.ok) {
        setStagedCourses((prev) => prev.filter((c) => c.id !== id));
        setSelectedIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      } else {
        toast({ title: "Could not publish", description: await getFetchErrorMessage(res), variant: "destructive" });
      }
    } catch {}
    setApprovingId(null);
  };

  const handleRejectSingle = async (id: number) => {
    setRejectingIds([id]);
  };

  const [clearingRejected, setClearingRejected] = useState(false);
  const handleClearRejected = async () => {
    if (!selectedUni || selectedUni === ALL) return;
    const uniId = parseInt(selectedUni);
    if (isNaN(uniId)) return;
    setClearingRejected(true);
    try {
      const res = await fetch(`/api/scrape/staged/clear-rejected/${uniId}`, { method: "POST" });
      if (res.ok) {
        const { deleted } = await res.json();
        toast({ title: `Cleared ${deleted} rejected course(s)`, description: "You can now re-scrape and they will appear in staging again." });
      } else {
        toast({ title: "Failed to clear rejected courses", description: await getFetchErrorMessage(res), variant: "destructive" });
      }
    } catch {
      toast({ title: "Failed to clear rejected courses", description: "Network error", variant: "destructive" });
    }
    setClearingRejected(false);
  };

  const handleDedupPending = async () => {
    if (!selectedUni || selectedUni === ALL) return;
    const uniId = parseInt(selectedUni);
    if (isNaN(uniId)) return;
    try {
      const res = await fetch(`/api/scrape/staged/dedup/${uniId}`, { method: "POST" });
      if (res.ok) {
        const { deleted } = await res.json();
        if (deleted > 0) {
          setStagedCourses((prev) => {
            const byName = new Map<string, StagedCourse>();
            for (const c of prev) {
              const key = c.courseName.toLowerCase().trim();
              const existing = byName.get(key);
              if (!existing || c.id > existing.id) byName.set(key, c);
            }
            return Array.from(byName.values());
          });
        }
        toast({ title: `Removed ${deleted} duplicate course(s)`, description: "The list now shows only the newest copy of each course." });
      } else {
        toast({ title: "Dedup failed", description: await getFetchErrorMessage(res), variant: "destructive" });
      }
    } catch {
      toast({ title: "Dedup failed", description: "Could not clean up duplicates. Please try again.", variant: "destructive" });
    }
  };

  const submitReject = async () => {
    if (!rejectingIds || rejectingIds.length === 0 || !rejectReason.trim()) return;
    const succeededIds = new Set<number>();
    try {
      for (const id of rejectingIds) {
        const res = await fetch(`/api/scrape/staged/${id}/reject`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reason: rejectReason.trim(),
            fieldKey: rejectFieldKey === "general" ? null : rejectFieldKey,
          }),
        });
        if (!res.ok) {
          toast({ title: "Reject failed", description: await getFetchErrorMessage(res), variant: "destructive" });
          return;
        }
        succeededIds.add(id);
      }
      setStagedCourses((prev) => prev.filter((c) => !succeededIds.has(c.id)));
      setSelectedIds((prev) => {
        const n = new Set(prev);
        for (const id of succeededIds) n.delete(id);
        return n;
      });
      setRejectingIds(null);
      setRejectReason("");
      setRejectFieldKey("general");
    } catch {}
  };

  const handleOpenReview = async (id: number) => {
    try {
      const res = await fetch(`/api/scrape/staged/${id}/review`);
      if (!res.ok) {
        toast({ title: "Could not load review", description: await getFetchErrorMessage(res), variant: "destructive" });
        return;
      }
      const data = await readResponseJson<CourseReviewPayload>(res);
      if (data) setReviewDetail(data);
    } catch {}
  };

  const handleSaveEdit = async () => {
    if (!editingCourse) return;
    try {
      const res = await fetch(`/api/scrape/staged/${editingCourse.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(editingCourse),
      });
      if (!res.ok) {
        toast({ title: "Save failed", description: await getFetchErrorMessage(res), variant: "destructive" });
        return;
      }
      const data = await readResponseJson<{ course?: StagedCourse }>(res);
      const updatedCourse = data?.course ?? editingCourse;
      setStagedCourses((prev) => prev.map((c) => c.id === editingCourse.id ? updatedCourse : c));
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

            <label className="flex items-start gap-2 mt-1 p-2.5 rounded-md border border-amber-200 bg-amber-50 cursor-pointer hover:bg-amber-100 transition-colors">
              <input
                type="checkbox"
                checked={fastMode}
                onChange={(e) => setFastMode(e.target.checked)}
                disabled={scraping}
                className="mt-0.5 w-4 h-4 accent-amber-600"
              />
              <div className="flex-1 text-xs">
                <div className="font-medium text-amber-900">Fast Mode (skip browser automation)</div>
                <div className="text-amber-700 mt-0.5">
                  5–10× faster (~1 min for 1000 pages). May miss JS-rendered fields on sites like VIT, Newcastle, UEL, RMIT (International toggle, expandable Entry Requirements). Recommended for static-HTML sites.
                </div>
              </div>
            </label>

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
                    English Requirements Page URL
                    <span className="ml-1 text-gray-400 font-normal">(optional — overrides auto-discovery)</span>
                  </label>
                  <Input
                    placeholder="https://university.edu/english-requirements"
                    value={requirementsPageUrl}
                    onChange={(e) => setRequirementsPageUrl(e.target.value)}
                    className="bg-white h-9 text-sm"
                    disabled={scraping}
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">
                    Academic Requirements Page URL
                    <span className="ml-1 text-gray-400 font-normal">(optional — academic/entry criteria)</span>
                  </label>
                  <Input
                    placeholder="https://university.edu/academic-requirements"
                    value={academicRequirementsPageUrl}
                    onChange={(e) => setAcademicRequirementsPageUrl(e.target.value)}
                    className="bg-white h-9 text-sm"
                    disabled={scraping}
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 mb-1 block">
                    Scholarships Page URL
                    <span className="ml-1 text-gray-400 font-normal">(optional — scholarship listings)</span>
                  </label>
                  <Input
                    placeholder="https://university.edu/scholarships"
                    value={scholarshipPageUrl}
                    onChange={(e) => setScholarshipPageUrl(e.target.value)}
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
              <UniversityCombobox
                value={selectedUni}
                onChange={(val) => {
                  setSelectedUni(val);
                  if (val && val !== ALL) {
                    const uni = uniData?.data?.find((u) => String(u.id) === val);
                    setScrapeUrls([uni?.scrapeUrl || ""]);
                    setFeePageUrl(uni?.feePageUrl || "");
                    setRequirementsPageUrl(uni?.requirementsPageUrl || "");
                    setScholarshipPageUrl((uni as { scholarshipPageUrl?: string })?.scholarshipPageUrl || "");
                    setAcademicRequirementsPageUrl((uni as { academicRequirementsPageUrl?: string })?.academicRequirementsPageUrl || "");
                    if (uni?.feePageUrl || uni?.requirementsPageUrl || (uni as { scholarshipPageUrl?: string })?.scholarshipPageUrl || (uni as { academicRequirementsPageUrl?: string })?.academicRequirementsPageUrl) setShowAdvanced(true);
                  } else {
                    setFeePageUrl("");
                    setRequirementsPageUrl("");
                    setScholarshipPageUrl("");
                    setAcademicRequirementsPageUrl("");
                  }
                }}
                universities={uniData?.data || []}
                disabled={scraping}
              />
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
                      const msg = await getFetchErrorMessage(resp);
                      setScrapeLogs([{ event: "error", message: msg }].slice(-MAX_SCRAPE_LOG_LINES));
                      setScraping(false);
                      return;
                    }
                    const data = await readResponseJson<{ jobId: string }>(resp);
                    if (!data?.jobId) {
                      setScrapeLogs([{ event: "error", message: "Invalid response from server" }].slice(-MAX_SCRAPE_LOG_LINES));
                      setScraping(false);
                      return;
                    }
                    setActiveJobId(data.jobId);
                    sessionStorage.setItem("activeScrapeJob", data.jobId);
                    setScrapeLogs([{ event: "status", message: "Re-scraping started (no AI, zero cost)..." }].slice(-MAX_SCRAPE_LOG_LINES));
                    pollJobStatus(data.jobId);
                  } catch (err) {
                    setScrapeLogs([{ event: "error", message: (err as Error).message }].slice(-MAX_SCRAPE_LOG_LINES));
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

              {progressLog && progressLog.total && (() => {
                const current = progressLog.current ?? 0;
                const total = progressLog.total ?? 1;
                const pct = (current / total) * 100;
                let eta: string | null = null;
                let elapsed: string | null = null;
                if (scrapeStartTime) {
                  // Use the ticking `now` state — it's bumped every second by
                  // the elapsed-timer effect, so the label increments live
                  // instead of only when the status poll lands.
                  const elapsedMs = now - scrapeStartTime;
                  const fmt = (ms: number) => {
                    const s = Math.max(0, Math.round(ms / 1000));
                    const m = Math.floor(s / 60);
                    const r = s % 60;
                    return m > 0 ? `${m}m ${r}s` : `${r}s`;
                  };
                  elapsed = fmt(elapsedMs);
                  if (current > 0 && current < total) {
                    const pacePerItem = elapsedMs / current;
                    const remainingMs = pacePerItem * (total - current);
                    eta = fmt(remainingMs);
                  }
                }
                return (
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs text-gray-500">
                      <span>{progressLog.message || "Scraping courses..."}</span>
                      <span className="tabular-nums">
                        {current}/{total}
                        {eta && (
                          <span className="ml-2 text-blue-600 font-medium">
                            ~{eta} left
                          </span>
                        )}
                        {elapsed && (
                          <span className="ml-2 text-gray-400">
                            ({elapsed} elapsed)
                          </span>
                        )}
                      </span>
                    </div>
                    <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-blue-500 rounded-full transition-all duration-300"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                  </div>
                );
              })()}

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

              <div ref={logRef} className="bg-gray-900 rounded-lg p-4 max-h-80 overflow-auto font-mono text-xs space-y-0.5">
                {scrapeLogs.map((log, i) => {
                  const phasePrefix = (phase?: string, sampleResult?: string) => {
                    if (sampleResult === "valid") return "[SAMPLE✓]";
                    if (sampleResult === "rejected") return "[SAMPLE✗]";
                    switch (phase) {
                      case "analyze":   return "[CLASSIFY]";
                      case "discover":  return "[DISCOVER]";
                      case "validate":  return "[VALIDATE]";
                      case "extract":   return "[EXTRACT ]";
                      case "fallback":  return "[FALLBACK]";
                      case "stage":     return "[STAGE   ]";
                      default:          return "[INFO    ]";
                    }
                  };
                  const logColor =
                    log.event === "error" ? "text-red-400" :
                    log.event === "approval_required" ? "text-amber-400 font-semibold" :
                    log.event === "course" && (log.status === "staged" || log.status?.startsWith("staged")) ? "text-green-400" :
                    log.event === "course" && log.status === "skipped" ? "text-yellow-500" :
                    log.event === "course" && log.status === "error" ? "text-red-400" :
                    log.event === "done" ? "text-cyan-400 font-bold" :
                    log.event === "status" && log.sampleResult === "valid" ? "text-green-300" :
                    log.event === "status" && log.sampleResult === "rejected" ? "text-red-300" :
                    log.event === "status" && log.phase === "discover" ? "text-blue-300" :
                    log.event === "status" && log.phase === "validate" ? "text-purple-300" :
                    log.event === "status" && log.phase === "extract" ? "text-orange-300" :
                    log.event === "status" && log.phase === "fallback" ? "text-yellow-300" :
                    log.event === "status" && log.phase === "analyze" ? "text-sky-300" :
                    "text-gray-300";
                  return (
                    <div key={i} className={logColor}>
                      {log.event === "status" && (
                        <span>
                          {phasePrefix(log.phase, log.sampleResult)} {log.message}
                          {log.totalCourses ? ` (${log.totalCourses} courses)` : ""}
                        </span>
                      )}
                      {log.event === "approval_required" && <span>[WAITING ] {log.message}</span>}
                      {log.event === "progress" && (
                        <span>[{String(log.current).padStart(4, " ")}/{log.total}] {log.message}</span>
                      )}
                      {log.event === "course" && (
                        <span>
                          {log.status === "staged" || log.status?.startsWith("staged") ? "  ✓" :
                           log.status === "skipped" ? "  –" :
                           log.status === "error" ? "  ✗" : "   "}{" "}
                          {log.name}
                          {log.status === "error" && log.message ? ` — ${log.message}` : ""}
                          {log.status && log.status !== "staged" && log.status !== "error" ? ` [${log.status}]` : ""}
                        </span>
                      )}
                      {log.event === "error" && <span>[ERROR   ] {log.message}</span>}
                      {log.event === "done" && (
                        <span>
                          ══ DONE ══ Found:{log.totalFound} | Staged:{log.imported} | Skipped:{log.skipped} | Errors:{log.errors}
                        </span>
                      )}
                    </div>
                  );
                })}
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

      {showReview && stagedCourses.length === 0 && selectedUni && selectedUni !== ALL && (
        <Card className="border border-amber-200 bg-amber-50">
          <CardContent className="py-4 flex items-center justify-between gap-4">
            <div className="flex items-start gap-2">
              <AlertTriangle className="w-5 h-5 text-amber-600 mt-0.5 shrink-0" />
              <div>
                <p className="text-sm font-medium text-amber-900">No pending courses — scrape returned 0 results</p>
                <p className="text-xs text-amber-700 mt-0.5">
                  Previously rejected courses block re-staging for 30 days. Click <strong>Clear rejected</strong> to remove that block, then scrape again.
                </p>
              </div>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="text-purple-600 border-purple-200 hover:bg-purple-50 shrink-0"
              onClick={handleClearRejected}
              disabled={clearingRejected}
            >
              {clearingRejected ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <XCircle className="w-3 h-3 mr-1" />}
              Clear rejected
            </Button>
          </CardContent>
        </Card>
      )}

      {showReview && stagedCourses.length > 0 && (
        <Card className="border-2 border-green-100">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2 text-lg">
                  <Eye className="w-5 h-5 text-green-600" />
                  Review Scraped Courses
                  <Badge className="bg-blue-100 text-blue-700">{stagedCourses.length} pending</Badge>
                </CardTitle>
                {lastScrapeInfo && (
                  <p className="text-xs text-gray-500 mt-1">
                    Last scrape: <span className="font-medium text-gray-700">
                      {lastScrapeInfo.staged} courses staged in{" "}
                      {lastScrapeInfo.durationMs != null
                        ? lastScrapeInfo.durationMs >= 3600000
                          ? `${Math.floor(lastScrapeInfo.durationMs / 3600000)}h ${Math.floor((lastScrapeInfo.durationMs % 3600000) / 60000)}m`
                          : `${Math.floor(lastScrapeInfo.durationMs / 60000)}m ${Math.floor((lastScrapeInfo.durationMs % 60000) / 1000)}s`
                        : "–"}
                    </span>
                    {lastScrapeInfo.startedAt && (
                      <> &bull; Started {new Date(lastScrapeInfo.startedAt).toISOString().replace("T", " ").slice(0, 16)} UTC</>
                    )}
                    {(lastScrapeInfo.skipped > 0 || lastScrapeInfo.errors > 0) && (
                      <> &bull; {lastScrapeInfo.skipped} skipped{lastScrapeInfo.errors > 0 ? `, ${lastScrapeInfo.errors} errors` : ""}</>
                    )}
                  </p>
                )}
              </div>
              <div className="flex gap-2">
                {selectedUni && selectedUni !== ALL && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-orange-600 border-orange-200 hover:bg-orange-50"
                    onClick={handleDedupPending}
                    title="Remove duplicate courses from previous scrape runs — keeps the newest copy of each course name"
                  >
                    Remove duplicates
                  </Button>
                )}
                {selectedUni && selectedUni !== ALL && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-purple-600 border-purple-200 hover:bg-purple-50"
                    onClick={handleClearRejected}
                    disabled={clearingRejected}
                    title="Delete all rejected staged courses for this university so they can be re-staged on the next scrape"
                  >
                    {clearingRejected ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <XCircle className="w-3 h-3 mr-1" />}
                    Clear rejected
                  </Button>
                )}
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
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              Review each course below. Edit any details or reject to discard. To approve and import, go to the university's <strong>Raw Data</strong> tab.
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
                      <th className="text-left p-2 font-medium text-gray-600">Course Location</th>
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
                          <div className="flex items-start gap-1 min-w-[280px] max-w-[420px]">
                            <span className="font-medium text-gray-800 break-words" title={course.courseName}>
                              {course.courseName}
                            </span>
                            {course.courseWebsite && (
                              <a
                                href={course.courseWebsite}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={`Verify: ${course.courseWebsite}`}
                                className="flex-shrink-0 text-blue-400 hover:text-blue-600 transition-colors mt-1"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <ExternalLink className="w-3.5 h-3.5" />
                              </a>
                            )}
                          </div>
                          {course.category && (
                            <div className="text-xs text-gray-400 break-words">{course.category}</div>
                          )}
                          <div className="flex flex-wrap gap-1 mt-1">
                            {course.autoPublishStatus && (
                              <Badge variant="outline" title="Auto-publish decision" className={`text-[10px] ${
                                course.autoPublishStatus === "approved" ? "text-green-700 border-green-200" :
                                course.autoPublishStatus === "rejected" ? "text-red-700 border-red-200" :
                                "text-amber-700 border-amber-200"
                              }`}>
                                Publish: {course.autoPublishStatus === "approved" ? "ready" : course.autoPublishStatus === "pending_review" ? "review" : course.autoPublishStatus}
                              </Badge>
                            )}
                            {course.eligibilityStatus && (
                              <Badge variant="outline" title="Eligibility for international on-campus students" className={`text-[10px] ${
                                course.eligibilityStatus === "eligible" ? "text-green-700 border-green-200" :
                                course.eligibilityStatus === "rejected" ? "text-red-700 border-red-200" :
                                "text-amber-700 border-amber-200"
                              }`}>
                                Eligibility: {course.eligibilityStatus}
                              </Badge>
                            )}
                          </div>
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
                          {course.courseLocation || <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2 text-xs text-gray-600">
                          {course.studyMode || <span className="text-gray-300">-</span>}
                        </td>
                        <td className="p-2">
                          <div className="flex gap-1 justify-center">
                            <Button
                              size="icon"
                              variant="ghost"
                              className="h-7 w-7 text-slate-600 hover:bg-slate-50"
                              onClick={() => handleOpenReview(course.id)}
                              title="Review evidence"
                            >
                              <Eye className="w-3.5 h-3.5" />
                            </Button>
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

      <Dialog open={!!rejectingIds} onOpenChange={(o) => {
        if (!o) {
          setRejectingIds(null);
          setRejectReason("");
          setRejectFieldKey("general");
        }
      }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>Reject With Reason</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="text-sm text-muted-foreground">
              Describe what was wrong on this university's website so the next rerun can use that guidance and produce more accurate data. This feedback is scoped to this university and its similar page layouts, not copied to other universities.
            </div>
            <div>
              <label className="text-sm font-medium">Field</label>
              <Select value={rejectFieldKey} onValueChange={setRejectFieldKey}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="general">General / whole course</SelectItem>
                  <SelectItem value="internationalFee">International Fee</SelectItem>
                  <SelectItem value="courseLocation">Course Location</SelectItem>
                  <SelectItem value="ieltsOverall">English Requirement</SelectItem>
                  <SelectItem value="courseName">Wrong Page / Course Match</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-sm font-medium">Reject reason</label>
              <Textarea
                rows={4}
                className="mt-1"
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="Example: On this university site, intake is shown under Start Date / Class start date, and location is under Campus Location. Use those labels for rerun. Do not copy this rule to other universities."
              />
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setRejectingIds(null);
                setRejectReason("");
                setRejectFieldKey("general");
              }}
            >
              Cancel
            </Button>
            <Button variant="destructive" onClick={submitReject} disabled={!rejectReason.trim()}>
              Reject And Save University Feedback
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!reviewDetail} onOpenChange={(o) => { if (!o) setReviewDetail(null); }}>
        <DialogContent className="max-w-4xl max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Evidence Review</DialogTitle>
          </DialogHeader>
          {reviewDetail && (
            <div className="space-y-4 text-sm">
              <div>
                <div className="font-medium text-base">{reviewDetail.course.courseName}</div>
                <div className="text-muted-foreground">
                  Eligibility: {reviewDetail.course.eligibilityStatus || "unknown"}
                  {reviewDetail.course.eligibilityReason ? ` - ${reviewDetail.course.eligibilityReason}` : ""}
                </div>
              </div>
              {reviewDetail.conflicts.length > 0 && (
                <div className="rounded border border-amber-200 bg-amber-50 p-3">
                  <div className="font-medium text-amber-800 mb-2">Conflicts</div>
                  <div className="space-y-2">
                    {reviewDetail.conflicts.map((conflict) => (
                      <div key={conflict.id} className="text-xs text-amber-900">
                        <span className="font-medium">{conflict.fieldKey}</span>: {conflict.valueA || "-"} vs {conflict.valueB || "-"}
                        {conflict.reason ? ` - ${conflict.reason}` : ""}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div className="space-y-3">
                {Array.from(new Set(reviewDetail.evidence.map((item) => item.fieldKey))).map((fieldKey) => {
                  const items = reviewDetail.evidence.filter((item) => item.fieldKey === fieldKey);
                  return (
                    <div key={fieldKey} className="rounded border p-3">
                      <div className="font-medium mb-2">{fieldKey}</div>
                      <div className="space-y-2">
                        {items.map((item) => (
                          <div key={item.id} className="rounded bg-slate-50 p-2 text-xs">
                            <div className="flex flex-wrap gap-2 mb-1">
                              <Badge variant="outline" className="text-[10px]">{item.decisionStatus}</Badge>
                              {item.selected && <Badge variant="outline" className="text-[10px] border-green-200 text-green-700">selected</Badge>}
                              <span className="text-muted-foreground">{item.pageType} / {item.extractionMethod}</span>
                              {typeof item.confidence === "number" && <span className="text-muted-foreground">confidence {Math.round(item.confidence * 100)}%</span>}
                            </div>
                            <div className="font-medium">{item.candidateValue || "-"}</div>
                            {item.snippet && <div className="text-muted-foreground mt-1">{item.snippet}</div>}
                            {item.sourceUrl && (
                              <a className="text-blue-600 hover:underline break-all" href={item.sourceUrl} target="_blank" rel="noreferrer">
                                {item.sourceUrl}
                              </a>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

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
                      <SelectItem value="Day">Day</SelectItem>
                      <SelectItem value="Semester">Semester</SelectItem>
                      <SelectItem value="Trimester">Trimester</SelectItem>
                      <SelectItem value="Quarter">Quarter</SelectItem>
                      <SelectItem value="Term">Term</SelectItem>
                      <SelectItem value="Hour">Hour</SelectItem>
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
                      <SelectItem value="AUD">AUD — Australian Dollar</SelectItem>
                      <SelectItem value="USD">USD — US Dollar</SelectItem>
                      <SelectItem value="GBP">GBP — British Pound</SelectItem>
                      <SelectItem value="EUR">EUR — Euro</SelectItem>
                      <SelectItem value="NZD">NZD — New Zealand Dollar</SelectItem>
                      <SelectItem value="CAD">CAD — Canadian Dollar</SelectItem>
                      <SelectItem value="SGD">SGD — Singapore Dollar</SelectItem>
                      <SelectItem value="HKD">HKD — Hong Kong Dollar</SelectItem>
                      <SelectItem value="JPY">JPY — Japanese Yen</SelectItem>
                      <SelectItem value="CNY">CNY — Chinese Yuan</SelectItem>
                      <SelectItem value="INR">INR — Indian Rupee</SelectItem>
                      <SelectItem value="NPR">NPR — Nepalese Rupee</SelectItem>
                      <SelectItem value="MYR">MYR — Malaysian Ringgit</SelectItem>
                      <SelectItem value="AED">AED — UAE Dirham</SelectItem>
                      <SelectItem value="ZAR">ZAR — South African Rand</SelectItem>
                      <SelectItem value="CHF">CHF — Swiss Franc</SelectItem>
                      <SelectItem value="KRW">KRW — South Korean Won</SelectItem>
                      <SelectItem value="THB">THB — Thai Baht</SelectItem>
                      <SelectItem value="IDR">IDR — Indonesian Rupiah</SelectItem>
                      <SelectItem value="PHP">PHP — Philippine Peso</SelectItem>
                      <SelectItem value="VND">VND — Vietnamese Dong</SelectItem>
                      <SelectItem value="BDT">BDT — Bangladeshi Taka</SelectItem>
                      <SelectItem value="LKR">LKR — Sri Lankan Rupee</SelectItem>
                      <SelectItem value="PKR">PKR — Pakistani Rupee</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex-1">
                  <label className="text-xs font-medium text-gray-500 mb-1 block">Fee Term</label>
                  <Select value={editingCourse.feeTerm || ""} onValueChange={(v) => setEditingCourse({ ...editingCourse, feeTerm: v || null })}>
                    <SelectTrigger><SelectValue placeholder="Term" /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="Annual">Annual (Per Year)</SelectItem>
                      <SelectItem value="Full Course">Full Course (Total)</SelectItem>
                      <SelectItem value="Total">Total</SelectItem>
                      <SelectItem value="Semester">Per Semester</SelectItem>
                      <SelectItem value="Trimester">Per Trimester</SelectItem>
                      <SelectItem value="Term">Per Term</SelectItem>
                      <SelectItem value="Session">Per Session</SelectItem>
                      <SelectItem value="Quarter">Per Quarter</SelectItem>
                      <SelectItem value="Per Unit">Per Unit</SelectItem>
                      <SelectItem value="Per Credit">Per Credit</SelectItem>
                      <SelectItem value="Per Credit Hour">Per Credit Hour</SelectItem>
                      <SelectItem value="Per Subject">Per Subject</SelectItem>
                      <SelectItem value="Per Module">Per Module</SelectItem>
                      <SelectItem value="Per Course">Per Course</SelectItem>
                      <SelectItem value="Per Month">Per Month</SelectItem>
                      <SelectItem value="Per Week">Per Week</SelectItem>
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
              <div>
                <label className="text-xs font-medium text-gray-500 mb-1 block">Course Location</label>
                <Input value={editingCourse.courseLocation || ""} onChange={(e) => setEditingCourse({ ...editingCourse, courseLocation: e.target.value || null })} />
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

      {/* ── Scrape History ─────────────────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Scrape History</h2>
          <Button variant="outline" size="sm" onClick={fetchHistory} disabled={loadingHistory}>
            <RefreshCw className={`w-4 h-4 mr-1 ${loadingHistory ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
        {loadingHistory && historyRuns.length === 0 ? (
          <div className="border rounded-xl p-10 text-center text-gray-400">Loading…</div>
        ) : historyRuns.length === 0 ? (
          <div className="border rounded-xl p-10 text-center text-gray-400">
            <Clock className="w-8 h-8 mx-auto mb-2 opacity-40" />
            <p>No scrape runs yet.</p>
          </div>
        ) : (
          <div className="space-y-2">
            {historyRuns.map((run) => {
              const isExpanded = expandedHistoryId === run.runtimeJobId;
              return (
                <div key={run.runtimeJobId} className="border rounded-xl bg-white overflow-hidden">
                  <div className="p-3 sm:p-4 flex flex-wrap items-center gap-x-4 gap-y-2">
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      {historyStatusBadge(run.status)}
                      <div className="min-w-0">
                        <div className="font-medium text-gray-800 truncate">
                          {run.universityName ?? "(unknown university)"}
                        </div>
                        <div className="text-xs text-gray-500 truncate">
                          {formatHistoryDate(run.startedAt)} &bull; {formatHistoryDuration(run.durationMs)}
                          {run.url ? <> &bull; <span className="text-gray-400">{run.url}</span></> : null}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-gray-600 whitespace-nowrap">
                      <span>Found: <span className="font-semibold text-gray-800">{run.totalFound ?? 0}</span></span>
                      <span>Staged: <span className="font-semibold text-gray-800">{run.stagedCount}</span></span>
                      <span>Approved: <span className="font-semibold text-green-700">{run.approvedCount}</span></span>
                      <span>Rejected: <span className="font-semibold text-red-700">{run.rejectedCount}</span></span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        variant={isExpanded && historyView === "logs" ? "default" : "outline"}
                        size="sm"
                        onClick={() => void openHistoryDetail(run.runtimeJobId, "logs")}
                      >
                        View Logs
                      </Button>
                      <Button
                        variant={isExpanded && historyView === "courses" ? "default" : "outline"}
                        size="sm"
                        onClick={() => void openHistoryDetail(run.runtimeJobId, "courses")}
                      >
                        View Courses
                      </Button>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="border-t bg-gray-50 p-3 sm:p-4">
                      {historyDetailLoading ? (
                        <div className="text-center text-gray-400 py-6">Loading details…</div>
                      ) : historyView === "logs" ? (
                        <div>
                          <div className="flex items-center gap-2 mb-2">
                            <Input
                              placeholder="Filter log lines…"
                              value={historyLogFilter}
                              onChange={(e) => setHistoryLogFilter(e.target.value)}
                              className="h-8 text-xs"
                            />
                            <span className="text-xs text-gray-500 whitespace-nowrap">
                              {historyDetail?.logs.length ?? 0} entries
                            </span>
                          </div>
                          <div className="max-h-96 overflow-auto bg-black text-green-200 font-mono text-xs rounded p-3">
                            {(historyDetail?.logs ?? [])
                              .filter((l) => {
                                if (!historyLogFilter) return true;
                                const f = historyLogFilter.toLowerCase();
                                return (
                                  l.event.toLowerCase().includes(f) ||
                                  String(l.message ?? "").toLowerCase().includes(f) ||
                                  String(l.phase ?? "").toLowerCase().includes(f)
                                );
                              })
                              .map((l) => (
                                <div key={l.sequence} className="whitespace-pre-wrap break-words leading-relaxed">
                                  <span className="text-gray-500">[{l.event}]</span>
                                  {l.phase ? <span className="text-blue-300"> [{String(l.phase)}]</span> : null}
                                  {l.message ? <> {String(l.message)}</> : null}
                                </div>
                              ))}
                            {(historyDetail?.logs.length ?? 0) === 0 && (
                              <div className="text-gray-500">No log lines recorded.</div>
                            )}
                          </div>
                        </div>
                      ) : (
                        <div>
                          <div className="text-xs text-gray-500 mb-2">
                            {historyDetail?.stagedCourses.length ?? 0} staged courses
                          </div>
                          <div className="max-h-96 overflow-auto border rounded bg-white">
                            <table className="w-full text-xs">
                              <thead className="bg-gray-100 sticky top-0">
                                <tr>
                                  <th className="text-left p-2 font-medium text-gray-600">Course</th>
                                  <th className="text-left p-2 font-medium text-gray-600">Level</th>
                                  <th className="text-left p-2 font-medium text-gray-600">Category</th>
                                  <th className="text-center p-2 font-medium text-gray-600">Status</th>
                                  <th className="text-right p-2 font-medium text-gray-600">Fee</th>
                                  <th className="text-center p-2 font-medium text-gray-600">IELTS</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y">
                                {(historyDetail?.stagedCourses ?? []).map((c) => (
                                  <tr key={c.id} className="hover:bg-gray-50">
                                    <td className="p-2 text-gray-800">{c.courseName ?? "—"}</td>
                                    <td className="p-2 text-gray-600">{c.degreeLevel ?? "—"}</td>
                                    <td className="p-2 text-gray-600">{c.category ?? "—"}</td>
                                    <td className="p-2 text-center text-gray-600">{c.status ?? "—"}</td>
                                    <td className="p-2 text-right text-gray-700">{c.internationalFee ?? "—"}</td>
                                    <td className="p-2 text-center text-gray-600">{c.ieltsOverall ?? "—"}</td>
                                  </tr>
                                ))}
                                {(historyDetail?.stagedCourses.length ?? 0) === 0 && (
                                  <tr><td colSpan={6} className="p-4 text-center text-gray-400">No staged courses recorded for this run.</td></tr>
                                )}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

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

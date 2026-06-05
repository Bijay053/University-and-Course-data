import { useState, useRef, useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Play, StopCircle, Loader2, Globe, CheckCircle2, AlertCircle,
  ChevronsUpDown, Search, Eye, RefreshCw, ChevronDown, X, Zap, TrendingUp,
  Bot, AlertTriangle, CheckCheck, Copy, Check,
} from "lucide-react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { CountrySelect } from "@/components/country-select";
import { useToast } from "@/hooks/use-toast";

// ── Types ────────────────────────────────────────────────────────────────────
type UniOption = { id: number; name: string; scrapeUrl?: string | null; feePageUrl?: string | null; requirementsPageUrl?: string | null };
type ScrapeLog = {
  event: string; message?: string; current?: number; total?: number; phase?: string;
  totalFound?: number; imported?: number; skipped?: number; errors?: number;
  kind?: string; drop_pct?: number; dropped_sample?: string[];
  dropped?: number; kept?: number;
  category_count?: number; total_kept?: number; category_pct?: number;
  pattern_breakdown?: Record<string, number>;
  /** Granular per-guard skip counts — present only on the "done" event. */
  skip_reasons?: Record<string, number>;
  /** Per-sub-reason sample URLs+names (up to 10 each) — present only on the "done" event. */
  skip_reason_samples?: Record<string, Array<{ url: string; name: string }>>;
  /** Run-level pipeline optimisation savings — present only when ≥1 skip fired. */
  performance_savings?: {
    http_fetches_skipped: number;
    vision_ocr_skipped: number;
    empty_text_ai_skipped: number;
    estimated_seconds_saved: number;
    estimated_ai_calls_saved: number;
    estimated_cost_saved_usd: number;
  } | null;
};

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

// ── AI Diagnostic types ───────────────────────────────────────────────────────
type DiagnoseRootCause = { issue: string; explanation: string; severity: "high" | "medium" | "low" };
type DiagnoseAction    = { action: string; detail: string; auto_fixable: boolean; fix_type?: "config" | "platform_bug" };
type DiagnosisPayload  = {
  summary: string;
  root_causes: DiagnoseRootCause[];
  recommended_actions: DiagnoseAction[];
  discovery_verdict?: string;
  location_verdict?: string;
};
type LevelBreakdown = {
  undergraduate: number;
  postgraduate: number;
  research: number;
  other: number;
  unknown: number;
};
type DeterministicIssue = {
  issue: string;
  severity: "critical" | "warning";
  check: string;
  detail: string;
  potential_causes?: string[];
  recipe_patch?: Record<string, unknown>;
  recipe_patch_description?: string;
};
type DiagnoseResult = {
  ok: boolean;
  job_id: string;
  university?: string;
  university_id?: number;
  job_stats?: { total_found: number; imported: number; skipped: number; errors: number; avg_completeness_pct: number };
  bad_location_samples?: string[];
  level_breakdown?: LevelBreakdown;
  deterministic_issues?: DeterministicIssue[];
  diagnosis?: DiagnosisPayload;
  fallback?: DiagnosisPayload;
  suggested_config?: Record<string, unknown>;
  error?: string;
};

export type ScrapeJobCardProps = {
  slotIndex: number;
  universities: UniOption[];
  onReviewReady: (jobId: string, uniName: string, force?: boolean) => void;
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
  const { toast } = useToast();
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
  const [copiedLogs, setCopiedLogs] = useState(false);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [uniName, setUniName] = useState("");
  const [progress, setProgress] = useState<{ current: number; total: number } | null>(null);
  const [startTime, setStartTime] = useState<number | null>(null);
  // Tracks when course *extraction* actually began (after discovery completes).
  // Using startTime for ETA inflates it hugely — discovery can take 5-9 minutes
  // and those minutes get divided into the per-course rate, making 5s/course
  // look like 86s/course and producing wildly wrong ETAs like "134m left".
  const extractionStartRef = useRef<number | null>(null);
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

  // Pipeline optimisation savings from the done event
  const [performanceSavings, setPerformanceSavings] = useState<{
    http_fetches_skipped: number;
    vision_ocr_skipped: number;
    empty_text_ai_skipped: number;
    estimated_seconds_saved: number;
    estimated_ai_calls_saved: number;
    estimated_cost_saved_usd: number;
  } | null>(null);

  // Category landing page rejection breakdown from the done event
  const [categoryDiagnostics, setCategoryDiagnostics] = useState<{
    skipReasons: Record<string, number>;
    skipReasonSamples: Record<string, Array<{ url: string; name: string }>>;
  } | null>(null);

  // AI Diagnostic state
  const [diagnoseResult, setDiagnoseResult] = useState<DiagnoseResult | null>(null);
  const [diagnoseLoading, setDiagnoseLoading] = useState(false);
  const [showDiagnosePanel, setShowDiagnosePanel] = useState(false);
  const [applyingFix, setApplyingFix] = useState(false);
  const [fixApplied, setFixApplied] = useState(false);

  type UrlFilterWarning = {
    kind: "high_drop_rate" | "category_pages";
    ruleType?: "block" | "allow";
    patternBreakdown?: Record<string, number>;
    dropPct?: number; dropped?: number; kept?: number;
    droppedSample?: string[];
    categoryPct?: number; categoryCount?: number; totalKept?: number;
  };
  const [urlFilterWarning, setUrlFilterWarning] = useState<UrlFilterWarning | null>(null);

  // URL filter test tool state
  type UrlTestResult = { url: string; passed: boolean; drop_reason?: string | null; matching_allow_pattern?: string | null; blocking_block_pattern?: string | null };
  type UrlTestSummary = { total: number; kept_count: number; dropped_count: number; drop_pct: number };
  const [urlTestInput, setUrlTestInput] = useState("");
  const [urlTestResults, setUrlTestResults] = useState<{ results: UrlTestResult[]; summary: UrlTestSummary } | null>(null);
  const [urlTestLoading, setUrlTestLoading] = useState(false);
  const [urlTestError, setUrlTestError] = useState<string | null>(null);
  const [showUrlTestPanel, setShowUrlTestPanel] = useState(false);

  // Smart YAML Fix repair candidates state
  type RepairCandidateData = {
    id: string;
    label: string;
    description: string;
    proposed_yaml: string | null;
    recipe_patch: Record<string, unknown>;
    confidence: number;
    expected_gain: number;
    problem_addressed?: string;
    simulation?: { before_count: number; after_count: number; method: string; note?: string };
  };
  const [repairCandidates, setRepairCandidates] = useState<RepairCandidateData[] | null>(null);
  const [repairLoading, setRepairLoading] = useState(false);
  const [validateResult, setValidateResult] = useState<{ before: number; after: number; total: number; sample_rescued?: string[] } | null>(null);
  const [validateLoading, setValidateLoading] = useState(false);
  const [applyingRepairFix, setApplyingRepairFix] = useState(false);
  const [repairFixApplied, setRepairFixApplied] = useState(false);

  const pollRef = useRef<number | null>(null);
  const logIndexRef = useRef(0);
  const pollInFlightRef = useRef(false);
  const pollFailRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const doneLogRef = useRef<HTMLDivElement>(null);
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

  // Track whether the user has scrolled up from the bottom of the live log.
  // When true, new log lines do NOT force a scroll-to-bottom so the user can
  // read earlier lines in peace. Auto-scroll resumes as soon as they scroll
  // back to within 40 px of the bottom.
  const userScrolledUpRef = useRef(false);
  // Set to true just before a programmatic scroll so the resulting onScroll
  // event doesn't mistakenly reset userScrolledUpRef to false.
  const skipNextScrollEventRef = useRef(false);

  // Scroll logs to bottom — skipped when the user has manually scrolled up.
  useEffect(() => {
    const el = logContainerRef.current;
    if (!el || userScrolledUpRef.current) return;
    skipNextScrollEventRef.current = true;
    el.scrollTop = el.scrollHeight;
  }, [logs]);

  // When transitioning to done, scroll the done-state log panel to the bottom
  // so the user sees the most recent extraction lines, not the old discovery logs.
  useEffect(() => {
    if (phase === "done" && doneLogRef.current) {
      doneLogRef.current.scrollTop = doneLogRef.current.scrollHeight;
    }
  }, [phase, logs]);

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
    extractionStartRef.current = null;
    setActiveJobId(null);
    setPhase("idle");
    setJobStatus(null);
    setLogs([]);
    setResultSummary(null);
    setPerformanceSavings(null);
    setCompletedJobId(null);
    setQualityData(null);
    setQualityError(null);
    setQualityStatus("idle");
    if (qualityPollRef.current) { clearTimeout(qualityPollRef.current); qualityPollRef.current = null; }
    setUniName("");
    setUrlFilterWarning(null);
    setUrlTestInput("");
    setUrlTestResults(null);
    setUrlTestError(null);
    setShowUrlTestPanel(false);
    setRepairCandidates(null);
    setRepairLoading(false);
    setValidateResult(null);
    setValidateLoading(false);
    setApplyingRepairFix(false);
    setRepairFixApplied(false);
    setCategoryDiagnostics(null);
  }, [slotKey, startTimeKey]);

  // Auto-load repair candidates when done with a URL filter warning
  useEffect(() => {
    if (phase === "done" && urlFilterWarning && urlFilterWarning.kind !== "category_pages" && completedJobId && repairCandidates === null && !repairLoading) {
      setRepairLoading(true);
      fetch(`/api/scrape/jobs/${completedJobId}/auto-repair-candidates`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => { if (data?.ok) setRepairCandidates(data.candidates || []); })
        .catch(() => {})
        .finally(() => setRepairLoading(false));
    }
  }, [phase, urlFilterWarning, completedJobId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleValidateRepairFix = useCallback(async (candidate: RepairCandidateData) => {
    if (!completedJobId) return;
    setValidateLoading(true);
    try {
      const patch = (candidate.recipe_patch as Record<string, Record<string, unknown>>)?.discovery || {};
      const droppedUrls = urlFilterWarning?.droppedSample || [];
      const res = await fetch(`/api/scrape/jobs/${completedJobId}/simulate-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          allow_url_patterns: (patch.allow_url_patterns as string[]) || [],
          block_url_patterns: (patch.block_url_patterns as string[]) || [],
          dropped_urls: droppedUrls,
        }),
      });
      const data = await res.json();
      if (data.ok) setValidateResult({ before: data.before, after: data.after, total: data.total, sample_rescued: data.sample_rescued });
    } catch { /* ignore */ } finally {
      setValidateLoading(false);
    }
  }, [completedJobId, urlFilterWarning]);

  const handleApplyRepairFix = useCallback(async (candidate: RepairCandidateData) => {
    if (!completedJobId) return;
    setApplyingRepairFix(true);
    try {
      const res = await fetch(`/api/scrape/jobs/${completedJobId}/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_patch: candidate.recipe_patch }),
      });
      const data = await res.json();
      if (data.ok) setRepairFixApplied(true);
    } catch { /* ignore */ } finally {
      setApplyingRepairFix(false);
    }
  }, [completedJobId]);

  const handleCopyLogs = useCallback(() => {
    const text = logs.map(l => l.message || l.event).join("\n");
    navigator.clipboard.writeText(text).then(() => {
      setCopiedLogs(true);
      setTimeout(() => setCopiedLogs(false), 2000);
    });
  }, [logs]);

  const handleTestUrlFilter = useCallback(async (jobId: string) => {
    const lines = urlTestInput.split("\n").map(l => l.trim()).filter(Boolean);
    if (!lines.length) return;
    setUrlTestLoading(true);
    setUrlTestError(null);
    setUrlTestResults(null);
    try {
      const res = await fetch(`/api/scrape/jobs/${jobId}/test-url-filter`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls: lines }),
      });
      if (!res.ok) {
        const msg = await getFetchErrorMessage(res);
        setUrlTestError(msg || `Error ${res.status}`);
        return;
      }
      const data = await readResponseJson<{ ok: boolean; results: UrlTestResult[]; summary: UrlTestSummary; error?: string }>(res);
      if (!data?.ok) {
        setUrlTestError(data?.error || "Unknown error");
      } else {
        setUrlTestResults({ results: data.results, summary: data.summary });
      }
    } catch (e) {
      setUrlTestError(String(e));
    } finally {
      setUrlTestLoading(false);
    }
  }, [urlTestInput]);

  const fetchDiagnose = useCallback(async (jobId: string) => {
    setDiagnoseLoading(true);
    setShowDiagnosePanel(true);
    try {
      const res = await fetch(`/api/scrape/jobs/${jobId}/diagnose`, {
        method: "POST",
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
      });
      if (!res.ok) {
        const msg = await getFetchErrorMessage(res);
        setDiagnoseResult({ ok: false, job_id: jobId, error: msg || `Error ${res.status}` });
        return;
      }
      const data = await readResponseJson<DiagnoseResult>(res);
      setDiagnoseResult(data || null);
    } catch (e) {
      setDiagnoseResult({ ok: false, job_id: jobId, error: String(e) });
    } finally {
      setDiagnoseLoading(false);
    }
  }, []);

  const applyFix = useCallback(async (jobId: string, patch: Record<string, unknown>) => {
    setApplyingFix(true);
    try {
      const res = await fetch(`/api/scrape/jobs/${jobId}/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ config_patch: patch }),
      });
      if (!res.ok) {
        const msg = await getFetchErrorMessage(res);
        toast({
          title: "Apply fix failed",
          description: msg || `HTTP ${res.status}`,
          variant: "destructive",
        });
        return;
      }
      setFixApplied(true);
      toast({
        title: "Config saved",
        description: "Fix applied — validate with Test Discovery or re-run the scrape to confirm improvement.",
      });
    } catch (e) {
      toast({
        title: "Apply fix failed",
        description: String(e),
        variant: "destructive",
      });
    } finally {
      setApplyingFix(false);
    }
  }, [toast]);

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
          if (progressLog) {
            const cur = progressLog.current ?? 0;
            setProgress({ current: cur, total: progressLog.total! });
            // Record the moment extraction actually starts (first course done).
            // Discovery can take many minutes; using job startTime for ETA inflates
            // per-course rate by the full discovery overhead.
            if (cur > 0 && extractionStartRef.current === null) {
              extractionStartRef.current = Date.now();
            }
          }

          const doneLog = data.logs.find((l) => l.event === "done");
          if (doneLog) {
            setResultSummary({
              imported: doneLog.imported ?? 0,
              skipped: doneLog.skipped ?? 0,
              errors: doneLog.errors ?? 0,
            });
            // Capture granular category-landing rejection breakdown if present
            if (doneLog.skip_reasons) {
              const catKeys = Object.keys(doneLog.skip_reasons).filter(k => k.startsWith("category_landing_page_"));
              if (catKeys.length > 0) {
                setCategoryDiagnostics({
                  skipReasons: doneLog.skip_reasons,
                  skipReasonSamples: doneLog.skip_reason_samples ?? {},
                });
              }
            }
            // Capture pipeline optimisation savings if any skips fired
            if (doneLog.performance_savings) {
              setPerformanceSavings(doneLog.performance_savings);
            }
          }

          // Detect URL filter warnings in live log stream
          for (const l of data.logs) {
            if (l.kind === "category_pages_detected") {
              setUrlFilterWarning({
                kind: "category_pages",
                categoryPct: l.category_pct,
                categoryCount: l.category_count,
                totalKept: l.total_kept,
              });
            } else if (l.kind === "extract_allow_url_filter" && (l.drop_pct ?? 0) > 40) {
              setUrlFilterWarning((prev) => {
                if (prev?.kind === "category_pages") return prev;
                return {
                  kind: "high_drop_rate",
                  ruleType: "allow",
                  dropPct: l.drop_pct,
                  dropped: l.dropped,
                  kept: l.kept,
                  droppedSample: l.dropped_sample,
                };
              });
            } else if (l.kind === "extract_block_url_filter" && (l.drop_pct ?? 0) > 40) {
              setUrlFilterWarning((prev) => {
                if (prev?.kind === "category_pages") return prev;
                return {
                  kind: "high_drop_rate",
                  ruleType: "block",
                  dropPct: l.drop_pct,
                  dropped: l.dropped,
                  kept: l.kept,
                  droppedSample: l.dropped_sample,
                  patternBreakdown: l.pattern_breakdown,
                };
              });
            }
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
    setPerformanceSavings(null);
    extractionStartRef.current = null;
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
              const allDispatched = (progressLog.current ?? 0) >= progressLog.total!;
              const extractionStart = extractionStartRef.current;
              const remaining = !allDispatched && extractionStart && (progressLog.current ?? 0) > 0
                ? fmt(((now - extractionStart) / (progressLog.current ?? 1)) * ((progressLog.total ?? 1) - (progressLog.current ?? 0)))
                : null;
              return (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-500">
                    <span>{allDispatched ? "Completing…" : "Scraping courses…"}</span>
                    <span className="tabular-nums">
                      {progressLog.current}/{progressLog.total}
                      {allDispatched
                        ? <span className="ml-2 text-amber-500 font-medium animate-pulse">finishing last batch…</span>
                        : remaining && <span className="ml-2 text-blue-500 font-medium">~{remaining} left</span>
                      }
                    </span>
                  </div>
                  <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full bg-blue-500 rounded-full transition-all" style={{ width: `${pct}%` }} />
                  </div>
                </div>
              );
            })() : null}

            {/* ── URL filter warning banner (shown during run when filter drops too much) ── */}
            {urlFilterWarning && (
              <div className={`rounded-lg border p-2.5 space-y-2 text-[11px] ${
                urlFilterWarning.kind === "category_pages" ? "bg-red-50 border-red-300" : "bg-amber-50 border-amber-300"
              }`}>
                <div className="flex items-center gap-1.5 font-semibold">
                  <AlertTriangle className={`w-3.5 h-3.5 shrink-0 ${urlFilterWarning.kind === "category_pages" ? "text-red-500" : "text-amber-500"}`} />
                  {urlFilterWarning.kind === "category_pages" ? (
                    <span className="text-red-800">Wrong pages — category pages only ({urlFilterWarning.categoryCount}/{urlFilterWarning.totalKept} URLs have no degree qualifier)</span>
                  ) : (
                    <span className="text-amber-800">URL filter removed {urlFilterWarning.dropPct?.toFixed(0)}% of discovered course pages</span>
                  )}
                </div>
                {urlFilterWarning.kind === "category_pages" ? (
                  <p className="text-red-700 leading-relaxed">
                    The <code className="font-mono bg-red-100 px-0.5 rounded">allow_url_patterns</code> filter is keeping subject/category listing pages, not individual course pages. Staged count will be 0.
                  </p>
                ) : (
                  <>
                    <p className="text-amber-700 leading-relaxed">
                      {urlFilterWarning.ruleType === "block"
                        ? <><code className="font-mono bg-amber-100 px-0.5 rounded">block_url_patterns</code> removed {urlFilterWarning.dropped} URLs — only {urlFilterWarning.kept} remain for extraction.</>
                        : <><code className="font-mono bg-amber-100 px-0.5 rounded">allow_url_patterns</code> only matched {urlFilterWarning.kept} URLs — {urlFilterWarning.dropped} were not matched and dropped.</>
                      }
                    </p>
                    {urlFilterWarning.patternBreakdown && Object.keys(urlFilterWarning.patternBreakdown).length > 0 && (
                      <div className="space-y-1">
                        <p className="text-[10px] font-semibold text-amber-700 uppercase tracking-wide">Problem rule</p>
                        {Object.entries(urlFilterWarning.patternBreakdown).sort(([,a],[,b]) => b - a).map(([pat, count]) => (
                          <div key={pat} className="flex items-center gap-1.5 bg-white border border-amber-200 rounded px-1.5 py-1">
                            <code className="font-mono text-[9px] text-red-700 flex-1 min-w-0 truncate" title={pat}>{pat}</code>
                            <span className="text-[9px] font-bold text-red-600 shrink-0">{count} URLs</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {urlFilterWarning.droppedSample && urlFilterWarning.droppedSample.length > 0 && (
                      <div className="space-y-0.5">
                        <p className="text-[10px] font-semibold text-amber-600 uppercase tracking-wide">Sample dropped</p>
                        <div className="bg-white border border-amber-200 rounded p-1.5 space-y-0.5 max-h-[80px] overflow-y-auto">
                          {urlFilterWarning.droppedSample.map((u, i) => (
                            <div key={i} className="font-mono text-[9px] text-gray-600 truncate" title={u}>{u.replace(/^https?:\/\/[^/]+/, "")}</div>
                          ))}
                        </div>
                      </div>
                    )}
                    {selectedUni && !isNaN(parseInt(selectedUni)) && (
                      <a href={`/universities/${selectedUni}/recipe`} className="inline-flex items-center gap-1 text-[10px] text-blue-600 hover:underline font-semibold">
                        Fix in Recipe Editor →
                      </a>
                    )}
                  </>
                )}
              </div>
            )}

            {/* Compact log stream */}
            <div className="relative">
              <div
                ref={logContainerRef}
                className="flex-1 min-h-[160px] max-h-[420px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed"
                onScroll={(e) => {
                  // Ignore scroll events we triggered ourselves (programmatic scrollTop=).
                  if (skipNextScrollEventRef.current) {
                    skipNextScrollEventRef.current = false;
                    return;
                  }
                  const el = e.currentTarget;
                  userScrolledUpRef.current = el.scrollHeight - el.scrollTop - el.clientHeight > 40;
                }}
              >
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
              {logs.length > 0 && (
                <button
                  onClick={handleCopyLogs}
                  title="Copy all logs"
                  className="absolute top-1.5 right-1.5 p-1 rounded text-gray-500 hover:text-white hover:bg-gray-700 transition-colors"
                >
                  {copiedLogs ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
                </button>
              )}
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
            {/* URL filter warning persists into done state */}
            {urlFilterWarning && (
              <div className={`rounded-lg border p-2.5 space-y-2 text-[11px] ${
                urlFilterWarning.kind === "category_pages" ? "bg-red-50 border-red-300" : "bg-amber-50 border-amber-300"
              }`}>
                <div className="flex items-center gap-1.5 font-semibold">
                  <AlertTriangle className={`w-3.5 h-3.5 shrink-0 ${urlFilterWarning.kind === "category_pages" ? "text-red-500" : "text-amber-500"}`} />
                  {urlFilterWarning.kind === "category_pages" ? (
                    <span className="text-red-800">Discovery issue: category pages were selected instead of course pages</span>
                  ) : (
                    <span className="text-amber-800">URL filter removed {urlFilterWarning.dropPct?.toFixed(0)}% of discovered course pages</span>
                  )}
                </div>
                {urlFilterWarning.kind === "category_pages" ? (
                  <p className="text-red-700 leading-relaxed">Fix the <code className="font-mono bg-red-100 px-0.5 rounded">allow_url_patterns</code> regex to match individual course detail pages, then re-run.</p>
                ) : (
                  <>
                    <p className="text-amber-700 leading-relaxed">
                      {urlFilterWarning.ruleType === "block"
                        ? <><code className="font-mono bg-amber-100 px-0.5 rounded">block_url_patterns</code> removed {urlFilterWarning.dropped} course URLs. Only {urlFilterWarning.kept} were extracted.</>
                        : <><code className="font-mono bg-amber-100 px-0.5 rounded">allow_url_patterns</code> only matched {urlFilterWarning.kept} URLs — {urlFilterWarning.dropped} valid course pages may have been dropped.</>
                      }
                    </p>
                    {urlFilterWarning.patternBreakdown && Object.keys(urlFilterWarning.patternBreakdown).length > 0 && (
                      <div className="space-y-1">
                        <p className="text-[10px] font-semibold text-amber-700 uppercase tracking-wide">Problem rule</p>
                        {Object.entries(urlFilterWarning.patternBreakdown).sort(([,a],[,b]) => b - a).map(([pat, count]) => (
                          <div key={pat} className="flex items-center gap-1.5 bg-white border border-amber-200 rounded px-1.5 py-1">
                            <code className="font-mono text-[9px] text-red-700 flex-1 min-w-0 truncate" title={pat}>{pat}</code>
                            <span className="text-[9px] font-bold text-red-600 shrink-0">{count} URLs dropped</span>
                          </div>
                        ))}
                      </div>
                    )}
                    {urlFilterWarning.droppedSample && urlFilterWarning.droppedSample.length > 0 && (
                      <div className="space-y-0.5">
                        <p className="text-[10px] font-semibold text-amber-600 uppercase tracking-wide">Sample dropped</p>
                        <div className="bg-white border border-amber-200 rounded p-1.5 space-y-0.5 max-h-[60px] overflow-y-auto">
                          {urlFilterWarning.droppedSample.map((u, i) => (
                            <div key={i} className="font-mono text-[9px] text-gray-600 truncate" title={u}>{u.replace(/^https?:\/\/[^/]+/, "")}</div>
                          ))}
                        </div>
                      </div>
                    )}
                    {selectedUni && !isNaN(parseInt(selectedUni)) && (
                      <a href={`/universities/${selectedUni}/recipe`} className="inline-flex items-center gap-1 text-[10px] text-blue-600 hover:underline font-semibold">
                        Fix in Recipe Editor →
                      </a>
                    )}

                    {/* ── Smart YAML Fix panel ── */}
                    {repairLoading && (
                      <div className="flex items-center gap-1.5 text-[10px] text-gray-500 pt-1">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        Analysing filter patterns…
                      </div>
                    )}
                    {(() => {
                      const smart = (repairCandidates || []).find(c => c.id === "smart_replace_patterns" && c.proposed_yaml);
                      if (!smart) return null;
                      return (
                        <div className="space-y-2 pt-1.5 border-t border-amber-200 mt-1.5">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[9px] font-bold px-1.5 py-0.5 bg-green-100 text-green-700 rounded-full border border-green-200 shrink-0">✓ Smart Fix Available</span>
                            <span className="text-[10px] text-gray-600 leading-snug">{smart.description}</span>
                          </div>
                          {smart.problem_addressed && (
                            <p className="text-[10px] text-amber-700 font-semibold">{smart.problem_addressed}</p>
                          )}
                          <div className="space-y-1">
                            <p className="text-[9px] font-semibold text-gray-500 uppercase tracking-wide">Proposed YAML Fix</p>
                            <pre className="text-[9px] font-mono bg-gray-900 text-green-300 rounded p-2 overflow-x-auto max-h-[160px] overflow-y-auto whitespace-pre leading-relaxed">{smart.proposed_yaml}</pre>
                          </div>
                          {!repairFixApplied ? (
                            <div className="flex items-center gap-2 flex-wrap">
                              <button
                                type="button"
                                onClick={() => { setValidateResult(null); handleValidateRepairFix(smart); }}
                                disabled={validateLoading}
                                className="text-[10px] bg-blue-600 hover:bg-blue-700 text-white px-2 py-1 rounded flex items-center gap-1 disabled:opacity-50"
                              >
                                {validateLoading ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <Eye className="w-2.5 h-2.5" />}
                                Validate Fix
                              </button>
                              {validateResult !== null && (
                                <span className="text-[10px] font-semibold">
                                  {validateResult.after > 0 && validateResult.before === 0 ? (
                                    <span className="text-green-600">✓ Before: {validateResult.before}/{validateResult.total} pass → After: {validateResult.after}/{validateResult.total} pass</span>
                                  ) : validateResult.after > validateResult.before ? (
                                    <span className="text-green-600">↑ Before: {validateResult.before}/{validateResult.total} → After: {validateResult.after}/{validateResult.total}</span>
                                  ) : (
                                    <span className="text-amber-600">Before: {validateResult.before}/{validateResult.total} → After: {validateResult.after}/{validateResult.total} — check patterns</span>
                                  )}
                                </span>
                              )}
                              <button
                                type="button"
                                onClick={() => handleApplyRepairFix(smart)}
                                disabled={applyingRepairFix || validateResult === null}
                                title={validateResult === null ? "Validate first to confirm the fix works" : undefined}
                                className="text-[10px] bg-green-600 hover:bg-green-700 text-white px-2 py-1 rounded flex items-center gap-1 disabled:opacity-50"
                              >
                                {applyingRepairFix ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <CheckCheck className="w-2.5 h-2.5" />}
                                Apply Fix
                              </button>
                            </div>
                          ) : (
                            <div className="text-[10px] text-green-700 font-semibold bg-green-50 border border-green-200 rounded px-2 py-1">
                              ✓ Config saved — validate with Test Discovery to confirm
                            </div>
                          )}
                          {validateResult?.sample_rescued && validateResult.sample_rescued.length > 0 && (
                            <div className="space-y-0.5">
                              <p className="text-[9px] font-semibold text-green-700 uppercase tracking-wide">Rescued URLs (new pattern matches)</p>
                              <div className="bg-green-50 border border-green-200 rounded p-1.5 space-y-0.5 max-h-[60px] overflow-y-auto">
                                {validateResult.sample_rescued.map((u, i) => (
                                  <div key={i} className="font-mono text-[9px] text-gray-600 truncate" title={u}>{u.replace(/^https?:\/\/[^/]+/, "")}</div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })()}
                  </>
                )}
                {/* URL filter test tool */}
                {completedJobId && (
                  <div className={`rounded border p-2 space-y-1.5 ${urlFilterWarning.kind === "category_pages" ? "border-red-200 bg-red-25" : "border-amber-200 bg-white"}`} style={{background: "rgba(255,255,255,0.6)"}}>
                    <button
                      type="button"
                      onClick={() => setShowUrlTestPanel(v => !v)}
                      className="flex items-center gap-1 text-[10px] font-semibold text-gray-600 hover:text-gray-800"
                    >
                      <ChevronDown className={`w-2.5 h-2.5 transition-transform ${showUrlTestPanel ? "rotate-180" : ""}`} />
                      Test URL Filter — simulate which URLs pass/fail the current config
                    </button>
                    {showUrlTestPanel && (
                      <div className="space-y-1.5 pt-1">
                        <p className="text-[9px] text-gray-500 leading-relaxed">
                          Paste candidate URLs below (one per line). The test uses this university's current allow_url_patterns, must_contain, and block_url_patterns config automatically.
                        </p>
                        <textarea
                          value={urlTestInput}
                          onChange={e => setUrlTestInput(e.target.value)}
                          placeholder={"https://www.jcu.edu.au/courses/bachelor-of-science\nhttps://www.jcu.edu.au/courses/linkassets/science"}
                          rows={3}
                          className="w-full text-[9px] font-mono rounded border border-gray-200 p-1.5 resize-none focus:outline-none focus:ring-1 focus:ring-blue-300"
                        />
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={() => handleTestUrlFilter(completedJobId)}
                            disabled={urlTestLoading || !urlTestInput.trim()}
                            className="text-[10px] bg-blue-600 hover:bg-blue-700 text-white px-2 py-1 rounded flex items-center gap-1 disabled:opacity-50"
                          >
                            {urlTestLoading ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <Eye className="w-2.5 h-2.5" />}
                            Simulate
                          </button>
                          {urlTestResults && (
                            <span className="text-[9px] text-gray-500">
                              {urlTestResults.summary.kept_count}/{urlTestResults.summary.total} pass
                              {urlTestResults.summary.dropped_count > 0 && (
                                <span className="text-red-600 ml-1">· {urlTestResults.summary.dropped_count} dropped ({urlTestResults.summary.drop_pct}%)</span>
                              )}
                            </span>
                          )}
                        </div>
                        {urlTestError && (
                          <div className="text-[9px] text-red-600 bg-red-50 rounded px-1.5 py-1">{urlTestError}</div>
                        )}
                        {urlTestResults && urlTestResults.results.length > 0 && (
                          <div className="space-y-0.5">
                            {urlTestResults.results.map((r, i) => (
                              <div key={i} className={`flex items-start gap-1.5 text-[9px] rounded px-1.5 py-1 ${r.passed ? "bg-green-50 border border-green-200" : "bg-red-50 border border-red-200"}`}>
                                <span className="shrink-0 font-bold">{r.passed ? "✅" : "❌"}</span>
                                <div className="min-w-0 flex-1">
                                  <div className="font-mono truncate text-gray-700" title={r.url}>{r.url}</div>
                                  {r.passed && r.matching_allow_pattern && (
                                    <div className="text-green-600 truncate">matched: <code className="bg-green-100 px-0.5 rounded">{r.matching_allow_pattern}</code></div>
                                  )}
                                  {!r.passed && r.drop_reason && (
                                    <div className="text-red-600 truncate">{r.drop_reason}</div>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

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

            {/* ── Pipeline Performance Savings ─────────────────────── */}
            {performanceSavings && (
              <div className="rounded-lg border border-blue-100 bg-blue-50 p-3">
                <div className="text-xs font-semibold text-blue-800 mb-2">⚡ Performance Optimisations</div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-blue-700">
                  {performanceSavings.http_fetches_skipped > 0 && (
                    <div className="flex justify-between">
                      <span>HTTP fetches skipped</span>
                      <span className="font-medium">{performanceSavings.http_fetches_skipped}</span>
                    </div>
                  )}
                  {performanceSavings.empty_text_ai_skipped > 0 && (
                    <div className="flex justify-between">
                      <span>Empty-text AI skipped</span>
                      <span className="font-medium">{performanceSavings.empty_text_ai_skipped}</span>
                    </div>
                  )}
                  {performanceSavings.vision_ocr_skipped > 0 && (
                    <div className="flex justify-between">
                      <span>Vision OCR skipped</span>
                      <span className="font-medium">{performanceSavings.vision_ocr_skipped}</span>
                    </div>
                  )}
                  {performanceSavings.estimated_seconds_saved > 0 && (
                    <div className="flex justify-between">
                      <span>Est. runtime saved</span>
                      <span className="font-medium">
                        {performanceSavings.estimated_seconds_saved >= 60
                          ? `${Math.round(performanceSavings.estimated_seconds_saved / 60)} min`
                          : `${performanceSavings.estimated_seconds_saved} s`}
                      </span>
                    </div>
                  )}
                  {performanceSavings.estimated_ai_calls_saved > 0 && (
                    <div className="flex justify-between">
                      <span>Est. AI calls saved</span>
                      <span className="font-medium">{performanceSavings.estimated_ai_calls_saved}</span>
                    </div>
                  )}
                  {performanceSavings.estimated_cost_saved_usd > 0 && (
                    <div className="flex justify-between col-span-2 border-t border-blue-200 pt-1 mt-0.5">
                      <span>Est. AI cost saved</span>
                      <span className="font-medium text-blue-900">${performanceSavings.estimated_cost_saved_usd.toFixed(4)}</span>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* ── Category Landing Rejection Diagnostics ──────────── */}
            {categoryDiagnostics && (() => {
              const { skipReasons, skipReasonSamples } = categoryDiagnostics;
              const CATEGORY_TOTAL =
                (skipReasons["category_landing_page_missing_degree_qualifier"] ?? 0) +
                (skipReasons["category_landing_page_url_block"] ?? 0) +
                (skipReasons["category_landing_page_title_block"] ?? 0) +
                (skipReasons["category_landing_page_url_suffix"] ?? 0);
              if (CATEGORY_TOTAL === 0) return null;

              const SUB_REASONS: Array<{
                key: string;
                title: string;
                explanation: string;
                yamlFix?: string;
                infoOnly?: boolean;
              }> = [
                {
                  key: "category_landing_page_missing_degree_qualifier",
                  title: "Missing Degree Qualifier",
                  explanation:
                    "These may be real course pages where the H1 does not start with Bachelor, Master, MSc, MBA, PhD, Diploma, etc. " +
                    "If the URLs are already tightly limited to course pages, add skip_degree_qualifier_check: true in YAML.",
                  yamlFix: "extraction:\n  staging:\n    skip_degree_qualifier_check: true",
                },
                {
                  key: "category_landing_page_url_block",
                  title: "URL Blocklist Matched",
                  explanation:
                    "The global URL blocklist rejected these pages. If these are real course URLs for this university, " +
                    "narrow the block rule or add a host/YAML override.",
                },
                {
                  key: "category_landing_page_title_block",
                  title: "Title Blocklist Matched",
                  explanation:
                    "The page title matched a generic title block (e.g. 'Courses', 'Browse programmes'). " +
                    "If these are real course pages, check h1_selectors or course_name extraction.",
                },
                {
                  key: "category_landing_page_url_suffix",
                  title: "Category URL Suffix",
                  explanation:
                    "These URLs end with a suffix pattern that indicates a subject/specialisation picker page (e.g. /courses, /programmes). " +
                    "They are usually correct to reject — individual course URLs should be deeper in the path.",
                  infoOnly: true,
                },
              ];

              const activeReasons = SUB_REASONS.filter(r => (skipReasons[r.key] ?? 0) > 0);
              if (activeReasons.length === 0) return null;

              return (
                <div className="border border-orange-200 rounded-lg overflow-hidden">
                  <div className="px-3 py-2 bg-orange-50 border-b border-orange-200 flex items-center gap-1.5">
                    <AlertTriangle className="w-3.5 h-3.5 text-orange-600 shrink-0" />
                    <span className="text-xs font-semibold text-orange-800">Category Rejection Diagnostics</span>
                    <span className="ml-auto text-[10px] font-bold text-orange-700 bg-orange-100 border border-orange-200 px-1.5 py-0.5 rounded-full">
                      {CATEGORY_TOTAL} pages
                    </span>
                  </div>
                  <div className="p-3 space-y-3">
                    {CATEGORY_TOTAL >= 10 && (
                      <div className="text-[11px] bg-orange-50 border border-orange-200 rounded px-2.5 py-1.5 text-orange-800 leading-relaxed">
                        ⚠ Many pages rejected as category landing pages. Exact reason breakdown below.
                      </div>
                    )}
                    {activeReasons.map(({ key, title, explanation, yamlFix, infoOnly }) => {
                      const count = skipReasons[key] ?? 0;
                      const samples = skipReasonSamples[key] ?? [];
                      const severity = count >= 10 ? "high" : count >= 3 ? "medium" : "low";
                      return (
                        <div key={key} className={`rounded border-l-2 pl-2.5 pr-2 py-2 space-y-1.5 ${
                          severity === "high"   ? "border-red-400 bg-red-50" :
                          severity === "medium" ? "border-amber-400 bg-amber-50" :
                                                  "border-gray-300 bg-gray-50"
                        }`}>
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className={`text-[11px] font-semibold ${
                              severity === "high" ? "text-red-800" : severity === "medium" ? "text-amber-800" : "text-gray-700"
                            }`}>{title}</span>
                            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded-full ${
                              severity === "high"   ? "bg-red-200 text-red-700" :
                              severity === "medium" ? "bg-amber-200 text-amber-700" :
                                                      "bg-gray-200 text-gray-600"
                            }`}>{count} pages · {severity}</span>
                            {infoOnly && (
                              <span className="text-[9px] bg-blue-100 text-blue-600 px-1 py-0.5 rounded">informational</span>
                            )}
                          </div>
                          <p className="text-[10px] leading-relaxed text-gray-700">{explanation}</p>
                          {yamlFix && (
                            <div>
                              <p className="text-[9px] font-semibold text-gray-500 uppercase tracking-wide mb-0.5">YAML Fix</p>
                              <pre className="text-[9px] font-mono bg-gray-900 text-green-300 rounded p-1.5 overflow-x-auto whitespace-pre leading-relaxed">{yamlFix}</pre>
                            </div>
                          )}
                          {samples.length > 0 && (
                            <div>
                              <p className="text-[9px] font-semibold text-gray-500 uppercase tracking-wide mb-0.5">
                                Sample rejected pages ({samples.length})
                              </p>
                              <div className="space-y-1 max-h-[130px] overflow-y-auto bg-white border border-gray-200 rounded p-1.5">
                                {samples.map((s, i) => (
                                  <div key={i} className="text-[9px]">
                                    {s.name && (
                                      <div className="font-medium text-gray-700 truncate" title={s.name}>{s.name}</div>
                                    )}
                                    <div className="font-mono text-gray-500 truncate" title={s.url}>
                                      {s.url.replace(/^https?:\/\/[^/]+/, "") || s.url}
                                    </div>
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })()}

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

            {/* ── AI Diagnostic Panel ─────────────────────────────────── */}
            {completedJobId && (
              <div className="border border-blue-200 rounded-lg overflow-hidden">
                <button
                  type="button"
                  className="w-full flex items-center justify-between px-3 py-2 bg-blue-50 hover:bg-blue-100 transition-colors"
                  onClick={() => {
                    if (!diagnoseResult && !diagnoseLoading) {
                      fetchDiagnose(completedJobId);
                    } else {
                      setShowDiagnosePanel((v) => !v);
                    }
                  }}
                >
                  <div className="flex items-center gap-1.5">
                    <Bot className="w-3.5 h-3.5 text-blue-600" />
                    <span className="text-xs font-semibold text-blue-800">AI Scrape Diagnostics</span>
                    {diagnoseLoading && <Loader2 className="w-3 h-3 animate-spin text-blue-400" />}
                    {diagnoseResult?.ok && !diagnoseLoading && (
                      <span className="text-[10px] bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded-full font-medium">
                        Analysed
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1">
                    {!diagnoseResult && !diagnoseLoading && (
                      <span className="text-[10px] text-blue-500 italic">Click to diagnose</span>
                    )}
                    <ChevronDown className={`w-3 h-3 text-blue-400 transition-transform ${showDiagnosePanel && diagnoseResult ? "rotate-180" : ""}`} />
                  </div>
                </button>

                {showDiagnosePanel && (
                  <div className="p-3 space-y-2.5">
                    {diagnoseLoading && (
                      <div className="flex items-center gap-2 text-[11px] text-blue-600">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        AI is analysing the scrape job…
                      </div>
                    )}

                    {diagnoseResult?.error && (
                      <div className="text-[10px] text-red-500 bg-red-50 rounded px-2 py-1">
                        {diagnoseResult.error}
                      </div>
                    )}

                    {diagnoseResult && !diagnoseLoading && (() => {
                      const d = diagnoseResult.diagnosis ?? diagnoseResult.fallback;
                      const stats = diagnoseResult.job_stats;
                      const lb = diagnoseResult.level_breakdown;
                      const di = diagnoseResult.deterministic_issues ?? [];
                      const hasCritical = di.some(i => i.severity === "critical");
                      return (
                        <>
                          {/* ── Discovery Health panel ─────────────────────── */}
                          {lb && (
                            <div className="rounded border border-gray-200 overflow-hidden">
                              <div className={`px-2.5 py-1.5 text-[10px] font-semibold flex items-center gap-1.5 ${hasCritical ? "bg-red-50 text-red-800 border-b border-red-200" : "bg-gray-50 text-gray-700 border-b border-gray-200"}`}>
                                {hasCritical
                                  ? <><AlertTriangle className="w-3 h-3 text-red-500" /> Discovery Health — Issues Found</>
                                  : <><CheckCheck className="w-3 h-3 text-green-500" /> Discovery Health</>
                                }
                              </div>
                              {([
                                { label: "Undergraduate courses", key: "undergraduate" as const },
                                { label: "Postgraduate courses",  key: "postgraduate"  as const },
                                { label: "Research programmes",   key: "research"      as const },
                              ] as const).map(({ label, key }) => {
                                const count = lb[key];
                                const isMissing = di.some(i => i.check === `${key}_count_zero`);
                                return (
                                  <div key={key} className={`flex items-center justify-between px-2.5 py-1.5 text-[10px] border-b border-gray-100 last:border-0 ${isMissing ? "bg-red-50" : "bg-white"}`}>
                                    <span className="text-gray-700">{label}</span>
                                    {count === 0 ? (
                                      <span className="font-semibold text-red-600 flex items-center gap-1">
                                        <span>❌</span> Missing
                                      </span>
                                    ) : (
                                      <span className="font-semibold text-green-700 flex items-center gap-1">
                                        <span>✅</span> Found {count}
                                      </span>
                                    )}
                                  </div>
                                );
                              })}
                              {(lb.other + lb.unknown) > 0 && (
                                <div className="flex items-center justify-between px-2.5 py-1.5 text-[10px] bg-white text-gray-500">
                                  <span>Other / unclassified</span>
                                  <span className="font-semibold">{lb.other + lb.unknown}</span>
                                </div>
                              )}
                            </div>
                          )}

                          {/* ── Deterministic critical issues ──────────────── */}
                          {di.length > 0 && (
                            <div className="space-y-1.5">
                              <p className="text-[10px] font-semibold text-red-600 uppercase tracking-wide flex items-center gap-1">
                                <AlertTriangle className="w-3 h-3" /> Critical Issues Found
                              </p>
                              {di.map((issue, i) => (
                                <div key={i} className="rounded px-2.5 py-2 border-l-2 border-red-500 bg-red-50 space-y-1">
                                  <div className="font-semibold text-red-800 text-[11px]">❌ {issue.issue}</div>
                                  <div className="text-red-700 text-[10px] leading-relaxed">{issue.detail}</div>
                                  {issue.potential_causes && issue.potential_causes.length > 0 && (
                                    <div className="pt-0.5">
                                      <p className="text-[9px] font-semibold text-red-500 uppercase tracking-wide mb-0.5">Potential causes</p>
                                      <ul className="space-y-0.5">
                                        {issue.potential_causes.map((c, j) => (
                                          <li key={j} className="text-[10px] text-red-600 flex items-start gap-1">
                                            <span className="shrink-0 mt-0.5">•</span>
                                            <span>{c}</span>
                                          </li>
                                        ))}
                                      </ul>
                                    </div>
                                  )}
                                  {issue.recipe_patch && (
                                    <div className="mt-1.5 pt-1.5 border-t border-red-200">
                                      {issue.recipe_patch_description && (
                                        <p className="text-[9px] text-red-600 mb-1 leading-snug">
                                          <span className="font-semibold">Quick fix: </span>{issue.recipe_patch_description}
                                        </p>
                                      )}
                                      <button
                                        onClick={() => applyFix(diagnoseResult!.job_id, issue.recipe_patch!)}
                                        disabled={applyingFix || fixApplied}
                                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                                      >
                                        {applyingFix ? (
                                          <><Loader2 className="w-2.5 h-2.5 animate-spin" /> Applying…</>
                                        ) : fixApplied ? (
                                          <><CheckCheck className="w-2.5 h-2.5" /> Applied</>
                                        ) : (
                                          <>⚡ Apply Advanced Fix</>
                                        )}
                                      </button>
                                    </div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}

                          {/* Stats row */}
                          {stats && (
                            <div className="flex items-center gap-3 flex-wrap text-[10px] text-gray-500 border-b border-gray-100 pb-2">
                              <span>Found: <strong className="text-gray-700">{stats.total_found}</strong></span>
                              <span>Staged: <strong className="text-green-700">{stats.imported}</strong></span>
                              <span>Errors: <strong className="text-red-600">{stats.errors}</strong></span>
                              <span>Completeness: <strong className={stats.avg_completeness_pct >= 85 ? "text-green-700" : "text-amber-700"}>{stats.avg_completeness_pct}%</strong></span>
                              {diagnoseResult.bad_location_samples && diagnoseResult.bad_location_samples.length > 0 && (
                                <span className="text-orange-600 flex items-center gap-0.5">
                                  <AlertTriangle className="w-2.5 h-2.5" />
                                  {diagnoseResult.bad_location_samples.length} nav-text location(s) detected
                                </span>
                              )}
                            </div>
                          )}

                          {/* AI summary (shown below deterministic issues) */}
                          {d?.summary && (
                            <p className="text-[11px] text-gray-700 leading-relaxed">{d.summary}</p>
                          )}

                          {/* Root causes */}
                          {d?.root_causes && d.root_causes.length > 0 && (
                            <div className="space-y-1.5">
                              <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Root Causes</p>
                              {d.root_causes.map((rc, i) => (
                                <div key={i} className={`rounded px-2 py-1.5 text-[10px] border-l-2 ${
                                  rc.severity === "high" ? "border-red-400 bg-red-50" :
                                  rc.severity === "medium" ? "border-amber-400 bg-amber-50" :
                                  "border-gray-300 bg-gray-50"
                                }`}>
                                  <div className="font-semibold text-gray-800 mb-0.5">{rc.issue}</div>
                                  <div className="text-gray-600 leading-relaxed">{rc.explanation}</div>
                                </div>
                              ))}
                            </div>
                          )}

                          {/* Recommended actions */}
                          {d?.recommended_actions && d.recommended_actions.length > 0 && (
                            <div className="space-y-1">
                              <p className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">Recommended Actions</p>
                              {d.recommended_actions.map((a, i) => {
                                const isPlatformBug = a.fix_type === "platform_bug";
                                return (
                                  <div key={i} className={`flex items-start gap-2 text-[10px] px-2 py-1.5 rounded border ${isPlatformBug ? "bg-slate-50 border-slate-200" : "bg-gray-50 border-gray-100"}`}>
                                    <div className={`shrink-0 mt-0.5 ${isPlatformBug ? "text-slate-400" : a.auto_fixable ? "text-green-500" : "text-gray-400"}`}>
                                      {isPlatformBug ? <span className="text-[10px]">🔧</span> : a.auto_fixable ? <CheckCheck className="w-3 h-3" /> : <AlertTriangle className="w-3 h-3" />}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                      <div className="font-semibold text-gray-700 mb-0.5 flex items-center gap-1">
                                        {a.action}
                                        {isPlatformBug && (
                                          <span className="text-[9px] bg-slate-200 text-slate-600 px-1 py-0.5 rounded">Developer fix required</span>
                                        )}
                                        {!isPlatformBug && a.auto_fixable && (
                                          <span className="text-[9px] bg-green-100 text-green-700 px-1 py-0.5 rounded">config fix</span>
                                        )}
                                      </div>
                                      <div className="text-gray-500 leading-relaxed">{a.detail}</div>
                                    </div>
                                  </div>
                                );
                              })}
                            </div>
                          )}

                          {/* Verdicts */}
                          {(d?.discovery_verdict || d?.location_verdict) && (
                            <div className="flex items-center gap-2 text-[10px] text-gray-400 border-t border-gray-100 pt-2 flex-wrap">
                              {d.discovery_verdict && (
                                <span className={`px-1.5 py-0.5 rounded ${
                                  d.discovery_verdict === "ok" ? "bg-green-100 text-green-700" : "bg-amber-100 text-amber-700"
                                }`}>
                                  Discovery: {d.discovery_verdict.replace(/_/g, " ")}
                                </span>
                              )}
                              {d.location_verdict && (
                                <span className={`px-1.5 py-0.5 rounded ${
                                  d.location_verdict === "ok" ? "bg-green-100 text-green-700" : "bg-orange-100 text-orange-700"
                                }`}>
                                  Location: {d.location_verdict.replace(/_/g, " ")}
                                </span>
                              )}
                            </div>
                          )}

                          {/* AI suggested config fix */}
                          {(() => {
                            const sc = diagnoseResult?.suggested_config;
                            const hasSc = sc && Object.keys(sc).length > 0;
                            if (!hasSc) return null;
                            return (
                              <div className="rounded border border-green-200 bg-green-50 p-2 space-y-1.5">
                                <p className="text-[10px] font-semibold text-green-800 flex items-center gap-1">
                                  <Zap className="w-2.5 h-2.5" /> AI has suggested config changes
                                </p>
                                <pre className="text-[9px] bg-white border border-green-100 rounded p-1.5 overflow-auto max-h-[100px] text-gray-700 font-mono leading-relaxed">
                                  {JSON.stringify(sc, null, 2)}
                                </pre>
                                <div className="flex items-center gap-1.5">
                                  {fixApplied ? (
                                    <span className="text-[10px] text-green-700 flex items-center gap-1">
                                      <CheckCheck className="w-3 h-3" /> Config saved — validate with Test Discovery to confirm
                                    </span>
                                  ) : (
                                    <button
                                      type="button"
                                      onClick={() => applyFix(completedJobId, sc as Record<string, unknown>)}
                                      disabled={applyingFix}
                                      className="text-[10px] bg-green-600 hover:bg-green-700 text-white px-2 py-1 rounded flex items-center gap-1 disabled:opacity-50"
                                    >
                                      {applyingFix ? <Loader2 className="w-2.5 h-2.5 animate-spin" /> : <CheckCheck className="w-2.5 h-2.5" />}
                                      Apply AI Fix
                                    </button>
                                  )}
                                </div>
                              </div>
                            );
                          })()}

                          {/* Re-run diagnosis + Open Scrape Agent */}
                          <div className="flex items-center gap-2 pt-1 border-t border-gray-100 flex-wrap">
                            <button
                              type="button"
                              onClick={() => fetchDiagnose(completedJobId)}
                              disabled={diagnoseLoading}
                              className="text-[10px] text-blue-500 hover:text-blue-700 disabled:opacity-50 flex items-center gap-1"
                            >
                              <Bot className="w-3 h-3" /> Re-run diagnosis
                            </button>
                            {(diagnoseResult?.university_id || (selectedUni && selectedUni !== ALL)) && (
                              <a
                                href={`/universities/${diagnoseResult?.university_id || selectedUni}/scrape-agent`}
                                className="text-[10px] text-purple-600 hover:text-purple-800 flex items-center gap-1 ml-auto"
                              >
                                <Bot className="w-3 h-3" /> Open Scrape Fix Agent →
                              </a>
                            )}
                          </div>
                        </>
                      );
                    })()}
                  </div>
                )}
              </div>
            )}

            {/* Full log (scrollable) */}
            <div className="relative">
              <div ref={doneLogRef} className="max-h-[600px] overflow-y-auto bg-gray-950 rounded-lg p-2 font-mono text-[10px] leading-relaxed">
                {logs.map((l, i) => (
                  <div key={i} className={`${logColor(l.event, l.phase)} break-words`}>{l.message || l.event}</div>
                ))}
              </div>
              {logs.length > 0 && (
                <button
                  onClick={handleCopyLogs}
                  title="Copy all logs"
                  className="absolute top-1.5 right-1.5 p-1 rounded text-gray-500 hover:text-white hover:bg-gray-700 transition-colors"
                >
                  {copiedLogs ? <Check className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
                </button>
              )}
            </div>

            <div className="flex gap-2">
              {completedJobId && resultSummary && resultSummary.imported > 0 && (
                <Button
                  onClick={() => completedJobId && onReviewReady(completedJobId, uniName, true)}
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

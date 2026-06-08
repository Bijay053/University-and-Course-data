import { useState, useEffect, useCallback } from "react";
import { SnapshotReplayPanel } from "@/components/snapshot-replay-panel";
import { useParams, useLocation } from "wouter";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  ArrowLeft, Bot, CheckCircle2, AlertTriangle, Loader2, Zap, RefreshCw,
  CheckCheck, X, Plus, Save, Settings2, Activity, Target, TrendingUp,
  ShieldAlert, Play, ExternalLink, FlaskConical, BarChart3, Wrench, RotateCcw,
  Search, Award, ListChecks, ArrowLeftRight, ChevronDown, ChevronUp,
  ShieldCheck, Clock, FlaskRound, ShieldX, FileEdit,
  BookOpen, XCircle,
} from "lucide-react";

// ── Certification Status Badge + Selector ─────────────────────────────────────
type CertStatus = "draft" | "testing" | "certified" | "needs_review" | "failed";

const CERT_CONFIG: Record<CertStatus, { label: string; bg: string; text: string; border: string; icon: React.ReactNode }> = {
  certified:    { label: "Certified",    bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200", icon: <ShieldCheck className="w-3.5 h-3.5" /> },
  testing:      { label: "Testing",      bg: "bg-blue-50",    text: "text-blue-700",    border: "border-blue-200",    icon: <FlaskRound className="w-3.5 h-3.5" /> },
  needs_review: { label: "Needs Review", bg: "bg-amber-50",   text: "text-amber-700",   border: "border-amber-200",   icon: <AlertTriangle className="w-3.5 h-3.5" /> },
  failed:       { label: "Failed",       bg: "bg-red-50",     text: "text-red-700",     border: "border-red-200",     icon: <ShieldX className="w-3.5 h-3.5" /> },
  draft:        { label: "Draft",        bg: "bg-gray-50",    text: "text-gray-600",    border: "border-gray-200",    icon: <FileEdit className="w-3.5 h-3.5" /> },
};

function CertStatusBadge({ status, className = "" }: { status: CertStatus; className?: string }) {
  const cfg = CERT_CONFIG[status] ?? CERT_CONFIG.draft;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold border ${cfg.bg} ${cfg.text} ${cfg.border} ${className}`}>
      {cfg.icon}
      {cfg.label}
    </span>
  );
}

function CertStatusSelector({ uniId, currentStatus, currentScore, onUpdated }: {
  uniId: number;
  currentStatus: CertStatus;
  currentScore: number | null;
  onUpdated: (status: CertStatus) => void;
}) {
  const [saving, setSaving] = useState(false);
  const { toast } = useToast();

  const setStatus = async (next: CertStatus) => {
    setSaving(true);
    try {
      const body: Record<string, unknown> = { status: next };
      if (next === "certified" && currentScore != null) body.score = currentScore;
      const res = await fetch(`${BASE}/api/universities/${uniId}/certification-status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await res.text());
      toast({ title: `Status set to "${CERT_CONFIG[next]?.label ?? next}"` });
      onUpdated(next);
    } catch (e) {
      toast({ title: "Failed to update status", description: String(e), variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-wrap gap-1.5 items-center">
      {(Object.keys(CERT_CONFIG) as CertStatus[]).map(s => (
        <button
          key={s}
          disabled={saving || s === currentStatus}
          onClick={() => setStatus(s)}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border transition-all
            ${s === currentStatus
              ? `${CERT_CONFIG[s].bg} ${CERT_CONFIG[s].text} ${CERT_CONFIG[s].border} ring-2 ring-offset-1 ring-current opacity-100`
              : "bg-white text-gray-500 border-gray-200 hover:border-gray-400 opacity-70 hover:opacity-100"
            } disabled:opacity-40 disabled:cursor-not-allowed`}
        >
          {CERT_CONFIG[s].icon}
          {CERT_CONFIG[s].label}
        </button>
      ))}
      {saving && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />}
    </div>
  );
}
import { useToast } from "@/hooks/use-toast";

const BASE = "";

// ── Types ─────────────────────────────────────────────────────────────────────

type AgentConfig = {
  university_id: number;
  university_name: string;
  scrape_url: string;
  admin_config: Record<string, unknown>;
  recipe?: Record<string, unknown>;
  health_score: number;
  latest_job_id: string | null;
  has_rollback?: boolean;
  job_stats: {
    total_found: number;
    imported: number;
    skipped: number;
    errors: number;
    avg_completeness_pct: number;
    min_expected_courses: number;
  };
};

type FixBlock = {
  error: string;
  message: string;
  total_urls: number;
  passing: number;
  dropped: number;
  drop_rate_pct: number;
  dropped_samples: string[];
  filter_applied: string[];
};

type RootCause = { issue: string; explanation: string; severity: "high" | "medium" | "low" };
type RecAction = { action: string; detail: string; auto_fixable: boolean };
type DiagnosisPayload = {
  summary: string;
  root_causes: RootCause[];
  recommended_actions: RecAction[];
  discovery_verdict?: string;
  location_verdict?: string;
};

type Phase3Evidence = {
  affected_count?: number;
  sample_url?: string;
  detected_snippets?: string[];
  page_signals?: Record<string, boolean>;
};

type Phase3Fix = {
  type: string;
  description: string;
  recipe_patch?: Record<string, unknown>;
};

type FixPreviewValidation = { label: string; ok: boolean; detail: string };
type FixPreviewFieldImpact = {
  field: string;
  current_pct: number;
  expected_pct: number;
  courses_affected: number;
  courses_total: number;
  fill_rate_estimate: number;
};
type FixPreviewResult = {
  ok: boolean;
  job_id: string;
  rec_id: string;
  problem: { field: string; courses_missing: number; current_pct: number; total_staged: number };
  confidence: number;
  confidence_reason: string;
  field_impact: FixPreviewFieldImpact;
  risk_level: "low" | "medium" | "critical";
  risk_reason: string;
  url_safety?: {
    total_urls: number; passing: number; dropped: number; drop_rate_pct: number;
    dropped_samples: string[]; kept_samples: string[]; blocked: boolean; warning: boolean;
    expected_courses_before?: number; expected_courses_after?: number;
  } | null;
  validations: FixPreviewValidation[];
  affected_course_names?: string[];
  evidence_urls?: Array<{ url: string; snippet: string }>;
  sample_before_after?: { course_name: string; field_label: string; before_value: string | null; after_value: string | null } | null;
};

type Phase3Rec = {
  id: string;
  severity: "critical" | "warning" | "info";
  title: string;
  description: string;
  root_cause: string;
  confidence: number;
  confidence_reason?: string;
  evidence?: Phase3Evidence;
  fix?: Phase3Fix | null;
};

type CourseProbeSummary = {
  probed: number;
  flags: Record<string, boolean>;
  per_page: Array<{
    url: string;
    is_ielts_url: boolean;
    is_fee_url: boolean;
    signals: Record<string, boolean>;
    detected_snippets: string[];
  }>;
};

type DeterministicIssue = {
  issue: string;
  severity: "critical" | "high" | "warning";
  check: string;
  detail: string;
  potential_causes?: string[];
  fix?: {
    type: string;
    action: string;
    yaml_keys?: Record<string, unknown>;
    note?: string;
  };
  recipe_patch?: Record<string, unknown>;
  recipe_patch_description?: string;
};

type DiagnoseResult = {
  ok: boolean;
  university?: string;
  university_id?: number;
  job_stats?: { total_found: number; imported: number; skipped: number; errors: number; avg_completeness_pct: number };
  bad_location_samples?: string[];
  diagnosis?: DiagnosisPayload;
  fallback?: DiagnosisPayload;
  suggested_config?: Record<string, unknown>;
  already_applied?: boolean;
  error?: string;
  phase3_recommendations?: Phase3Rec[];
  course_probe_summary?: CourseProbeSummary;
  deterministic_issues?: DeterministicIssue[];
  level_breakdown?: Record<string, number>;
};

type SimChange = { field: string; before: string | null; after: string | null };
type SimSample = { id: number; name: string; changes: SimChange[] };

// ── Operator confidence features ───────────────────────────────────────────────

type RecipeCoverageItem = {
  id: string;
  title: string;
  category: string;
  fix_type: "recipe_fix" | "config" | "platform_bug";
  has_recipe_patch: boolean;
  field: string | null;
  recipe_keys: string[];
  description: string;
};
type RecipeCoverageCategory = {
  id: string;
  label: string;
  total: number;
  covered: number;
  items: RecipeCoverageItem[];
};
type RecipeCoverage = {
  total: number;
  covered: number;
  missing_count: number;
  coverage_pct: number;
  categories: RecipeCoverageCategory[];
  missing: RecipeCoverageItem[];
};

type CertDimension = { score: number; label: string; detail: string };
type CertificationScore = {
  available: boolean;
  reason?: string;
  overall_score: number;
  cert_level: "certified" | "good" | "needs_work" | "poor";
  dimensions: { discovery: CertDimension; extraction: CertDimension; quality: CertDimension };
  last_scrape: { job_id: string; staged: number; started_at: string | null; status: string };
};

type FieldDelta = { field: string; label: string; before: number; after: number; delta: number };
type ScrapeComparison = {
  available: boolean;
  reason?: string;
  current: { job_id: string; staged: number; started_at: string | null };
  previous: { job_id: string; staged: number; started_at: string | null };
  staged_delta: number;
  field_deltas: FieldDelta[];
};
type SimResult = { total: number; changed: number; samples: SimSample[]; message?: string };

type FilterImpact = {
  ok: boolean;
  has_filters: boolean;
  total_urls: number;
  after_filter: number;
  dropped: number;
  drop_rate_pct: number;
  kept_samples: string[];
  dropped_samples: string[];
  filter_config: { allow_url_patterns: string[]; must_contain: string[]; block_url_patterns: string[] };
  status: "ok" | "warning" | "critical";
  historical_pts?: number;
  message?: string;
};

type BrowserSeedResult = {
  raw_candidates: number;
  after_filter: number;
  dropped: number;
  drop_rate_pct: number;
  sample_passing: string[];
  sample_dropped: string[];
  ok: boolean;
  skipped?: boolean;
  reason?: string;
  warning?: string;
  error?: string;
};

type SeedResult = {
  seed_url: string;
  status_code: number;
  raw_candidates: number;
  after_filter: number;
  dropped: number;
  drop_rate_pct: number;
  sample_passing: string[];
  sample_dropped: string[];
  ok: boolean;
  warning?: string;
  error?: string;
  browser_test?: BrowserSeedResult;
};

type DiscoveryTest = {
  ok: boolean;
  seed_results: SeedResult[];
  total_raw: number;
  total_passing: number;
  total_dropped: number;
  agg_drop_rate_pct: number;
  warnings: string[];
  has_filters: boolean;
  browser_fallback_used: boolean;
  fast_only: boolean;
  safety_score: number;
  safety_score_breakdown: { historical_pts: number; seed_pts: number; config_pts: number };
  safety_level: "safe" | "warning" | "dangerous";
  agg_status: "ok" | "warning" | "critical";
  filter_config: { allow_url_patterns: string[]; must_contain: string[]; block_url_patterns: string[] };
  error?: string;
};

type ExtractionIssue = {
  field: string;
  issue_type: string;
  severity: "critical" | "high" | "medium" | "low";
  count: number;
  pct: number;
  label: string;
  detail: string;
  examples: string[];
  suggested_fix: string;
  fix_type?: "config" | "platform_bug" | "recipe_fix";
  suggested_recipe?: Record<string, unknown>;
};
type ExtractionQualityResult = {
  ok: boolean;
  job_id?: string;
  university?: string;
  course_count: number;
  avg_completeness_pct: number;
  field_fill_rates: Record<string, number>;
  field_labels: Record<string, string>;
  issues: ExtractionIssue[];
  extraction_score: number;
  message?: string;
  error?: string;
};

// ── Auto Repair types ─────────────────────────────────────────────────────────

type RepairSimulation = {
  method: "historical_filter" | "job_stats" | "estimated";
  before_count: number;
  after_count: number;
  drop_rate_before_pct: number;
  drop_rate_after_pct: number;
  historical_url_count: number;
  sample_urls_rescued: string[];
  sample_urls_kept: string[];
  note: string;
};

type RepairCandidate = {
  id: string;
  rank: number;
  label: string;
  description: string;
  category: "url_filter" | "discovery" | "url_rewrite" | "extraction";
  problem_addressed: string;
  recipe_patch: Record<string, unknown>;
  simulation: RepairSimulation;
  confidence: number;
  is_recommended: boolean;
  safety_gate_passed: boolean;
  expected_gain: number;
  selection_reason: string;
};

type RepairCandidatesResult = {
  ok: boolean;
  problem: string;
  raw_discovered: number;
  after_filter: number;
  imported: number;
  historical_url_count: number;
  candidates: RepairCandidate[];
};

// ── Tag input helper ──────────────────────────────────────────────────────────

function TagInput({
  values, onChange, placeholder,
}: { values: string[]; onChange: (v: string[]) => void; placeholder?: string }) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const t = draft.trim();
    if (t && !values.includes(t)) onChange([...values, t]);
    setDraft("");
  };
  return (
    <div className="space-y-1.5">
      <div className="flex gap-1.5 flex-wrap min-h-[28px]">
        {values.map((v) => (
          <span key={v} className="flex items-center gap-1 bg-blue-50 border border-blue-200 text-blue-800 text-[11px] px-2 py-0.5 rounded-full">
            {v}
            <button type="button" onClick={() => onChange(values.filter((x) => x !== v))}>
              <X className="w-2.5 h-2.5 hover:text-red-500" />
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-1.5">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
          placeholder={placeholder}
          className="h-7 text-xs"
        />
        <Button type="button" size="sm" variant="outline" onClick={add} className="h-7 px-2">
          <Plus className="w-3 h-3" />
        </Button>
      </div>
    </div>
  );
}

// ── Health Score ring ─────────────────────────────────────────────────────────

function HealthRing({ score }: { score: number }) {
  const r = 36;
  const circ = 2 * Math.PI * r;
  const dash = (score / 100) * circ;
  const color = score >= 75 ? "#16a34a" : score >= 50 ? "#d97706" : "#dc2626";
  return (
    <div className="relative flex items-center justify-center w-24 h-24">
      <svg width="96" height="96" viewBox="0 0 96 96" className="-rotate-90">
        <circle cx="48" cy="48" r={r} fill="none" stroke="#e5e7eb" strokeWidth="8" />
        <circle
          cx="48" cy="48" r={r} fill="none" stroke={color} strokeWidth="8"
          strokeDasharray={`${dash} ${circ - dash}`} strokeLinecap="round"
          style={{ transition: "stroke-dasharray 0.6s ease" }}
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="text-2xl font-bold leading-none" style={{ color }}>{score}</span>
        <span className="text-[9px] text-gray-400 font-medium">/ 100</span>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function ScrapeAgentPage() {
  const { id } = useParams<{ id: string }>();
  const [, navigate] = useLocation();
  const { toast } = useToast();
  const uniId = Number(id);

  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [certStatus, setCertStatus] = useState<CertStatus>("draft");
  const [lastCertifiedScore, setLastCertifiedScore] = useState<number | null>(null);

  // Config editor state — flat fields built from admin_config
  const [rejectOnline, setRejectOnline] = useState(true);
  const [minExpected, setMinExpected] = useState(0);
  const [mustContain, setMustContain] = useState<string[]>([]);
  const [blockPatterns, setBlockPatterns] = useState<string[]>([]);
  const [alwaysBrowser, setAlwaysBrowser] = useState(false);
  const [alwaysSitemap, setAlwaysSitemap] = useState(false);
  const [bfsPageBudget, setBfsPageBudget] = useState<string>("");
  const [seedUrls, setSeedUrls] = useState<string[]>([]);
  const [extraUrls, setExtraUrls] = useState<string[]>([]);

  // Recipe state
  const [feeSourceUrls, setFeeSourceUrls] = useState<string[]>([]);
  const [feeTerm, setFeeTerm] = useState<string>("");
  const [feeCalcMode, setFeeCalcMode] = useState<string>("use_source_value_only");
  const [feePreventRollup, setFeePreventRollup] = useState(true);
  const [ieltsMapping, setIeltsMapping] = useState<{ overall: string; band: string }[]>([]);
  const [nameRemoveAfter, setNameRemoveAfter] = useState<string[]>([]);
  const [nameRemoveYear, setNameRemoveYear] = useState(false);
  const [locAllowed, setLocAllowed] = useState<string[]>([]);
  const [locReject, setLocReject] = useState<string[]>([]);
  const [locReplace, setLocReplace] = useState<{ from: string; to: string }[]>([]);
  const [modeFromLoc, setModeFromLoc] = useState(false);
  const [modeOnlineKws, setModeOnlineKws] = useState<string[]>([]);
  const [degreeMapping, setDegreeMapping] = useState<{ level: string; keywords: string[] }[]>([]);
  const [followLinksFee, setFollowLinksFee] = useState<string[]>([]);
  const [followLinksEnglish, setFollowLinksEnglish] = useState<string[]>([]);
  const [savingRecipe, setSavingRecipe] = useState(false);
  const [simResult, setSimResult] = useState<SimResult | null>(null);
  const [simulating, setSimulating] = useState(false);

  const [saving, setSaving] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnoseResult, setDiagnoseResult] = useState<DiagnoseResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [appliedConfig, setAppliedConfig] = useState<Record<string, unknown> | null>(null);
  const [fixBlock, setFixBlock] = useState<FixBlock | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [autoRepairing, setAutoRepairing] = useState(false);
  const [autoRepairResult, setAutoRepairResult] = useState<{
    status: string; filter_cleared: string; new_job_id?: string | null; message: string; has_rollback?: boolean;
  } | null>(null);
  const [filterImpact, setFilterImpact] = useState<FilterImpact | null>(null);
  const [loadingImpact, setLoadingImpact] = useState(false);
  const [discoveryTest, setDiscoveryTest] = useState<DiscoveryTest | null>(null);
  const [testingDiscovery, setTestingDiscovery] = useState(false);
  const [fastOnly, setFastOnly] = useState(false);
  const [discoveryFixLoading, setDiscoveryFixLoading] = useState(false);
  const [discoveryFixResult, setDiscoveryFixResult] = useState<{
    status: string; filter_cleared: string; message: string; has_rollback?: boolean;
  } | null>(null);
  const [checkingExtraction, setCheckingExtraction] = useState(false);
  const [extractionResult, setExtractionResult] = useState<ExtractionQualityResult | null>(null);
  const [loadingCandidates, setLoadingCandidates] = useState(false);
  const [repairCandidates, setRepairCandidates] = useState<RepairCandidatesResult | null>(null);
  const [applyingCandidateId, setApplyingCandidateId] = useState<string | null>(null);
  const [postRepairCandidate, setPostRepairCandidate] = useState<RepairCandidate | null>(null);
  const [postRepairDiscovery, setPostRepairDiscovery] = useState<DiscoveryTest | null>(null);
  const [runningPostRepair, setRunningPostRepair] = useState(false);
  const [launchingFullScrape, setLaunchingFullScrape] = useState(false);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const [res, certRes] = await Promise.all([
        fetch(`${BASE}/api/universities/${uniId}/agent-config`),
        fetch(`${BASE}/api/universities/${uniId}/certification-status`),
      ]);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AgentConfig = await res.json();
      setConfig(data);
      if (certRes.ok) {
        const certData = await certRes.json();
        setCertStatus((certData.certification_status ?? "draft") as CertStatus);
        setLastCertifiedScore(certData.last_certified_score ?? null);
      }

      // Populate editor from admin_config
      const ac = data.admin_config || {};
      const disc = (ac.discovery as Record<string, unknown>) || {};
      const extr = (ac.extraction as Record<string, unknown>) || {};
      const filters = (extr.filters as Record<string, unknown>) || {};
      const oo = (filters.online_only as Record<string, unknown>) || {};

      setRejectOnline(oo.enabled !== false); // default true
      setMinExpected(Number(ac._min_expected_courses) || 0);
      setMustContain((disc.must_contain as string[]) || []);
      setBlockPatterns((disc.block_url_patterns as string[]) || []);
      setAlwaysBrowser(Boolean(disc.always_browser_discover));
      setAlwaysSitemap(Boolean(disc.always_sitemap_supplement));
      setBfsPageBudget(disc.bfs_page_budget != null ? String(disc.bfs_page_budget) : "");
      setSeedUrls((disc.seed_urls as string[]) || []);
      setExtraUrls((disc.extra_course_urls as string[]) || []);

      // Load recipe
      const rec = (data.recipe || {}) as Record<string, unknown>;
      setFeeSourceUrls((rec.fee_source_urls as string[]) || []);
      setFeeTerm((rec.fee_term as string) || "");
      setFeeCalcMode((rec.fee_calculation_mode as string) || "use_source_value_only");
      setFeePreventRollup(rec.fee_prevent_full_course_rollup !== false);
      const rawMap = (rec.ielts_component_mapping as Record<string, number>) || {};
      setIeltsMapping(Object.entries(rawMap).map(([overall, band]) => ({ overall, band: String(band) })));
      setNameRemoveAfter((rec.course_name_remove_after as string[]) || []);
      setNameRemoveYear(Boolean(rec.course_name_remove_year_suffix));
      setLocAllowed((rec.location_allowed_values as string[]) || []);
      setLocReject((rec.location_reject_values as string[]) || []);
      const rawReplace = (rec.location_replace as Record<string, string>) || {};
      setLocReplace(Object.entries(rawReplace).map(([from, to]) => ({ from, to })));
      setModeFromLoc(Boolean(rec.study_mode_from_location));
      setModeOnlineKws((rec.study_mode_online_keywords as string[]) || []);
      const rawDM = (rec.degree_mapping as Record<string, string[]>) || {};
      setDegreeMapping(Object.entries(rawDM).map(([level, keywords]) => ({ level, keywords: keywords || [] })));
      setFollowLinksFee((rec.fee_follow_links as string[]) || []);
      setFollowLinksEnglish((rec.follow_links as string[]) || []);
    } catch (e) {
      toast({ title: "Failed to load config", description: String(e), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [uniId, toast]);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  const loadFilterImpact = useCallback(async () => {
    if (!uniId) return;
    setLoadingImpact(true);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/filter-impact`);
      if (!res.ok) return;
      const data: FilterImpact = await res.json();
      setFilterImpact(data);
    } catch { /* non-fatal */ } finally {
      setLoadingImpact(false);
    }
  }, [uniId]);

  useEffect(() => { loadFilterImpact(); }, [loadFilterImpact]);

  const runDiscoveryTest = useCallback(async () => {
    if (!uniId) return;
    setTestingDiscovery(true);
    setDiscoveryTest(null);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/test-discovery?fast_only=${fastOnly}`, { method: "POST" });
      const data: DiscoveryTest = await res.json();
      setDiscoveryTest(data);
      // Refresh historical simulation too so scores stay in sync
      loadFilterImpact();
    } catch (e) {
      toast({ title: "Discovery test failed", description: String(e), variant: "destructive" });
    } finally {
      setTestingDiscovery(false);
    }
  }, [uniId, loadFilterImpact, toast]);

  // Fix the URL filter that is blocking 100% of discovered links, then re-test
  const fixUrlFilterAndRetry = useCallback(async () => {
    if (!uniId || !discoveryTest) return;
    const fc = discoveryTest.filter_config;

    // Determine which filter to clear (priority: must_contain → allow_url_patterns → block_url_patterns)
    let filterCleared = "";
    let recipePatch: Record<string, unknown> = {};

    if (fc.must_contain && fc.must_contain.length > 0) {
      filterCleared = "must_contain";
      recipePatch = { discovery: { must_contain: [] } };
    } else if (fc.allow_url_patterns && fc.allow_url_patterns.length > 0) {
      filterCleared = "allow_url_patterns";
      recipePatch = { discovery: { allow_url_patterns: [] } };
    } else if (fc.block_url_patterns && fc.block_url_patterns.length > 0) {
      filterCleared = "block_url_patterns";
      recipePatch = { discovery: { block_url_patterns: [] } };
    } else {
      toast({ title: "No active filter found", description: "Could not determine which filter to clear.", variant: "destructive" });
      return;
    }

    setDiscoveryFixLoading(true);
    setDiscoveryFixResult(null);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/auto-repair-filter`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipe_patch: recipePatch, filter_cleared: filterCleared }),
      });
      const data = await res.json();
      setDiscoveryFixResult(data);
      if (data.status === "ok") {
        // Auto re-run discovery test to show the effect
        await runDiscoveryTest();
      }
    } catch (e) {
      toast({ title: "Fix failed", description: String(e), variant: "destructive" });
    } finally {
      setDiscoveryFixLoading(false);
    }
  }, [uniId, discoveryTest, toast, runDiscoveryTest]);

  const generateRepairCandidates = useCallback(async () => {
    const jobId = config?.latest_job_id;
    if (!jobId) {
      toast({ title: "No scrape job found", description: "Run a scrape first.", variant: "destructive" });
      return;
    }
    setLoadingCandidates(true);
    setRepairCandidates(null);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/auto-repair-candidates`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: RepairCandidatesResult = await res.json();
      setRepairCandidates(data);
    } catch (err) {
      toast({ title: "Auto Repair failed", description: String(err), variant: "destructive" });
    } finally {
      setLoadingCandidates(false);
    }
  }, [config?.latest_job_id, toast]);

  const applyRepairCandidate = useCallback(async (candidate: RepairCandidate) => {
    const jobId = config?.latest_job_id;
    if (!jobId) return;
    setApplyingCandidateId(candidate.id);
    setPostRepairCandidate(null);
    setPostRepairDiscovery(null);
    try {
      // Step 1: Apply the config patch — do NOT trigger a full scrape yet
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/auto-repair-filter`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recipe_patch: candidate.recipe_patch,
          filter_cleared: candidate.id,
          trigger_scrape: false,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await res.json();
      setRepairCandidates(null);
      setPostRepairCandidate(candidate);

      // Step 2: Auto-run fast Test Discovery to validate the fix immediately
      setRunningPostRepair(true);
      const discRes = await fetch(
        `${BASE}/api/universities/${uniId}/test-discovery?fast_only=true`,
        { method: "POST" },
      );
      if (discRes.ok) {
        const discData: DiscoveryTest = await discRes.json();
        setPostRepairDiscovery(discData);
        setDiscoveryTest(discData);
        const found = discData.total_found ?? 0;
        if (found > 0) {
          toast({
            title: `Fix validated ✓ — ${found} URL${found === 1 ? "" : "s"} found`,
            description: "The config change is working. Run a full scrape when ready.",
          });
        } else {
          toast({
            title: "Fix saved — not yet validated",
            description: "Test discovery still returned 0 URLs. Try a different fix or review the block/allow patterns.",
            variant: "destructive",
          });
        }
      } else {
        toast({ title: "Fix saved", description: "Config applied — validation discovery failed. Re-run scrape to confirm." });
      }
    } catch (err) {
      toast({ title: "Apply failed", description: String(err), variant: "destructive" });
    } finally {
      setApplyingCandidateId(null);
      setRunningPostRepair(false);
      await loadConfig();
    }
  }, [config?.latest_job_id, uniId, toast, loadConfig]);

  const launchFullScrapeAfterRepair = useCallback(async () => {
    if (!uniId) return;
    setLaunchingFullScrape(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ university_id: uniId }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast({ title: "Full scrape started", description: "Monitor progress in Scraping Jobs." });
      setPostRepairCandidate(null);
      setPostRepairDiscovery(null);
      await loadConfig();
    } catch (err) {
      toast({ title: "Failed to start scrape", description: String(err), variant: "destructive" });
    } finally {
      setLaunchingFullScrape(false);
    }
  }, [uniId, toast, loadConfig]);

  // Build admin_config dict from UI state
  const buildAdminConfig = () => {
    const disc: Record<string, unknown> = {};
    if (mustContain.length) disc.must_contain = mustContain;
    if (blockPatterns.length) disc.block_url_patterns = blockPatterns;
    if (alwaysBrowser) disc.always_browser_discover = true;
    if (alwaysSitemap) disc.always_sitemap_supplement = true;
    if (bfsPageBudget) disc.bfs_page_budget = parseInt(bfsPageBudget, 10) || null;
    if (seedUrls.length) disc.seed_urls = seedUrls;
    if (extraUrls.length) disc.extra_course_urls = extraUrls;

    const cfg: Record<string, unknown> = {};
    if (Object.keys(disc).length) cfg.discovery = disc;
    cfg.extraction = { filters: { online_only: { enabled: rejectOnline } } };
    if (minExpected > 0) cfg._min_expected_courses = minExpected;
    return cfg;
  };

  const buildRecipe = () => {
    const r: Record<string, unknown> = {};
    if (feeSourceUrls.length) r.fee_source_urls = feeSourceUrls;
    if (feeTerm) r.fee_term = feeTerm;
    r.fee_calculation_mode = feeCalcMode;
    r.fee_prevent_full_course_rollup = feePreventRollup;
    if (ieltsMapping.length) {
      const m: Record<string, number> = {};
      ieltsMapping.forEach(({ overall, band }) => {
        const v = parseFloat(band);
        if (overall.trim() && !isNaN(v)) m[overall.trim()] = v;
      });
      if (Object.keys(m).length) r.ielts_component_mapping = m;
    }
    if (nameRemoveAfter.length) r.course_name_remove_after = nameRemoveAfter;
    if (nameRemoveYear) r.course_name_remove_year_suffix = true;
    if (locAllowed.length) r.location_allowed_values = locAllowed;
    if (locReject.length) r.location_reject_values = locReject;
    if (locReplace.length) {
      const rr: Record<string, string> = {};
      locReplace.forEach(({ from, to }) => { if (from.trim()) rr[from.trim()] = to; });
      if (Object.keys(rr).length) r.location_replace = rr;
    }
    if (modeFromLoc) r.study_mode_from_location = true;
    if (modeOnlineKws.length) r.study_mode_online_keywords = modeOnlineKws;
    if (degreeMapping.length) {
      const dm: Record<string, string[]> = {};
      degreeMapping.forEach(({ level, keywords }) => { if (level.trim() && keywords.length) dm[level.trim()] = keywords; });
      if (Object.keys(dm).length) r.degree_mapping = dm;
    }
    if (followLinksFee.length) r.fee_follow_links = followLinksFee;
    if (followLinksEnglish.length) r.follow_links = followLinksEnglish;
    return r;
  };

  const saveRecipe = async () => {
    setSavingRecipe(true);
    try {
      const body = buildRecipe();
      const res = await fetch(`${BASE}/api/universities/${uniId}/recipe`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast({ title: "Recipe saved", description: "Cleaning rules will apply on the next scrape." });
      await loadConfig();
    } catch (e) {
      toast({ title: "Save failed", description: String(e), variant: "destructive" });
    } finally {
      setSavingRecipe(false);
    }
  };

  const simulateRecipe = async () => {
    setSimulating(true);
    setSimResult(null);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/recipe/simulate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recipe: buildRecipe() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSimResult(await res.json());
    } catch (e) {
      toast({ title: "Simulation failed", description: String(e), variant: "destructive" });
    } finally {
      setSimulating(false);
    }
  };

  const applyRecipeFix = async (suggested: Record<string, unknown>) => {
    setSavingRecipe(true);
    try {
      const merged = { ...buildRecipe(), ...suggested };
      const res = await fetch(`${BASE}/api/universities/${uniId}/recipe`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(merged),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast({ title: "Recipe fix applied!", description: "Re-run the scrape to see the results." });
      await loadConfig();
    } catch (e) {
      toast({ title: "Apply fix failed", description: String(e), variant: "destructive" });
    } finally {
      setSavingRecipe(false);
    }
  };

  const saveConfig = async () => {
    setSaving(true);
    try {
      const body = buildAdminConfig();
      const res = await fetch(`${BASE}/api/universities/${uniId}/agent-config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast({ title: "Scrape rules saved", description: "Config will take effect on the next scrape." });
      await loadConfig();
    } catch (e) {
      toast({ title: "Save failed", description: String(e), variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const runDiagnosis = async () => {
    const jobId = config?.latest_job_id;
    if (!jobId) {
      toast({ title: "No scrape job found", description: "Run a scrape first, then diagnose.", variant: "destructive" });
      return;
    }
    setDiagnosing(true);
    setDiagnoseResult(null);
    setAutoRepairResult(null);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/diagnose`, { method: "POST" });
      const data: DiagnoseResult = await res.json();
      setDiagnoseResult(data);

      // ── Auto-repair: if 100% filter drop detected, fix it automatically ──
      const allFilteredIssue = (data.deterministic_issues || []).find(
        (di: any) => di.check === "all_filtered" && di.recipe_patch
      );
      if (allFilteredIssue?.recipe_patch) {
        // Determine which filter key was cleared from the patch
        const discPatch = allFilteredIssue.recipe_patch?.discovery || {};
        const filterCleared = Object.keys(discPatch)[0] || "url_filter";

        setAutoRepairing(true);
        try {
          const repairRes = await fetch(`${BASE}/api/scrape/jobs/${jobId}/auto-repair-filter`, {
            method: "POST",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              recipe_patch: allFilteredIssue.recipe_patch,
              filter_cleared: filterCleared,
            }),
          });
          if (!repairRes.ok) throw new Error(await repairRes.text());
          const repairData = await repairRes.json();
          setAutoRepairResult(repairData);
          if (repairData.status === "ok") {
            // Reload config so job list updates
            await loadConfig();
          }
        } catch (repairErr) {
          setAutoRepairResult({
            status: "error",
            filter_cleared: filterCleared,
            message: `Auto-repair failed: ${repairErr}`,
          });
        } finally {
          setAutoRepairing(false);
        }
      }
    } catch (e) {
      setDiagnoseResult({ ok: false, error: String(e) });
    } finally {
      setDiagnosing(false);
    }
  };

  const runExtractionQuality = async () => {
    const jobId = config?.latest_job_id;
    if (!jobId) {
      toast({ title: "No scrape job found", description: "Run a scrape first.", variant: "destructive" });
      return;
    }
    setCheckingExtraction(true);
    setExtractionResult(null);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/extraction-quality`, { method: "POST" });
      const data: ExtractionQualityResult = await res.json();
      setExtractionResult(data);
    } catch (e) {
      setExtractionResult({ ok: false, error: String(e), course_count: 0, avg_completeness_pct: 0, field_fill_rates: {}, field_labels: {}, issues: [], extraction_score: 0 });
    } finally {
      setCheckingExtraction(false);
    }
  };

  const applyFix = async (patch: Record<string, unknown>, force = false) => {
    const jobId = config?.latest_job_id;
    if (!jobId) return;
    setApplying(true);
    setFixBlock(null);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config_patch: patch, force }),
      });
      if (res.status === 422) {
        const errBody = await res.json();
        const detail = errBody?.detail ?? errBody;
        setFixBlock(detail as FixBlock);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAppliedConfig(data.new_admin_config);
      setFixBlock(null);
      if (data.url_warning) {
        toast({
          title: "Fix applied with warning",
          description: `must_contain drops ${data.url_warning.drop_rate_pct}% of known course URLs — watch the next scrape closely.`,
          variant: "destructive",
        });
      } else {
        toast({ title: "Fix applied!", description: "Config updated — re-run the scrape to see results." });
      }
      await loadConfig();
    } catch (e) {
      toast({ title: "Apply fix failed", description: String(e), variant: "destructive" });
    } finally {
      setApplying(false);
    }
  };

  const rollbackFix = async () => {
    const jobId = config?.latest_job_id;
    if (!jobId) return;
    setRollingBack(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/rollback-fix`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail || `HTTP ${res.status}`);
      }
      toast({ title: "Reverted!", description: "Config restored to before the last AI fix." });
      setAppliedConfig(null);
      setFixBlock(null);
      await loadConfig();
    } catch (e) {
      toast({ title: "Rollback failed", description: String(e), variant: "destructive" });
    } finally {
      setRollingBack(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400 gap-2">
        <Loader2 className="w-5 h-5 animate-spin" /> Loading Scrape Agent…
      </div>
    );
  }


  const d = diagnoseResult?.diagnosis ?? diagnoseResult?.fallback;
  const suggestedConfig = diagnoseResult?.suggested_config || {};
  const hasSuggestions = Object.keys(suggestedConfig).length > 0;
  const js = config?.job_stats;

  return (
    <div className="max-w-4xl mx-auto p-4 space-y-6">
      {/* Header */}
      <div className="flex items-start gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate(`/universities/${uniId}`)} className="mt-0.5">
          <ArrowLeft className="w-4 h-4 mr-1" /> Back
        </Button>
        <div>
          <h1 className="text-xl font-bold flex items-center gap-2">
            <Bot className="w-5 h-5 text-blue-600" />
            Scrape Fix Agent
          </h1>
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-sm text-gray-500">{config?.university_name}</p>
            <CertStatusBadge status={certStatus} />
          </div>
          {config?.scrape_url && (
            <a href={config.scrape_url} target="_blank" rel="noreferrer"
               className="text-xs text-blue-500 hover:underline flex items-center gap-1">
              {config.scrape_url} <ExternalLink className="w-2.5 h-2.5" />
            </a>
          )}
        </div>
      </div>

      {/* ── Certification Status ──────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4">
        <h2 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-emerald-600" /> Certification Status
        </h2>
        <div className="space-y-3">
          <div className="flex items-center gap-3 text-sm text-gray-600">
            <span>Current:</span>
            <CertStatusBadge status={certStatus} className="text-sm px-3 py-1" />
            {lastCertifiedScore != null && certStatus === "certified" && (
              <span className="text-xs text-gray-400">Score at certification: <strong>{lastCertifiedScore}</strong></span>
            )}
          </div>
          <p className="text-xs text-gray-400">Set status manually to track operator confidence in this university's scrape config.</p>
          <CertStatusSelector
            uniId={uniId}
            currentStatus={certStatus}
            currentScore={lastCertifiedScore}
            onUpdated={(s) => setCertStatus(s)}
          />
        </div>
      </div>

      {/* ── Health Score ─────────────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4">
        <h2 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
          <Activity className="w-4 h-4 text-green-600" /> Scrape Health Score
        </h2>
        <div className="flex items-center gap-6 flex-wrap">
          <HealthRing score={config?.health_score ?? 0} />
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 flex-1 min-w-0">
            <StatCard icon={<Target className="w-3.5 h-3.5" />} label="Expected" value={js?.min_expected_courses || "—"} sub="courses" color="text-gray-600" />
            <StatCard icon={<TrendingUp className="w-3.5 h-3.5" />} label="Found" value={js?.total_found ?? 0} sub="URLs discovered" color="text-blue-600" />
            <StatCard icon={<CheckCircle2 className="w-3.5 h-3.5" />} label="Staged" value={js?.imported ?? 0} sub="courses imported" color="text-green-600" />
            <StatCard icon={<ShieldAlert className="w-3.5 h-3.5" />} label="Errors" value={js?.errors ?? 0} sub="extraction errors" color={js?.errors ? "text-red-600" : "text-gray-400"} />
            <StatCard icon={<Zap className="w-3.5 h-3.5" />} label="Completeness" value={`${js?.avg_completeness_pct ?? 0}%`} sub="avg field fill" color={(js?.avg_completeness_pct ?? 0) >= 85 ? "text-green-600" : "text-amber-600"} />
            <StatCard icon={<X className="w-3.5 h-3.5" />} label="Skipped" value={js?.skipped ?? 0} sub="filtered out" color="text-orange-500" />
          </div>
        </div>
        {config?.health_score !== undefined && (
          <div className="mt-3 pt-3 border-t">
            <HealthLabel score={config.health_score} />
          </div>
        )}
      </div>

      {/* ── Certification Score ───────────────────────────────────────────── */}
      <CertificationScoreCard uniId={uniId} />

      {/* ── URL Filter Kill Banner ─────────────────────────────────────────── */}
      {js && (js.total_found ?? 0) > 50 && (js.imported ?? 0) === 0 && (
        <div className="bg-red-50 border border-red-300 rounded-xl p-4 flex gap-3">
          <ShieldAlert className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
          <div className="space-y-1 min-w-0">
            <p className="text-sm font-semibold text-red-800">URL filter likely blocked all courses</p>
            <p className="text-xs text-red-700">
              Discovery found <strong>{js.total_found}</strong> URLs but <strong>0 courses were staged</strong>.
              This almost always means a URL filter (<code className="bg-red-100 px-0.5 rounded">must_contain</code>,{" "}
              <code className="bg-red-100 px-0.5 rounded">allow_url_patterns</code>, or{" "}
              <code className="bg-red-100 px-0.5 rounded">block_url_patterns</code>) dropped every course link.
            </p>
            {config?.has_rollback && (
              <button
                onClick={rollbackFix}
                disabled={rollingBack}
                className="mt-1 inline-flex items-center gap-1.5 text-xs font-medium bg-red-600 hover:bg-red-700 text-white px-3 py-1.5 rounded-md transition-colors disabled:opacity-50"
              >
                {rollingBack ? <Loader2 className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />}
                Revert Last AI Fix
              </button>
            )}
            <p className="text-xs text-red-600 pt-1">
              Check the URL Filter Impact panel below to see which URLs are being dropped.
            </p>
          </div>
        </div>
      )}

      {/* ── URL Filter Impact Panel ───────────────────────────────────────── */}
      {(filterImpact?.has_filters || loadingImpact) && (
        <div className={`border rounded-xl p-4 space-y-3 ${
          filterImpact?.status === "critical" ? "bg-red-50 border-red-300" :
          filterImpact?.status === "warning" ? "bg-amber-50 border-amber-300" :
          "bg-white border-gray-200"
        }`}>
          <div className="flex items-center gap-2">
            <ShieldAlert className={`w-4 h-4 ${
              filterImpact?.status === "critical" ? "text-red-600" :
              filterImpact?.status === "warning" ? "text-amber-600" :
              "text-gray-400"
            }`} />
            <h3 className="text-sm font-semibold text-gray-700">URL Filter Impact</h3>
            {loadingImpact && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />}
            {filterImpact && !loadingImpact && (
              <span className={`ml-auto text-xs font-medium px-2 py-0.5 rounded-full ${
                filterImpact.status === "critical" ? "bg-red-100 text-red-700" :
                filterImpact.status === "warning" ? "bg-amber-100 text-amber-700" :
                "bg-green-100 text-green-700"
              }`}>
                {filterImpact.status === "critical" ? "Critical — too restrictive" :
                 filterImpact.status === "warning" ? "Warning — high drop rate" :
                 "OK"}
              </span>
            )}
          </div>

          {filterImpact && (
            <>
              <p className="text-xs text-gray-600">
                Simulated against <strong>{filterImpact.total_urls}</strong> historical course URLs.
                {" "}<strong className={filterImpact.status !== "ok" ? "text-red-700" : "text-green-700"}>
                  {filterImpact.dropped} dropped ({filterImpact.drop_rate_pct}%)
                </strong>
                {" · "}
                <strong className="text-green-700">{filterImpact.after_filter} kept</strong>
              </p>

              {/* Active filter config */}
              <div className="bg-white/70 rounded-lg p-2.5 text-xs space-y-1">
                {filterImpact.filter_config.allow_url_patterns.length > 0 && (
                  <div><span className="font-medium text-gray-700">allow_url_patterns: </span>
                    <span className="text-gray-600 font-mono">{filterImpact.filter_config.allow_url_patterns.join(", ")}</span>
                  </div>
                )}
                {filterImpact.filter_config.must_contain.length > 0 && (
                  <div><span className="font-medium text-gray-700">must_contain: </span>
                    <span className="text-gray-600 font-mono">{filterImpact.filter_config.must_contain.join(", ")}</span>
                  </div>
                )}
                {filterImpact.filter_config.block_url_patterns.length > 0 && (
                  <div><span className="font-medium text-gray-700">block_url_patterns: </span>
                    <span className="text-gray-600 font-mono">{filterImpact.filter_config.block_url_patterns.join(", ")}</span>
                  </div>
                )}
              </div>

              {/* Sample URLs */}
              {filterImpact.dropped_samples.length > 0 && (
                <div className="space-y-1">
                  <p className="text-[11px] font-semibold text-red-700 uppercase tracking-wide">Dropped samples</p>
                  <div className="space-y-0.5">
                    {filterImpact.dropped_samples.slice(0, 5).map((u, i) => (
                      <p key={i} className="text-[11px] text-red-700 font-mono truncate">{u}</p>
                    ))}
                  </div>
                </div>
              )}
              {filterImpact.kept_samples.length > 0 && (
                <div className="space-y-1">
                  <p className="text-[11px] font-semibold text-green-700 uppercase tracking-wide">Kept samples</p>
                  <div className="space-y-0.5">
                    {filterImpact.kept_samples.slice(0, 5).map((u, i) => (
                      <p key={i} className="text-[11px] text-green-700 font-mono truncate">{u}</p>
                    ))}
                  </div>
                </div>
              )}

              {filterImpact.message && (
                <p className="text-xs text-gray-500 italic">{filterImpact.message}</p>
              )}
            </>
          )}
        </div>
      )}

      {/* ── Test Discovery Panel ──────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4 space-y-3">
        {/* Header row */}
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <Target className="w-4 h-4 text-blue-600 shrink-0" />
            <h3 className="text-sm font-semibold text-gray-700">Test Discovery</h3>
            {discoveryTest?.browser_fallback_used && (
              <span className="text-[10px] bg-purple-100 text-purple-700 border border-purple-200 px-1.5 py-0.5 rounded font-medium">
                Browser used
              </span>
            )}
          </div>

          {/* Safety score badge — shown once test has run */}
          {discoveryTest && (
            <div className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold border ${
              discoveryTest.safety_level === "safe"
                ? "bg-green-50 border-green-300 text-green-800"
                : discoveryTest.safety_level === "warning"
                ? "bg-amber-50 border-amber-300 text-amber-800"
                : "bg-red-50 border-red-300 text-red-800"
            }`}>
              <ShieldAlert className="w-3 h-3" />
              Safety: {discoveryTest.safety_score}/100
              {" · "}
              {discoveryTest.safety_level === "safe" ? "Safe" :
               discoveryTest.safety_level === "warning" ? "Warning" : "Dangerous"}
            </div>
          )}

          {/* Fast-only toggle */}
          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={fastOnly}
              onChange={e => setFastOnly(e.target.checked)}
              className="w-3.5 h-3.5 rounded accent-blue-600"
            />
            <span className="text-[11px] text-gray-500">Fast HTTP only</span>
          </label>

          <button
            onClick={runDiscoveryTest}
            disabled={testingDiscovery}
            className="inline-flex items-center gap-1.5 text-xs font-medium bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-3 py-1.5 rounded-md transition-colors"
          >
            {testingDiscovery
              ? <><Loader2 className="w-3 h-3 animate-spin" />{fastOnly ? "Testing (HTTP)…" : "Testing (HTTP + Browser)…"}</>
              : <><Play className="w-3 h-3" />Test Discovery</>}
          </button>
        </div>

        {/* Mode explanation */}
        {!discoveryTest && !testingDiscovery && (
          <p className="text-[11px] text-gray-400 -mt-1">
            {fastOnly
              ? "Fast HTTP mode: only httpx. Faster but may miss JS-rendered sites."
              : "Auto mode: tries HTTP first, then automatically runs browser for sites that return 403 or fewer than 5 course links."}
          </p>
        )}

        {/* Score breakdown — shown after test */}
        {discoveryTest && (
          <div className="space-y-3">
            {/* Score bar */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[11px] text-gray-500">
                <span>Safety Score</span>
                <span className="font-medium text-gray-700">{discoveryTest.safety_score}/100</span>
              </div>
              <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    discoveryTest.safety_score >= 90 ? "bg-green-500" :
                    discoveryTest.safety_score >= 70 ? "bg-amber-400" : "bg-red-500"
                  }`}
                  style={{ width: `${discoveryTest.safety_score}%` }}
                />
              </div>
              {/* Breakdown chips */}
              <div className="flex gap-2 flex-wrap text-[11px]">
                {[
                  { label: "Historical URLs", pts: discoveryTest.safety_score_breakdown.historical_pts, max: 30 },
                  { label: "Live seed test", pts: discoveryTest.safety_score_breakdown.seed_pts, max: 40 },
                  { label: "Config check", pts: discoveryTest.safety_score_breakdown.config_pts, max: 30 },
                ].map(({ label, pts, max }) => (
                  <span key={label} className={`px-2 py-0.5 rounded-full border text-[10px] font-medium ${
                    pts === max ? "bg-green-50 border-green-200 text-green-700" :
                    pts >= max * 0.5 ? "bg-amber-50 border-amber-200 text-amber-700" :
                    "bg-red-50 border-red-200 text-red-700"
                  }`}>
                    {label}: {pts}/{max}
                  </span>
                ))}
              </div>
            </div>

            {/* Aggregate stats */}
            <div className="grid grid-cols-3 gap-2">
              <div className="bg-gray-50 rounded-lg p-2 text-center">
                <p className="text-base font-bold text-gray-800">{discoveryTest.total_raw}</p>
                <p className="text-[10px] text-gray-500">raw discovered</p>
              </div>
              <div className={`rounded-lg p-2 text-center ${discoveryTest.total_passing > 0 ? "bg-green-50" : "bg-gray-50"}`}>
                <p className={`text-base font-bold ${discoveryTest.total_passing > 0 ? "text-green-700" : "text-gray-400"}`}>
                  {discoveryTest.total_passing}
                </p>
                <p className="text-[10px] text-gray-500">pass filter</p>
              </div>
              <div className={`rounded-lg p-2 text-center ${discoveryTest.total_dropped > 0 && discoveryTest.agg_drop_rate_pct >= 20 ? "bg-red-50" : "bg-gray-50"}`}>
                <p className={`text-base font-bold ${discoveryTest.total_dropped > 0 && discoveryTest.agg_drop_rate_pct >= 20 ? "text-red-700" : "text-gray-500"}`}>
                  {discoveryTest.agg_drop_rate_pct}%
                </p>
                <p className="text-[10px] text-gray-500">drop rate</p>
              </div>
            </div>

            {/* ── Inline fix when 100% of URLs are dropped by a filter ─────── */}
            {discoveryTest.total_raw > 0 && discoveryTest.total_passing === 0 && discoveryTest.has_filters && (
              <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2.5 space-y-2">
                <div className="flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 text-red-600 shrink-0" />
                  <p className="text-xs font-semibold text-red-800">
                    {discoveryTest.total_raw} course URL{discoveryTest.total_raw !== 1 ? "s" : ""} discovered — all blocked by{" "}
                    {discoveryTest.filter_config.must_contain?.length
                      ? "must_contain filter"
                      : discoveryTest.filter_config.allow_url_patterns?.length
                      ? "allow_url_patterns filter"
                      : "block_url_patterns filter"}
                  </p>
                </div>
                {discoveryFixResult?.status === "ok" ? (
                  <div className="flex items-center gap-2 text-emerald-700 text-xs font-medium">
                    <CheckCheck className="w-4 h-4 shrink-0" />
                    Filter cleared ({discoveryFixResult.filter_cleared}) — re-testing discovery…
                  </div>
                ) : (
                  <button
                    onClick={fixUrlFilterAndRetry}
                    disabled={discoveryFixLoading}
                    className="flex items-center gap-1.5 text-xs font-semibold text-white bg-red-600 hover:bg-red-700 disabled:opacity-60 rounded-lg px-3 py-1.5 transition-colors"
                  >
                    {discoveryFixLoading
                      ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Clearing filter…</>
                      : <><Zap className="w-3.5 h-3.5" /> Fix URL Filter & Retry</>}
                  </button>
                )}
              </div>
            )}

            {/* Warnings */}
            {discoveryTest.warnings.length > 0 && (
              <div className="space-y-1.5">
                {discoveryTest.warnings.map((w, i) => (
                  <div key={i} className="flex gap-2 bg-amber-50 border border-amber-200 rounded-lg p-2.5">
                    <ShieldAlert className="w-3.5 h-3.5 text-amber-600 shrink-0 mt-0.5" />
                    <p className="text-xs text-amber-800">{w}</p>
                  </div>
                ))}
              </div>
            )}

            {/* Per-seed results */}
            <div className="space-y-2">
              {discoveryTest.seed_results.map((sr, i) => {
                // Determine overall card health from best available source
                const best = sr.browser_test && !sr.browser_test.skipped && sr.browser_test.ok
                  ? sr.browser_test : null;
                const effectiveOk = best ? best.raw_candidates >= 5 : (sr.ok && sr.raw_candidates >= 5);
                const effectiveDrop = best ? best.drop_rate_pct : sr.drop_rate_pct;
                const cardBg = !effectiveOk
                  ? "bg-amber-50 border-amber-200"
                  : effectiveDrop >= 70 ? "bg-red-50 border-red-200"
                  : "bg-gray-50 border-gray-200";

                return (
                  <div key={i} className={`border rounded-lg p-3 space-y-2 ${cardBg}`}>
                    {/* Seed URL */}
                    <p className="text-[11px] font-mono text-gray-700 truncate font-medium">{sr.seed_url}</p>

                    {/* HTTP phase row */}
                    <div className="flex items-center gap-2">
                      <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded shrink-0 ${
                        !sr.ok || sr.raw_candidates < 5
                          ? "bg-red-100 text-red-700"
                          : sr.drop_rate_pct >= 70 ? "bg-red-100 text-red-700"
                          : sr.drop_rate_pct >= 20 ? "bg-amber-100 text-amber-700"
                          : "bg-green-100 text-green-700"
                      }`}>HTTP</span>
                      <p className="text-[11px] text-gray-600 flex-1">
                        {!sr.ok
                          ? <span className="text-red-700">{sr.error ?? `${sr.status_code} — blocked or JS-rendered`}</span>
                          : sr.raw_candidates < 5
                          ? <span className="text-amber-700">{sr.raw_candidates} links — too few (JS-rendered site?)</span>
                          : <>
                              <span className="text-green-700 font-medium">{sr.raw_candidates} links</span>
                              {sr.dropped > 0 && <span className="text-red-700"> · {sr.dropped} dropped ({sr.drop_rate_pct}%)</span>}
                              {sr.after_filter > 0 && <span className="text-green-700"> · {sr.after_filter} pass</span>}
                            </>
                        }
                      </p>
                    </div>

                    {/* Browser phase row — only when browser was used for this seed */}
                    {sr.browser_test && !sr.browser_test.skipped && (
                      <div className="flex items-start gap-2">
                        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${
                          !sr.browser_test.ok || (sr.browser_test.raw_candidates ?? 0) < 5
                            ? "bg-red-100 text-red-700"
                            : (sr.browser_test.drop_rate_pct ?? 0) >= 70 ? "bg-red-100 text-red-700"
                            : (sr.browser_test.drop_rate_pct ?? 0) >= 20 ? "bg-amber-100 text-amber-700"
                            : "bg-purple-100 text-purple-700"
                        }`}>Browser</span>
                        {sr.browser_test.ok ? (
                          <div className="flex-1 space-y-1">
                            <p className="text-[11px] text-gray-600">
                              <span className={sr.browser_test.raw_candidates >= 5 ? "text-purple-700 font-medium" : "text-amber-700"}>
                                {sr.browser_test.raw_candidates} links found
                              </span>
                              {(sr.browser_test.dropped ?? 0) > 0 && (
                                <span className="text-red-700"> · {sr.browser_test.dropped} dropped ({sr.browser_test.drop_rate_pct}%)</span>
                              )}
                              {(sr.browser_test.after_filter ?? 0) > 0 && (
                                <span className="text-green-700"> · {sr.browser_test.after_filter} pass filter</span>
                              )}
                            </p>
                            {sr.browser_test.sample_dropped && sr.browser_test.sample_dropped.length > 0 && (
                              <div>
                                <p className="text-[10px] font-semibold text-red-700 uppercase tracking-wide mb-0.5">Dropped</p>
                                {sr.browser_test.sample_dropped.slice(0, 3).map((u, j) => (
                                  <p key={j} className="text-[10px] font-mono text-red-700 truncate">{u}</p>
                                ))}
                              </div>
                            )}
                            {sr.browser_test.sample_passing && sr.browser_test.sample_passing.length > 0 && (
                              <div>
                                <p className="text-[10px] font-semibold text-green-700 uppercase tracking-wide mb-0.5">Passing</p>
                                {sr.browser_test.sample_passing.slice(0, 3).map((u, j) => (
                                  <p key={j} className="text-[10px] font-mono text-green-700 truncate">{u}</p>
                                ))}
                              </div>
                            )}
                          </div>
                        ) : (
                          <p className="text-[11px] text-red-700 flex-1">{sr.browser_test.error ?? "Browser test failed"}</p>
                        )}
                      </div>
                    )}

                    {/* HTTP-only sample URLs (when no browser fallback) */}
                    {(!sr.browser_test || sr.browser_test.skipped) && sr.ok && sr.raw_candidates >= 5 && (
                      <>
                        {sr.sample_dropped.length > 0 && (
                          <div>
                            <p className="text-[10px] font-semibold text-red-700 uppercase tracking-wide mb-0.5">Dropped</p>
                            {sr.sample_dropped.slice(0, 3).map((u, j) => (
                              <p key={j} className="text-[10px] font-mono text-red-700 truncate">{u}</p>
                            ))}
                          </div>
                        )}
                        {sr.sample_passing.length > 0 && (
                          <div>
                            <p className="text-[10px] font-semibold text-green-700 uppercase tracking-wide mb-0.5">Passing</p>
                            {sr.sample_passing.slice(0, 3).map((u, j) => (
                              <p key={j} className="text-[10px] font-mono text-green-700 truncate">{u}</p>
                            ))}
                          </div>
                        )}
                      </>
                    )}

                    {/* Warning for this seed */}
                    {sr.warning && (
                      <p className="text-[11px] text-amber-800 bg-amber-100 rounded px-2 py-1">{sr.warning}</p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {!discoveryTest && !testingDiscovery && (
          <p className="text-xs text-gray-400">
            Click <strong>Test Discovery</strong> to fetch seed URLs live, count course-link candidates,
            and simulate how URL filters affect them — before triggering a real scrape.
          </p>
        )}
      </div>

      {/* ── Scrape Rules Editor ───────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4 space-y-5">
        <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <Settings2 className="w-4 h-4 text-purple-600" /> Scrape Rules Editor
          <span className="text-[10px] font-normal text-gray-400 ml-1">No YAML or code required — changes apply on next scrape</span>
        </h2>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
          {/* Reject online-only */}
          <FieldRow label="Reject Online-Only Courses" hint="Exclude fully-online courses (important for visa eligibility)">
            <div className="flex items-center gap-2 mt-1">
              <Switch checked={rejectOnline} onCheckedChange={setRejectOnline} id="reject-online" />
              <Label htmlFor="reject-online" className="text-xs cursor-pointer">
                {rejectOnline ? "ON — online courses rejected" : "OFF — all modes allowed"}
              </Label>
            </div>
          </FieldRow>

          {/* Expected course count */}
          <FieldRow label="Expected Course Count" hint="How many courses does this university typically offer?">
            <Input
              type="number"
              min={0}
              value={minExpected || ""}
              onChange={(e) => setMinExpected(parseInt(e.target.value, 10) || 0)}
              placeholder="e.g. 150"
              className="h-8 text-xs mt-1"
            />
          </FieldRow>

          {/* Always browser discover */}
          <FieldRow label="Use Browser (Playwright) Discovery" hint="Enable for JavaScript-rendered sites (Cloudflare, React SPAs)">
            <div className="flex items-center gap-2 mt-1">
              <Switch checked={alwaysBrowser} onCheckedChange={setAlwaysBrowser} id="browser-disc" />
              <Label htmlFor="browser-disc" className="text-xs cursor-pointer">
                {alwaysBrowser ? "ON — browser used for discovery" : "OFF — HTTP only"}
              </Label>
            </div>
          </FieldRow>

          {/* Sitemap supplement */}
          <FieldRow label="Always Supplement with Sitemap" hint="Merge sitemap URLs even when BFS finds enough results">
            <div className="flex items-center gap-2 mt-1">
              <Switch checked={alwaysSitemap} onCheckedChange={setAlwaysSitemap} id="sitemap-sup" />
              <Label htmlFor="sitemap-sup" className="text-xs cursor-pointer">
                {alwaysSitemap ? "ON — sitemap always merged" : "OFF — sitemap only as fallback"}
              </Label>
            </div>
          </FieldRow>

          {/* BFS page budget */}
          <FieldRow label="Page Discovery Budget" hint="Max pages the crawler visits (raise for large sites with pagination)">
            <Input
              type="number"
              min={5}
              max={500}
              value={bfsPageBudget}
              onChange={(e) => setBfsPageBudget(e.target.value)}
              placeholder="Default: 25 (fast) / 80 (full)"
              className="h-8 text-xs mt-1"
            />
          </FieldRow>
        </div>

        {/* URL must-contain */}
        <FieldRow label="URL Must-Contain Patterns" hint="Only keep URLs that include one of these substrings (e.g. /courses/, /study/)">
          <div className="mt-1">
            <TagInput values={mustContain} onChange={setMustContain} placeholder="e.g. /courses/" />
          </div>
        </FieldRow>

        {/* Block URL patterns */}
        <FieldRow label="Block URL Patterns (regex)" hint="Drop any discovered URL matching these regex patterns">
          <div className="mt-1">
            <TagInput values={blockPatterns} onChange={setBlockPatterns} placeholder="e.g. /apprenticeship" />
          </div>
        </FieldRow>

        {/* Discovery Seed URLs */}
        <FieldRow
          label="Discovery Seed URLs"
          hint="Course listing pages the scraper visits FIRST (highest priority). Add the real /courses/undergraduate and /courses/postgraduate pages here."
        >
          <Textarea
            value={seedUrls.join("\n")}
            onChange={(e) => setSeedUrls(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
            placeholder={"https://uni.edu/study/undergraduate/courses\nhttps://uni.edu/study/postgraduate/courses"}
            className="text-xs mt-1 min-h-[80px] font-mono border-blue-200 focus-visible:ring-blue-400"
            rows={3}
          />
          {seedUrls.length > 0 && (
            <p className="text-[10px] text-blue-600 mt-1 flex items-center gap-1">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-blue-500" />
              {seedUrls.length} seed URL{seedUrls.length > 1 ? "s" : ""} — scraper will visit these first before generic discovery
            </p>
          )}
        </FieldRow>

        {/* Known individual course pages (post-discovery injection) */}
        <FieldRow label="Known Individual Course Pages" hint="Direct course page URLs injected after discovery completes — for specific courses that discovery misses entirely">
          <Textarea
            value={extraUrls.join("\n")}
            onChange={(e) => setExtraUrls(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean))}
            placeholder={"https://uni.edu/courses/postgraduate/mba\nhttps://uni.edu/courses/undergraduate/bsc-computing"}
            className="text-xs mt-1 min-h-[60px] font-mono"
            rows={2}
          />
        </FieldRow>

        <div className="flex items-center gap-2 pt-2 border-t">
          <Button onClick={saveConfig} disabled={saving} className="gap-1.5 h-8 text-xs">
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            Save Scrape Rules
          </Button>
          {appliedConfig && (
            <span className="text-xs text-green-600 flex items-center gap-1">
              <CheckCircle2 className="w-3.5 h-3.5" /> AI fix applied — rules updated
            </span>
          )}
        </div>
      </div>

      {/* ── Data Cleaning Recipe ──────────────────────────────────────────── */}
      <RecipeEditor
        feeSourceUrls={feeSourceUrls} setFeeSourceUrls={setFeeSourceUrls}
        feeTerm={feeTerm} setFeeTerm={setFeeTerm}
        feeCalcMode={feeCalcMode} setFeeCalcMode={setFeeCalcMode}
        feePreventRollup={feePreventRollup} setFeePreventRollup={setFeePreventRollup}
        ieltsMapping={ieltsMapping} setIeltsMapping={setIeltsMapping}
        nameRemoveAfter={nameRemoveAfter} setNameRemoveAfter={setNameRemoveAfter}
        nameRemoveYear={nameRemoveYear} setNameRemoveYear={setNameRemoveYear}
        locAllowed={locAllowed} setLocAllowed={setLocAllowed}
        locReject={locReject} setLocReject={setLocReject}
        locReplace={locReplace} setLocReplace={setLocReplace}
        modeFromLoc={modeFromLoc} setModeFromLoc={setModeFromLoc}
        modeOnlineKws={modeOnlineKws} setModeOnlineKws={setModeOnlineKws}
        degreeMapping={degreeMapping} setDegreeMapping={setDegreeMapping}
        followLinksFee={followLinksFee} setFollowLinksFee={setFollowLinksFee}
        followLinksEnglish={followLinksEnglish} setFollowLinksEnglish={setFollowLinksEnglish}
        saving={savingRecipe} onSave={saveRecipe}
        onSimulate={simulateRecipe} simulating={simulating}
      />

      {/* ── Recipe Simulation Result ─────────────────────────────────────── */}
      {simResult && (
        <div className="bg-white border rounded-xl p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
              <span className="text-base">🧪</span> Recipe Simulation
              <Badge variant="outline" className={`text-[9px] px-1.5 ${simResult.changed > 0 ? "border-amber-400 text-amber-700 bg-amber-50" : "border-green-400 text-green-700 bg-green-50"}`}>
                {simResult.changed}/{simResult.total} courses affected
              </Badge>
            </h2>
            <button onClick={() => setSimResult(null)} className="p-1 rounded hover:bg-gray-100 text-gray-400"><X className="w-3.5 h-3.5" /></button>
          </div>
          {simResult.message && <p className="text-xs text-gray-500">{simResult.message}</p>}
          {simResult.samples.length === 0 && simResult.total > 0 && (
            <p className="text-xs text-green-700 flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5" /> No changes — recipe has no effect on the {simResult.total} most recent staged courses.</p>
          )}
          {simResult.samples.map(sample => (
            <div key={sample.id} className="border rounded-lg p-3 space-y-2 bg-gray-50">
              <p className="text-xs font-semibold text-gray-700 truncate">{sample.name}</p>
              <div className="space-y-1">
                {sample.changes.map((ch, i) => (
                  <div key={i} className="grid grid-cols-[80px_1fr_auto_1fr] gap-1.5 items-start text-[11px]">
                    <span className="text-gray-500 font-medium truncate">{ch.field}</span>
                    <span className="font-mono text-red-600 bg-red-50 rounded px-1 py-0.5 break-all line-through">{ch.before ?? "—"}</span>
                    <span className="text-gray-400 self-center">→</span>
                    <span className="font-mono text-green-700 bg-green-50 rounded px-1 py-0.5 break-all">{ch.after ?? "—"}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── AI Diagnosis ───────────────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
            <Bot className="w-4 h-4 text-blue-600" /> AI Scrape Diagnosis
          </h2>
          <div className="flex items-center gap-2">
            {config?.latest_job_id ? (
              <span className="text-[10px] text-gray-400">Job: {config.latest_job_id.slice(0, 8)}…</span>
            ) : (
              <span className="text-[10px] text-amber-500">No scrape job yet</span>
            )}
            <Button
              size="sm"
              onClick={runDiagnosis}
              disabled={diagnosing || !config?.latest_job_id}
              className="h-7 text-xs gap-1.5"
            >
              {diagnosing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
              {diagnosing ? "Analysing…" : "Run Diagnosis"}
            </Button>
            <Button
              size="sm"
              onClick={generateRepairCandidates}
              disabled={loadingCandidates || !config?.latest_job_id}
              variant="outline"
              className="h-7 text-xs gap-1.5 border-violet-300 text-violet-700 hover:bg-violet-50"
            >
              {loadingCandidates ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wrench className="w-3 h-3" />}
              {loadingCandidates ? "Generating fixes…" : "Auto Repair"}
            </Button>
          </div>
        </div>

        {!diagnoseResult && !diagnosing && (
          <div className="text-center py-8 text-gray-400 text-sm">
            <Bot className="w-8 h-8 mx-auto mb-2 opacity-30" />
            Click <strong>Run Diagnosis</strong> to let AI analyse the last scrape job and suggest fixes.
          </div>
        )}

        {(diagnosing || autoRepairing) && (
          <div className="flex items-center justify-center gap-2 py-8 text-blue-500 text-sm">
            <Loader2 className="w-5 h-5 animate-spin" />
            {autoRepairing
              ? "AI detected a filter issue — applying fix and restarting scrape…"
              : "AI is reading the scrape logs and thinking…"}
          </div>
        )}

        {diagnoseResult && !diagnosing && (() => {
          const stats = diagnoseResult.job_stats;
          return (
            <div className="space-y-4">
              {/* Stats row */}
              {stats && (
                <div className="flex flex-wrap gap-3 text-xs p-3 bg-gray-50 rounded-lg border">
                  <Stat label="Found" value={stats.total_found} />
                  <Stat label="Staged" value={stats.imported} good />
                  <Stat label="Skipped" value={stats.skipped} warn />
                  <Stat label="Errors" value={stats.errors} bad={stats.errors > 0} />
                  <Stat label="Completeness" value={`${stats.avg_completeness_pct}%`} good={stats.avg_completeness_pct >= 85} />
                </div>
              )}

              {/* Discovery Health panel */}
              {diagnoseResult.level_breakdown && (() => {
                const lb = diagnoseResult.level_breakdown as Record<string, number>;
                const staged = stats?.imported ?? 0;
                const discoveryIssueChecks = new Set([
                  "zero_courses_discovered", "low_course_count", "all_filtered",
                  "undergraduate_count_zero", "postgraduate_count_zero",
                ]);
                const discoveryIssues = (diagnoseResult.deterministic_issues || []).filter(
                  (di: any) => discoveryIssueChecks.has(di.check)
                );
                const hasDiscoveryIssue = discoveryIssues.length > 0;
                const levels = [
                  { key: "undergraduate", label: "Undergraduate", count: lb.undergraduate ?? 0 },
                  { key: "postgraduate",  label: "Postgraduate",  count: lb.postgraduate ?? 0 },
                  { key: "research",      label: "Research",      count: lb.research ?? 0 },
                  { key: "other",         label: "Other/Unknown", count: (lb.other ?? 0) + (lb.unknown ?? 0) },
                ];
                return (
                  <div className="rounded-lg border border-blue-100 bg-blue-50 p-3 space-y-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-blue-800 uppercase tracking-wide flex items-center gap-1.5">
                        <BookOpen className="w-3.5 h-3.5" />
                        Discovery Health
                      </span>
                      {hasDiscoveryIssue && (
                        <a
                          href={`/universities/${diagnoseResult.university_id}/recipe`}
                          className="text-[11px] font-semibold text-blue-700 hover:text-blue-900 flex items-center gap-1 px-2 py-0.5 rounded bg-blue-100 hover:bg-blue-200 transition-colors shrink-0"
                        >
                          Open Recipe Editor →
                        </a>
                      )}
                    </div>

                    {/* Expected vs found */}
                    {minExpected > 0 && (
                      <div className={`rounded px-3 py-2 text-xs flex items-start gap-2 ${
                        staged < minExpected
                          ? "bg-red-50 border border-red-200 text-red-800"
                          : "bg-green-50 border border-green-200 text-green-800"
                      }`}>
                        {staged < minExpected
                          ? <XCircle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-red-500" />
                          : <CheckCircle2 className="w-3.5 h-3.5 shrink-0 mt-0.5 text-green-500" />}
                        <span>
                          Only <strong>{staged}</strong> courses found, expected at least <strong>{minExpected}</strong>.
                          {staged < minExpected && (
                            <span className="ml-1">
                              Likely causes: missing listing URL, URL filter too strict, or courses loaded via API.
                            </span>
                          )}
                        </span>
                      </div>
                    )}

                    {/* Level breakdown grid */}
                    <div className="grid grid-cols-4 gap-2">
                      {levels.map(({ key, label, count }) => (
                        <div key={key} className={`rounded p-2 text-center border ${
                          count === 0 ? "bg-red-50 border-red-200" : "bg-white border-gray-100"
                        }`}>
                          <div className={`text-base font-bold leading-tight ${count === 0 ? "text-red-600" : "text-gray-800"}`}>
                            {count}
                          </div>
                          <div className="text-[9px] text-muted-foreground leading-tight mt-0.5">{label}</div>
                          <div className="text-[11px] mt-0.5">{count === 0 ? "❌" : "✅"}</div>
                        </div>
                      ))}
                    </div>

                    {/* Recommended actions when discovery issues exist */}
                    {hasDiscoveryIssue && (
                      <div className="rounded bg-white border border-blue-200 p-3">
                        <p className="text-[11px] font-semibold text-blue-800 mb-1.5">
                          Recommended actions to fix missing courses:
                        </p>
                        <ol className="text-[11px] text-gray-600 space-y-1 list-decimal list-inside">
                          <li>Add missing course listing URL — <strong>Recipe Editor → Discovery → Seed URLs</strong></li>
                          <li>Run <strong>Test Discovery</strong> to see per-URL link counts</li>
                          <li>Review <strong>dropped URL samples</strong> — filter may be removing valid courses</li>
                          <li>Enable <strong>Sitemap supplement</strong> if the listing page misses some courses</li>
                          <li>Add a <strong>JSON API endpoint</strong> if courses are served via API, not HTML</li>
                        </ol>
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* Error */}
              {diagnoseResult.error && (
                <div className="p-3 bg-red-50 rounded-lg text-sm text-red-600 border border-red-200">
                  {diagnoseResult.error}
                </div>
              )}

              {/* ── Auto-repair banner: shown when system fixed filter automatically ── */}
              {autoRepairResult && (
                <div className={`rounded-xl border px-4 py-3 flex items-start gap-3 ${
                  autoRepairResult.status === "ok"
                    ? "border-emerald-300 bg-emerald-50"
                    : autoRepairResult.status === "trigger_failed"
                    ? "border-amber-300 bg-amber-50"
                    : "border-red-300 bg-red-50"
                }`}>
                  <div className={`shrink-0 mt-0.5 ${
                    autoRepairResult.status === "ok" ? "text-emerald-600" :
                    autoRepairResult.status === "trigger_failed" ? "text-amber-600" : "text-red-600"
                  }`}>
                    {autoRepairResult.status === "ok"
                      ? <CheckCheck className="w-5 h-5" />
                      : <AlertTriangle className="w-5 h-5" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className={`text-sm font-semibold mb-1 ${
                      autoRepairResult.status === "ok" ? "text-emerald-800" :
                      autoRepairResult.status === "trigger_failed" ? "text-amber-800" : "text-red-800"
                    }`}>
                      {autoRepairResult.status === "ok"
                        ? "✓ Auto-repaired — scrape restarted"
                        : autoRepairResult.status === "trigger_failed"
                        ? "Filter fixed — please start scrape manually"
                        : "Auto-repair failed"}
                    </p>
                    <p className="text-xs text-gray-700 leading-relaxed">{autoRepairResult.message}</p>
                    {autoRepairResult.new_job_id && (
                      <p className="text-[10px] text-gray-500 mt-1.5 font-mono">
                        New job: {autoRepairResult.new_job_id}
                      </p>
                    )}
                    {autoRepairResult.has_rollback && (
                      <p className="text-[10px] text-emerald-700 mt-1">
                        Previous config saved — rollback available if results are unexpected.
                      </p>
                    )}
                  </div>
                </div>
              )}

              {/* Deterministic issues — shown BEFORE AI summary, no LLM involved */}
              {diagnoseResult.deterministic_issues && diagnoseResult.deterministic_issues.length > 0 && (
                <div className="space-y-2">
                  {diagnoseResult.deterministic_issues.map((di, i) => {
                    const isZero = di.check === "zero_courses_discovered";
                    const isAutoRepaired = di.check === "all_filtered" && autoRepairResult?.status === "ok";
                    const borderCls = isAutoRepaired
                      ? "border-emerald-400 bg-emerald-50/60"
                      : di.severity === "critical"
                      ? "border-red-500 bg-red-50"
                      : di.severity === "high"
                      ? "border-orange-400 bg-orange-50"
                      : "border-amber-300 bg-amber-50";
                    return (
                      <div key={i} className={`rounded-lg border-l-4 px-3 py-2.5 ${borderCls}`}>
                        <div className="flex items-center gap-2 mb-1">
                          {isAutoRepaired
                            ? <CheckCheck className="w-3.5 h-3.5 shrink-0 text-emerald-600" />
                            : <AlertTriangle className={`w-3.5 h-3.5 shrink-0 ${di.severity === "critical" ? "text-red-500" : "text-orange-500"}`} />}
                          <span className="text-xs font-semibold text-gray-800">{di.issue}</span>
                          {isAutoRepaired
                            ? <Badge variant="outline" className="text-[9px] px-1.5 ml-auto border-emerald-300 text-emerald-700">auto-repaired</Badge>
                            : <Badge variant="outline" className={`text-[9px] px-1.5 ml-auto ${di.severity === "critical" ? "border-red-300 text-red-600" : "border-orange-300 text-orange-600"}`}>
                                {di.severity}
                              </Badge>}
                        </div>
                        {/* Show full detail only when NOT auto-repaired (dev info, not needed by client) */}
                        {!isAutoRepaired && (
                          <>
                            <p className="text-xs text-gray-600 leading-relaxed">{di.detail}</p>
                            {di.potential_causes && di.potential_causes.length > 0 && (
                              <ul className="mt-1.5 space-y-0.5">
                                {di.potential_causes.map((c: string, j: number) => (
                                  <li key={j} className="text-[10px] text-gray-500 flex items-start gap-1">
                                    <span className="shrink-0 mt-0.5">•</span>{c}
                                  </li>
                                ))}
                              </ul>
                            )}
                            {di.fix && (
                              <div className={`mt-2 rounded px-2.5 py-2 border ${isZero ? "bg-blue-50 border-blue-200" : "bg-white border-gray-200"}`}>
                                <p className={`text-[10px] font-semibold mb-1.5 ${isZero ? "text-blue-700" : "text-gray-700"}`}>
                                  {isZero ? "⚙️ Required YAML fix" : "🔧 Fix"}
                                </p>
                                <p className="text-[10px] text-gray-600 mb-1.5">{di.fix.action}</p>
                                {di.fix.yaml_keys && (
                                  <div className="space-y-1">
                                    {Object.entries(di.fix.yaml_keys).map(([k, v]) => (
                                      <div key={k} className="flex items-start gap-1.5 font-mono text-[10px]">
                                        <span className="text-blue-700 shrink-0">{k}:</span>
                                        <span className="text-gray-600">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                                      </div>
                                    ))}
                                  </div>
                                )}
                                {di.fix.note && (
                                  <p className="text-[10px] text-gray-500 mt-1.5 italic">{di.fix.note}</p>
                                )}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}

              {/* Summary */}
              {d?.summary && (
                <p className="text-sm text-gray-700 leading-relaxed p-3 bg-blue-50 rounded-lg border border-blue-100">
                  {d.summary}
                </p>
              )}

              {/* Root causes */}
              {d?.root_causes && d.root_causes.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Root Causes</p>
                  {d.root_causes.map((rc, i) => (
                    <div key={i} className={`rounded-lg px-3 py-2.5 text-sm border-l-4 ${
                      rc.severity === "high" ? "border-red-500 bg-red-50" :
                      rc.severity === "medium" ? "border-amber-400 bg-amber-50" :
                      "border-gray-300 bg-gray-50"
                    }`}>
                      <div className="font-semibold text-gray-800 text-xs mb-1 flex items-center gap-2">
                        {rc.severity === "high" && <AlertTriangle className="w-3.5 h-3.5 text-red-500" />}
                        {rc.issue}
                        <Badge variant="outline" className={`text-[9px] px-1.5 ${
                          rc.severity === "high" ? "border-red-300 text-red-600" :
                          rc.severity === "medium" ? "border-amber-300 text-amber-600" :
                          "border-gray-300 text-gray-500"
                        }`}>{rc.severity}</Badge>
                      </div>
                      <p className="text-xs text-gray-600 leading-relaxed">{rc.explanation}</p>
                    </div>
                  ))}
                </div>
              )}

              {/* Recommended actions */}
              {d?.recommended_actions && d.recommended_actions.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Recommended Actions</p>
                  {d.recommended_actions.map((a, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-sm px-3 py-2.5 rounded-lg bg-gray-50 border border-gray-100">
                      <div className={`shrink-0 mt-0.5 ${a.auto_fixable ? "text-green-500" : "text-gray-400"}`}>
                        {a.auto_fixable ? <CheckCheck className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="font-semibold text-xs text-gray-700 mb-0.5 flex items-center gap-1.5">
                          {a.action}
                          {a.auto_fixable && (
                            <span className="text-[9px] bg-green-100 text-green-700 px-1.5 py-0.5 rounded-full font-medium">auto-fixable</span>
                          )}
                        </div>
                        <p className="text-xs text-gray-500 leading-relaxed">{a.detail}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Verdicts */}
              {(d?.discovery_verdict || d?.location_verdict) && (
                <div className="flex flex-wrap gap-2">
                  {d.discovery_verdict && (
                    <Badge variant="outline" className={`text-xs ${
                      d.discovery_verdict === "ok" ? "border-green-300 text-green-700 bg-green-50" : "border-amber-300 text-amber-700 bg-amber-50"
                    }`}>
                      Discovery: {d.discovery_verdict.replace(/_/g, " ")}
                    </Badge>
                  )}
                  {d.location_verdict && (
                    <Badge variant="outline" className={`text-xs ${
                      d.location_verdict === "ok" ? "border-green-300 text-green-700 bg-green-50" : "border-orange-300 text-orange-700 bg-orange-50"
                    }`}>
                      Location: {d.location_verdict.replace(/_/g, " ")}
                    </Badge>
                  )}
                </div>
              )}

              {/* Course probe summary banner */}
              {diagnoseResult.course_probe_summary && diagnoseResult.course_probe_summary.probed > 0 && (() => {
                const probe = diagnoseResult.course_probe_summary!;
                const flagLabels: Record<string, string> = {
                  international_fee_text_found: "Intl fee text on page",
                  csp_text_found: "Domestic/CSP fee on page",
                  fee_text_in_blank_pages: "Fee in blank courses",
                  english_section_found: "English section detected",
                  english_link_found: "English link detected",
                  band_text_found: "Band text detected",
                  ielts_overall_text_found: "IELTS score on page",
                  ielts_components_text_found: "IELTS components on page",
                  cloudflare_blocked_courses: "Cloudflare blocking pages",
                };
                const activeFlags = Object.entries(probe.flags).filter(([, v]) => v);
                return (
                  <div className="border border-blue-200 rounded-lg p-3 bg-blue-50 space-y-2">
                    <p className="text-[10px] font-semibold text-blue-800 uppercase tracking-wide flex items-center gap-1.5">
                      <Search className="w-3 h-3" /> Live page probe — {probe.probed} pages checked
                    </p>
                    {activeFlags.length > 0 ? (
                      <div className="flex flex-wrap gap-1.5">
                        {activeFlags.map(([key]) => (
                          <span key={key} className={`text-[9px] rounded-full px-2 py-0.5 border font-medium ${
                            key === "cloudflare_blocked_courses"
                              ? "bg-red-50 border-red-200 text-red-700"
                              : key === "csp_text_found"
                              ? "bg-amber-50 border-amber-200 text-amber-700"
                              : "bg-green-50 border-green-200 text-green-700"
                          }`}>
                            {key === "cloudflare_blocked_courses" ? "⚠ " : "✓ "}{flagLabels[key] ?? key.replace(/_/g, " ")}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="text-[10px] text-blue-600">No notable signals detected in sampled pages.</p>
                    )}
                  </div>
                );
              })()}

              {/* Phase 3 Recommendations */}
              {diagnoseResult.phase3_recommendations && diagnoseResult.phase3_recommendations.length > 0 && (
                <div className="space-y-2">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex items-center gap-1.5">
                    <FlaskConical className="w-3.5 h-3.5 text-orange-500" /> Extraction Fix Opportunities
                    <span className="text-[9px] bg-orange-50 border border-orange-200 text-orange-700 rounded-full px-1.5 py-0.5 normal-case font-medium tracking-normal">
                      {diagnoseResult.phase3_recommendations.length} finding{diagnoseResult.phase3_recommendations.length !== 1 ? "s" : ""}
                    </span>
                  </p>
                  {diagnoseResult.phase3_recommendations.map((rec) => (
                    <Phase3RecCard key={rec.id} rec={rec} jobId={config?.latest_job_id ?? null} uniId={uniId} />
                  ))}
                </div>
              )}

              {/* ── Post-repair validation panel ──────────────────────────── */}
              {(postRepairCandidate || runningPostRepair) && !repairCandidates && (() => {
                const disc = postRepairDiscovery;
                const isSafe = disc && disc.safety_level === "safe" && disc.total_raw > 0;
                const isWarning = disc && disc.safety_level === "warning";
                const isDangerous = disc && (!isSafe && !isWarning);

                return (
                  <div className="border border-violet-300 rounded-xl p-3 bg-violet-50 space-y-3">
                    {/* Header */}
                    <div className="flex items-center gap-2">
                      <Wrench className="w-4 h-4 text-violet-600 shrink-0" />
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-bold text-violet-900">Fix Applied — Validating</p>
                        {postRepairCandidate && (
                          <p className="text-[10px] text-violet-600 truncate">{postRepairCandidate.label}</p>
                        )}
                      </div>
                      {disc && (
                        <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-semibold border ${
                          isSafe ? "bg-green-50 border-green-300 text-green-800"
                          : isWarning ? "bg-amber-50 border-amber-300 text-amber-800"
                          : "bg-red-50 border-red-300 text-red-800"
                        }`}>
                          {isSafe ? <CheckCheck className="w-3 h-3" />
                          : isWarning ? <AlertTriangle className="w-3 h-3" />
                          : <ShieldAlert className="w-3 h-3" />}
                          {isSafe ? "Safe to scrape" : isWarning ? "Warning" : "Fix didn't work"}
                        </div>
                      )}
                    </div>

                    {/* Discovery running */}
                    {runningPostRepair && (
                      <div className="flex items-center gap-2 text-violet-600 text-xs py-1">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        Running fast Test Discovery to validate the fix…
                      </div>
                    )}

                    {/* Discovery result */}
                    {disc && !runningPostRepair && (
                      <div className="grid grid-cols-3 gap-1.5 text-center text-[10px]">
                        <div className="bg-white rounded border border-gray-200 p-2">
                          <div className="font-bold text-gray-800 text-sm">{disc.total_raw}</div>
                          <div className="text-gray-400">Links found</div>
                        </div>
                        <div className="bg-white rounded border border-gray-200 p-2">
                          <div className="font-bold text-emerald-700 text-sm">{disc.total_passing}</div>
                          <div className="text-gray-400">Pass filter</div>
                        </div>
                        <div className="bg-white rounded border border-gray-200 p-2">
                          <div className={`font-bold text-sm ${isSafe ? "text-green-600" : isWarning ? "text-amber-600" : "text-red-600"}`}>
                            {disc.safety_score}/100
                          </div>
                          <div className="text-gray-400">Safety score</div>
                        </div>
                      </div>
                    )}

                    {/* Action buttons */}
                    {disc && !runningPostRepair && (
                      <div className="space-y-2">
                        {isSafe && (
                          <>
                            <p className="text-[10px] text-green-700 font-medium">
                              ✓ Test Discovery confirmed {disc.total_passing} course URLs are reachable. Safe to run full scrape.
                            </p>
                            <Button
                              size="sm"
                              onClick={launchFullScrapeAfterRepair}
                              disabled={launchingFullScrape}
                              className="w-full h-8 text-xs gap-1.5 bg-green-600 hover:bg-green-700 text-white"
                            >
                              {launchingFullScrape
                                ? <><Loader2 className="w-3 h-3 animate-spin" /> Starting scrape…</>
                                : <><Play className="w-3 h-3" /> Run Full Scrape</>}
                            </Button>
                          </>
                        )}
                        {!isSafe && (
                          <>
                            <p className="text-[10px] text-amber-700 font-medium">
                              {disc.total_raw === 0
                                ? "⚠ Test Discovery still found 0 links. This fix didn't help — try the next candidate."
                                : `⚠ Only ${disc.total_passing} URLs pass the filter (score ${disc.safety_score}/100). Consider trying the next fix or adjusting the config manually.`}
                            </p>
                            <Button
                              size="sm"
                              onClick={generateRepairCandidates}
                              disabled={loadingCandidates}
                              variant="outline"
                              className="w-full h-7 text-xs gap-1.5 border-violet-300 text-violet-700 hover:bg-violet-50"
                            >
                              {loadingCandidates ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
                              Try Another Fix
                            </Button>
                          </>
                        )}
                        <button
                          type="button"
                          onClick={() => { setPostRepairCandidate(null); setPostRepairDiscovery(null); }}
                          className="text-[10px] text-gray-400 hover:text-gray-600 w-full text-center"
                        >
                          Dismiss
                        </button>
                      </div>
                    )}
                  </div>
                );
              })()}

              {/* ── Auto Repair: loading spinner ──────────────────────────── */}
              {loadingCandidates && (
                <div className="flex items-center gap-2 py-4 text-violet-600 text-xs">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Simulating fix candidates against historical URLs…
                </div>
              )}

              {/* ── Auto Repair: ranked candidates panel ─────────────────── */}
              {repairCandidates && !loadingCandidates && !autoRepairResult && (() => {
                const { candidates, problem, raw_discovered, after_filter, historical_url_count } = repairCandidates;
                const recommended = candidates.find(c => c.is_recommended);
                const others = candidates.filter(c => !c.is_recommended);
                const problemLabel: Record<string, string> = {
                  url_filter_drop: "URL filter dropped all discovered links",
                  partial_filter: "URL filter dropped >50% of discovered links",
                  low_discovery: "Discovery found 0 links (JS-rendered site)",
                  low_count: "Fewer courses found than expected",
                };

                const CategoryIcon = ({ cat }: { cat: string }) => {
                  if (cat === "url_filter") return <ShieldAlert className="w-3 h-3 shrink-0" />;
                  if (cat === "discovery") return <Search className="w-3 h-3 shrink-0" />;
                  return <Wrench className="w-3 h-3 shrink-0" />;
                };

                const MethodBadge = ({ method }: { method: string }) => {
                  const styles: Record<string, string> = {
                    historical_filter: "bg-emerald-100 text-emerald-700 border-emerald-200",
                    job_stats: "bg-blue-100 text-blue-700 border-blue-200",
                    estimated: "bg-amber-100 text-amber-700 border-amber-200",
                  };
                  const labels: Record<string, string> = {
                    historical_filter: "simulated",
                    job_stats: "job data",
                    estimated: "estimated",
                  };
                  return (
                    <span className={`text-[9px] px-1.5 py-0.5 rounded border font-medium ${styles[method] ?? styles.estimated}`}>
                      {labels[method] ?? method}
                    </span>
                  );
                };

                const CountBar = ({ before, after }: { before: number; after: number }) => {
                  const max = Math.max(before, after, 1);
                  return (
                    <div className="flex items-center gap-2 text-[10px]">
                      <span className="text-gray-500 w-10 text-right">{before}</span>
                      <div className="flex-1 h-3 bg-gray-100 rounded-full overflow-hidden flex">
                        <div
                          className="h-full bg-red-300 rounded-l-full"
                          style={{ width: `${(before / max) * 100}%` }}
                        />
                      </div>
                      <span className="text-gray-400">→</span>
                      <div className="flex-1 h-3 bg-gray-100 rounded-full overflow-hidden flex">
                        <div
                          className="h-full bg-emerald-400 rounded-l-full transition-all"
                          style={{ width: `${(after / max) * 100}%` }}
                        />
                      </div>
                      <span className="text-emerald-700 font-semibold w-10">{after}</span>
                    </div>
                  );
                };

                const CandidateCard = ({ c, isTop }: { c: RepairCandidate; isTop: boolean }) => {
                  const isApplying = applyingCandidateId === c.id;
                  const anyApplying = applyingCandidateId !== null;
                  return (
                    <div className={`rounded-lg border p-3 space-y-2.5 ${
                      isTop
                        ? "border-violet-300 bg-violet-50"
                        : "border-gray-200 bg-white"
                    }`}>
                      {/* Header row */}
                      <div className="flex items-start gap-2 flex-wrap">
                        <CategoryIcon cat={c.category} />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            {isTop && (
                              <span className="text-[9px] bg-violet-600 text-white rounded-full px-2 py-0.5 font-semibold">
                                Recommended
                              </span>
                            )}
                            {!c.safety_gate_passed && (
                              <span className="text-[9px] bg-red-100 text-red-700 border border-red-200 rounded-full px-2 py-0.5 font-medium">
                                Safety gate failed
                              </span>
                            )}
                            <MethodBadge method={c.simulation.method} />
                          </div>
                          <p className="text-[11px] font-semibold text-gray-800 mt-0.5">{c.label}</p>
                          <p className="text-[10px] text-gray-500 leading-relaxed">{c.description}</p>
                        </div>
                        <div className="text-right shrink-0">
                          <div className="text-lg font-bold text-violet-700">{c.confidence}%</div>
                          <div className="text-[9px] text-gray-400">confidence</div>
                        </div>
                      </div>

                      {/* Count bar */}
                      <div>
                        <div className="flex justify-between text-[9px] text-gray-400 mb-1">
                          <span>URLs before fix</span>
                          <span>URLs after fix</span>
                        </div>
                        <CountBar before={c.simulation.before_count} after={c.simulation.after_count} />
                      </div>

                      {/* Drop rate + gain */}
                      <div className="grid grid-cols-3 gap-1.5 text-center text-[10px]">
                        <div className="bg-white rounded border border-gray-200 p-1.5">
                          <div className="font-bold text-red-600">{c.simulation.drop_rate_before_pct}%</div>
                          <div className="text-gray-400">Drop before</div>
                        </div>
                        <div className="bg-white rounded border border-gray-200 p-1.5">
                          <div className="font-bold text-emerald-600">{c.simulation.drop_rate_after_pct}%</div>
                          <div className="text-gray-400">Drop after</div>
                        </div>
                        <div className="bg-white rounded border border-gray-200 p-1.5">
                          <div className="font-bold text-violet-700">+{c.expected_gain}</div>
                          <div className="text-gray-400">Expected gain</div>
                        </div>
                      </div>

                      {/* Rescued URL samples */}
                      {c.simulation.sample_urls_rescued.length > 0 && (
                        <div>
                          <p className="text-[9px] font-semibold text-emerald-700 mb-0.5">Sample URLs this fix rescues:</p>
                          <ul className="text-[9px] text-emerald-800 font-mono space-y-0.5 max-h-[60px] overflow-auto">
                            {c.simulation.sample_urls_rescued.map((u, i) => (
                              <li key={i} className="truncate">✓ {u}</li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {/* Why this fix was selected */}
                      {c.selection_reason && (
                        <div className={`rounded px-2 py-1.5 text-[9px] leading-relaxed flex items-start gap-1.5 ${
                          isTop
                            ? "bg-violet-100 border border-violet-200 text-violet-800"
                            : "bg-gray-50 border border-gray-200 text-gray-600"
                        }`}>
                          <span className="font-semibold shrink-0">Why:</span>
                          <span>{c.selection_reason}</span>
                        </div>
                      )}

                      {/* Simulation note */}
                      {c.simulation.note && (
                        <p className="text-[9px] text-amber-700 bg-amber-50 border border-amber-100 rounded px-2 py-1 leading-relaxed">
                          {c.simulation.note}
                        </p>
                      )}

                      {/* Apply button */}
                      {c.safety_gate_passed && (
                        <Button
                          size="sm"
                          onClick={() => applyRepairCandidate(c)}
                          disabled={anyApplying}
                          className={`w-full h-7 text-xs gap-1.5 ${
                            isTop
                              ? "bg-violet-600 hover:bg-violet-700 text-white"
                              : "bg-gray-100 hover:bg-gray-200 text-gray-700 border border-gray-300"
                          }`}
                        >
                          {isApplying ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCheck className="w-3 h-3" />}
                          {isApplying
                            ? "Applying fix…"
                            : isTop
                              ? "Apply Recommended Fix"
                              : "Apply This Fix"}
                        </Button>
                      )}
                    </div>
                  );
                };

                return (
                  <div className="border border-violet-200 rounded-xl p-3 bg-violet-50/40 space-y-3">
                    {/* Panel header */}
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="flex items-center gap-2">
                          <Wrench className="w-4 h-4 text-violet-600" />
                          <p className="text-xs font-bold text-violet-900">Auto Repair Results</p>
                          <span className="text-[9px] bg-violet-100 text-violet-700 border border-violet-200 rounded-full px-2 py-0.5 font-medium">
                            {candidates.length} fix{candidates.length !== 1 ? "es" : ""} tested
                          </span>
                        </div>
                        <p className="text-[10px] text-gray-500 mt-0.5">
                          Problem: <strong>{problemLabel[problem] ?? problem}</strong>
                          {" · "}
                          {raw_discovered} discovered
                          {" → "}
                          {after_filter} passed filter
                          {historical_url_count > 0 && ` · simulated against ${historical_url_count} historical URLs`}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="text-gray-400 hover:text-gray-600 p-0.5"
                        onClick={() => setRepairCandidates(null)}
                        title="Dismiss"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>

                    {candidates.length === 0 && (
                      <p className="text-xs text-gray-400 text-center py-3">
                        No fix candidates passed the safety gate (after_count &gt; 0, drop_rate &lt; 70%).
                        Try running Test Discovery first to see what the scraper is finding.
                      </p>
                    )}

                    {/* Recommended fix */}
                    {recommended && (
                      <div>
                        <p className="text-[9px] font-semibold text-violet-700 uppercase tracking-wide mb-1.5">Recommended fix</p>
                        <CandidateCard c={recommended} isTop={true} />
                      </div>
                    )}

                    {/* Other options */}
                    {others.length > 0 && (
                      <div>
                        <p className="text-[9px] font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                          Other options ({others.length})
                        </p>
                        <div className="space-y-2">
                          {others.map(c => (
                            <CandidateCard key={c.id} c={c} isTop={false} />
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Refresh link */}
                    <div className="flex justify-end">
                      <button
                        type="button"
                        onClick={generateRepairCandidates}
                        disabled={loadingCandidates}
                        className="text-[10px] text-violet-500 hover:text-violet-700 flex items-center gap-1"
                      >
                        <RefreshCw className="w-3 h-3" /> Re-run simulation
                      </button>
                    </div>
                  </div>
                );
              })()}

              {/* Suggested config + Apply Fix — hidden when auto-repair already handled the issue */}
              {hasSuggestions && !autoRepairResult && (() => {
                const sugDisc = (suggestedConfig.discovery ?? {}) as Record<string, unknown>;
                const sugExt = (suggestedConfig.extraction ?? {}) as Record<string, unknown>;
                const hasMustContain = Array.isArray(sugDisc.must_contain) && (sugDisc.must_contain as string[]).length > 0;

                // Build grouped sections so each concern area is clearly labelled
                type YamlGroup = { label: string; color: string; keys: Record<string, unknown> };
                const groups: YamlGroup[] = [];

                if (Object.keys(sugDisc).length > 0)
                  groups.push({ label: "Discovery fix", color: "blue", keys: { discovery: sugDisc } });

                const feeKws = sugExt.international_fee_keywords;
                if (feeKws) groups.push({ label: "Fee fix", color: "green", keys: { extraction: { international_fee_keywords: feeKws } } });

                const smOpts = sugExt.study_mode;
                if (smOpts) groups.push({ label: "Study mode fix", color: "purple", keys: { extraction: { study_mode: smOpts } } });

                const intakeOpts = sugExt.intake;
                if (intakeOpts) groups.push({ label: "Intake fix", color: "amber", keys: { extraction: { intake: intakeOpts } } });

                const engOpts = sugExt.english;
                if (engOpts) groups.push({ label: "English requirements fix", color: "red", keys: { extraction: { english: engOpts } } });

                const filtersOpts = sugExt.filters;
                if (filtersOpts) groups.push({ label: "Filter fix", color: "orange", keys: { extraction: { filters: filtersOpts } } });

                const knownExtKeys = new Set(["international_fee_keywords", "study_mode", "intake", "english", "filters"]);
                const otherExt = Object.fromEntries(Object.entries(sugExt).filter(([k]) => !knownExtKeys.has(k)));
                if (Object.keys(otherExt).length > 0)
                  groups.push({ label: "Extraction fix", color: "gray", keys: { extraction: otherExt } });

                // Fallback: show raw if no group matched anything meaningful
                const showRaw = groups.length === 0;

                const groupBorder: Record<string, string> = {
                  blue: "border-blue-200 bg-blue-50", green: "border-green-200 bg-green-50",
                  purple: "border-purple-200 bg-purple-50", amber: "border-amber-200 bg-amber-50",
                  red: "border-red-200 bg-red-50", orange: "border-orange-200 bg-orange-50",
                  gray: "border-gray-200 bg-gray-50",
                };
                const groupText: Record<string, string> = {
                  blue: "text-blue-800", green: "text-green-800", purple: "text-purple-800",
                  amber: "text-amber-800", red: "text-red-800", orange: "text-orange-800",
                  gray: "text-gray-700",
                };

                return (
                  <div className="border border-green-200 rounded-lg p-3 bg-green-50 space-y-3">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Zap className="w-4 h-4 text-green-600" />
                      <p className="text-xs font-semibold text-green-800">AI has suggested config changes</p>
                      {hasMustContain && (
                        <span className="flex items-center gap-1 text-[9px] bg-amber-100 text-amber-800 border border-amber-300 rounded-full px-2 py-0.5 font-medium">
                          <AlertTriangle className="w-3 h-3" /> contains must_contain filter — will be validated before saving
                        </span>
                      )}
                    </div>

                    {/* Grouped YAML sections by concern area */}
                    {!showRaw && (
                      <div className="space-y-2">
                        {groups.map((g, i) => (
                          <div key={i} className={`border rounded p-2 ${groupBorder[g.color]}`}>
                            <p className={`text-[10px] font-semibold mb-1 ${groupText[g.color]}`}>{g.label}</p>
                            <pre className="text-[10px] bg-white border border-gray-100 rounded px-2 py-1 overflow-auto max-h-[120px] text-gray-700 font-mono leading-relaxed">
                              {JSON.stringify(g.keys, null, 2)}
                            </pre>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Fallback: raw JSON when no section matched */}
                    {showRaw && (
                      <pre className="text-[10px] bg-white border border-green-100 rounded p-2 overflow-auto max-h-[200px] text-gray-700 font-mono leading-relaxed">
                        {JSON.stringify(suggestedConfig, null, 2)}
                      </pre>
                    )}

                    {/* FixBlock: shown when backend blocked the apply due to high drop rate */}
                    {fixBlock && (
                      <div className="rounded-lg border border-red-300 bg-red-50 p-3 space-y-2">
                        <div className="flex items-center gap-2">
                          <ShieldAlert className="w-4 h-4 text-red-600 shrink-0" />
                          <p className="text-xs font-bold text-red-800">Fix Blocked — AI Generated Bad Config</p>
                        </div>
                        <p className="text-xs text-red-700">{fixBlock.message}</p>
                        <div className="grid grid-cols-3 gap-2 text-center text-[10px]">
                          <div className="bg-white rounded border border-red-200 p-1.5">
                            <div className="font-bold text-gray-800">{fixBlock.total_urls}</div>
                            <div className="text-gray-500">Known URLs</div>
                          </div>
                          <div className="bg-white rounded border border-green-200 p-1.5">
                            <div className="font-bold text-green-700">{fixBlock.passing}</div>
                            <div className="text-gray-500">Would pass</div>
                          </div>
                          <div className="bg-white rounded border border-red-200 p-1.5">
                            <div className="font-bold text-red-600">{fixBlock.dropped} ({fixBlock.drop_rate_pct}%)</div>
                            <div className="text-gray-500">Dropped</div>
                          </div>
                        </div>
                        {fixBlock.dropped_samples?.length > 0 && (
                          <div>
                            <p className="text-[9px] font-semibold text-red-700 mb-1">Sample dropped course URLs:</p>
                            <ul className="text-[9px] text-red-800 font-mono space-y-0.5 max-h-[80px] overflow-auto">
                              {fixBlock.dropped_samples.map((u, i) => <li key={i} className="truncate">✗ {u}</li>)}
                            </ul>
                          </div>
                        )}
                        {fixBlock.error === "must_contain_filter_high_drop_rate" && (
                          <div className="flex items-center gap-2 pt-1">
                            <Button
                              size="sm"
                              onClick={() => applyFix(suggestedConfig as Record<string, unknown>, true)}
                              disabled={applying}
                              className="bg-amber-600 hover:bg-amber-700 h-7 text-[10px] gap-1"
                            >
                              {applying ? <Loader2 className="w-3 h-3 animate-spin" /> : <AlertTriangle className="w-3 h-3" />}
                              Force Apply Anyway ({fixBlock.drop_rate_pct}% drop)
                            </Button>
                            <span className="text-[9px] text-amber-700">Not recommended — verify URL patterns first.</span>
                          </div>
                        )}
                        {fixBlock.error !== "must_contain_filter_high_drop_rate" && (
                          <p className="text-[9px] text-red-600 font-medium">This fix cannot be applied. Edit the must_contain patterns in the Config Editor below and save manually instead.</p>
                        )}
                      </div>
                    )}

                    {!fixBlock && (
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          onClick={() => applyFix(suggestedConfig as Record<string, unknown>)}
                          disabled={applying}
                          className="bg-green-600 hover:bg-green-700 h-8 text-xs gap-1.5"
                        >
                          {applying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCheck className="w-3.5 h-3.5" />}
                          {applying ? "Validating & applying…" : "Apply AI Fix"}
                        </Button>
                        <span className="text-[10px] text-green-700">
                          {hasMustContain ? "Filter will be safety-checked before saving." : "Review the changes above, then approve to update scrape rules."}
                        </span>
                      </div>
                    )}
                  </div>
                );
              })()}

              {!hasSuggestions && diagnoseResult.already_applied && (
                <div className="border border-blue-200 rounded-lg p-3 bg-blue-50 flex items-start gap-2">
                  <CheckCheck className="w-4 h-4 text-blue-500 mt-0.5 shrink-0" />
                  <div>
                    <p className="text-xs font-semibold text-blue-800">Config already applied</p>
                    <p className="text-xs text-blue-700 mt-0.5">
                      The suggested fixes are already saved. Run a new scrape to see if the results improve.
                    </p>
                  </div>
                </div>
              )}

              {!hasSuggestions && !diagnoseResult.already_applied && diagnoseResult.ok && (
                <p className="text-xs text-gray-400 text-center py-2">AI found no config changes to suggest for this issue.</p>
              )}

              {/* Re-run + Rollback */}
              <div className="flex items-center gap-2 pt-2 border-t flex-wrap">
                <button
                  type="button"
                  onClick={runDiagnosis}
                  disabled={diagnosing}
                  className="text-xs text-blue-500 hover:text-blue-700 flex items-center gap-1"
                >
                  <RefreshCw className="w-3 h-3" /> Re-run diagnosis
                </button>
                {config?.has_rollback && (
                  <button
                    type="button"
                    onClick={rollbackFix}
                    disabled={rollingBack}
                    className="text-xs text-amber-600 hover:text-amber-800 flex items-center gap-1 border border-amber-300 rounded px-2 py-0.5 bg-amber-50 hover:bg-amber-100"
                  >
                    {rollingBack ? <Loader2 className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />}
                    Revert Last AI Fix
                  </button>
                )}
                {config?.latest_job_id && (
                  <a
                    href={`/scraping`}
                    className="text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1 ml-auto"
                  >
                    <ExternalLink className="w-3 h-3" /> View in Scraping Jobs
                  </a>
                )}
              </div>
            </div>
          );
        })()}
      </div>

      {/* ── Extraction Quality ────────────────────────────────────────────── */}
      <div className="bg-white border rounded-xl p-4 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
            <FlaskConical className="w-4 h-4 text-purple-600" /> Extraction Quality
            <span className="text-[10px] font-normal text-gray-400 ml-1">Per-field fill rates · defect detection · no AI cost</span>
          </h2>
          <div className="flex items-center gap-2">
            {extractionResult && (
              <button
                type="button"
                onClick={runExtractionQuality}
                disabled={checkingExtraction}
                className="text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1"
              >
                <RefreshCw className="w-3 h-3" /> Re-check
              </button>
            )}
            <Button
              size="sm"
              onClick={runExtractionQuality}
              disabled={checkingExtraction || !config?.latest_job_id}
              className="h-7 text-xs gap-1.5 bg-purple-600 hover:bg-purple-700"
            >
              {checkingExtraction ? <Loader2 className="w-3 h-3 animate-spin" /> : <BarChart3 className="w-3 h-3" />}
              {checkingExtraction ? "Checking…" : "Check Extraction Quality"}
            </Button>
          </div>
        </div>

        {!extractionResult && !checkingExtraction && (
          <div className="text-center py-8 text-gray-400 text-sm">
            <FlaskConical className="w-8 h-8 mx-auto mb-2 opacity-30" />
            Click <strong>Check Extraction Quality</strong> to scan all staged courses for data defects — university name in title, missing IELTS, nav text as location, suspicious fees, and more.
          </div>
        )}

        {checkingExtraction && (
          <div className="flex items-center justify-center gap-2 py-8 text-purple-500 text-sm">
            <Loader2 className="w-5 h-5 animate-spin" />
            Scanning all staged courses for extraction defects…
          </div>
        )}

        {extractionResult && !checkingExtraction && (() => {
          const eq = extractionResult;
          const critCount = eq.issues.filter(i => i.severity === "critical").length;
          const highCount = eq.issues.filter(i => i.severity === "high").length;
          return (
            <div className="space-y-5">
              {eq.error && (
                <div className="p-3 bg-red-50 rounded-lg text-sm text-red-600 border border-red-200">{eq.error}</div>
              )}

              {eq.message && (
                <div className="p-3 bg-gray-50 rounded-lg text-sm text-gray-500">{eq.message}</div>
              )}

              {eq.course_count > 0 && (
                <>
                  {/* Score + summary row */}
                  <div className="flex items-start gap-5 flex-wrap">
                    <ExtractionScoreRing score={eq.extraction_score} />
                    <div className="flex-1 min-w-0 space-y-1.5 pt-1">
                      <div className="text-xs text-gray-500">
                        Scanned <strong className="text-gray-700">{eq.course_count}</strong> staged courses
                        &nbsp;·&nbsp;avg completeness <strong className="text-gray-700">{eq.avg_completeness_pct}%</strong>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {critCount > 0 && (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">
                            {critCount} critical issue{critCount > 1 ? "s" : ""}
                          </span>
                        )}
                        {highCount > 0 && (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-orange-100 text-orange-700 font-medium">
                            {highCount} high issue{highCount > 1 ? "s" : ""}
                          </span>
                        )}
                        {eq.issues.length === 0 && (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-green-100 text-green-700 font-medium">
                            No defects detected
                          </span>
                        )}
                      </div>
                      <ExtractionQualityLabel score={eq.extraction_score} />
                    </div>
                  </div>

                  {/* Field fill rates */}
                  <div>
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                      <BarChart3 className="w-3.5 h-3.5" /> Field Fill Rates
                    </p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1.5">
                      {Object.entries(eq.field_fill_rates).map(([field, pct]) => (
                        <FillRateBar
                          key={field}
                          label={eq.field_labels[field] || field}
                          pct={pct}
                          hasIssue={eq.issues.some(i => i.field === field)}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Issues */}
                  {eq.issues.length > 0 && (
                    <div>
                      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                        <AlertTriangle className="w-3.5 h-3.5" /> Detected Defects
                      </p>
                      <div className="space-y-2">
                        {eq.issues.map((issue, i) => (
                          <ExtractionIssueCard key={i} issue={issue} onApplyFix={applyRecipeFix} />
                        ))}
                      </div>
                    </div>
                  )}

                  {eq.issues.length === 0 && (
                    <div className="flex items-start gap-2 p-3 bg-green-50 border border-green-200 rounded-lg">
                      <CheckCircle2 className="w-4 h-4 text-green-500 mt-0.5 shrink-0" />
                      <div>
                        <p className="text-xs font-semibold text-green-800">No extraction defects found</p>
                        <p className="text-xs text-green-700 mt-0.5">
                          All checked patterns are clean. If completeness is still low, run the AI Discovery Diagnosis above to check for discovery-level issues.
                        </p>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })()}
      </div>

      {/* ── After fix: run scrape prompt ─────────────────────────────────── */}
      {/* ── Post-Scrape Comparison ────────────────────────────────────────── */}
      <ScrapeComparisonPanel uniId={uniId} />

      {/* ── Recipe Coverage Audit ─────────────────────────────────────────── */}
      <RecipeCoveragePanel />

      {/* ── HTML Snapshot Replay ──────────────────────────────────────────── */}
      {config?.latest_job_id && (
        <SnapshotReplayPanel jobId={config.latest_job_id} />
      )}

      {appliedConfig && (
        <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 flex items-center gap-3">
          <CheckCircle2 className="w-5 h-5 text-blue-600 shrink-0" />
          <div>
            <p className="text-sm font-semibold text-blue-800">Config applied — ready to re-scrape</p>
            <p className="text-xs text-blue-600">Go to the Scraping page and run a new scrape for {config?.university_name} to see the results.</p>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => navigate("/scraping")}
            className="ml-auto shrink-0 border-blue-300 text-blue-700 hover:bg-blue-100"
          >
            Go to Scraping
          </Button>
        </div>
      )}
    </div>
  );
}

// ── Operator confidence components ─────────────────────────────────────────────

function CertificationScoreCard({ uniId }: { uniId: number }) {
  const [score, setScore] = useState<CertificationScore | null>(null);

  useEffect(() => {
    fetch(`${BASE}/api/scrape/${uniId}/certification-score`)
      .then((r) => r.json())
      .then(setScore)
      .catch(() => {});
  }, [uniId]);

  if (!score?.available) return null;

  const levelCfg = {
    certified: { color: "text-emerald-700", bg: "bg-emerald-50 border-emerald-200", label: "Certified" },
    good:       { color: "text-blue-700",    bg: "bg-blue-50 border-blue-200",       label: "Good"      },
    needs_work: { color: "text-amber-700",   bg: "bg-amber-50 border-amber-200",     label: "Needs Work"},
    poor:       { color: "text-red-700",     bg: "bg-red-50 border-red-200",         label: "Poor"      },
  };
  const lc = levelCfg[score.cert_level] ?? levelCfg.needs_work;

  const ScoreBar = ({ val }: { val: number }) => (
    <div className="flex-1 bg-gray-200 rounded-full h-1.5 overflow-hidden">
      <div
        className={`h-full rounded-full ${val >= 85 ? "bg-emerald-500" : val >= 70 ? "bg-blue-500" : val >= 50 ? "bg-amber-500" : "bg-red-500"}`}
        style={{ width: `${val}%` }}
      />
    </div>
  );

  const started = score.last_scrape.started_at
    ? new Date(score.last_scrape.started_at).toLocaleDateString()
    : "—";

  return (
    <div className={`border rounded-xl p-4 space-y-3 ${lc.bg}`}>
      <div className="flex items-center gap-3 flex-wrap">
        <Award className={`w-4 h-4 ${lc.color}`} />
        <span className={`text-sm font-bold ${lc.color}`}>{lc.label}</span>
        <span className={`text-xl font-bold ${lc.color}`}>{score.overall_score}%</span>
        <span className="text-[10px] text-gray-400 ml-auto">
          Last scrape: {score.last_scrape.staged} courses · {started}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {(Object.values(score.dimensions) as CertDimension[]).map((dim) => (
          <div key={dim.label} className="space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-gray-500">{dim.label}</span>
              <span className="text-[10px] font-bold text-gray-700">{dim.score}%</span>
            </div>
            <ScoreBar val={dim.score} />
            <p className="text-[9px] text-gray-400 leading-tight">{dim.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function ScrapeComparisonPanel({ uniId }: { uniId: number }) {
  const [data, setData] = useState<ScrapeComparison | null>(null);

  useEffect(() => {
    fetch(`${BASE}/api/scrape/${uniId}/scrape-comparison`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => {});
  }, [uniId]);

  if (!data?.available) return null;
  const hasChanges = data.field_deltas.some((f) => f.delta !== 0) || data.staged_delta !== 0;
  if (!hasChanges) return null;

  const DeltaChip = ({ delta }: { delta: number }) => (
    <span
      className={`text-[10px] font-bold px-1.5 rounded shrink-0 ${
        delta > 0 ? "text-emerald-700 bg-emerald-100" : delta < 0 ? "text-red-700 bg-red-100" : "text-gray-400"
      }`}
    >
      {delta > 0 ? `+${delta}` : delta === 0 ? "—" : delta}pp
    </span>
  );

  const visibleFields = data.field_deltas.filter((f) => f.before > 0 || f.after > 0);

  const prevDate = data.previous.started_at ? new Date(data.previous.started_at).toLocaleDateString() : "—";
  const curDate = data.current.started_at ? new Date(data.current.started_at).toLocaleDateString() : "—";

  return (
    <div className="bg-white border rounded-xl p-4 space-y-3">
      <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
        <ArrowLeftRight className="w-4 h-4 text-violet-600" /> Post-Scrape Comparison
        <span className="text-[10px] font-normal text-gray-400">Before fix vs after fix · field fill rates</span>
      </h2>

      {/* Staged courses delta */}
      <div className="grid grid-cols-3 gap-2 text-center text-xs">
        <div className="bg-gray-50 border rounded-lg p-2.5">
          <div className="text-lg font-bold text-gray-600">{data.previous.staged}</div>
          <div className="text-[9px] text-gray-400 mt-0.5">Before · {prevDate}</div>
          <div className="text-[9px] text-gray-500">Courses staged</div>
        </div>
        <div className="flex items-center justify-center">
          <ArrowLeftRight className="w-4 h-4 text-gray-300" />
        </div>
        <div
          className={`border rounded-lg p-2.5 ${
            data.staged_delta > 0 ? "bg-emerald-50 border-emerald-200" : data.staged_delta < 0 ? "bg-red-50 border-red-200" : "bg-gray-50"
          }`}
        >
          <div className={`text-lg font-bold ${data.staged_delta > 0 ? "text-emerald-700" : data.staged_delta < 0 ? "text-red-700" : "text-gray-600"}`}>
            {data.current.staged}
          </div>
          <div className="text-[9px] text-gray-400 mt-0.5">After · {curDate}</div>
          <DeltaChip delta={data.staged_delta} />
        </div>
      </div>

      {/* Per-field fill-rate bars */}
      {visibleFields.length > 0 && (
        <div className="space-y-1.5">
          {visibleFields.map((f) => (
            <div key={f.field} className="flex items-center gap-2">
              <span className="w-24 text-[10px] text-gray-500 shrink-0">{f.label}</span>
              <span className="w-7 text-right text-[10px] text-gray-400 shrink-0">{f.before}%</span>
              <div className="flex-1 bg-gray-100 rounded-full h-2 relative overflow-hidden">
                <div className="absolute inset-y-0 left-0 bg-gray-300 rounded-full" style={{ width: `${f.before}%` }} />
                {f.after > f.before && (
                  <div className="absolute inset-y-0 rounded-full bg-emerald-400" style={{ left: `${f.before}%`, width: `${f.after - f.before}%` }} />
                )}
                {f.after < f.before && (
                  <div className="absolute inset-y-0 rounded-full bg-red-400" style={{ left: `${f.after}%`, width: `${f.before - f.after}%` }} />
                )}
              </div>
              <span className={`w-7 text-[10px] font-medium shrink-0 ${f.after >= f.before ? "text-emerald-700" : "text-red-600"}`}>{f.after}%</span>
              <DeltaChip delta={f.delta} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function RecipeCoveragePanel() {
  const [data, setData] = useState<RecipeCoverage | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const toggle = async () => {
    if (data) { setOpen((v) => !v); return; }
    setLoading(true);
    try {
      const r = await fetch(`${BASE}/api/scrape/recipe-coverage`);
      setData(await r.json());
      setOpen(true);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-white border rounded-xl p-4 space-y-3">
      <button type="button" className="w-full flex items-center justify-between group" onClick={toggle}>
        <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <ListChecks className="w-4 h-4 text-indigo-600" />
          Recipe Coverage Audit
          {data && (
            <span className={`text-[10px] font-medium px-2 py-0.5 rounded-full ${data.coverage_pct >= 80 ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>
              {data.covered}/{data.total} covered
            </span>
          )}
          {!data && <span className="text-[10px] font-normal text-gray-400">Can every detected problem be fixed from Recipe Editor?</span>}
        </h2>
        {loading
          ? <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400" />
          : open
            ? <ChevronUp className="w-4 h-4 text-gray-400" />
            : <ChevronDown className="w-4 h-4 text-gray-400" />
        }
      </button>

      {data && open && (
        <div className="space-y-4">
          {/* Summary row */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-sm font-bold text-gray-800">Covered: {data.covered} / {data.total} issues</span>
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${data.coverage_pct >= 80 ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"}`}>
              {data.coverage_pct}% recipe-fixable
            </span>
            {data.missing_count > 0 && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-medium">
                {data.missing_count} missing recipe fix
              </span>
            )}
          </div>

          {/* Per-category breakdown */}
          <div className="space-y-3">
            {data.categories.map((cat) => (
              <div key={cat.id} className="space-y-1">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">{cat.label}</span>
                  <span className="text-[10px] text-gray-400">{cat.covered}/{cat.total}</span>
                </div>
                {cat.items.map((item) => (
                  <div
                    key={item.id}
                    className={`flex items-start gap-2 px-2 py-1.5 rounded-lg text-xs ${
                      item.has_recipe_patch
                        ? "bg-emerald-50 border border-emerald-100"
                        : "bg-red-50 border border-red-200"
                    }`}
                  >
                    <span className={`text-[11px] font-bold mt-0.5 shrink-0 ${item.has_recipe_patch ? "text-emerald-600" : "text-red-500"}`}>
                      {item.has_recipe_patch ? "✓" : "✗"}
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="font-medium text-gray-700">{item.title}</span>
                      {item.recipe_keys.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-0.5">
                          {item.recipe_keys.map((k) => (
                            <span key={k} className="text-[9px] font-mono bg-white border border-emerald-200 text-emerald-700 rounded px-1 py-0.5">
                              {k}
                            </span>
                          ))}
                        </div>
                      )}
                      {!item.has_recipe_patch && (
                        <p className="text-[10px] text-red-600 mt-0.5">
                          {item.fix_type === "platform_bug" ? "Requires developer fix" : "Config-level fix only (YAML)"}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>

          {/* Missing list summary */}
          {data.missing.length > 0 && (
            <div className="border border-red-200 rounded-lg p-3 bg-red-50 space-y-2">
              <p className="text-xs font-semibold text-red-800">Missing recipe fix ({data.missing.length}):</p>
              {data.missing.map((m) => (
                <div key={m.id} className="flex items-start gap-2">
                  <span className="text-[9px] font-mono bg-white border border-red-200 rounded px-1 py-0.5 text-red-600 shrink-0 mt-0.5">
                    {m.id}
                  </span>
                  <span className="text-xs text-red-700">{m.title}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Small reusable helpers ─────────────────────────────────────────────────────

function StatCard({ icon, label, value, sub, color }: {
  icon: React.ReactNode; label: string; value: string | number; sub: string; color?: string;
}) {
  return (
    <div className="bg-gray-50 rounded-lg p-2.5 border min-w-0">
      <div className={`flex items-center gap-1.5 ${color || "text-gray-600"} mb-0.5`}>
        {icon}
        <span className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">{label}</span>
      </div>
      <div className={`text-xl font-bold leading-none ${color || "text-gray-800"}`}>{value}</div>
      <div className="text-[9px] text-gray-400 mt-0.5">{sub}</div>
    </div>
  );
}

function FieldRow({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <Label className="text-xs font-semibold text-gray-700">{label}</Label>
      {hint && <p className="text-[10px] text-gray-400 leading-tight">{hint}</p>}
      {children}
    </div>
  );
}

// ── Recipe Section accordion helper ──────────────────────────────────────────
function RecipeSection({ title, icon, children, active }: {
  title: string; icon: React.ReactNode; children: React.ReactNode; active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`border rounded-lg overflow-hidden ${active ? "border-teal-400" : "border-gray-200"}`}>
      <button
        type="button"
        className={`w-full flex items-center gap-2 px-3 py-2 text-xs font-semibold text-left ${open ? "bg-teal-50" : "bg-gray-50 hover:bg-gray-100"}`}
        onClick={() => setOpen(o => !o)}
      >
        {icon}
        <span className="flex-1">{title}</span>
        {active && <span className="text-[9px] font-medium text-teal-600 bg-teal-100 border border-teal-300 px-1.5 py-0.5 rounded-full">active</span>}
        <span className="text-gray-400 text-[10px]">{open ? "▲" : "▼"}</span>
      </button>
      {open && <div className="px-3 py-3 space-y-3 bg-white">{children}</div>}
    </div>
  );
}

// ── RecipeEditor component ─────────────────────────────────────────────────────
type IeltsRow = { overall: string; band: string };
type ReplaceRow = { from: string; to: string };

type DegreeMappingRow = { level: string; keywords: string[] };

function RecipeEditor(props: {
  feeSourceUrls: string[]; setFeeSourceUrls: (v: string[]) => void;
  feeTerm: string; setFeeTerm: (v: string) => void;
  feeCalcMode: string; setFeeCalcMode: (v: string) => void;
  feePreventRollup: boolean; setFeePreventRollup: (v: boolean) => void;
  ieltsMapping: IeltsRow[]; setIeltsMapping: (v: IeltsRow[]) => void;
  nameRemoveAfter: string[]; setNameRemoveAfter: (v: string[]) => void;
  nameRemoveYear: boolean; setNameRemoveYear: (v: boolean) => void;
  locAllowed: string[]; setLocAllowed: (v: string[]) => void;
  locReject: string[]; setLocReject: (v: string[]) => void;
  locReplace: ReplaceRow[]; setLocReplace: (v: ReplaceRow[]) => void;
  modeFromLoc: boolean; setModeFromLoc: (v: boolean) => void;
  modeOnlineKws: string[]; setModeOnlineKws: (v: string[]) => void;
  degreeMapping: DegreeMappingRow[]; setDegreeMapping: (v: DegreeMappingRow[]) => void;
  followLinksFee: string[]; setFollowLinksFee: (v: string[]) => void;
  followLinksEnglish: string[]; setFollowLinksEnglish: (v: string[]) => void;
  saving: boolean; onSave: () => void;
  onSimulate: () => void; simulating: boolean;
}) {
  const {
    feeSourceUrls, setFeeSourceUrls, feeTerm, setFeeTerm,
    feeCalcMode, setFeeCalcMode, feePreventRollup, setFeePreventRollup,
    ieltsMapping, setIeltsMapping,
    nameRemoveAfter, setNameRemoveAfter, nameRemoveYear, setNameRemoveYear,
    locAllowed, setLocAllowed, locReject, setLocReject,
    locReplace, setLocReplace, modeFromLoc, setModeFromLoc,
    modeOnlineKws, setModeOnlineKws,
    degreeMapping, setDegreeMapping,
    followLinksFee, setFollowLinksFee, followLinksEnglish, setFollowLinksEnglish,
    saving, onSave, onSimulate, simulating,
  } = props;

  const hasFee = feeSourceUrls.length > 0 || feeTerm !== "" || feeCalcMode !== "use_source_value_only" || !feePreventRollup;
  const hasIelts = ieltsMapping.length > 0;
  const hasName = nameRemoveAfter.length > 0 || nameRemoveYear;
  const hasLoc = locAllowed.length > 0 || locReject.length > 0 || locReplace.length > 0;
  const hasMode = modeFromLoc;
  const hasDegree = degreeMapping.length > 0;
  const hasLinks = followLinksFee.length > 0 || followLinksEnglish.length > 0;
  const anyActive = hasFee || hasIelts || hasName || hasLoc || hasMode || hasDegree || hasLinks;

  const updateIelts = (i: number, field: "overall" | "band", val: string) => {
    const next = [...ieltsMapping];
    next[i] = { ...next[i], [field]: val };
    setIeltsMapping(next);
  };
  const removeIelts = (i: number) => setIeltsMapping(ieltsMapping.filter((_, idx) => idx !== i));
  const addIelts = () => setIeltsMapping([...ieltsMapping, { overall: "", band: "" }]);

  const updateReplace = (i: number, field: "from" | "to", val: string) => {
    const next = [...locReplace];
    next[i] = { ...next[i], [field]: val };
    setLocReplace(next);
  };
  const removeReplace = (i: number) => setLocReplace(locReplace.filter((_, idx) => idx !== i));
  const addReplace = () => setLocReplace([...locReplace, { from: "", to: "" }]);

  return (
    <div className="bg-white border rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-700 flex items-center gap-2">
          <FlaskConical className="w-4 h-4 text-teal-600" />
          Data Cleaning Recipe
          {anyActive && (
            <Badge variant="outline" className="text-[9px] px-1.5 border-teal-400 text-teal-700 bg-teal-50">
              {[hasFee && "fee", hasIelts && "IELTS", hasName && "name", hasLoc && "location", hasMode && "mode", hasDegree && "degree", hasLinks && "links"]
                .filter(Boolean).join(" · ")} rules active
            </Badge>
          )}
        </h2>
        <Button onClick={onSave} disabled={saving} size="sm" variant="outline" className="h-7 text-xs gap-1 border-teal-400 text-teal-700 hover:bg-teal-50">
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          Save Recipe
        </Button>
      </div>

      <p className="text-[11px] text-gray-500 leading-relaxed">
        Rules applied to every extracted course <strong>before</strong> it is staged — no code required.
        Changes take effect on the next scrape run. Open a section to configure it.
      </p>

      <div className="space-y-2">
        {/* ── Fee Rules ── */}
        <RecipeSection title="Fee Rules" icon={<span className="text-[13px]">💰</span>} active={hasFee}>
          <FieldRow label="Fee Schedule Page URL(s)"
            hint="The university's international fee list page. The scraper reads fees from here instead of each course page.">
            <Textarea
              value={feeSourceUrls.join("\n")}
              onChange={e => setFeeSourceUrls(e.target.value.split("\n").map(s => s.trim()).filter(Boolean))}
              placeholder="https://www.uni.edu.au/fees/international-students"
              className="text-xs mt-1 min-h-[56px] font-mono"
              rows={2}
            />
          </FieldRow>
          <FieldRow label="Fee Term Override"
            hint="Force a specific term label for all fees at this university. Leave blank to auto-detect.">
            <select
              value={feeTerm}
              onChange={e => setFeeTerm(e.target.value)}
              className="mt-1 h-8 text-xs border border-gray-200 rounded px-2 w-full bg-white"
            >
              <option value="">Auto-detect (default)</option>
              <option value="Annual">Annual</option>
              <option value="Per Unit">Per Unit</option>
              <option value="Full Course">Full Course</option>
            </select>
          </FieldRow>
          <FieldRow label="Fee Calculation Mode"
            hint="'Use source value only' prevents the Full Course rollup bug (multiplying annual × duration).">
            <select
              value={feeCalcMode}
              onChange={e => setFeeCalcMode(e.target.value)}
              className="mt-1 h-8 text-xs border border-gray-200 rounded px-2 w-full bg-white"
            >
              <option value="use_source_value_only">Use source value only (default — no conversion)</option>
              <option value="full_course_to_annual">Full Course ÷ duration = Annual equivalent</option>
              <option value="per_unit_to_annual">Per Unit × 8 credit points = Annual estimate</option>
            </select>
          </FieldRow>
          <div className="flex items-center gap-2">
            <Switch
              id="fee-prevent-rollup"
              checked={feePreventRollup}
              onCheckedChange={setFeePreventRollup}
            />
            <Label htmlFor="fee-prevent-rollup" className="text-xs text-gray-700 cursor-pointer">
              Prevent Full Course rollup (mark as Annual if scraper returns Full Course term)
            </Label>
          </div>
        </RecipeSection>

        {/* ── IELTS Component Mapping ── */}
        <RecipeSection title="IELTS Component Mapping" icon={<span className="text-[13px]">📝</span>} active={hasIelts}>
          <p className="text-[11px] text-gray-500">
            Maps IELTS overall score → minimum each-band score (Reading/Writing/Listening/Speaking).
            Applied when the course page shows only an overall band but no per-component scores.
          </p>
          <div className="space-y-1.5">
            <div className="grid grid-cols-[1fr_1fr_auto] gap-1.5 text-[10px] font-semibold text-gray-500 px-0.5">
              <span>Overall (e.g. 6.0)</span>
              <span>Each Band (e.g. 5.5)</span>
              <span />
            </div>
            {ieltsMapping.map((row, i) => (
              <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-1.5 items-center">
                <Input
                  value={row.overall}
                  onChange={e => updateIelts(i, "overall", e.target.value)}
                  placeholder="6.0"
                  className="h-7 text-xs"
                />
                <Input
                  value={row.band}
                  onChange={e => updateIelts(i, "band", e.target.value)}
                  placeholder="5.5"
                  className="h-7 text-xs"
                />
                <button type="button" onClick={() => removeIelts(i)}
                  className="p-1 rounded hover:bg-red-50 text-gray-400 hover:text-red-500">
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
            <Button type="button" size="sm" variant="outline" onClick={addIelts}
              className="h-7 text-xs gap-1">
              <Plus className="w-3 h-3" /> Add row
            </Button>
          </div>
        </RecipeSection>

        {/* ── Course Name Cleanup ── */}
        <RecipeSection title="Course Name Cleanup" icon={<span className="text-[13px]">✏️</span>} active={hasName}>
          <FieldRow label="Remove everything after…"
            hint="Strip the course name from the first occurrence of any of these strings. Useful for removing site name suffixes like '| University Name'.">
            <div className="mt-1">
              <TagInput
                values={nameRemoveAfter}
                onChange={setNameRemoveAfter}
                placeholder='e.g. | Southern Cross University'
              />
            </div>
          </FieldRow>
          <div className="flex items-center gap-2 mt-1">
            <Switch
              id="name-remove-year"
              checked={nameRemoveYear}
              onCheckedChange={setNameRemoveYear}
            />
            <Label htmlFor="name-remove-year" className="text-xs text-gray-700 cursor-pointer">
              Remove trailing year suffix (e.g. "Master of Science 2025" → "Master of Science")
            </Label>
          </div>
        </RecipeSection>

        {/* ── Location Cleanup ── */}
        <RecipeSection title="Location Cleanup" icon={<span className="text-[13px]">📍</span>} active={hasLoc}>
          <FieldRow label="Only keep these campus values"
            hint="Allowlist — if non-empty, only location strings containing one of these entries are stored (case-insensitive). Others are cleared.">
            <div className="mt-1">
              <TagInput values={locAllowed} onChange={setLocAllowed} placeholder="e.g. Gold Coast" />
            </div>
          </FieldRow>
          <FieldRow label="Reject if location contains…"
            hint="If any of these strings appears in the extracted location, the location is cleared entirely. Use to remove nav text contamination.">
            <div className="mt-1">
              <TagInput values={locReject} onChange={setLocReject} placeholder="e.g. How to Apply" />
            </div>
          </FieldRow>
          <FieldRow label="Replace text in location"
            hint="String replacements applied before filtering. Useful for normalising abbreviations.">
            <div className="mt-1.5 space-y-1.5">
              {locReplace.length > 0 && (
                <div className="grid grid-cols-[1fr_auto_1fr_auto] gap-1 text-[10px] font-semibold text-gray-500 px-0.5">
                  <span>Find</span><span></span><span>Replace with</span><span />
                </div>
              )}
              {locReplace.map((row, i) => (
                <div key={i} className="grid grid-cols-[1fr_auto_1fr_auto] gap-1 items-center">
                  <Input value={row.from} onChange={e => updateReplace(i, "from", e.target.value)}
                    placeholder="SCU Online" className="h-7 text-xs" />
                  <span className="text-[10px] text-gray-400 px-1">→</span>
                  <Input value={row.to} onChange={e => updateReplace(i, "to", e.target.value)}
                    placeholder="Online" className="h-7 text-xs" />
                  <button type="button" onClick={() => removeReplace(i)}
                    className="p-1 rounded hover:bg-red-50 text-gray-400 hover:text-red-500">
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
              <Button type="button" size="sm" variant="outline" onClick={addReplace}
                className="h-7 text-xs gap-1">
                <Plus className="w-3 h-3" /> Add replacement
              </Button>
            </div>
          </FieldRow>
        </RecipeSection>

        {/* ── Study Mode Rules ── */}
        <RecipeSection title="Study Mode Rules" icon={<span className="text-[13px]">🎓</span>} active={hasMode}>
          <div className="flex items-center gap-2">
            <Switch
              id="mode-from-loc"
              checked={modeFromLoc}
              onCheckedChange={setModeFromLoc}
            />
            <Label htmlFor="mode-from-loc" className="text-xs text-gray-700 cursor-pointer">
              Derive Study Mode from Location (Online/Blended/On Campus) when study mode is blank
            </Label>
          </div>
          {modeFromLoc && (
            <FieldRow label="Online keywords"
              hint="Keywords in the location string that indicate online delivery. Default: online, distance, virtual.">
              <div className="mt-1">
                <TagInput values={modeOnlineKws} onChange={setModeOnlineKws} placeholder="e.g. online" />
              </div>
            </FieldRow>
          )}
        </RecipeSection>

        {/* ── Degree Level Mapping ── */}
        <RecipeSection title="Degree Level Mapping" icon={<span className="text-[13px]">🎓</span>} active={hasDegree}>
          <p className="text-[11px] text-gray-500">
            Map unusual or abbreviated degree names to a canonical level. The first matching row wins.
          </p>
          <div className="space-y-2">
            {degreeMapping.map((row, i) => (
              <div key={i} className="border rounded-lg p-2.5 space-y-1.5 bg-gray-50">
                <div className="flex items-center gap-2">
                  <select
                    value={row.level}
                    onChange={e => {
                      const next = [...degreeMapping];
                      next[i] = { ...next[i], level: e.target.value };
                      setDegreeMapping(next);
                    }}
                    className="h-7 text-xs border border-gray-200 rounded px-2 bg-white flex-1"
                  >
                    <option value="">— select canonical level —</option>
                    {["Bachelor", "Master", "Doctorate", "Associate Degree", "Graduate Certificate", "Graduate Diploma", "Certificate", "Diploma", "Other"].map(l => (
                      <option key={l} value={l}>{l}</option>
                    ))}
                  </select>
                  <button type="button" onClick={() => setDegreeMapping(degreeMapping.filter((_, idx) => idx !== i))}
                    className="p-1 rounded hover:bg-red-50 text-gray-400 hover:text-red-500">
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
                <div>
                  <p className="text-[10px] text-gray-500 mb-1">Matches if the extracted degree name contains any of these keywords (case-insensitive):</p>
                  <TagInput
                    values={row.keywords}
                    onChange={kws => {
                      const next = [...degreeMapping];
                      next[i] = { ...next[i], keywords: kws };
                      setDegreeMapping(next);
                    }}
                    placeholder="e.g. BSc, BA, BBus"
                  />
                </div>
              </div>
            ))}
            <Button type="button" size="sm" variant="outline" onClick={() => setDegreeMapping([...degreeMapping, { level: "", keywords: [] }])}
              className="h-7 text-xs gap-1">
              <Plus className="w-3 h-3" /> Add mapping
            </Button>
          </div>
        </RecipeSection>

        {/* ── Link Following Rules ── */}
        <RecipeSection title="Link Following Rules" icon={<span className="text-[13px]">🔗</span>} active={hasLinks}>
          <p className="text-[11px] text-gray-500">
            When the scraper finds these link texts on a page, it follows those links to extract fees or English requirements.
            Useful when fee tables or IELTS requirements live on a separate linked page.
          </p>
          <FieldRow label="Fee page link texts"
            hint="Follow links with these labels to reach the international fee page. e.g. 'International Fees', 'Tuition Fees'.">
            <div className="mt-1">
              <TagInput values={followLinksFee} onChange={setFollowLinksFee} placeholder="e.g. International Fees" />
            </div>
          </FieldRow>
          <FieldRow label="English requirements link texts"
            hint="Follow links with these labels to reach English entry requirements. e.g. 'English Requirements', 'Entry Requirements'.">
            <div className="mt-1">
              <TagInput values={followLinksEnglish} onChange={setFollowLinksEnglish} placeholder="e.g. English Requirements" />
            </div>
          </FieldRow>
        </RecipeSection>
      </div>

      <div className="flex items-center gap-2 pt-1 border-t flex-wrap">
        <Button onClick={onSave} disabled={saving || simulating} className="gap-1.5 h-8 text-xs bg-teal-600 hover:bg-teal-700">
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          Save Recipe
        </Button>
        <Button onClick={onSimulate} disabled={saving || simulating} variant="outline" className="gap-1.5 h-8 text-xs border-violet-400 text-violet-700 hover:bg-violet-50">
          {simulating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <span className="text-sm">🧪</span>}
          Preview Changes
        </Button>
        <p className="text-[10px] text-gray-400">Preview shows before/after on real staged data · Save to apply on next scrape</p>
      </div>
    </div>
  );
}

function Stat({ label, value, good, bad, warn }: {
  label: string; value: string | number; good?: boolean; bad?: boolean; warn?: boolean;
}) {
  const cls = bad ? "text-red-600 font-bold" : good ? "text-green-700 font-bold" : warn ? "text-amber-600" : "text-gray-700";
  return (
    <span>
      {label}: <span className={cls}>{value}</span>
    </span>
  );
}

function HealthLabel({ score }: { score: number }) {
  if (score >= 75) return <p className="text-xs text-green-700 font-medium flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5" /> Good — scraping is working well</p>;
  if (score >= 50) return <p className="text-xs text-amber-700 font-medium flex items-center gap-1.5"><AlertTriangle className="w-3.5 h-3.5" /> Fair — some issues detected, review the diagnosis</p>;
  return <p className="text-xs text-red-700 font-medium flex items-center gap-1.5"><ShieldAlert className="w-3.5 h-3.5" /> Poor — significant scraping problems, use AI diagnosis to fix</p>;
}

// ── Extraction Quality helpers ─────────────────────────────────────────────────

function ExtractionScoreRing({ score }: { score: number }) {
  const r = 36;
  const circ = 2 * Math.PI * r;
  const dash = (Math.min(score, 100) / 100) * circ;
  const color = score >= 75 ? "#7c3aed" : score >= 50 ? "#d97706" : "#dc2626";
  return (
    <div className="relative flex items-center justify-center w-24 h-24 shrink-0">
      <svg width="96" height="96" viewBox="0 0 96 96" className="-rotate-90">
        <circle cx="48" cy="48" r={r} fill="none" stroke="#e5e7eb" strokeWidth="8" />
        <circle
          cx="48" cy="48" r={r} fill="none" stroke={color} strokeWidth="8"
          strokeDasharray={`${dash} ${circ - dash}`} strokeLinecap="round"
          style={{ transition: "stroke-dasharray 0.6s ease" }}
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="text-2xl font-bold leading-none" style={{ color }}>{score}</span>
        <span className="text-[9px] text-gray-400 font-medium">/ 100</span>
      </div>
    </div>
  );
}

function ExtractionQualityLabel({ score }: { score: number }) {
  if (score >= 75) return <p className="text-xs text-purple-700 font-medium flex items-center gap-1.5"><CheckCircle2 className="w-3.5 h-3.5" /> Good extraction quality</p>;
  if (score >= 50) return <p className="text-xs text-amber-700 font-medium flex items-center gap-1.5"><AlertTriangle className="w-3.5 h-3.5" /> Fair — some fields need attention</p>;
  return <p className="text-xs text-red-700 font-medium flex items-center gap-1.5"><ShieldAlert className="w-3.5 h-3.5" /> Poor — significant extraction defects detected</p>;
}

function FillRateBar({ label, pct, hasIssue }: { label: string; pct: number; hasIssue: boolean }) {
  const color =
    pct >= 90 ? "bg-green-500" :
    pct >= 70 ? "bg-amber-400" :
    pct >= 40 ? "bg-orange-400" : "bg-red-500";
  const textColor =
    pct >= 90 ? "text-green-700" :
    pct >= 70 ? "text-amber-700" :
    pct >= 40 ? "text-orange-600" : "text-red-600";
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-[11px] text-gray-600 w-32 shrink-0 truncate" title={label}>
        {hasIssue && <AlertTriangle className="w-2.5 h-2.5 text-amber-500 inline mr-0.5 -mt-0.5" />}
        {label}
      </span>
      <div className="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-[10px] font-semibold w-9 text-right shrink-0 ${textColor}`}>{pct}%</span>
    </div>
  );
}

function ExtractionIssueCard({
  issue,
  onApplyFix,
}: {
  issue: ExtractionIssue;
  onApplyFix?: (suggested: Record<string, unknown>) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [applying, setApplying] = useState(false);
  const isPlatformBug = issue.fix_type === "platform_bug";
  const isRecipeFix = issue.fix_type === "recipe_fix";

  const borderColor = isPlatformBug
    ? "border-slate-400 bg-slate-50"
    : isRecipeFix
    ? issue.severity === "critical" ? "border-teal-500 bg-teal-50"
      : issue.severity === "high" ? "border-teal-400 bg-teal-50"
      : "border-teal-300 bg-teal-50"
    : issue.severity === "critical" ? "border-red-500 bg-red-50"
    : issue.severity === "high" ? "border-orange-400 bg-orange-50"
    : "border-amber-300 bg-amber-50";

  const severityBadgeColor =
    issue.severity === "critical" ? "border-red-300 text-red-600" :
    issue.severity === "high" ? "border-orange-300 text-orange-600" :
    "border-amber-300 text-amber-600";

  const icon = isPlatformBug
    ? <Wrench className="w-3.5 h-3.5 text-slate-500 shrink-0 mt-0.5" />
    : isRecipeFix
    ? <FlaskConical className="w-3.5 h-3.5 text-teal-600 shrink-0 mt-0.5" />
    : issue.severity === "critical" ? <AlertTriangle className="w-3.5 h-3.5 text-red-500 shrink-0" />
    : issue.severity === "high" ? <AlertTriangle className="w-3.5 h-3.5 text-orange-500 shrink-0" />
    : <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />;

  const handleApply = async () => {
    if (!onApplyFix || !issue.suggested_recipe) return;
    setApplying(true);
    await onApplyFix(issue.suggested_recipe);
    setApplying(false);
  };

  return (
    <div className={`rounded-lg border-l-4 px-3 py-2.5 ${borderColor}`}>
      <div className="flex items-start gap-2">
        {icon}
        <div className="flex-1 min-w-0">
          {/* Header row */}
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-semibold text-gray-800">{issue.label}</span>
            {isPlatformBug ? (
              <Badge variant="outline" className="text-[9px] px-1.5 border-slate-400 text-slate-600 bg-slate-100">
                🔧 Rare edge case
              </Badge>
            ) : isRecipeFix ? (
              <Badge variant="outline" className="text-[9px] px-1.5 border-teal-400 text-teal-700 bg-teal-100">
                🧪 Recipe fix
              </Badge>
            ) : (
              <Badge variant="outline" className={`text-[9px] px-1.5 ${severityBadgeColor}`}>
                {issue.severity}
              </Badge>
            )}
            <span className="text-[10px] text-gray-500 ml-auto">
              {issue.count} course{issue.count !== 1 ? "s" : ""} ({issue.pct}%)
            </span>
          </div>

          {/* Detail */}
          <p className="text-xs text-gray-600 leading-relaxed mt-0.5">{issue.detail}</p>

          {/* Examples */}
          {issue.examples.length > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {issue.examples.map((ex, i) => (
                <p key={i} className="text-[10px] font-mono bg-white border border-gray-200 rounded px-1.5 py-0.5 text-gray-600 truncate" title={ex}>
                  {ex}
                </p>
              ))}
            </div>
          )}

          {/* Fix section */}
          {isPlatformBug ? (
            <div className="mt-2 flex items-start gap-1.5 p-2 bg-slate-100 border border-slate-200 rounded text-[11px] text-slate-700 leading-relaxed">
              <Wrench className="w-3 h-3 shrink-0 mt-0.5 text-slate-500" />
              <span>
                <strong className="text-slate-800">Uncommon extraction pattern.</strong>{" "}
                This issue was not resolved by standard Recipe Editor settings. Check whether a custom CSS/XPath field selector in the Recipe Editor can target this data, or contact support if the pattern is new.
              </span>
            </div>
          ) : isRecipeFix ? (
            <div className="mt-2 space-y-1.5">
              <button
                type="button"
                onClick={() => setExpanded(e => !e)}
                className="text-[10px] text-teal-600 hover:text-teal-800 flex items-center gap-0.5"
              >
                {expanded ? "Hide recipe fix ▲" : "Show recipe fix ▼"}
              </button>
              {expanded && (
                <div className="p-2 bg-white border border-teal-200 rounded text-[11px] text-gray-700 leading-relaxed">
                  <span className="font-semibold text-teal-700">Recipe Editor fix: </span>
                  {issue.suggested_fix}
                </div>
              )}
              {issue.suggested_recipe && onApplyFix && (
                <button
                  type="button"
                  onClick={handleApply}
                  disabled={applying}
                  className="flex items-center gap-1 text-[10px] font-medium text-white bg-teal-600 hover:bg-teal-700 disabled:opacity-50 rounded px-2 py-0.5"
                >
                  {applying
                    ? <><Loader2 className="w-3 h-3 animate-spin" /> Applying…</>
                    : <><Zap className="w-3 h-3" /> Apply Recipe Fix</>}
                </button>
              )}
            </div>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setExpanded(e => !e)}
                className="text-[10px] text-blue-500 hover:text-blue-700 mt-1.5 flex items-center gap-0.5"
              >
                {expanded ? "Hide fix ▲" : "Show configuration fix ▼"}
              </button>
              {expanded && (
                <div className="mt-1.5 p-2 bg-white border border-blue-100 rounded text-[11px] text-gray-700 leading-relaxed">
                  <span className="font-semibold text-blue-700">Configuration fix: </span>
                  {issue.suggested_fix}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Fix Preview Modal ──────────────────────────────────────────────────────────

function FixPreviewModal({
  rec,
  jobId,
  uniId,
  onClose,
  onApplied,
}: {
  rec: Phase3Rec;
  jobId: string;
  uniId: number;
  onClose: () => void;
  onApplied: () => void;
}) {
  const { toast } = useToast();
  const [preview, setPreview] = useState<FixPreviewResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validationResult, setValidationResult] = useState<DiscoveryTest | null>(null);
  const [showAllCourses, setShowAllCourses] = useState(false);
  const [launching, setLaunching] = useState(false);

  // Derive primary field from rec id
  const fieldMap: Record<string, string> = {
    missing_ielts_follow_link: "ielts_overall",
    missing_ielts_page_has_data: "ielts_overall",
    missing_ielts_no_link: "ielts_overall",
    band_mapping_not_applied: "ielts_overall",
    band_mapping_ielts_blank: "ielts_overall",
    ielts_components_missing: "ielts_overall",
    missing_fee_follow_link: "international_fee",
    missing_fee_page_has_text: "international_fee",
    missing_fee_tab: "international_fee",
    missing_fee_unknown: "international_fee",
    suspiciously_low_fee: "international_fee",
    fee_visible_not_extracted: "international_fee",
    csp_domestic_fee_detected: "international_fee",
    missing_degree_level: "degree_level",
    garbage_location: "course_location",
    course_name_pipe_suffix: "course_name",
    zero_discovery: "discovery",
    low_course_count: "discovery",
    study_mode_blended: "study_mode",
    all_filtered: "discovery",
    undergraduate_count_zero: "discovery",
    postgraduate_count_zero: "discovery",
  };
  const field = fieldMap[rec.id] || "";

  useEffect(() => {
    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/preview-fix`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            rec_id: rec.id,
            field,
            recipe_patch: rec.fix?.recipe_patch ?? {},
            confidence: rec.confidence,
            evidence: rec.evidence ?? {},
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err?.detail?.message ?? err?.detail ?? `HTTP ${res.status}`);
        }
        setPreview(await res.json());
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : "Failed to load preview");
      } finally {
        setLoading(false);
      }
    };
    run();
  }, [jobId, rec.id, field]);

  const handleApply = async () => {
    if (!rec.fix?.recipe_patch) return;
    setApplying(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config_patch: rec.fix.recipe_patch }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail?.message ?? err?.detail ?? `HTTP ${res.status}`);
      }
      toast({ title: "Fix applied", description: "Running discovery validation…" });
      onApplied();
      setApplied(true);
      setApplying(false);
      // Auto-run fast test discovery so the operator sees immediate validation
      setValidating(true);
      try {
        const vRes = await fetch(`${BASE}/api/universities/${uniId}/test-discovery?fast_only=true`, { method: "POST" });
        if (vRes.ok) setValidationResult(await vRes.json());
      } catch { /* silent — user can run manually */ } finally {
        setValidating(false);
      }
    } catch (e: unknown) {
      toast({
        title: "Apply failed",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "destructive",
      });
      setApplying(false);
    }
  };

  // ── Safe-to-scrape gate ──────────────────────────────────────────────────────
  // Determines whether a full scrape is safe to run after applying the fix.
  // Filter fixes (url_safety present) are held to a stricter standard.
  const isFilterFix = Boolean(preview?.url_safety);
  const scrapeBlockReasons: string[] = (() => {
    if (!validationResult) return [];
    const reasons: string[] = [];
    const { agg_status, agg_drop_rate_pct, total_passing, total_dropped } = validationResult;
    if (agg_status === "critical") {
      reasons.push("Discovery status is critical.");
    }
    if (isFilterFix) {
      if (agg_drop_rate_pct >= 70) {
        reasons.push(`URL filter would drop ${agg_drop_rate_pct}% of discovered course URLs.`);
      }
      if (total_passing === 0) {
        reasons.push("After-filter course count is 0 — no courses would be discovered.");
      } else if (total_passing < 5) {
        reasons.push(`Seed URL returned fewer than 5 courses (${total_passing} found).`);
      }
      if (total_dropped > total_passing * 2) {
        reasons.push(`Filter drops more than twice the number of passing URLs (${total_dropped} dropped vs ${total_passing} passing).`);
      }
    }
    if (agg_status === "warning" && reasons.length === 0) {
      reasons.push("Discovery returned warnings — review issues above before scraping.");
    }
    return reasons;
  })();
  const safeToScrape = applied && validationResult != null && scrapeBlockReasons.length === 0;

  const handleRunFullScrape = async () => {
    setLaunching(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ university_id: uniId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail?.message ?? err?.detail ?? `HTTP ${res.status}`);
      }
      toast({ title: "Full scrape started", description: "Monitor progress in Scraping Jobs." });
      onClose();
    } catch (e: unknown) {
      toast({
        title: "Failed to start scrape",
        description: e instanceof Error ? e.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setLaunching(false);
    }
  };

  const riskColor = {
    low: "text-green-700 bg-green-50 border-green-200",
    medium: "text-amber-700 bg-amber-50 border-amber-300",
    critical: "text-red-700 bg-red-50 border-red-300",
  };
  const riskIcon = {
    low: <CheckCircle2 className="w-3.5 h-3.5 text-green-600" />,
    medium: <AlertTriangle className="w-3.5 h-3.5 text-amber-500" />,
    critical: <ShieldAlert className="w-3.5 h-3.5 text-red-500" />,
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-lg max-h-[90vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b bg-gray-50 shrink-0">
          <div className="flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-teal-600" />
            <span className="text-sm font-semibold text-gray-800">Fix Preview</span>
          </div>
          <button onClick={onClose} className="p-1 rounded hover:bg-gray-200 text-gray-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 px-4 py-3 space-y-3">
          {/* Problem summary — always show from rec */}
          <div>
            <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1">Problem</p>
            <p className="text-sm font-medium text-gray-800">{rec.title}</p>
            <p className="text-[11px] text-gray-500 mt-0.5">{rec.description}</p>
          </div>

          {loading && (
            <div className="flex items-center gap-2 py-6 justify-center text-gray-400">
              <Loader2 className="w-4 h-4 animate-spin" />
              <span className="text-xs">Simulating fix…</span>
            </div>
          )}

          {error && (
            <div className="p-2 bg-red-50 border border-red-200 rounded text-[11px] text-red-700">
              {error}
            </div>
          )}

          {preview && !loading && (
            <>
              {/* Evidence URLs — from scraped field evidence with snippets */}
              {preview && (preview.evidence_urls?.length ?? 0) > 0 && (
                <div>
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Evidence Found On</p>
                  <div className="space-y-1.5">
                    {preview.evidence_urls!.map((ev, i) => (
                      <div key={i} className="p-2 bg-teal-50 border border-teal-100 rounded">
                        <a href={ev.url} target="_blank" rel="noreferrer"
                          className="text-[9px] text-blue-600 hover:underline block truncate font-mono flex items-center gap-1">
                          <ExternalLink className="w-2.5 h-2.5 shrink-0" />
                          {ev.url}
                        </a>
                        {ev.snippet && (
                          <p className="text-[10px] font-mono bg-white border border-teal-100 rounded px-1.5 py-0.5 text-gray-600 whitespace-pre-wrap break-words mt-1">
                            …{ev.snippet.trim()}…
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Fallback evidence block (page signals from rec) */}
              {(!preview?.evidence_urls?.length) && rec.evidence && (rec.evidence.detected_snippets?.length || rec.evidence.page_signals) && (
                <div>
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Evidence</p>
                  <div className="p-2 bg-teal-50 border border-teal-100 rounded space-y-1.5">
                    {rec.evidence.sample_url && (
                      <a href={rec.evidence.sample_url} target="_blank" rel="noreferrer"
                        className="text-[9px] text-blue-600 hover:underline block truncate font-mono">
                        {rec.evidence.sample_url}
                      </a>
                    )}
                    {rec.evidence.detected_snippets?.slice(0, 2).map((snip, i) => (
                      <p key={i} className="text-[10px] font-mono bg-white border border-teal-100 rounded px-1.5 py-0.5 text-gray-600 whitespace-pre-wrap break-words">
                        {snip}
                      </p>
                    ))}
                    {rec.evidence.page_signals && Object.keys(rec.evidence.page_signals).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {Object.keys(rec.evidence.page_signals).map(sig => (
                          <span key={sig} className="text-[9px] bg-white border border-green-200 text-green-700 rounded-full px-1.5 py-0.5">
                            ✓ {sig.replace(/_/g, " ")}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Confidence */}
              <div>
                <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Confidence</p>
                <div className="flex items-center gap-2 mb-1.5">
                  <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
                    <div
                      className={`h-2 rounded-full ${preview.confidence >= 85 ? "bg-green-500" : preview.confidence >= 65 ? "bg-amber-400" : "bg-red-400"}`}
                      style={{ width: `${preview.confidence}%` }}
                    />
                  </div>
                  <span className={`text-xs font-bold ${preview.confidence >= 85 ? "text-green-700" : preview.confidence >= 65 ? "text-amber-700" : "text-red-700"}`}>
                    {preview.confidence}%
                  </span>
                </div>
                <p className="text-[11px] text-gray-600 leading-relaxed">{preview.confidence_reason}</p>
              </div>

              {/* Expected Impact */}
              <div>
                <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Expected Impact</p>
                <div className="grid grid-cols-2 gap-2">
                  <div className="p-2 bg-gray-50 border border-gray-200 rounded text-center">
                    <p className="text-[9px] text-gray-400 mb-0.5">
                      {preview.field_impact.field.replace(/_/g, " ")} now
                    </p>
                    <p className="text-lg font-bold text-gray-500">{preview.field_impact.current_pct}%</p>
                  </div>
                  <div className="p-2 bg-teal-50 border border-teal-200 rounded text-center">
                    <p className="text-[9px] text-teal-500 mb-0.5">after fix (est.)</p>
                    <p className="text-lg font-bold text-teal-700">{preview.field_impact.expected_pct}%</p>
                  </div>
                </div>
                <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-gray-600">
                  <TrendingUp className="w-3 h-3 text-teal-500 shrink-0" />
                  <span>
                    <strong className="text-teal-700">+{Math.round(preview.field_impact.expected_pct - preview.field_impact.current_pct)} pp</strong>
                    {" "}gain · {preview.field_impact.courses_affected} courses affected
                    {preview.field_impact.courses_total > 0 && ` of ${preview.field_impact.courses_total} total`}
                  </span>
                </div>
              </div>

              {/* Affected Courses — expandable list */}
              {(preview.affected_course_names?.length ?? 0) > 0 && (
                <div>
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">
                    Affected Courses ({preview.affected_course_names!.length}{preview.affected_course_names!.length === 50 ? "+" : ""})
                  </p>
                  <div className="border border-gray-200 rounded overflow-hidden">
                    <ul className="divide-y divide-gray-100 max-h-[160px] overflow-auto">
                      {(showAllCourses ? preview.affected_course_names! : preview.affected_course_names!.slice(0, 8)).map((name, i) => (
                        <li key={i} className="px-2.5 py-1.5 text-[10px] text-gray-700 hover:bg-gray-50">{name}</li>
                      ))}
                    </ul>
                    {preview.affected_course_names!.length > 8 && (
                      <button
                        type="button"
                        onClick={() => setShowAllCourses(s => !s)}
                        className="w-full text-[9px] text-teal-600 hover:text-teal-800 py-1.5 bg-gray-50 border-t border-gray-200 font-medium"
                      >
                        {showAllCourses
                          ? "Show fewer ▲"
                          : `Show ${preview.affected_course_names!.length - 8} more ▼`}
                      </button>
                    )}
                  </div>
                </div>
              )}

              {/* Preview After Fix — before/after sample */}
              {preview.sample_before_after && (
                <div>
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Preview After Fix</p>
                  <p className="text-[9px] text-gray-400 mb-1.5 truncate">Sample: {preview.sample_before_after.course_name}</p>
                  <div className="grid grid-cols-2 gap-2">
                    <div className="p-2.5 bg-gray-50 border border-gray-200 rounded">
                      <p className="text-[9px] font-semibold text-gray-400 mb-1">Before</p>
                      <p className="text-xs font-semibold text-gray-500">{preview.sample_before_after.field_label}</p>
                      <p className="text-[11px] text-gray-400 italic mt-0.5">blank</p>
                    </div>
                    <div className="p-2.5 bg-teal-50 border border-teal-200 rounded">
                      <p className="text-[9px] font-semibold text-teal-500 mb-1">After</p>
                      <p className="text-xs font-semibold text-teal-700">{preview.sample_before_after.field_label}</p>
                      <p className="text-[11px] text-teal-800 font-mono mt-0.5">
                        {preview.sample_before_after.after_value ?? "—"}
                      </p>
                    </div>
                  </div>
                </div>
              )}

              {/* Risk */}
              <div>
                <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Risk</p>
                <div className={`flex items-start gap-2 p-2 rounded border text-[11px] leading-relaxed ${riskColor[preview.risk_level]}`}>
                  <span className="mt-0.5 shrink-0">{riskIcon[preview.risk_level]}</span>
                  <div className="flex-1">
                    <span className="font-semibold capitalize">{preview.risk_level}</span>
                    {" — "}
                    {preview.risk_reason}
                  </div>
                  <span className="shrink-0 flex items-center gap-0.5 text-[9px] font-semibold text-green-700 bg-green-100 border border-green-200 rounded-full px-1.5 py-0.5">
                    <RotateCcw className="w-2.5 h-2.5" /> Rollback available
                  </span>
                </div>
                {preview.url_safety && (
                  <div className="mt-1.5 p-2 bg-gray-50 border border-gray-200 rounded text-[10px] text-gray-600 space-y-0.5">
                    <p className="font-medium text-gray-700">URL Filter Check</p>
                    <p>{preview.url_safety.total_urls} known URLs tested · {preview.url_safety.passing} pass · {preview.url_safety.dropped} dropped ({preview.url_safety.drop_rate_pct}%)</p>
                    {(preview.url_safety.expected_courses_before != null && preview.url_safety.expected_courses_after != null) && (
                      <p className="text-amber-700 font-medium">
                        Staged courses: {preview.url_safety.expected_courses_before} → {preview.url_safety.expected_courses_after} expected after filter
                      </p>
                    )}
                    {preview.url_safety.dropped_samples.length > 0 && (
                      <p className="font-mono text-red-600 truncate">Dropped: {preview.url_safety.dropped_samples[0]}</p>
                    )}
                  </div>
                )}
              </div>

              {/* Validation checklist */}
              <div>
                <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Validation</p>
                <div className="space-y-1">
                  {preview.validations.map((v, i) => (
                    <div key={i} className="flex items-start gap-2">
                      {v.ok
                        ? <CheckCircle2 className="w-3 h-3 text-green-500 mt-0.5 shrink-0" />
                        : <X className="w-3 h-3 text-red-500 mt-0.5 shrink-0" />}
                      <div className="text-[11px]">
                        <span className={`font-medium ${v.ok ? "text-green-700" : "text-red-700"}`}>{v.label}</span>
                        <span className="text-gray-500 ml-1">{v.detail}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Recipe settings */}
              {rec.fix?.recipe_patch && Object.keys(rec.fix.recipe_patch).length > 0 && (
                <div>
                  <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Recipe Settings</p>
                  <div className="p-2 bg-white border border-orange-100 rounded space-y-1">
                    {Object.entries(rec.fix.recipe_patch).map(([key, val]) => (
                      <div key={key} className="flex items-start gap-1.5">
                        <code className="text-[9px] bg-orange-50 border border-orange-200 text-orange-800 rounded px-1.5 py-0.5 font-mono shrink-0">{key}</code>
                        <span className="text-[10px] text-gray-600 break-words">{JSON.stringify(val)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Blocked message */}
              {preview.risk_level === "critical" && (
                <div className="p-2.5 bg-red-50 border border-red-300 rounded text-[11px] text-red-800 leading-relaxed space-y-1">
                  <p><strong>Fix blocked automatically.</strong> {preview.risk_reason}</p>
                  {(preview.url_safety?.expected_courses_before != null && preview.url_safety?.expected_courses_after != null) && (
                    <p>Course projection: <strong>{preview.url_safety.expected_courses_before}</strong> staged courses → <strong className="text-red-700">{preview.url_safety.expected_courses_after}</strong> remaining after this filter change.</p>
                  )}
                  <p>Review the URL filter settings manually before applying.</p>
                </div>
              )}

              {/* Post-apply validation panel */}
              {applied && (
                <div className="border border-teal-200 rounded bg-teal-50 overflow-hidden">
                  <div className="flex items-center gap-2 px-3 py-2 bg-teal-600">
                    <CheckCircle2 className="w-3.5 h-3.5 text-white shrink-0" />
                    <span className="text-xs font-semibold text-white">Fix applied — Discovery Validation</span>
                  </div>
                  <div className="px-3 py-2.5 space-y-1.5">
                    {validating && (
                      <div className="flex items-center gap-2 text-[11px] text-teal-700">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        Running fast discovery check…
                      </div>
                    )}
                    {!validating && !validationResult && (
                      <p className="text-[11px] text-teal-700">Discovery validation could not run — trigger a manual test discovery to verify.</p>
                    )}
                    {validationResult && (
                      <div className="space-y-1.5">
                        {/* Status row */}
                        <div className="flex items-center gap-3 flex-wrap">
                          <span className={`text-[11px] font-semibold ${validationResult.total_passing > 0 ? "text-green-700" : "text-red-700"}`}>
                            {validationResult.total_passing > 0 ? "✓" : "✗"} {validationResult.total_passing} URLs passing
                          </span>
                          {validationResult.total_dropped > 0 && (
                            <span className="text-[11px] text-amber-700">{validationResult.total_dropped} dropped ({validationResult.agg_drop_rate_pct}%)</span>
                          )}
                          <span className={`text-[9px] px-1.5 py-0.5 rounded-full font-medium ${
                            validationResult.agg_status === "ok" ? "bg-green-100 text-green-700 border border-green-200"
                            : validationResult.agg_status === "warning" ? "bg-amber-100 text-amber-700 border border-amber-200"
                            : "bg-red-100 text-red-700 border border-red-200"
                          }`}>
                            {validationResult.agg_status}
                          </span>
                        </div>

                        {/* Discovery warnings */}
                        {validationResult.warnings?.slice(0, 2).map((w, i) => (
                          <p key={i} className="text-[10px] text-gray-500">• {w}</p>
                        ))}

                        {/* Safe-to-scrape verdict */}
                        {safeToScrape ? (
                          <div className="flex items-center gap-1.5 mt-1 p-2 bg-green-50 border border-green-200 rounded text-[11px] text-green-800">
                            <CheckCircle2 className="w-3.5 h-3.5 text-green-600 shrink-0" />
                            <span>
                              {isFilterFix
                                ? "Filter validated — safe to run full scrape."
                                : "Discovery unaffected — safe to run full scrape."}
                            </span>
                          </div>
                        ) : (
                          <div className="mt-1 p-2 bg-red-50 border border-red-200 rounded space-y-1">
                            <p className="text-[11px] font-semibold text-red-800 flex items-center gap-1">
                              <ShieldAlert className="w-3.5 h-3.5 shrink-0" /> Cannot run full scrape yet.
                            </p>
                            <p className="text-[10px] text-red-700 font-medium">Recipe validation failed:</p>
                            {scrapeBlockReasons.map((r, i) => (
                              <p key={i} className="text-[10px] text-red-700">• {r}</p>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t bg-gray-50 shrink-0 gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-xs text-gray-500 hover:text-gray-700 px-3 py-1.5 rounded border border-gray-200 hover:bg-gray-100"
          >
            {applied ? "Close" : "Cancel"}
          </button>

          {/* Pre-apply: Apply Fix button */}
          {!applied && preview && rec.fix?.recipe_patch && (
            <button
              type="button"
              onClick={handleApply}
              disabled={applying || preview.risk_level === "critical" || loading}
              className="flex items-center gap-1.5 text-xs font-medium text-white bg-teal-600 hover:bg-teal-700 disabled:opacity-50 disabled:cursor-not-allowed rounded px-3 py-1.5"
            >
              {applying
                ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Applying…</>
                : preview.risk_level === "critical"
                  ? <><ShieldAlert className="w-3.5 h-3.5" /> Fix Blocked</>
                  : <><Zap className="w-3.5 h-3.5" /> Apply Fix</>}
            </button>
          )}

          {/* Post-apply, still validating */}
          {applied && validating && (
            <span className="text-[11px] text-teal-600 flex items-center gap-1.5">
              <Loader2 className="w-3 h-3 animate-spin" /> Validating…
            </span>
          )}

          {/* Post-apply, validation complete: Run Full Scrape gate */}
          {applied && !validating && validationResult && (
            <div className="flex flex-col items-end gap-1">
              <button
                type="button"
                onClick={safeToScrape ? handleRunFullScrape : undefined}
                disabled={!safeToScrape || launching}
                className={`flex items-center gap-1.5 text-xs font-medium rounded px-3 py-1.5 transition-colors ${
                  safeToScrape
                    ? "text-white bg-green-600 hover:bg-green-700"
                    : "text-gray-400 bg-gray-100 border border-gray-200 cursor-not-allowed"
                } disabled:opacity-60`}
              >
                {launching
                  ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Starting…</>
                  : safeToScrape
                    ? <><Play className="w-3.5 h-3.5" /> Run Full Scrape</>
                    : <><ShieldAlert className="w-3.5 h-3.5" /> Run Full Scrape Blocked</>}
              </button>
              {!safeToScrape && (
                <p className="text-[9px] text-red-600 text-right max-w-[220px] leading-tight">
                  Fix the recipe issue above before running a scrape.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Phase 3 Recommendation Card ───────────────────────────────────────────────

function Phase3RecCard({ rec, jobId, uniId }: { rec: Phase3Rec; jobId: string | null; uniId: number }) {
  const [expanded, setExpanded] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probeStarted, setProbeStarted] = useState(false);
  const { toast } = useToast();
  const isCritical = rec.severity === "critical";
  const hasEvidence = Boolean(rec.evidence?.detected_snippets?.length || rec.evidence?.sample_url);
  const hasRecipePatch = rec.fix?.recipe_patch && Object.keys(rec.fix.recipe_patch).length > 0;
  const isAutoConfigureRec = rec.fix?.type === "auto_configure";
  const canPreview = Boolean(jobId && hasRecipePatch && !isAutoConfigureRec);

  const BASE = import.meta.env.BASE_URL?.replace(/\/$/, "") ?? "";

  const runAutoConfig = async () => {
    setProbing(true);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/probe`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setProbeStarted(true);
      toast({
        title: "Auto-Configure started",
        description: "Gemini is probing the live site to derive URL patterns. Check the Intelligence tab in ~30 s.",
      });
    } catch (e) {
      toast({ title: "Auto-Configure failed to start", description: String(e), variant: "destructive" });
    } finally {
      setProbing(false);
    }
  };

  const borderCls = isCritical
    ? "border-orange-400 bg-orange-50"
    : "border-amber-300 bg-amber-50";

  return (
    <>
      {showPreview && jobId && (
        <FixPreviewModal
          rec={rec}
          jobId={jobId}
          uniId={uniId}
          onClose={() => setShowPreview(false)}
          onApplied={() => { /* applied — keep modal open for validation */ }}
        />
      )}
      <div className={`rounded-lg border-l-4 px-3 py-2.5 ${borderCls}`}>
        <div className="flex items-start gap-2">
          <FlaskConical className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${isCritical ? "text-orange-500" : "text-amber-500"}`} />
          <div className="flex-1 min-w-0">
            {/* Header */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-semibold text-gray-800">{rec.title}</span>
              <Badge variant="outline" className={`text-[9px] px-1.5 ${isCritical ? "border-orange-300 text-orange-700 bg-orange-100" : "border-amber-300 text-amber-700 bg-amber-100"}`}>
                {rec.severity}
              </Badge>
              <span className="text-[9px] text-gray-400 ml-auto">{Math.round(rec.confidence * 100)}% confidence</span>
            </div>

            {/* Description */}
            <p className="text-xs text-gray-600 leading-relaxed mt-0.5">{rec.description}</p>

            {/* Root cause */}
            <p className="text-[10px] text-gray-500 italic mt-1">{rec.root_cause}</p>

            {/* Action row: Preview Fix / Auto-Configure button + details toggle */}
            <div className="flex items-center gap-3 mt-2 flex-wrap">
              {canPreview && (
                <button
                  type="button"
                  onClick={() => setShowPreview(true)}
                  className="flex items-center gap-1 text-[10px] font-semibold text-white bg-teal-600 hover:bg-teal-700 rounded px-2.5 py-1"
                >
                  <TrendingUp className="w-3 h-3" />
                  Preview Fix
                </button>
              )}
              {isAutoConfigureRec && !probeStarted && (
                <button
                  type="button"
                  onClick={runAutoConfig}
                  disabled={probing}
                  className="flex items-center gap-1 text-[10px] font-semibold text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-60 rounded px-2.5 py-1"
                >
                  {probing
                    ? <><Loader2 className="w-3 h-3 animate-spin" /> Starting…</>
                    : <><Zap className="w-3 h-3" /> Run Auto-Configure</>}
                </button>
              )}
              {isAutoConfigureRec && probeStarted && (
                <span className="flex items-center gap-1 text-[10px] text-blue-700 font-medium bg-blue-50 border border-blue-200 rounded px-2 py-0.5">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  Probing… check Intelligence tab in ~30 s
                </span>
              )}
              {(hasEvidence || hasRecipePatch || rec.fix?.description) && (
                <button
                  type="button"
                  onClick={() => setExpanded(e => !e)}
                  className="text-[10px] text-teal-600 hover:text-teal-800 flex items-center gap-0.5"
                >
                  {expanded ? "Hide details ▲" : `Show ${[hasEvidence && "evidence", hasRecipePatch && "recipe", isAutoConfigureRec && !hasEvidence && "how to fix"].filter(Boolean).join(" + ")} ▼`}
                </button>
              )}
            </div>

            {expanded && (
              <div className="mt-1.5 space-y-2">
                {/* Page evidence snippets */}
                {hasEvidence && (
                  <div className="p-2 bg-white border border-teal-100 rounded space-y-1">
                    <p className="text-[10px] font-semibold text-teal-700 mb-1">Page evidence</p>
                    {rec.evidence?.sample_url && (
                      <a href={rec.evidence.sample_url} target="_blank" rel="noreferrer"
                        className="text-[9px] text-blue-600 hover:underline block truncate font-mono">
                        {rec.evidence.sample_url}
                      </a>
                    )}
                    {rec.evidence?.detected_snippets?.map((snip, i) => (
                      <p key={i} className="text-[10px] font-mono bg-gray-50 border border-gray-200 rounded px-1.5 py-0.5 text-gray-600 whitespace-pre-wrap break-words">
                        {snip}
                      </p>
                    ))}
                    {rec.evidence?.page_signals && Object.keys(rec.evidence.page_signals).length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {Object.keys(rec.evidence.page_signals).map(sig => (
                          <span key={sig} className="text-[9px] bg-green-50 border border-green-200 text-green-700 rounded-full px-1.5 py-0.5">
                            ✓ {sig.replace(/_/g, " ")}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Recipe patch */}
                {hasRecipePatch && (
                  <div className="p-2 bg-white border border-orange-100 rounded">
                    <p className="text-[10px] font-semibold text-orange-700 mb-1">Recipe Editor settings to configure</p>
                    <div className="space-y-1">
                      {Object.entries(rec.fix!.recipe_patch!).map(([key, val]) => (
                        <div key={key} className="flex items-start gap-1.5">
                          <code className="text-[9px] bg-orange-50 border border-orange-200 text-orange-800 rounded px-1.5 py-0.5 font-mono shrink-0">{key}</code>
                          <span className="text-[10px] text-gray-600 break-words">{JSON.stringify(val)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Fix description */}
                {rec.fix?.description && (
                  <p className="text-[11px] text-gray-700 leading-relaxed p-2 bg-teal-50 border border-teal-100 rounded">
                    <span className="font-semibold text-teal-700">How to fix: </span>
                    {rec.fix.description}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

import { useState, useEffect, useCallback } from "react";
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
  ShieldAlert, Play, ExternalLink, FlaskConical, BarChart3, Wrench,
} from "lucide-react";
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
  job_stats: {
    total_found: number;
    imported: number;
    skipped: number;
    errors: number;
    avg_completeness_pct: number;
    min_expected_courses: number;
  };
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
  const [savingRecipe, setSavingRecipe] = useState(false);

  const [saving, setSaving] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnoseResult, setDiagnoseResult] = useState<DiagnoseResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [appliedConfig, setAppliedConfig] = useState<Record<string, unknown> | null>(null);
  const [checkingExtraction, setCheckingExtraction] = useState(false);
  const [extractionResult, setExtractionResult] = useState<ExtractionQualityResult | null>(null);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/universities/${uniId}/agent-config`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AgentConfig = await res.json();
      setConfig(data);

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
    } catch (e) {
      toast({ title: "Failed to load config", description: String(e), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [uniId, toast]);

  useEffect(() => { loadConfig(); }, [loadConfig]);

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
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/diagnose`, { method: "POST" });
      const data: DiagnoseResult = await res.json();
      setDiagnoseResult(data);
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

  const applyFix = async (patch: Record<string, unknown>) => {
    const jobId = config?.latest_job_id;
    if (!jobId) return;
    setApplying(true);
    try {
      const res = await fetch(`${BASE}/api/scrape/jobs/${jobId}/apply-fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config_patch: patch }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAppliedConfig(data.new_admin_config);
      toast({ title: "Fix applied!", description: "Config updated — re-run the scrape to see results." });
      await loadConfig();
    } catch (e) {
      toast({ title: "Apply fix failed", description: String(e), variant: "destructive" });
    } finally {
      setApplying(false);
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
          <p className="text-sm text-gray-500">{config?.university_name}</p>
          {config?.scrape_url && (
            <a href={config.scrape_url} target="_blank" rel="noreferrer"
               className="text-xs text-blue-500 hover:underline flex items-center gap-1">
              {config.scrape_url} <ExternalLink className="w-2.5 h-2.5" />
            </a>
          )}
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
        saving={savingRecipe} onSave={saveRecipe}
      />

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
          </div>
        </div>

        {!diagnoseResult && !diagnosing && (
          <div className="text-center py-8 text-gray-400 text-sm">
            <Bot className="w-8 h-8 mx-auto mb-2 opacity-30" />
            Click <strong>Run Diagnosis</strong> to let AI analyse the last scrape job and suggest fixes.
          </div>
        )}

        {diagnosing && (
          <div className="flex items-center justify-center gap-2 py-8 text-blue-500 text-sm">
            <Loader2 className="w-5 h-5 animate-spin" />
            AI is reading the scrape logs and thinking…
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

              {/* Error */}
              {diagnoseResult.error && (
                <div className="p-3 bg-red-50 rounded-lg text-sm text-red-600 border border-red-200">
                  {diagnoseResult.error}
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

              {/* Suggested config + Apply Fix */}
              {hasSuggestions && (
                <div className="border border-green-200 rounded-lg p-3 bg-green-50 space-y-3">
                  <div className="flex items-center gap-2">
                    <Zap className="w-4 h-4 text-green-600" />
                    <p className="text-xs font-semibold text-green-800">AI has suggested config changes</p>
                  </div>
                  <pre className="text-[10px] bg-white border border-green-100 rounded p-2 overflow-auto max-h-[200px] text-gray-700 font-mono leading-relaxed">
                    {JSON.stringify(suggestedConfig, null, 2)}
                  </pre>
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      onClick={() => applyFix(suggestedConfig as Record<string, unknown>)}
                      disabled={applying}
                      className="bg-green-600 hover:bg-green-700 h-8 text-xs gap-1.5"
                    >
                      {applying ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCheck className="w-3.5 h-3.5" />}
                      {applying ? "Applying…" : "Apply AI Fix"}
                    </Button>
                    <span className="text-[10px] text-green-700">Review the changes above, then approve to update scrape rules.</span>
                  </div>
                </div>
              )}

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

              {/* Re-run */}
              <div className="flex items-center gap-2 pt-2 border-t">
                <button
                  type="button"
                  onClick={runDiagnosis}
                  disabled={diagnosing}
                  className="text-xs text-blue-500 hover:text-blue-700 flex items-center gap-1"
                >
                  <RefreshCw className="w-3 h-3" /> Re-run diagnosis
                </button>
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
  saving: boolean; onSave: () => void;
}) {
  const {
    feeSourceUrls, setFeeSourceUrls, feeTerm, setFeeTerm,
    feeCalcMode, setFeeCalcMode, feePreventRollup, setFeePreventRollup,
    ieltsMapping, setIeltsMapping,
    nameRemoveAfter, setNameRemoveAfter, nameRemoveYear, setNameRemoveYear,
    locAllowed, setLocAllowed, locReject, setLocReject,
    locReplace, setLocReplace, modeFromLoc, setModeFromLoc,
    modeOnlineKws, setModeOnlineKws, saving, onSave,
  } = props;

  const hasFee = feeSourceUrls.length > 0 || feeTerm !== "" || feeCalcMode !== "use_source_value_only" || !feePreventRollup;
  const hasIelts = ieltsMapping.length > 0;
  const hasName = nameRemoveAfter.length > 0 || nameRemoveYear;
  const hasLoc = locAllowed.length > 0 || locReject.length > 0 || locReplace.length > 0;
  const hasMode = modeFromLoc;
  const anyActive = hasFee || hasIelts || hasName || hasLoc || hasMode;

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
              {[hasFee && "fee", hasIelts && "IELTS", hasName && "name", hasLoc && "location", hasMode && "mode"]
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
      </div>

      <div className="flex items-center gap-2 pt-1 border-t">
        <Button onClick={onSave} disabled={saving} className="gap-1.5 h-8 text-xs bg-teal-600 hover:bg-teal-700">
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
          Save Recipe
        </Button>
        <p className="text-[10px] text-gray-400">Rules apply on the next scrape run</p>
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
                🔧 Platform bug
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
                <strong className="text-slate-800">Developer fix required.</strong>{" "}
                This cannot be corrected through portal settings. The data extraction logic needs to be updated by the development team.
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

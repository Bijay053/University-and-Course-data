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
  ShieldAlert, Play, ExternalLink,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";

const BASE = "";

// ── Types ─────────────────────────────────────────────────────────────────────

type AgentConfig = {
  university_id: number;
  university_name: string;
  scrape_url: string;
  admin_config: Record<string, unknown>;
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

  const [saving, setSaving] = useState(false);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnoseResult, setDiagnoseResult] = useState<DiagnoseResult | null>(null);
  const [applying, setApplying] = useState(false);
  const [appliedConfig, setAppliedConfig] = useState<Record<string, unknown> | null>(null);

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

              {!hasSuggestions && diagnoseResult.ok && (
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

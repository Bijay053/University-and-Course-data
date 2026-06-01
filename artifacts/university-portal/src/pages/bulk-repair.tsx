import { useState, useMemo } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  Languages, DollarSign, TrendingUp, Search, CheckSquare, Square,
  RefreshCw, Wrench, FlaskConical, ChevronRight, AlertTriangle,
  CheckCircle2, XCircle, ExternalLink, Bot, Info, ShieldAlert,
  History, User, Calendar, ChevronDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { Link } from "wouter";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

async function apiFetch(path: string, opts?: RequestInit) {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(opts?.headers ?? {}) },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ── Types ────────────────────────────────────────────────────────────────────
type IssueKey = "ielts_low" | "fee_low" | "discovery_low";

interface UniScan {
  id: number;
  name: string;
  country: string | null;
  scrape_url: string | null;
  certification_status: string;
  health_score: number;
  ielts_pct: number | null;
  fee_pct: number | null;
  duration_pct: number | null;
  location_pct: number | null;
  staged_courses: number;
  last_scrape_at: string | null;
  ielts_low: boolean;
  fee_low: boolean;
  discovery_low: boolean;
  has_any_issue: boolean;
}

interface ScanData {
  summary: Record<IssueKey | "total", number>;
  thresholds: { ielts: number; fee: number; discovery: number };
  universities: UniScan[];
}

interface ApplyResult {
  total: number;
  succeeded: number;
  failed: number;
  results: Array<{
    id: number;
    name: string;
    ok: boolean;
    repair_job_id?: string;
    repair_count?: number;
    marked_testing?: boolean;
    error?: string;
  }>;
}

interface PreviewUni {
  id: number;
  name: string;
  no_seed_url: boolean;
  no_repair_targets: boolean;
  repair_target_count: number;
  active_course_count: number;
}

interface PreviewData {
  selected: number;
  estimated_jobs: number;
  universities: PreviewUni[];
  risks: {
    no_seed_url: string[];
    no_repair_targets: string[];
  };
}

interface HistoryEntry {
  id: number;
  created_at: string;
  triggered_by_email: string;
  triggered_by_name: string | null;
  issue_types: string[];
  selected_count: number;
  queued_count: number;
  skipped_count: number;
  failed_count: number;
  mark_testing: boolean;
  university_names: string[];
}

// ── Issue config ─────────────────────────────────────────────────────────────
const ISSUE_CFG: Record<IssueKey, {
  label: string; shortLabel: string; desc: string;
  bg: string; border: string; text: string; icon: React.ReactNode;
  fix: string;
}> = {
  ielts_low:      { label: "IELTS Quality < 50%",    shortLabel: "IELTS",     desc: "Universities where fewer than half of staged courses have an IELTS score extracted.", bg: "bg-purple-50", border: "border-purple-200", text: "text-purple-700", icon: <Languages className="h-5 w-5" />, fix: "Run Repair Scrape" },
  fee_low:        { label: "Fee Quality < 60%",       shortLabel: "Fees",      desc: "Universities where fewer than 60% of staged courses have an international fee populated.", bg: "bg-amber-50", border: "border-amber-200", text: "text-amber-700", icon: <DollarSign className="h-5 w-5" />, fix: "Run Repair Scrape" },
  discovery_low:  { label: "Discovery Score < 70",    shortLabel: "Discovery", desc: "Universities whose overall health score is below 70 — low course discovery or high error rate.", bg: "bg-red-50", border: "border-red-200", text: "text-red-700", icon: <TrendingUp className="h-5 w-5" />, fix: "Run Repair Scrape" },
};

// ── Fill-rate bar ─────────────────────────────────────────────────────────────
function FillBar({ pct, threshold }: { pct: number | null; threshold: number }) {
  if (pct === null) return <span className="text-xs text-gray-300">—</span>;
  const bad = pct < threshold;
  const color = bad
    ? pct < threshold / 2 ? "bg-red-500" : "bg-amber-400"
    : "bg-emerald-500";
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-1.5 rounded-full bg-gray-100 overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className={`text-xs font-medium tabular-nums ${bad ? "text-red-600" : "text-gray-600"}`}>
        {pct.toFixed(0)}%
      </span>
    </div>
  );
}

function ScorePill({ score }: { score: number }) {
  const color =
    score >= 80 ? "bg-emerald-100 text-emerald-800" :
    score >= 60 ? "bg-blue-100 text-blue-800" :
    score >= 40 ? "bg-amber-100 text-amber-800" :
    "bg-red-100 text-red-800";
  return (
    <span className={`inline-flex items-center justify-center w-9 h-9 rounded-full font-bold text-sm ${color}`}>
      {score}
    </span>
  );
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const days = Math.floor(diff / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

// ── History view ──────────────────────────────────────────────────────────────
function HistoryView() {
  const [expanded, setExpanded] = useState<number | null>(null);
  const { data, isLoading } = useQuery<{ history: HistoryEntry[] }>({
    queryKey: ["bulk-repair-history"],
    queryFn: () => apiFetch("/api/bulk-repair/history?limit=50"),
    staleTime: 30_000,
  });

  const entries = data?.history ?? [];

  function absTime(iso: string) {
    return new Date(iso).toLocaleString("en-AU", {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }

  if (isLoading) return <div className="py-12 text-center text-gray-400 text-sm">Loading history…</div>;

  if (entries.length === 0) return (
    <div className="py-16 text-center text-gray-400">
      <History className="h-10 w-10 mx-auto mb-3 opacity-30" />
      <p className="text-sm">No bulk repair actions recorded yet.</p>
      <p className="text-xs mt-1">Each confirmed repair will appear here.</p>
    </div>
  );

  return (
    <div className="space-y-3">
      {entries.map(e => (
        <div key={e.id} className="rounded-xl border bg-white shadow-sm overflow-hidden">
          {/* Summary row */}
          <button
            className="w-full flex items-center gap-4 px-5 py-4 text-left hover:bg-gray-50 transition-colors"
            onClick={() => setExpanded(expanded === e.id ? null : e.id)}
          >
            <div className="h-9 w-9 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
              <Wrench className="h-4 w-4 text-blue-600" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold text-sm text-gray-900">Bulk Repair</span>
                {e.mark_testing && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200">+ Testing</span>
                )}
                {e.failed_count > 0 && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-red-50 text-red-700 border border-red-200">{e.failed_count} failed</span>
                )}
              </div>
              <div className="text-xs text-gray-500 mt-0.5 flex items-center gap-3 flex-wrap">
                <span className="flex items-center gap-1"><User className="h-3 w-3" />{e.triggered_by_name || e.triggered_by_email}</span>
                <span className="flex items-center gap-1"><Calendar className="h-3 w-3" />{absTime(e.created_at)}</span>
              </div>
            </div>
            <div className="flex items-center gap-4 shrink-0 text-sm">
              <div className="text-center hidden sm:block">
                <div className="font-bold text-gray-900">{e.selected_count}</div>
                <div className="text-xs text-gray-400">Selected</div>
              </div>
              <div className="text-center hidden sm:block">
                <div className="font-bold text-emerald-700">{e.queued_count}</div>
                <div className="text-xs text-gray-400">Queued</div>
              </div>
              {e.skipped_count > 0 && (
                <div className="text-center hidden sm:block">
                  <div className="font-bold text-amber-600">{e.skipped_count}</div>
                  <div className="text-xs text-gray-400">Skipped</div>
                </div>
              )}
              <ChevronDown className={`h-4 w-4 text-gray-400 transition-transform ${expanded === e.id ? "rotate-180" : ""}`} />
            </div>
          </button>

          {/* Expanded detail */}
          {expanded === e.id && (
            <div className="border-t bg-gray-50 px-5 py-4 space-y-3">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {[
                  { label: "Selected",  val: e.selected_count,  color: "text-gray-900" },
                  { label: "Queued",    val: e.queued_count,    color: "text-emerald-700" },
                  { label: "Skipped",   val: e.skipped_count,   color: "text-amber-600" },
                  { label: "Failed",    val: e.failed_count,    color: "text-red-600" },
                ].map(({ label, val, color }) => (
                  <div key={label} className="rounded-lg bg-white border px-3 py-2 text-center">
                    <div className={`text-xl font-bold ${color}`}>{val}</div>
                    <div className="text-xs text-gray-400">{label}</div>
                  </div>
                ))}
              </div>
              <div>
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">Universities ({e.university_names.length})</div>
                <div className="flex flex-wrap gap-1.5">
                  {e.university_names.slice(0, 20).map(n => (
                    <span key={n} className="text-xs px-2 py-0.5 rounded-full bg-white border text-gray-700">{n}</span>
                  ))}
                  {e.university_names.length > 20 && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">+{e.university_names.length - 20} more</span>
                  )}
                </div>
              </div>
              <div className="text-xs text-gray-400 flex items-center gap-1.5">
                <User className="h-3 w-3" />
                Triggered by <span className="font-medium text-gray-600">{e.triggered_by_email}</span>
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────
type Step = "scan" | "select" | "applying" | "results";

export default function BulkRepairPage() {
  const { toast } = useToast();
  const [viewMode, setViewMode] = useState<"workflow" | "history">("workflow");
  const [step, setStep] = useState<Step>("scan");
  const [activeIssues, setActiveIssues] = useState<Set<IssueKey>>(new Set());
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [markTesting, setMarkTesting] = useState(false);
  const [search, setSearch] = useState("");
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewData, setPreviewData] = useState<PreviewData | null>(null);

  const { data, isLoading, isError, refetch, isFetching } = useQuery<ScanData>({
    queryKey: ["bulk-repair-scan"],
    queryFn: () => apiFetch("/api/bulk-repair/scan"),
    staleTime: 120_000,
  });

  const previewMutation = useMutation({
    mutationFn: (ids: number[]) =>
      apiFetch("/api/bulk-repair/preview", { method: "POST", body: JSON.stringify({ university_ids: ids }) }),
    onSuccess: (res: PreviewData) => {
      setPreviewData(res);
      setPreviewOpen(true);
    },
    onError: () => {
      toast({ title: "Preview failed", description: "Could not load preview. Try again.", variant: "destructive" });
    },
  });

  const applyMutation = useMutation({
    mutationFn: (body: { university_ids: number[]; repair_scrape: boolean; mark_testing: boolean }) =>
      apiFetch("/api/bulk-repair/apply", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (res: ApplyResult) => {
      setApplyResult(res);
      setStep("results");
      setPreviewOpen(false);
    },
    onError: () => {
      toast({ title: "Apply failed", variant: "destructive" });
      setStep("select");
      setPreviewOpen(false);
    },
  });

  // Toggle issue filter card
  function toggleIssue(key: IssueKey) {
    setActiveIssues(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setSelected(new Set());
    setStep("select");
  }

  // Filtered universities for the table
  const filtered = useMemo(() => {
    if (!data) return [];
    let rows = data.universities.filter(u => u.has_any_issue);
    if (activeIssues.size > 0) {
      rows = rows.filter(u => [...activeIssues].some(k => u[k]));
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(u => u.name.toLowerCase().includes(q) || (u.country ?? "").toLowerCase().includes(q));
    }
    return rows;
  }, [data, activeIssues, search]);

  // Select/deselect helpers
  function toggleAll() {
    if (selected.size === filtered.length) setSelected(new Set());
    else setSelected(new Set(filtered.map(u => u.id)));
  }
  function toggleOne(id: number) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function handleApply() {
    if (selected.size === 0) return;
    previewMutation.mutate([...selected]);
  }

  function handleConfirmApply() {
    setStep("applying");
    applyMutation.mutate({
      university_ids: [...selected],
      repair_scrape: true,
      mark_testing: markTesting,
    });
  }

  const summary = data?.summary ?? { ielts_low: 0, fee_low: 0, discovery_low: 0, total: 0 };
  const thresholds = data?.thresholds ?? { ielts: 50, fee: 60, discovery: 70 };

  // ── Results step ────────────────────────────────────────────────────────────
  if (step === "results" && applyResult) {
    return (
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold tracking-tight">Bulk Repair — Results</h1>
          <Button variant="outline" size="sm" onClick={() => { setStep("scan"); setSelected(new Set()); setApplyResult(null); refetch(); }}>
            <RefreshCw className="h-4 w-4 mr-1" />Start new scan
          </Button>
        </div>
        <div className="grid grid-cols-3 gap-4">
          {[
            { label: "Queued",    value: applyResult.succeeded, color: "text-emerald-700 bg-emerald-50 border-emerald-200" },
            { label: "Failed",    value: applyResult.failed,    color: "text-red-700 bg-red-50 border-red-200" },
            { label: "Total",     value: applyResult.total,     color: "text-gray-700 bg-gray-50 border-gray-200" },
          ].map(c => (
            <div key={c.label} className={`rounded-xl border p-4 text-center ${c.color}`}>
              <div className="text-3xl font-bold">{c.value}</div>
              <div className="text-xs font-semibold uppercase tracking-wide mt-1">{c.label}</div>
            </div>
          ))}
        </div>
        <div className="rounded-xl border bg-white overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-gray-50/60">
                <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">University</th>
                <th className="px-4 py-2.5 text-center text-xs font-semibold text-gray-400 uppercase tracking-wider">Status</th>
                <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {applyResult.results.map(r => (
                <tr key={r.id} className={r.ok ? "" : "bg-red-50/30"}>
                  <td className="px-4 py-3 font-medium">{r.name ?? `#${r.id}`}</td>
                  <td className="px-4 py-3 text-center">
                    {r.ok
                      ? <CheckCircle2 className="h-4 w-4 text-emerald-600 mx-auto" />
                      : <XCircle className="h-4 w-4 text-red-500 mx-auto" />}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {r.ok
                      ? [
                          r.repair_count ? `Repair queued — ${r.repair_count} courses` : "No courses need repair",
                          r.marked_testing ? "→ Testing" : "",
                        ].filter(Boolean).join(", ")
                      : r.error}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex gap-2 text-sm text-gray-500 items-center">
          <Info className="h-4 w-4 flex-shrink-0" />
          Repair jobs are running in the background. Check the{" "}
          <Link href="/cert-dashboard" className="text-blue-600 hover:underline ml-1">Cert Dashboard</Link>
          {" "}to see updated scores.
        </div>
      </div>
    );
  }

  // ── Applying step ───────────────────────────────────────────────────────────
  if (step === "applying") {
    return (
      <div className="flex flex-col items-center gap-6 pt-16">
        <div className="h-16 w-16 rounded-full bg-blue-50 flex items-center justify-center">
          <Wrench className="h-8 w-8 text-blue-500 animate-pulse" />
        </div>
        <div className="text-center">
          <h2 className="text-xl font-semibold">Queuing repair jobs…</h2>
          <p className="text-sm text-gray-500 mt-1">Triggering repair for {selected.size} universities.</p>
        </div>
      </div>
    );
  }

  // ── Scan + Select steps ─────────────────────────────────────────────────────
  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Bulk Repair</h1>
          <p className="text-sm text-gray-500 mt-0.5">Find quality issues across universities, select affected ones, and queue repairs in one action.</p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant={viewMode === "history" ? "default" : "outline"}
            size="sm"
            onClick={() => setViewMode(viewMode === "history" ? "workflow" : "history")}
            className="gap-1.5"
          >
            <History className="h-4 w-4" />History
          </Button>
          {viewMode === "workflow" && (
            <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching} className="gap-1.5">
              <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />Refresh
            </Button>
          )}
        </div>
      </div>

      {/* History view */}
      {viewMode === "history" && <HistoryView />}

      {/* Workflow content — hidden when viewing history */}
      {viewMode === "workflow" && <>

      {/* Workflow steps hint */}
      <div className="flex items-center gap-2 text-xs text-gray-400 overflow-x-auto">
        {["Choose issue type", "Select universities", "Apply fixes", "Validate"].map((s, i, arr) => (
          <span key={s} className="flex items-center gap-2 whitespace-nowrap">
            <span className={`px-2 py-0.5 rounded-full ${i === 0 && step === "scan" || i === 1 && step === "select" ? "bg-blue-100 text-blue-700 font-medium" : ""}`}>{i + 1}. {s}</span>
            {i < arr.length - 1 && <ChevronRight className="h-3 w-3 flex-shrink-0" />}
          </span>
        ))}
      </div>

      {/* Issue type cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {(Object.keys(ISSUE_CFG) as IssueKey[]).map(key => {
          const cfg = ISSUE_CFG[key];
          const count = summary[key] ?? 0;
          const active = activeIssues.has(key);
          return (
            <button
              key={key}
              onClick={() => toggleIssue(key)}
              disabled={isLoading}
              className={`text-left p-4 rounded-xl border-2 transition-all ${cfg.bg} ${
                active
                  ? `${cfg.border} shadow-md ring-2 ring-offset-1 ${cfg.text}`
                  : "border-transparent hover:border-gray-200"
              }`}
            >
              <div className={`flex items-center gap-2 mb-2 ${cfg.text}`}>
                {cfg.icon}
                <span className="font-semibold text-sm">{cfg.label}</span>
              </div>
              <div className={`text-4xl font-bold mb-1 ${cfg.text}`}>
                {isLoading ? "—" : count}
              </div>
              <div className="text-xs text-gray-500 leading-snug">{cfg.desc}</div>
              {active && (
                <div className={`mt-2 text-xs font-medium ${cfg.text} flex items-center gap-1`}>
                  <CheckCircle2 className="h-3.5 w-3.5" /> Filtering by this issue
                </div>
              )}
            </button>
          );
        })}
      </div>

      {isError && (
        <div className="p-4 rounded-lg bg-red-50 border border-red-200 text-red-600 text-sm flex items-center gap-2">
          <XCircle className="h-4 w-4" />Failed to load scan data. <Button variant="link" size="sm" onClick={() => refetch()} className="text-red-600">Retry</Button>
        </div>
      )}

      {/* University table */}
      {(step === "select" || filtered.length > 0) && (
        <div className="space-y-3">
          {/* Table controls */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3">
            <div className="text-sm text-gray-600">
              {activeIssues.size > 0
                ? <><span className="font-medium">{filtered.length}</span> universities match {[...activeIssues].map(k => ISSUE_CFG[k].shortLabel).join(" + ")}</>
                : <><span className="font-medium">{filtered.length}</span> universities with quality issues</>}
            </div>
            <div className="flex-1" />
            <div className="relative w-full sm:w-56">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
              <Input placeholder="Search…" value={search} onChange={e => setSearch(e.target.value)} className="pl-8 h-8 text-sm" />
            </div>
          </div>

          {/* Action bar — above the table */}
          {selected.size > 0 && (
            <div className="bg-blue-50 border border-blue-200 rounded-xl px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-3">
              <div className="flex items-center gap-3 flex-1">
                <div className="h-8 w-8 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
                  <Wrench className="h-4 w-4 text-blue-600" />
                </div>
                <div>
                  <div className="font-semibold text-sm text-blue-900">{selected.size} {selected.size === 1 ? "university" : "universities"} selected</div>
                  <div className="text-xs text-blue-600">Queue repair scrape — backfills missing IELTS, fees, duration, location</div>
                </div>
              </div>
              <label className="flex items-center gap-2 cursor-pointer shrink-0">
                <input
                  type="checkbox"
                  checked={markTesting}
                  onChange={e => setMarkTesting(e.target.checked)}
                  className="h-4 w-4 rounded border-gray-300 accent-blue-600"
                />
                <span className="text-sm text-blue-800 flex items-center gap-1">
                  <FlaskConical className="h-3.5 w-3.5 text-blue-500" />
                  Move to Testing
                </span>
              </label>
              <div className="flex gap-2 shrink-0">
                <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())} className="h-8 text-blue-700 hover:bg-blue-100">
                  Clear
                </Button>
                <Button
                  size="sm"
                  className="h-8 gap-1.5 bg-blue-600 hover:bg-blue-700 text-white px-4"
                  onClick={handleApply}
                  disabled={applyMutation.isPending}
                >
                  <Wrench className="h-3.5 w-3.5" />
                  Apply Repair to {selected.size}{markTesting ? " + Testing" : ""}
                </Button>
              </div>
            </div>
          )}

          <div className="rounded-xl border bg-white overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-gray-50/60">
                    <th className="w-8 px-3 py-2.5">
                      <button onClick={toggleAll} className="text-gray-400 hover:text-gray-600">
                        {selected.size > 0 && selected.size === filtered.length
                          ? <CheckSquare className="h-4 w-4 text-blue-600" />
                          : <Square className="h-4 w-4" />}
                      </button>
                    </th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">University</th>
                    <th className="px-4 py-2.5 text-center text-xs font-semibold text-gray-400 uppercase tracking-wider">Score</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">IELTS</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Fees</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Issues</th>
                    <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Last Scrape</th>
                    <th className="px-4 py-2.5 text-right text-xs font-semibold text-gray-400 uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {isLoading ? (
                    Array.from({ length: 5 }).map((_, i) => (
                      <tr key={i} className="animate-pulse">
                        {Array.from({ length: 8 }).map((__, j) => (
                          <td key={j} className="px-4 py-3"><div className="h-4 bg-gray-100 rounded" /></td>
                        ))}
                      </tr>
                    ))
                  ) : filtered.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="px-4 py-12 text-center text-gray-400">
                        {activeIssues.size > 0 ? "No universities match the selected issue filters." : "No quality issues found — all universities look healthy!"}
                      </td>
                    </tr>
                  ) : (
                    filtered.map(uni => {
                      const isSelected = selected.has(uni.id);
                      return (
                        <tr
                          key={uni.id}
                          className={`transition-colors cursor-pointer group ${isSelected ? "bg-blue-50/40" : "hover:bg-gray-50/60"}`}
                          onClick={() => toggleOne(uni.id)}
                        >
                          <td className="px-3 py-3" onClick={e => { e.stopPropagation(); toggleOne(uni.id); }}>
                            {isSelected
                              ? <CheckSquare className="h-4 w-4 text-blue-600" />
                              : <Square className="h-4 w-4 text-gray-300 group-hover:text-gray-400" />}
                          </td>
                          <td className="px-4 py-3">
                            <div className="font-medium text-gray-900 max-w-[200px] truncate">{uni.name}</div>
                            {uni.country && <div className="text-xs text-gray-400">{uni.country}</div>}
                          </td>
                          <td className="px-4 py-3 text-center"><ScorePill score={uni.health_score} /></td>
                          <td className="px-4 py-3"><FillBar pct={uni.ielts_pct} threshold={thresholds.ielts} /></td>
                          <td className="px-4 py-3"><FillBar pct={uni.fee_pct} threshold={thresholds.fee} /></td>
                          <td className="px-4 py-3">
                            <div className="flex flex-wrap gap-1">
                              {uni.ielts_low     && <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs bg-purple-50 text-purple-700 border border-purple-200"><Languages className="h-3 w-3" />IELTS</span>}
                              {uni.fee_low       && <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs bg-amber-50  text-amber-700  border border-amber-200" ><DollarSign className="h-3 w-3" />Fees</span>}
                              {uni.discovery_low && <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs bg-red-50    text-red-700    border border-red-200"   ><TrendingUp className="h-3 w-3" />Discovery</span>}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-xs text-gray-500">{relativeTime(uni.last_scrape_at)}</td>
                          <td className="px-4 py-3" onClick={e => e.stopPropagation()}>
                            <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                              <Link href={`/universities/${uni.id}/scrape-agent`}>
                                <Button variant="outline" size="sm" className="h-7 px-2 text-xs border-blue-200 text-blue-700 hover:bg-blue-50 gap-1">
                                  <Bot className="h-3 w-3" />Agent
                                </Button>
                              </Link>
                              <Link href={`/universities/${uni.id}`}>
                                <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-gray-400">
                                  <ExternalLink className="h-3 w-3" />
                                </Button>
                              </Link>
                            </div>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Empty state when nothing selected */}
      {!isLoading && !isError && filtered.length === 0 && activeIssues.size === 0 && !data && (
        <div className="text-center py-16 text-gray-400">
          <AlertTriangle className="h-12 w-12 mx-auto mb-3 opacity-30" />
          <p>Click an issue card above to scan for affected universities.</p>
        </div>
      )}

      </>}

      {/* ── Bulk Repair Preview Modal ─────────────────────────────────────── */}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Wrench className="h-5 w-5 text-blue-600" />
              Confirm Bulk Repair
            </DialogTitle>
            <DialogDescription>
              Review what will be queued before confirming.
            </DialogDescription>
          </DialogHeader>

          {previewData && (
            <div className="space-y-4 py-1">
              {/* Summary row */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg bg-gray-50 border px-4 py-3 text-center">
                  <div className="text-2xl font-bold text-gray-900">{previewData.selected}</div>
                  <div className="text-xs text-gray-500 mt-0.5">Universities selected</div>
                </div>
                <div className="rounded-lg bg-blue-50 border border-blue-200 px-4 py-3 text-center">
                  <div className="text-2xl font-bold text-blue-700">{previewData.estimated_jobs}</div>
                  <div className="text-xs text-blue-500 mt-0.5">Scrape jobs to queue</div>
                </div>
              </div>

              {/* Issue breakdown — computed from scan data */}
              {(() => {
                const sel = data?.universities.filter(u => selected.has(u.id)) ?? [];
                const ieltsCount = sel.filter(u => u.ielts_low).length;
                const feeCount = sel.filter(u => u.fee_low).length;
                const discCount = sel.filter(u => u.discovery_low).length;
                return (
                  <div className="rounded-lg border divide-y text-sm">
                    <div className="px-4 py-2 font-medium text-gray-600 text-xs uppercase tracking-wide bg-gray-50">Expected fixes</div>
                    {ieltsCount > 0 && (
                      <div className="px-4 py-2.5 flex items-center justify-between">
                        <span className="flex items-center gap-2 text-purple-700"><Languages className="h-3.5 w-3.5" />IELTS quality issue</span>
                        <span className="font-semibold text-gray-900">{ieltsCount} {ieltsCount === 1 ? "university" : "universities"}</span>
                      </div>
                    )}
                    {feeCount > 0 && (
                      <div className="px-4 py-2.5 flex items-center justify-between">
                        <span className="flex items-center gap-2 text-amber-700"><DollarSign className="h-3.5 w-3.5" />Fee quality issue</span>
                        <span className="font-semibold text-gray-900">{feeCount} {feeCount === 1 ? "university" : "universities"}</span>
                      </div>
                    )}
                    {discCount > 0 && (
                      <div className="px-4 py-2.5 flex items-center justify-between">
                        <span className="flex items-center gap-2 text-red-700"><TrendingUp className="h-3.5 w-3.5" />Discovery score issue</span>
                        <span className="font-semibold text-gray-900">{discCount} {discCount === 1 ? "university" : "universities"}</span>
                      </div>
                    )}
                    {ieltsCount === 0 && feeCount === 0 && discCount === 0 && (
                      <div className="px-4 py-2.5 text-gray-400 text-xs">No specific issue filters active — all selected universities will be queued.</div>
                    )}
                  </div>
                );
              })()}

              {/* Risk signals */}
              {(previewData.risks.no_seed_url.length > 0 || previewData.risks.no_repair_targets.length > 0) && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 divide-y divide-amber-200 text-sm">
                  <div className="px-4 py-2 font-medium text-amber-700 text-xs uppercase tracking-wide flex items-center gap-1.5">
                    <ShieldAlert className="h-3.5 w-3.5" />Risk
                  </div>
                  {previewData.risks.no_seed_url.length > 0 && (
                    <div className="px-4 py-2.5">
                      <div className="font-medium text-amber-800">{previewData.risks.no_seed_url.length} {previewData.risks.no_seed_url.length === 1 ? "university has" : "universities have"} no seed URL</div>
                      <div className="text-xs text-amber-600 mt-0.5 truncate">{previewData.risks.no_seed_url.join(", ")}</div>
                    </div>
                  )}
                  {previewData.risks.no_repair_targets.length > 0 && (
                    <div className="px-4 py-2.5">
                      <div className="font-medium text-amber-800">{previewData.risks.no_repair_targets.length} {previewData.risks.no_repair_targets.length === 1 ? "university has" : "universities have"} no repair targets</div>
                      <div className="text-xs text-amber-600 mt-0.5">These will be skipped — no active courses with missing fields and a page URL.</div>
                    </div>
                  )}
                </div>
              )}

              {markTesting && (
                <div className="flex items-center gap-2 text-sm text-blue-700 bg-blue-50 border border-blue-200 rounded-lg px-4 py-2.5">
                  <FlaskConical className="h-4 w-4 shrink-0" />
                  All selected universities will also be moved to <strong>Testing</strong> status.
                </div>
              )}
            </div>
          )}

          <DialogFooter className="gap-2 pt-2">
            <Button variant="outline" onClick={() => setPreviewOpen(false)} disabled={applyMutation.isPending}>
              Cancel
            </Button>
            <Button
              className="bg-blue-600 hover:bg-blue-700 text-white gap-1.5"
              onClick={handleConfirmApply}
              disabled={applyMutation.isPending || (previewData?.estimated_jobs === 0)}
            >
              <Wrench className="h-4 w-4" />
              {applyMutation.isPending ? "Queuing…" : `Confirm — Queue ${previewData?.estimated_jobs ?? 0} ${(previewData?.estimated_jobs ?? 0) === 1 ? "job" : "jobs"}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

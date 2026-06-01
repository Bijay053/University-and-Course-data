import { useState, useMemo } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  Languages, DollarSign, TrendingUp, Search, CheckSquare, Square,
  RefreshCw, Wrench, FlaskConical, ChevronRight, AlertTriangle,
  CheckCircle2, XCircle, ExternalLink, Bot, Info,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
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

// ── Main page ─────────────────────────────────────────────────────────────────
type Step = "scan" | "select" | "applying" | "results";

export default function BulkRepairPage() {
  const { toast } = useToast();
  const [step, setStep] = useState<Step>("scan");
  const [activeIssues, setActiveIssues] = useState<Set<IssueKey>>(new Set());
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [markTesting, setMarkTesting] = useState(false);
  const [search, setSearch] = useState("");
  const [applyResult, setApplyResult] = useState<ApplyResult | null>(null);

  const { data, isLoading, isError, refetch, isFetching } = useQuery<ScanData>({
    queryKey: ["bulk-repair-scan"],
    queryFn: () => apiFetch("/api/bulk-repair/scan"),
    staleTime: 120_000,
  });

  const applyMutation = useMutation({
    mutationFn: (body: { university_ids: number[]; repair_scrape: boolean; mark_testing: boolean }) =>
      apiFetch("/api/bulk-repair/apply", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (res: ApplyResult) => {
      setApplyResult(res);
      setStep("results");
    },
    onError: () => {
      toast({ title: "Apply failed", variant: "destructive" });
      setStep("select");
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
      <div className="p-6 max-w-3xl mx-auto space-y-6">
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
      <div className="p-6 max-w-xl mx-auto flex flex-col items-center gap-6 pt-20">
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
    <div className="p-6 max-w-screen-xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Bulk Repair</h1>
          <p className="text-sm text-gray-500 mt-0.5">Find quality issues across universities, select affected ones, and queue repairs in one action.</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isFetching} className="gap-1.5">
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />Refresh
        </Button>
      </div>

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

      {/* Sticky action bar */}
      {selected.size > 0 && (
        <div className="sticky bottom-4 z-20">
          <div className="bg-white border border-gray-200 rounded-2xl shadow-xl px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-4">
            <div className="flex items-center gap-3 flex-1">
              <div className="h-9 w-9 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
                <Wrench className="h-4 w-4 text-blue-600" />
              </div>
              <div>
                <div className="font-semibold text-sm">{selected.size} {selected.size === 1 ? "university" : "universities"} selected</div>
                <div className="text-xs text-gray-500">Queue repair scrape for each — backfills missing IELTS, fees, duration, location</div>
              </div>
            </div>

            <label className="flex items-center gap-2 cursor-pointer shrink-0">
              <input
                type="checkbox"
                checked={markTesting}
                onChange={e => setMarkTesting(e.target.checked)}
                className="h-4 w-4 rounded border-gray-300 accent-blue-600"
              />
              <span className="text-sm text-gray-600 flex items-center gap-1">
                <FlaskConical className="h-3.5 w-3.5 text-blue-500" />
                Move to Testing
              </span>
            </label>

            <div className="flex gap-2">
              <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())} className="h-9">
                Clear
              </Button>
              <Button
                size="sm"
                className="h-9 gap-1.5 bg-blue-600 hover:bg-blue-700 text-white px-5"
                onClick={handleApply}
                disabled={applyMutation.isPending}
              >
                <Wrench className="h-4 w-4" />
                Apply Repair to {selected.size}
                {markTesting && " + Testing"}
              </Button>
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
    </div>
  );
}

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Send, CheckCircle2, AlertTriangle, Clock, Zap, BarChart3,
  RefreshCw, ThumbsUp, ThumbsDown, Pause, FileText, TrendingUp,
  ChevronDown, ChevronUp, Info, Play,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useToast } from "@/hooks/use-toast";

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

interface PubStats {
  ready_to_publish: number;
  needs_review: number;
  held: number;
  unscored: number;
  universities_with_ready: number;
  total_auto_published: number;
  total_manually_published: number;
  total_rejected: number;
  total_held: number;
  auto_publish_rate: number | null;
  published_today: number;
}

interface ReviewItem {
  id: number;
  university_id: number;
  university_name: string;
  university_country: string;
  course_name: string;
  degree_level: string | null;
  pub_score: number | null;
  pub_decision: string | null;
  pub_decision_reason: string | null;
  pub_score_breakdown: {
    completeness: number;
    confidence: number;
    open_conflicts: number;
    critical_conflicts: number;
    conflict_penalty: number;
  } | null;
  completeness: number | null;
  avg_verification_confidence: number | null;
  eligibility_confidence: number | null;
  open_conflicts: number;
  critical_conflicts: number;
  international_fee: number | null;
  ielts_overall: number | null;
  status: string;
  auto_publish_status: string;
  created_at: string | null;
}

interface LedgerEntry {
  id: number;
  scraped_course_id: number | null;
  university_id: number;
  university_name: string;
  course_name: string;
  action: string;
  pub_score: number | null;
  pub_score_breakdown: Record<string, number> | null;
  actor: string;
  reason: string | null;
  created_at: string | null;
}

function ScoreBar({ score }: { score: number | null }) {
  if (score === null) return <span className="text-xs text-muted-foreground">—</span>;
  const pct = Math.min(100, Math.max(0, score));
  const color = score >= 90 ? "bg-emerald-500" : score >= 70 ? "bg-amber-400" : "bg-rose-400";
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-1.5 bg-muted rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-xs font-bold tabular-nums ${score >= 90 ? "text-emerald-700" : score >= 70 ? "text-amber-700" : "text-rose-700"}`}>
        {score}
      </span>
    </div>
  );
}

function DecisionBadge({ decision }: { decision: string | null }) {
  if (!decision) return <Badge variant="outline" className="text-xs">Unscored</Badge>;
  if (decision === "auto_publish") return <Badge className="bg-emerald-100 text-emerald-800 text-xs">Auto Publish</Badge>;
  if (decision === "needs_review") return <Badge className="bg-amber-100 text-amber-800 text-xs">Needs Review</Badge>;
  if (decision === "hold") return <Badge className="bg-rose-100 text-rose-800 text-xs">Hold</Badge>;
  return <Badge variant="outline" className="text-xs">{decision}</Badge>;
}

function ActionBadge({ action }: { action: string }) {
  const cfg: Record<string, string> = {
    auto_published: "bg-emerald-100 text-emerald-800",
    manually_published: "bg-blue-100 text-blue-800",
    queued_review: "bg-amber-100 text-amber-800",
    rejected: "bg-red-100 text-red-800",
    held: "bg-slate-100 text-slate-700",
  };
  const label: Record<string, string> = {
    auto_published: "Auto Published",
    manually_published: "Manually Published",
    queued_review: "Queued Review",
    rejected: "Rejected",
    held: "Held",
  };
  return (
    <Badge className={`text-xs ${cfg[action] ?? "bg-slate-100 text-slate-700"}`}>
      {label[action] ?? action}
    </Badge>
  );
}

function StatCard({ label, value, sub, icon: Icon, color }: {
  label: string; value: string | number; sub?: string;
  icon: typeof Send; color: string;
}) {
  return (
    <div className="bg-white border rounded-lg p-4 flex items-start gap-3">
      <div className={`p-2 rounded-md ${color}`}>
        <Icon className="h-4 w-4" />
      </div>
      <div>
        <div className="text-2xl font-bold leading-none">{value}</div>
        <div className="text-xs text-muted-foreground mt-0.5">{label}</div>
        {sub && <div className="text-xs font-medium text-muted-foreground mt-0.5">{sub}</div>}
      </div>
    </div>
  );
}

function BreakdownTooltip({ bd }: { bd: ReviewItem["pub_score_breakdown"] }) {
  if (!bd) return null;
  return (
    <div className="text-xs text-muted-foreground space-y-0.5 mt-1">
      <div>Completeness: <span className="font-medium">{bd.completeness}%</span></div>
      <div>Confidence: <span className="font-medium">{bd.confidence}%</span></div>
      {bd.open_conflicts > 0 && (
        <div className="text-rose-600">
          Conflicts: {bd.critical_conflicts} critical, {bd.open_conflicts - bd.critical_conflicts} open
        </div>
      )}
    </div>
  );
}

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffH = (now.getTime() - d.getTime()) / 3600000;
  if (diffH < 1) return `${Math.floor(diffH * 60)}m ago`;
  if (diffH < 24) return `${Math.floor(diffH)}h ago`;
  if (diffH < 168) return `${Math.floor(diffH / 24)}d ago`;
  return d.toLocaleDateString();
}

export default function PublishingPage() {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"queue" | "ledger">("queue");
  const [decisionFilter, setDecisionFilter] = useState<string>("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [actionReason, setActionReason] = useState<Record<number, string>>({});

  const { data: stats, isLoading: statsLoading } = useQuery<PubStats>({
    queryKey: ["pub-stats"],
    queryFn: () => apiFetch("/api/publishing/stats"),
    refetchInterval: 15000,
  });

  const { data: queue = [], isLoading: queueLoading, refetch: refetchQueue } = useQuery<ReviewItem[]>({
    queryKey: ["pub-queue", decisionFilter],
    queryFn: () => apiFetch(
      `/api/publishing/review-queue?limit=100${decisionFilter !== "all" ? `&decision=${decisionFilter}` : ""}`
    ),
    refetchInterval: 15000,
  });

  const { data: ledger = [], isLoading: ledgerLoading } = useQuery<LedgerEntry[]>({
    queryKey: ["pub-ledger"],
    queryFn: () => apiFetch("/api/publishing/ledger?limit=200"),
    enabled: tab === "ledger",
    refetchInterval: 30000,
  });

  const runPassMut = useMutation({
    mutationFn: () => apiFetch("/api/publishing/run", { method: "POST", body: JSON.stringify({}) }),
    onSuccess: (d) => {
      toast({
        title: "Publishing pass complete",
        description: `Scored ${d.scored} · Auto-published ${d.auto_published} · Review ${d.needs_review} · Held ${d.held}`,
      });
      qc.invalidateQueries({ queryKey: ["pub-stats"] });
      qc.invalidateQueries({ queryKey: ["pub-queue"] });
      qc.invalidateQueries({ queryKey: ["pub-ledger"] });
    },
    onError: (e) => toast({ title: "Run failed", description: String(e), variant: "destructive" }),
  });

  function itemMutation(action: "approve" | "reject" | "hold") {
    return useMutation({
      mutationFn: ({ id, reason }: { id: number; reason: string }) =>
        apiFetch(`/api/publishing/review/${id}/${action}`, {
          method: "POST",
          body: JSON.stringify({ reason }),
        }),
      onSuccess: (_d, { id }) => {
        const labels = { approve: "Approved ✓", reject: "Rejected", hold: "Held" };
        toast({ title: labels[action], description: `Course action recorded` });
        qc.invalidateQueries({ queryKey: ["pub-stats"] });
        qc.invalidateQueries({ queryKey: ["pub-queue"] });
        qc.invalidateQueries({ queryKey: ["pub-ledger"] });
        setActionReason(r => { const n = { ...r }; delete n[id]; return n; });
      },
      onError: (e) => toast({ title: "Action failed", description: String(e), variant: "destructive" }),
    });
  }

  const approveMut = itemMutation("approve");
  const rejectMut = itemMutation("reject");
  const holdMut = itemMutation("hold");

  function toggleExpand(id: number) {
    setExpanded(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  }

  const queueByDecision = {
    all: queue.length,
    auto_publish: queue.filter(r => r.pub_decision === "auto_publish").length,
    needs_review: queue.filter(r => r.pub_decision === "needs_review").length,
    hold: queue.filter(r => r.pub_decision === "hold").length,
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Send className="h-6 w-6 text-indigo-600" />
            Autonomous Publishing
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            AI-scored confidence engine — auto-publishes high-confidence courses, queues the rest for human review
          </p>
        </div>
        <div className="flex flex-wrap gap-2 shrink-0">
          <Button
            variant="outline" size="sm"
            onClick={() => {
              qc.invalidateQueries({ queryKey: ["pub-stats"] });
              qc.invalidateQueries({ queryKey: ["pub-queue"] });
              qc.invalidateQueries({ queryKey: ["pub-ledger"] });
              toast({ title: "Refreshed", description: "Stats and queue updated." });
            }}
          >
            <RefreshCw className="h-4 w-4 mr-1" /> Refresh
          </Button>
          <Button
            size="sm"
            className="bg-indigo-600 hover:bg-indigo-700 text-white"
            onClick={() => runPassMut.mutate()}
            disabled={runPassMut.isPending}
          >
            <Play className="h-4 w-4 mr-1" />
            {runPassMut.isPending ? "Running…" : "Run Publishing Pass"}
          </Button>
        </div>
      </div>

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <StatCard label="Ready to Publish" value={stats.ready_to_publish} icon={CheckCircle2} color="bg-emerald-100 text-emerald-600" />
          <StatCard label="Needs Review" value={stats.needs_review} icon={AlertTriangle} color="bg-amber-100 text-amber-600" />
          <StatCard label="Held" value={stats.held} icon={Pause} color="bg-rose-100 text-rose-600" />
          <StatCard label="Unscored" value={stats.unscored} sub="run a pass" icon={Clock} color="bg-slate-100 text-slate-600" />
          <StatCard
            label="Auto Publish Rate"
            value={stats.auto_publish_rate != null ? `${stats.auto_publish_rate}%` : "—"}
            sub="all time"
            icon={TrendingUp}
            color="bg-indigo-100 text-indigo-600"
          />
          <StatCard label="Published Today" value={stats.published_today} icon={Zap} color="bg-blue-100 text-blue-600" />
        </div>
      )}

      {/* How it works */}
      <div className="bg-indigo-50 border border-indigo-200 rounded-lg p-4">
        <div className="flex items-start gap-3">
          <Info className="h-5 w-5 text-indigo-600 mt-0.5 flex-shrink-0" />
          <div>
            <p className="text-sm font-semibold text-indigo-900">Publishing Confidence Score</p>
            <div className="text-xs text-indigo-700 mt-1 flex flex-wrap gap-x-6 gap-y-1">
              <span><strong>45%</strong> Completeness (13 review fields)</span>
              <span><strong>45%</strong> Verification Confidence</span>
              <span><strong>10%</strong> Conflict-free bonus</span>
              <span className="text-indigo-700">−15 pts per critical conflict · −3 pts per open conflict (max −30)</span>
            </div>
            <div className="text-xs text-indigo-700 mt-2 flex flex-wrap gap-x-6 gap-y-1">
              <span className="text-emerald-700 font-medium">Score ≥ 90 + 0 critical conflicts → <strong>Auto Publish</strong></span>
              <span className="text-amber-700 font-medium">70–89 → <strong>Needs Review</strong></span>
              <span className="text-rose-700 font-medium">&lt; 70 → <strong>Hold</strong></span>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-muted rounded-md p-1 w-fit">
        {([["queue", "Review Queue"], ["ledger", "Publishing Ledger"]] as const).map(([t, label]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-1.5 text-sm rounded transition-colors ${
              tab === t ? "bg-white shadow-sm font-medium" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {label}
            {t === "queue" && queue.length > 0 && (
              <span className="ml-2 inline-flex items-center justify-center min-w-[18px] h-[18px] rounded-full bg-indigo-600 text-white text-[10px] font-bold px-1">
                {queue.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Review Queue */}
      {tab === "queue" && (
        <div className="space-y-4">
          {/* Decision filter sub-tabs */}
          <div className="flex gap-1 flex-wrap">
            {(["all", "needs_review", "hold", "auto_publish"] as const).map(f => (
              <button
                key={f}
                onClick={() => setDecisionFilter(f)}
                className={`px-3 py-1 text-xs rounded-full border transition-colors whitespace-nowrap ${
                  decisionFilter === f
                    ? "bg-indigo-600 text-white border-indigo-600"
                    : "bg-white text-slate-600 border-slate-200 hover:border-indigo-300"
                }`}
              >
                {f === "all" && `All (${queueByDecision.all})`}
                {f === "needs_review" && `Needs Review (${queueByDecision.needs_review})`}
                {f === "hold" && `Hold (${queueByDecision.hold})`}
                {f === "auto_publish" && `Ready (${queueByDecision.auto_publish})`}
              </button>
            ))}
          </div>

          {queueLoading ? (
            <div className="text-center py-12 text-muted-foreground text-sm">Loading review queue…</div>
          ) : queue.length === 0 ? (
            <div className="bg-white border rounded-lg p-12 text-center">
              <CheckCircle2 className="h-12 w-12 mx-auto mb-3 text-emerald-400" />
              <p className="font-medium text-muted-foreground">No courses in queue</p>
              <p className="text-sm text-muted-foreground mt-1">
                Run a <strong>Publishing Pass</strong> to score pending staged courses.
              </p>
            </div>
          ) : (
            <div className="bg-white border rounded-lg overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-muted/50 border-b">
                    <tr>
                      <th className="text-left px-4 py-3 font-medium">Course</th>
                      <th className="text-left px-4 py-3 font-medium">Score</th>
                      <th className="text-left px-4 py-3 font-medium">Decision</th>
                      <th className="text-left px-4 py-3 font-medium">Fee / IELTS</th>
                      <th className="text-left px-4 py-3 font-medium">Conflicts</th>
                      <th className="text-right px-4 py-3 font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {queue.map(item => (
                      <>
                        <tr
                          key={item.id}
                          className={`hover:bg-muted/20 transition-colors cursor-pointer ${
                            item.pub_decision === "hold" ? "border-l-2 border-l-rose-400" :
                            item.pub_decision === "needs_review" ? "border-l-2 border-l-amber-400" :
                            item.pub_decision === "auto_publish" ? "border-l-2 border-l-emerald-400" : ""
                          }`}
                          onClick={() => toggleExpand(item.id)}
                        >
                          <td className="px-4 py-3">
                            <div className="font-medium text-sm leading-tight">{item.course_name}</div>
                            <div className="text-xs text-muted-foreground">{item.university_name} · {item.university_country}</div>
                            {item.degree_level && (
                              <div className="text-xs text-muted-foreground/70">{item.degree_level}</div>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            <ScoreBar score={item.pub_score} />
                            <BreakdownTooltip bd={item.pub_score_breakdown} />
                          </td>
                          <td className="px-4 py-3">
                            <DecisionBadge decision={item.pub_decision} />
                            {item.pub_decision_reason && (
                              <div className="text-xs text-muted-foreground mt-1 max-w-[180px] leading-tight">
                                {item.pub_decision_reason}
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-3 text-xs">
                            {item.international_fee != null ? (
                              <div className="text-slate-700">
                                ${item.international_fee.toLocaleString()} / yr
                              </div>
                            ) : <span className="text-muted-foreground">No fee</span>}
                            {item.ielts_overall != null && (
                              <div className="text-muted-foreground mt-0.5">IELTS {item.ielts_overall}</div>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            {item.critical_conflicts > 0 ? (
                              <div className="flex items-center gap-1 text-rose-700">
                                <AlertTriangle className="h-3 w-3" />
                                <span className="text-xs font-medium">{item.critical_conflicts} critical</span>
                              </div>
                            ) : item.open_conflicts > 0 ? (
                              <div className="flex items-center gap-1 text-amber-700">
                                <AlertTriangle className="h-3 w-3" />
                                <span className="text-xs">{item.open_conflicts} open</span>
                              </div>
                            ) : (
                              <div className="flex items-center gap-1 text-emerald-600">
                                <CheckCircle2 className="h-3 w-3" />
                                <span className="text-xs">None</span>
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-3" onClick={e => e.stopPropagation()}>
                            <div className="flex items-center justify-end gap-1">
                              <Button
                                size="sm" variant="ghost"
                                className="h-7 px-2 text-xs text-emerald-700 hover:bg-emerald-50"
                                onClick={() => approveMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual approval" })}
                                disabled={approveMut.isPending}
                                title="Approve & publish"
                              >
                                <ThumbsUp className="h-3 w-3" />
                              </Button>
                              <Button
                                size="sm" variant="ghost"
                                className="h-7 px-2 text-xs text-slate-500 hover:bg-slate-50"
                                onClick={() => holdMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual hold" })}
                                disabled={holdMut.isPending}
                                title="Hold"
                              >
                                <Pause className="h-3 w-3" />
                              </Button>
                              <Button
                                size="sm" variant="ghost"
                                className="h-7 px-2 text-xs text-rose-700 hover:bg-rose-50"
                                onClick={() => rejectMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual rejection" })}
                                disabled={rejectMut.isPending}
                                title="Reject"
                              >
                                <ThumbsDown className="h-3 w-3" />
                              </Button>
                              {expanded.has(item.id) ? (
                                <ChevronUp className="h-3 w-3 text-muted-foreground ml-1" />
                              ) : (
                                <ChevronDown className="h-3 w-3 text-muted-foreground ml-1" />
                              )}
                            </div>
                          </td>
                        </tr>
                        {expanded.has(item.id) && (
                          <tr key={`${item.id}-detail`} className="bg-slate-50/60">
                            <td colSpan={6} className="px-4 py-3">
                              <div className="flex flex-wrap gap-6">
                                <div>
                                  <div className="text-xs font-semibold text-muted-foreground mb-1">Score Breakdown</div>
                                  {item.pub_score_breakdown ? (
                                    <div className="text-xs space-y-1">
                                      <div className="flex gap-3">
                                        <span className="text-muted-foreground w-24">Completeness</span>
                                        <span className="font-medium">{item.pub_score_breakdown.completeness}%</span>
                                        <div className="w-20 h-1.5 bg-muted rounded-full self-center overflow-hidden">
                                          <div className="h-full bg-indigo-400 rounded-full" style={{ width: `${item.pub_score_breakdown.completeness}%` }} />
                                        </div>
                                      </div>
                                      <div className="flex gap-3">
                                        <span className="text-muted-foreground w-24">Confidence</span>
                                        <span className="font-medium">{item.pub_score_breakdown.confidence}%</span>
                                        <div className="w-20 h-1.5 bg-muted rounded-full self-center overflow-hidden">
                                          <div className="h-full bg-indigo-400 rounded-full" style={{ width: `${item.pub_score_breakdown.confidence}%` }} />
                                        </div>
                                      </div>
                                      <div className="flex gap-3">
                                        <span className="text-muted-foreground w-24">Conflict penalty</span>
                                        <span className={`font-medium ${item.pub_score_breakdown.conflict_penalty > 0 ? "text-rose-600" : "text-emerald-600"}`}>
                                          {item.pub_score_breakdown.conflict_penalty > 0 ? `-${item.pub_score_breakdown.conflict_penalty}` : "None"}
                                        </span>
                                      </div>
                                    </div>
                                  ) : <span className="text-xs text-muted-foreground">Not yet scored</span>}
                                </div>
                                <div>
                                  <div className="text-xs font-semibold text-muted-foreground mb-1">Add Reason (optional)</div>
                                  <div className="flex gap-2 items-center">
                                    <Input
                                      className="h-7 text-xs w-48"
                                      placeholder="Reason for action…"
                                      value={actionReason[item.id] || ""}
                                      onChange={e => setActionReason(r => ({ ...r, [item.id]: e.target.value }))}
                                      onClick={e => e.stopPropagation()}
                                    />
                                    <div className="flex gap-1">
                                      <Button
                                        size="sm"
                                        className="h-7 px-3 text-xs bg-emerald-600 hover:bg-emerald-700 text-white"
                                        onClick={e => { e.stopPropagation(); approveMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual approval" }); }}
                                      >
                                        Approve
                                      </Button>
                                      <Button
                                        size="sm" variant="outline"
                                        className="h-7 px-3 text-xs"
                                        onClick={e => { e.stopPropagation(); holdMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual hold" }); }}
                                      >
                                        Hold
                                      </Button>
                                      <Button
                                        size="sm" variant="outline"
                                        className="h-7 px-3 text-xs text-rose-600 border-rose-200 hover:bg-rose-50"
                                        onClick={e => { e.stopPropagation(); rejectMut.mutate({ id: item.id, reason: actionReason[item.id] || "Manual rejection" }); }}
                                      >
                                        Reject
                                      </Button>
                                    </div>
                                  </div>
                                </div>
                              </div>
                            </td>
                          </tr>
                        )}
                      </>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Publishing Ledger */}
      {tab === "ledger" && (
        <>
          {ledgerLoading ? (
            <div className="text-center py-12 text-muted-foreground text-sm">Loading ledger…</div>
          ) : ledger.length === 0 ? (
            <div className="bg-white border rounded-lg p-12 text-center">
              <FileText className="h-12 w-12 mx-auto mb-3 text-muted-foreground/30" />
              <p className="font-medium text-muted-foreground">No ledger entries yet</p>
              <p className="text-sm text-muted-foreground mt-1">Entries appear after running a Publishing Pass.</p>
            </div>
          ) : (
            <div className="bg-white border rounded-lg overflow-hidden">
              {/* Summary row */}
              {stats && (
                <div className="px-4 py-3 border-b bg-muted/30 flex flex-wrap gap-6 text-xs text-muted-foreground">
                  <span>Auto published: <strong className="text-emerald-700">{stats.total_auto_published}</strong></span>
                  <span>Manually published: <strong className="text-blue-700">{stats.total_manually_published}</strong></span>
                  <span>Rejected: <strong className="text-rose-700">{stats.total_rejected}</strong></span>
                  <span>Held: <strong className="text-slate-700">{stats.total_held}</strong></span>
                  {stats.auto_publish_rate != null && (
                    <span>Auto rate: <strong className="text-indigo-700">{stats.auto_publish_rate}%</strong></span>
                  )}
                </div>
              )}
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-muted/50 border-b">
                    <tr>
                      <th className="text-left px-4 py-3 font-medium">Course</th>
                      <th className="text-left px-4 py-3 font-medium">Action</th>
                      <th className="text-left px-4 py-3 font-medium">Score</th>
                      <th className="text-left px-4 py-3 font-medium">Actor</th>
                      <th className="text-left px-4 py-3 font-medium">Reason</th>
                      <th className="text-left px-4 py-3 font-medium">When</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {ledger.map(entry => (
                      <tr key={entry.id} className="hover:bg-muted/20 transition-colors">
                        <td className="px-4 py-3">
                          <div className="font-medium text-sm leading-tight">{entry.course_name}</div>
                          <div className="text-xs text-muted-foreground">{entry.university_name}</div>
                        </td>
                        <td className="px-4 py-3">
                          <ActionBadge action={entry.action} />
                        </td>
                        <td className="px-4 py-3">
                          <ScoreBar score={entry.pub_score} />
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="outline" className="text-xs">
                            {entry.actor === "system" ? "🤖 System" : "👤 Human"}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground max-w-[200px] truncate" title={entry.reason || ""}>
                          {entry.reason || "—"}
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                          {fmtDate(entry.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

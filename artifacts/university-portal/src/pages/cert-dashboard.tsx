import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ShieldCheck, ShieldX, FlaskConical, FileEdit, AlertTriangle,
  RefreshCw, ExternalLink, TrendingDown, Bot, Clock, ChevronDown, ChevronUp,
  Search, Wrench, User, CheckCircle2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { SettingsTabs } from "@/components/settings-tabs";
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
type CertStatus = "certified" | "testing" | "needs_review" | "failed" | "draft";

interface UniRow {
  id: number;
  name: string;
  country: string | null;
  scrape_url: string | null;
  certification_status: CertStatus;
  last_certified_score: number | null;
  last_certified_at: string | null;
  current_health_score: number;
  score_drop: number | null;
  last_scrape_at: string | null;
  staged_courses: number;
  total_found: number;
}

interface DashboardData {
  summary: Record<CertStatus, number>;
  universities: UniRow[];
}

// ── Status config ────────────────────────────────────────────────────────────
const CERT_CFG: Record<CertStatus, {
  label: string; bg: string; text: string; border: string;
  cardBg: string; cardBorder: string; icon: React.ReactNode;
}> = {
  certified:    { label: "Certified",    bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200", cardBg: "bg-emerald-50", cardBorder: "border-emerald-200", icon: <ShieldCheck className="h-5 w-5 text-emerald-600" /> },
  testing:      { label: "Testing",      bg: "bg-blue-50",    text: "text-blue-700",    border: "border-blue-200",    cardBg: "bg-blue-50",    cardBorder: "border-blue-200",    icon: <FlaskConical className="h-5 w-5 text-blue-600" /> },
  needs_review: { label: "Needs Review", bg: "bg-amber-50",   text: "text-amber-700",   border: "border-amber-200",   cardBg: "bg-amber-50",   cardBorder: "border-amber-200",   icon: <AlertTriangle className="h-5 w-5 text-amber-600" /> },
  failed:       { label: "Failed",       bg: "bg-red-50",     text: "text-red-700",     border: "border-red-200",     cardBg: "bg-red-50",     cardBorder: "border-red-200",     icon: <ShieldX className="h-5 w-5 text-red-600" /> },
  draft:        { label: "Draft",        bg: "bg-gray-50",    text: "text-gray-500",    border: "border-gray-200",    cardBg: "bg-gray-50",    cardBorder: "border-gray-200",    icon: <FileEdit className="h-5 w-5 text-gray-400" /> },
};

const STATUS_ORDER: CertStatus[] = ["certified", "needs_review", "failed", "testing", "draft"];

function CertBadge({ status }: { status: CertStatus | string }) {
  const cfg = CERT_CFG[(status as CertStatus)] ?? CERT_CFG.draft;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold border ${cfg.bg} ${cfg.text} ${cfg.border}`}>
      {cfg.icon} {cfg.label}
    </span>
  );
}

function ScorePill({ score }: { score: number }) {
  const color =
    score >= 80 ? "bg-emerald-100 text-emerald-800" :
    score >= 60 ? "bg-blue-100 text-blue-800" :
    score >= 40 ? "bg-amber-100 text-amber-800" :
    "bg-red-100 text-red-800";
  return (
    <span className={`inline-flex items-center justify-center w-10 h-10 rounded-full font-bold text-sm ${color}`}>
      {score}
    </span>
  );
}

function DropBadge({ drop }: { drop: number | null }) {
  if (drop === null) return <span className="text-xs text-gray-300">—</span>;
  if (drop <= 0) return <span className="text-xs text-emerald-600 font-medium">+{Math.abs(drop)}</span>;
  if (drop <= 10) return <span className="text-xs text-amber-600 font-semibold">−{drop}</span>;
  return (
    <span className="inline-flex items-center gap-0.5 text-xs text-red-600 font-bold">
      <TrendingDown className="h-3 w-3" />−{drop}
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

// ── Sortable table header ────────────────────────────────────────────────────
type SortKey = "name" | "current_health_score" | "score_drop" | "staged_courses" | "last_scrape_at";

function SortTh({
  label, sortKey, current, dir, onSort,
}: {
  label: string; sortKey: SortKey;
  current: SortKey; dir: "asc" | "desc";
  onSort: (k: SortKey) => void;
}) {
  const active = current === sortKey;
  return (
    <th
      className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider cursor-pointer select-none hover:text-gray-600"
      onClick={() => onSort(sortKey)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {active ? (dir === "asc" ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />) : null}
      </span>
    </th>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────
export default function CertDashboardPage() {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"all" | "queue">("all");
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [filterStatus, setFilterStatus] = useState<CertStatus | "all">("all");
  const [pageSize, setPageSize] = useState<number>(25);
  const [page, setPage] = useState<number>(1);

  const { data, isLoading, isError, refetch, isFetching } = useQuery<DashboardData>({
    queryKey: ["cert-dashboard"],
    queryFn: () => apiFetch("/api/universities/cert-dashboard"),
    staleTime: 60_000,
  });

  const { data: historyData } = useQuery<{ history: Array<{
    id: number; created_at: string; triggered_by_name: string | null;
    triggered_by_email: string; selected_count: number; queued_count: number;
    skipped_count: number; failed_count: number; mark_testing: boolean;
    university_names: string[];
  }> }>({
    queryKey: ["bulk-repair-history-dash"],
    queryFn: () => apiFetch("/api/bulk-repair/history?limit=5"),
    staleTime: 60_000,
  });

  const patchMutation = useMutation({
    mutationFn: ({ uniId, status }: { uniId: number; status: string }) =>
      apiFetch(`/api/universities/${uniId}/certification-status`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cert-dashboard"] });
      toast({ title: "Status updated" });
    },
    onError: () => toast({ title: "Failed to update status", variant: "destructive" }),
  });

  function handleSort(key: SortKey) {
    if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortKey(key); setSortDir(key === "score_drop" ? "desc" : "asc"); }
    setPage(1);
  }

  function handleSearch(v: string) { setSearch(v); setPage(1); }
  function handleTab(v: "all" | "queue") { setTab(v); setPage(1); }
  function handleFilter(v: CertStatus | "all") { setFilterStatus(v); setPage(1); }
  function handlePageSize(v: number) { setPageSize(v); setPage(1); }

  const universities = useMemo(() => {
    if (!data) return [];
    let rows = data.universities;

    if (tab === "queue") {
      rows = rows.filter(r => r.score_drop !== null && r.score_drop > 0);
    }
    if (filterStatus !== "all") {
      rows = rows.filter(r => r.certification_status === filterStatus);
    }
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(r => r.name.toLowerCase().includes(q) || (r.country ?? "").toLowerCase().includes(q));
    }

    return [...rows].sort((a, b) => {
      let av: number | string, bv: number | string;
      switch (sortKey) {
        case "name":               av = a.name; bv = b.name; break;
        case "current_health_score": av = a.current_health_score; bv = b.current_health_score; break;
        case "score_drop":         av = a.score_drop ?? -999; bv = b.score_drop ?? -999; break;
        case "staged_courses":     av = a.staged_courses; bv = b.staged_courses; break;
        case "last_scrape_at":     av = a.last_scrape_at ?? ""; bv = b.last_scrape_at ?? ""; break;
        default:                   av = a.name; bv = b.name;
      }
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
  }, [data, tab, filterStatus, search, sortKey, sortDir]);

  const totalFiltered = universities.length;
  const totalPages = Math.max(1, Math.ceil(totalFiltered / pageSize));
  const safePage = Math.min(page, totalPages);
  const pagedUniversities = universities.slice((safePage - 1) * pageSize, safePage * pageSize);

  const summary = data?.summary ?? { certified: 0, testing: 0, needs_review: 0, failed: 0, draft: 0 };
  const total = Object.values(summary).reduce((s, n) => s + n, 0);
  const queueCount = (data?.universities ?? []).filter(r => r.score_drop !== null && r.score_drop > 0).length;

  if (isError) return (
    <div className="p-8 text-center text-red-500">
      Failed to load dashboard. <Button variant="outline" size="sm" onClick={() => refetch()}>Retry</Button>
    </div>
  );

  return (
    <div className="p-6 max-w-screen-2xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Certification Dashboard</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {total} universities — live cert status and quality scores
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          className="gap-1.5"
        >
          <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      <SettingsTabs />

      {/* Summary Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {STATUS_ORDER.map(st => {
          const cfg = CERT_CFG[st];
          const count = summary[st] ?? 0;
          const isFiltered = filterStatus === st;
          return (
            <button
              key={st}
              onClick={() => handleFilter(isFiltered ? "all" : st)}
              className={`text-left p-4 rounded-xl border-2 transition-all ${cfg.cardBg} ${
                isFiltered ? `${cfg.cardBorder} shadow-md ring-2 ring-offset-1 ring-current` : "border-transparent hover:border-gray-200"
              }`}
            >
              <div className="flex items-center gap-2 mb-1">
                {cfg.icon}
                <span className={`text-xs font-semibold uppercase tracking-wide ${cfg.text}`}>{cfg.label}</span>
              </div>
              <div className={`text-3xl font-bold ${cfg.text}`}>{isLoading ? "—" : count}</div>
            </button>
          );
        })}
      </div>

      {/* Tabs + Filters */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 border-b pb-3">
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1 self-start">
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${tab === "all" ? "bg-white shadow text-gray-900" : "text-gray-500 hover:text-gray-700"}`}
            onClick={() => handleTab("all")}
          >
            All Universities
          </button>
          <button
            className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors flex items-center gap-1.5 ${tab === "queue" ? "bg-white shadow text-gray-900" : "text-gray-500 hover:text-gray-700"}`}
            onClick={() => handleTab("queue")}
          >
            Certification Queue
            {queueCount > 0 && (
              <span className="inline-flex items-center justify-center h-5 min-w-[20px] px-1 rounded-full bg-amber-500 text-white text-xs font-bold">
                {queueCount}
              </span>
            )}
          </button>
        </div>
        <div className="flex-1" />
        <div className="relative w-full sm:w-64">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
          <Input
            placeholder="Search universities…"
            value={search}
            onChange={e => handleSearch(e.target.value)}
            className="pl-8 h-8 text-sm"
          />
        </div>
      </div>

      {/* Queue description */}
      {tab === "queue" && (
        <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-50 border border-amber-100 text-sm text-amber-800">
          <TrendingDown className="h-4 w-4 mt-0.5 flex-shrink-0" />
          <div>
            <strong>Certification Queue</strong> — universities sorted by biggest score decline from their certified baseline.
            Universities drop into this list automatically when the watchdog detects a drop &gt;15 points.
          </div>
        </div>
      )}

      {/* Table */}
      <div className="rounded-xl border bg-white overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-gray-50/60">
                <SortTh label="University"    sortKey="name"                 current={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Status</th>
                <SortTh label="Current Score" sortKey="current_health_score" current={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-left text-xs font-semibold text-gray-400 uppercase tracking-wider">Cert Score</th>
                <SortTh label="Drop"          sortKey="score_drop"           current={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Last Scrape"   sortKey="last_scrape_at"       current={sortKey} dir={sortDir} onSort={handleSort} />
                <SortTh label="Staged"        sortKey="staged_courses"       current={sortKey} dir={sortDir} onSort={handleSort} />
                <th className="px-4 py-2.5 text-right text-xs font-semibold text-gray-400 uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {isLoading ? (
                Array.from({ length: 6 }).map((_, i) => (
                  <tr key={i} className="animate-pulse">
                    {Array.from({ length: 8 }).map((__, j) => (
                      <td key={j} className="px-4 py-3"><div className="h-4 bg-gray-100 rounded w-full" /></td>
                    ))}
                  </tr>
                ))
              ) : totalFiltered === 0 ? (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-gray-400">
                    {tab === "queue" ? "No universities in the certification queue." : "No universities match your filter."}
                  </td>
                </tr>
              ) : (
                pagedUniversities.map(uni => (
                  <tr key={uni.id} className="hover:bg-gray-50/60 transition-colors group">
                    <td className="px-4 py-3 font-medium text-gray-900 max-w-[240px]">
                      <div className="truncate">{uni.name}</div>
                      {uni.country && <div className="text-xs text-gray-400">{uni.country}</div>}
                    </td>
                    <td className="px-4 py-3">
                      <CertBadge status={uni.certification_status} />
                    </td>
                    <td className="px-4 py-3">
                      <ScorePill score={uni.current_health_score} />
                    </td>
                    <td className="px-4 py-3">
                      {uni.last_certified_score !== null ? (
                        <span className="text-sm font-medium text-gray-700">{uni.last_certified_score}</span>
                      ) : (
                        <span className="text-xs text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <DropBadge drop={uni.score_drop} />
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1 text-xs text-gray-500">
                        <Clock className="h-3 w-3 flex-shrink-0" />
                        {relativeTime(uni.last_scrape_at)}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-600">
                      {uni.staged_courses > 0 ? (
                        <span className="inline-flex items-center justify-center bg-blue-50 text-blue-700 border border-blue-100 text-xs font-semibold px-2 py-0.5 rounded-full">
                          {uni.staged_courses}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                        <Link href={`/universities/${uni.id}/scrape-agent`}>
                          <Button variant="outline" size="sm" className="h-7 px-2 gap-1 text-xs border-blue-200 text-blue-700 hover:bg-blue-50">
                            <Bot className="h-3 w-3" />Agent
                          </Button>
                        </Link>
                        <Link href={`/universities/${uni.id}`}>
                          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs text-gray-500">
                            <ExternalLink className="h-3 w-3" />
                          </Button>
                        </Link>
                        {uni.certification_status === "needs_review" && (
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-7 px-2 text-xs border-emerald-200 text-emerald-700 hover:bg-emerald-50"
                            onClick={() => patchMutation.mutate({ uniId: uni.id, status: "certified" })}
                            disabled={patchMutation.isPending}
                          >
                            <ShieldCheck className="h-3 w-3 mr-1" />Re-certify
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        {!isLoading && totalFiltered > 0 && (
          <div className="px-4 py-2.5 border-t bg-gray-50/40 flex flex-col sm:flex-row sm:items-center gap-3 text-xs text-gray-500">
            {/* Left: row count info */}
            <div className="flex items-center gap-2">
              <span>
                {totalFiltered === 0 ? "0" : `${(safePage - 1) * pageSize + 1}–${Math.min(safePage * pageSize, totalFiltered)}`} of {totalFiltered} universities
              </span>
              {totalFiltered !== (data?.universities.length ?? 0) && (
                <span className="text-gray-400">(filtered from {data?.universities.length ?? 0})</span>
              )}
            </div>
            <div className="flex-1" />
            {/* Centre: page-size selector */}
            <div className="flex items-center gap-1.5">
              <span className="text-gray-400">Rows per page:</span>
              {([10, 25, 50, 100] as const).map(n => (
                <button
                  key={n}
                  onClick={() => handlePageSize(n)}
                  className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                    pageSize === n
                      ? "bg-blue-600 text-white"
                      : "text-gray-500 hover:bg-gray-100"
                  }`}
                >
                  {n}
                </button>
              ))}
            </div>
            {/* Right: prev / page numbers / next */}
            <div className="flex items-center gap-1">
              <button
                disabled={safePage <= 1}
                onClick={() => setPage(p => Math.max(1, p - 1))}
                className="px-2 py-0.5 rounded text-xs font-medium text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                ‹ Prev
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter(p => p === 1 || p === totalPages || Math.abs(p - safePage) <= 1)
                .reduce<(number | "…")[]>((acc, p, idx, arr) => {
                  if (idx > 0 && p - (arr[idx - 1] as number) > 1) acc.push("…");
                  acc.push(p);
                  return acc;
                }, [])
                .map((p, i) =>
                  p === "…" ? (
                    <span key={`ellipsis-${i}`} className="px-1 text-gray-300">…</span>
                  ) : (
                    <button
                      key={p}
                      onClick={() => setPage(p as number)}
                      className={`min-w-[24px] px-1.5 py-0.5 rounded text-xs font-medium transition-colors ${
                        safePage === p
                          ? "bg-blue-600 text-white"
                          : "text-gray-500 hover:bg-gray-100"
                      }`}
                    >
                      {p}
                    </button>
                  )
                )}
              <button
                disabled={safePage >= totalPages}
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                className="px-2 py-0.5 rounded text-xs font-medium text-gray-500 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Next ›
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── Bulk Repair History panel ────────────────────────────────────── */}
      {historyData && historyData.history.length > 0 && (
        <div className="rounded-xl border bg-white shadow-sm overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3 border-b bg-gray-50/60">
            <div className="flex items-center gap-2 font-semibold text-sm text-gray-700">
              <Wrench className="h-4 w-4 text-blue-600" />
              Bulk Repair History
            </div>
            <a href="/bulk?tab=repair" className="text-xs text-blue-600 hover:underline">View all →</a>
          </div>
          <div className="divide-y">
            {historyData.history.map(e => {
              const relTime = (() => {
                const diff = Date.now() - new Date(e.created_at).getTime();
                const days = Math.floor(diff / 86400000);
                if (days === 0) return "Today";
                if (days === 1) return "Yesterday";
                if (days < 7) return `${days}d ago`;
                return new Date(e.created_at).toLocaleDateString("en-AU", { day: "2-digit", month: "short" });
              })();
              return (
                <div key={e.id} className="flex items-center gap-4 px-5 py-3 text-sm">
                  <div className="h-7 w-7 rounded-full bg-blue-100 flex items-center justify-center shrink-0">
                    <Wrench className="h-3.5 w-3.5 text-blue-600" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-gray-900">Bulk Repair</span>
                      {e.mark_testing && <span className="text-xs px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200">+Testing</span>}
                      {e.failed_count > 0 && <span className="text-xs px-1.5 py-0.5 rounded bg-red-50 text-red-700 border border-red-200">{e.failed_count} failed</span>}
                    </div>
                    <div className="text-xs text-gray-400 mt-0.5 flex items-center gap-2">
                      <span className="flex items-center gap-1"><User className="h-3 w-3" />{e.triggered_by_name || e.triggered_by_email}</span>
                      <span>·</span>
                      <span>{relTime}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-4 text-xs shrink-0">
                    <div className="text-center">
                      <div className="font-bold text-gray-900">{e.selected_count}</div>
                      <div className="text-gray-400">Selected</div>
                    </div>
                    <div className="text-center">
                      <div className="font-bold text-emerald-700">{e.queued_count}</div>
                      <div className="text-gray-400">Queued</div>
                    </div>
                    {e.skipped_count > 0 && (
                      <div className="text-center">
                        <div className="font-bold text-amber-600">{e.skipped_count}</div>
                        <div className="text-gray-400">Skipped</div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity, Eye, EyeOff, RefreshCw, Play, CheckCircle2, AlertTriangle,
  Clock, TrendingUp, Zap, Globe, Radio, BarChart3, ChevronDown, ChevronUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
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

interface WatcherStats {
  total_watchers: number;
  enabled: number;
  disabled: number;
  changed_today: number;
  scrapes_triggered_today: number;
  due_for_check: number;
  avg_change_frequency_days: number | null;
}

interface Watcher {
  id: number;
  university_id: number;
  university_name: string;
  university_country: string;
  enabled: boolean;
  monitoring_strategy: string;
  probe_url: string | null;
  last_probe_result: string | null;
  last_probe_status_code: number | null;
  last_probe_error: string | null;
  consecutive_unchanged: number;
  total_checks: number;
  total_changes_detected: number;
  total_scrapes_triggered: number;
  change_frequency_days: number | null;
  check_interval_hours: number;
  last_checked_at: string | null;
  last_changed_at: string | null;
  last_triggered_at: string | null;
  next_check_at: string | null;
  last_scrape_job_id: string | null;
}

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffH = (now.getTime() - d.getTime()) / 3600000;
  if (diffH < 1) return `${Math.floor(diffH * 60)}m ago`;
  if (diffH < 24) return `${Math.floor(diffH)}h ago`;
  return `${Math.floor(diffH / 24)}d ago`;
}

function fmtNext(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const diffH = (d.getTime() - now.getTime()) / 3600000;
  if (diffH < 0) return "Due now";
  if (diffH < 1) return `${Math.floor(diffH * 60)}m`;
  if (diffH < 24) return `${Math.floor(diffH)}h`;
  return `${Math.floor(diffH / 24)}d`;
}

function strategyColor(s: string) {
  if (s === "deep") return "bg-purple-100 text-purple-800";
  if (s === "active") return "bg-blue-100 text-blue-800";
  return "bg-slate-100 text-slate-700";
}

function resultBadge(result: string | null) {
  if (!result) return <span className="text-muted-foreground text-xs">—</span>;
  if (result === "changed") return <Badge className="bg-amber-100 text-amber-800 text-xs">Changed</Badge>;
  if (result === "unchanged") return <Badge className="bg-emerald-100 text-emerald-800 text-xs">Unchanged</Badge>;
  if (result === "error") return <Badge className="bg-red-100 text-red-800 text-xs">Error</Badge>;
  return <Badge variant="outline" className="text-xs">{result}</Badge>;
}

function StatCard({ label, value, sub, icon: Icon, color }: {
  label: string; value: string | number; sub?: string;
  icon: typeof Activity; color: string;
}) {
  return (
    <div className="bg-white border rounded-lg p-4 flex items-start gap-3">
      <div className={`p-2 rounded-md ${color}`}>
        <Icon className="h-4 w-4" />
      </div>
      <div>
        <div className="text-2xl font-bold leading-none">{value}</div>
        <div className="text-xs text-muted-foreground mt-0.5">{label}</div>
        {sub && <div className="text-xs text-muted-foreground mt-0.5 font-medium">{sub}</div>}
      </div>
    </div>
  );
}

export default function MonitoringPage() {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"all" | "enabled" | "disabled">("all");
  const [probing, setProbing] = useState<number | null>(null);
  const [sortBy, setSortBy] = useState<"name" | "last_checked" | "next_check">("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const { data: stats, isLoading: statsLoading } = useQuery<WatcherStats>({
    queryKey: ["monitoring-stats"],
    queryFn: () => apiFetch("/api/monitoring/stats"),
    refetchInterval: 30000,
  });

  const { data: watchers = [], isLoading: watchersLoading } = useQuery<Watcher[]>({
    queryKey: ["monitoring-watchers"],
    queryFn: () => apiFetch("/api/monitoring"),
    refetchInterval: 30000,
  });

  const enableMut = useMutation({
    mutationFn: (uniId: number) => apiFetch(`/api/monitoring/${uniId}/enable`, { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["monitoring-watchers"] }); qc.invalidateQueries({ queryKey: ["monitoring-stats"] }); },
    onError: (e) => toast({ title: "Error", description: String(e), variant: "destructive" }),
  });

  const disableMut = useMutation({
    mutationFn: (uniId: number) => apiFetch(`/api/monitoring/${uniId}/disable`, { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["monitoring-watchers"] }); qc.invalidateQueries({ queryKey: ["monitoring-stats"] }); },
    onError: (e) => toast({ title: "Error", description: String(e), variant: "destructive" }),
  });

  const bulkEnableMut = useMutation({
    mutationFn: () => apiFetch("/api/monitoring/bulk-enable", { method: "POST" }),
    onSuccess: (d) => {
      toast({ title: "Bulk enabled", description: `Monitoring enabled for ${d.enabled_count} universities` });
      qc.invalidateQueries({ queryKey: ["monitoring-watchers"] });
      qc.invalidateQueries({ queryKey: ["monitoring-stats"] });
    },
    onError: (e) => toast({ title: "Error", description: String(e), variant: "destructive" }),
  });

  async function handleProbe(uniId: number) {
    setProbing(uniId);
    try {
      const res = await apiFetch(`/api/monitoring/${uniId}/probe`, { method: "POST" });
      qc.invalidateQueries({ queryKey: ["monitoring-watchers"] });
      qc.invalidateQueries({ queryKey: ["monitoring-stats"] });
      toast({
        title: res.changed ? "Change detected!" : "No change",
        description: res.changed
          ? `Change detected — scrape ${res.scrape_triggered ? "triggered" : "not triggered"}`
          : "Page fingerprint unchanged since last check",
      });
    } catch (e) {
      toast({ title: "Probe failed", description: String(e), variant: "destructive" });
    } finally {
      setProbing(null);
    }
  }

  function toggleSort(col: typeof sortBy) {
    if (sortBy === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortBy(col); setSortDir("asc"); }
  }

  const filtered = watchers
    .filter(w => filter === "all" || (filter === "enabled" ? w.enabled : !w.enabled))
    .sort((a, b) => {
      let diff = 0;
      if (sortBy === "name") diff = a.university_name.localeCompare(b.university_name);
      else if (sortBy === "last_checked") diff = (a.last_checked_at ?? "").localeCompare(b.last_checked_at ?? "");
      else if (sortBy === "next_check") diff = (a.next_check_at ?? "").localeCompare(b.next_check_at ?? "");
      return sortDir === "asc" ? diff : -diff;
    });

  const SortIcon = ({ col }: { col: typeof sortBy }) =>
    sortBy === col ? (sortDir === "asc" ? <ChevronUp className="h-3 w-3 inline" /> : <ChevronDown className="h-3 w-3 inline" />) : null;

  if (statsLoading || watchersLoading) {
    return (
      <div className="flex items-center justify-center h-64 text-muted-foreground text-sm">
        Loading monitoring dashboard…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Radio className="h-6 w-6 text-violet-600" />
            Autonomous Monitoring
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Lightweight change probes — scrapes only trigger when content actually changes
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline" size="sm"
            onClick={() => { qc.invalidateQueries({ queryKey: ["monitoring-watchers"] }); qc.invalidateQueries({ queryKey: ["monitoring-stats"] }); }}
          >
            <RefreshCw className="h-4 w-4 mr-1" /> Refresh
          </Button>
          <Button
            size="sm" variant="outline"
            onClick={() => bulkEnableMut.mutate()}
            disabled={bulkEnableMut.isPending}
          >
            <Zap className="h-4 w-4 mr-1" />
            {bulkEnableMut.isPending ? "Enabling…" : "Enable All"}
          </Button>
        </div>
      </div>

      {/* Stats cards */}
      {stats && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <StatCard label="Total watchers" value={stats.total_watchers} icon={Globe} color="bg-violet-100 text-violet-600" />
          <StatCard label="Monitoring enabled" value={stats.enabled} icon={Eye} color="bg-emerald-100 text-emerald-600" />
          <StatCard label="Changed today" value={stats.changed_today} icon={AlertTriangle} color="bg-amber-100 text-amber-600" />
          <StatCard label="Scrapes triggered" value={stats.scrapes_triggered_today} sub="today" icon={Play} color="bg-blue-100 text-blue-600" />
          <StatCard label="Due for check" value={stats.due_for_check} icon={Clock} color="bg-rose-100 text-rose-600" />
          <StatCard
            label="Avg change freq"
            value={stats.avg_change_frequency_days != null ? `${stats.avg_change_frequency_days}d` : "—"}
            icon={TrendingUp} color="bg-slate-100 text-slate-600"
          />
        </div>
      )}

      {/* How it works (collapsed info) */}
      <div className="bg-violet-50 border border-violet-200 rounded-lg p-4">
        <div className="flex items-start gap-3">
          <Activity className="h-5 w-5 text-violet-600 mt-0.5 flex-shrink-0" />
          <div>
            <p className="text-sm font-semibold text-violet-900">How monitoring works</p>
            <p className="text-xs text-violet-700 mt-1">
              Every 30 minutes Celery checks universities whose <code className="bg-violet-100 px-1 rounded">next_check_at</code> has passed.
              <strong> Passive</strong> probes send a HEAD request (zero bandwidth) and compare ETag/Last-Modified.
              <strong> Active</strong> downloads the homepage and hashes the content.
              <strong> Deep</strong> also hashes the sitemap. A scrape is triggered only when a fingerprint changes.
              Probe intervals adapt to each university's learned change frequency: fast-changing sites → every 6h; stable sites → weekly.
            </p>
          </div>
        </div>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 bg-muted rounded-md p-1 w-fit">
        {(["all", "enabled", "disabled"] as const).map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1.5 text-sm rounded transition-colors ${
              filter === f ? "bg-white shadow-sm font-medium" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {f === "all" ? `All (${watchers.length})` : f === "enabled" ? `Enabled (${stats?.enabled ?? 0})` : `Disabled (${stats?.disabled ?? 0})`}
          </button>
        ))}
      </div>

      {/* Watcher table */}
      {filtered.length === 0 ? (
        <div className="bg-white border rounded-lg p-12 text-center text-muted-foreground">
          <Radio className="h-12 w-12 mx-auto mb-4 text-muted-foreground/30" />
          <p className="font-medium">No watchers yet</p>
          <p className="text-sm mt-1">Click <strong>Enable All</strong> to start monitoring universities that have a scrape URL configured.</p>
        </div>
      ) : (
        <div className="bg-white border rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 border-b">
                <tr>
                  <th className="text-left px-4 py-3 font-medium cursor-pointer hover:text-foreground" onClick={() => toggleSort("name")}>
                    University <SortIcon col="name" />
                  </th>
                  <th className="text-left px-4 py-3 font-medium">Strategy</th>
                  <th className="text-left px-4 py-3 font-medium">Last result</th>
                  <th className="text-left px-4 py-3 font-medium cursor-pointer hover:text-foreground" onClick={() => toggleSort("last_checked")}>
                    Checked <SortIcon col="last_checked" />
                  </th>
                  <th className="text-left px-4 py-3 font-medium cursor-pointer hover:text-foreground" onClick={() => toggleSort("next_check")}>
                    Next check <SortIcon col="next_check" />
                  </th>
                  <th className="text-left px-4 py-3 font-medium">Changes</th>
                  <th className="text-left px-4 py-3 font-medium">Scrapes</th>
                  <th className="text-right px-4 py-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {filtered.map(w => (
                  <tr key={w.id} className={`hover:bg-muted/30 transition-colors ${!w.enabled ? "opacity-60" : ""}`}>
                    <td className="px-4 py-3">
                      <div className="font-medium text-sm">{w.university_name}</div>
                      <div className="text-xs text-muted-foreground">{w.university_country}</div>
                      {w.probe_url && (
                        <div className="text-xs text-muted-foreground/70 truncate max-w-[200px]" title={w.probe_url}>
                          {w.probe_url.replace(/^https?:\/\//, "")}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${strategyColor(w.monitoring_strategy)}`}>
                        {w.monitoring_strategy}
                      </span>
                      <div className="text-xs text-muted-foreground mt-0.5">
                        every {w.check_interval_hours < 24 ? `${w.check_interval_hours}h` : `${w.check_interval_hours / 24}d`}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      {resultBadge(w.last_probe_result)}
                      {w.last_probe_error && (
                        <div className="text-xs text-red-600 mt-0.5 max-w-[160px] truncate" title={w.last_probe_error}>
                          {w.last_probe_error}
                        </div>
                      )}
                      {w.total_checks > 0 && (
                        <div className="text-xs text-muted-foreground mt-0.5">{w.total_checks} checks</div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{fmtDate(w.last_checked_at)}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs font-medium ${w.next_check_at && new Date(w.next_check_at) <= new Date() ? "text-rose-600" : "text-muted-foreground"}`}>
                        {fmtNext(w.next_check_at)}
                      </span>
                      {w.change_frequency_days != null && (
                        <div className="text-xs text-muted-foreground mt-0.5">
                          avg {w.change_frequency_days.toFixed(1)}d
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {w.total_changes_detected > 0 ? (
                        <div className="flex items-center gap-1 text-amber-700">
                          <AlertTriangle className="h-3 w-3" />
                          <span className="text-xs font-medium">{w.total_changes_detected}</span>
                        </div>
                      ) : (
                        <div className="flex items-center gap-1 text-emerald-600">
                          <CheckCircle2 className="h-3 w-3" />
                          <span className="text-xs">{w.consecutive_unchanged > 0 ? `${w.consecutive_unchanged} stable` : "0"}</span>
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">
                      {w.total_scrapes_triggered > 0 ? (
                        <div className="flex items-center gap-1 text-blue-600">
                          <Play className="h-3 w-3" />
                          {w.total_scrapes_triggered}
                        </div>
                      ) : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1.5">
                        <Button
                          size="sm" variant="ghost"
                          className="h-7 px-2 text-xs"
                          onClick={() => handleProbe(w.university_id)}
                          disabled={probing === w.university_id || !w.probe_url}
                          title={w.probe_url ? "Run probe now" : "No probe URL configured"}
                        >
                          {probing === w.university_id ? (
                            <RefreshCw className="h-3 w-3 animate-spin" />
                          ) : (
                            <BarChart3 className="h-3 w-3" />
                          )}
                          <span className="ml-1 hidden sm:inline">Probe</span>
                        </Button>
                        {w.enabled ? (
                          <Button
                            size="sm" variant="ghost"
                            className="h-7 px-2 text-xs text-muted-foreground"
                            onClick={() => disableMut.mutate(w.university_id)}
                            disabled={disableMut.isPending}
                          >
                            <EyeOff className="h-3 w-3" />
                            <span className="ml-1 hidden sm:inline">Pause</span>
                          </Button>
                        ) : (
                          <Button
                            size="sm" variant="ghost"
                            className="h-7 px-2 text-xs"
                            onClick={() => enableMut.mutate(w.university_id)}
                            disabled={enableMut.isPending}
                          >
                            <Eye className="h-3 w-3" />
                            <span className="ml-1 hidden sm:inline">Watch</span>
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

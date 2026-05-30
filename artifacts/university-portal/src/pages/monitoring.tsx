import { useState, useEffect, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Activity, Eye, EyeOff, RefreshCw, Play, CheckCircle2, AlertTriangle,
  Clock, TrendingUp, Zap, Globe, Radio, BarChart3, ChevronDown, ChevronUp,
  Search, Bell, X, BellOff,
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

const DISMISSED_KEY = "monitoring_dismissed_change_notifications";

function getDismissed(): Set<number> {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function saveDismissed(ids: Set<number>) {
  localStorage.setItem(DISMISSED_KEY, JSON.stringify([...ids]));
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
  const [filter, setFilter] = useState<"all" | "enabled" | "disabled" | "changed">("all");
  const [search, setSearch] = useState("");
  const [probing, setProbing] = useState<number | null>(null);
  const [sortBy, setSortBy] = useState<"name" | "last_checked" | "next_check">("name");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [dismissed, setDismissed] = useState<Set<number>>(getDismissed);
  const [monPage, setMonPage] = useState(1);
  const [monPageSize, setMonPageSize] = useState(25);

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
        title: res.changed ? "⚠ Change detected!" : "No change",
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

  function dismissNotification(uniId: number) {
    const next = new Set(dismissed);
    next.add(uniId);
    setDismissed(next);
    saveDismissed(next);
  }

  function dismissAll() {
    const next = new Set(changeNotifications.map(w => w.university_id));
    setDismissed(next);
    saveDismissed(next);
  }

  // Universities with a detected change that haven't been dismissed
  const changeNotifications = useMemo(() =>
    watchers.filter(w =>
      w.last_probe_result === "changed" &&
      !dismissed.has(w.university_id)
    ),
    [watchers, dismissed]
  );

  const changedCount = watchers.filter(w => w.total_changes_detected > 0).length;

  const q = search.toLowerCase().trim();
  const filtered = watchers
    .filter(w => {
      if (filter === "enabled") return w.enabled;
      if (filter === "disabled") return !w.enabled;
      if (filter === "changed") return w.total_changes_detected > 0;
      return true;
    })
    .filter(w => {
      if (!q) return true;
      return (
        w.university_name.toLowerCase().includes(q) ||
        (w.university_country ?? "").toLowerCase().includes(q) ||
        (w.probe_url ?? "").toLowerCase().includes(q)
      );
    })
    .sort((a, b) => {
      let diff = 0;
      if (sortBy === "name") diff = a.university_name.localeCompare(b.university_name);
      else if (sortBy === "last_checked") diff = (a.last_checked_at ?? "").localeCompare(b.last_checked_at ?? "");
      else if (sortBy === "next_check") diff = (a.next_check_at ?? "").localeCompare(b.next_check_at ?? "");
      return sortDir === "asc" ? diff : -diff;
    });

  useEffect(() => { setMonPage(1); }, [filter, search, sortBy, sortDir]);

  const monTotalPages = Math.max(1, Math.ceil(filtered.length / monPageSize));
  const safeMonPage = Math.min(monPage, monTotalPages);
  const paginatedFiltered = filtered.slice((safeMonPage - 1) * monPageSize, safeMonPage * monPageSize);

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
            {changeNotifications.length > 0 && (
              <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-amber-500 text-white text-[10px] font-bold">
                {changeNotifications.length}
              </span>
            )}
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

      {/* Change notifications banner */}
      {changeNotifications.length > 0 && (
        <div className="border border-amber-300 bg-amber-50 rounded-lg overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-amber-200 bg-amber-100/60">
            <div className="flex items-center gap-2">
              <Bell className="h-4 w-4 text-amber-700" />
              <span className="text-sm font-semibold text-amber-900">
                {changeNotifications.length === 1
                  ? "1 university changed since last probe"
                  : `${changeNotifications.length} universities changed since last probe`}
              </span>
            </div>
            <button
              onClick={dismissAll}
              className="flex items-center gap-1 text-xs text-amber-700 hover:text-amber-900 transition-colors"
              title="Dismiss all notifications"
            >
              <BellOff className="h-3.5 w-3.5" />
              Dismiss all
            </button>
          </div>
          <div className="divide-y divide-amber-200">
            {changeNotifications.map(w => (
              <div key={w.university_id} className="flex items-center justify-between px-4 py-2.5 hover:bg-amber-50/80 transition-colors">
                <div className="flex items-start gap-3">
                  <AlertTriangle className="h-4 w-4 text-amber-600 mt-0.5 flex-shrink-0" />
                  <div>
                    <span className="text-sm font-medium text-amber-900">{w.university_name}</span>
                    <span className="text-xs text-amber-700 ml-2">{w.university_country}</span>
                    <div className="text-xs text-amber-700 mt-0.5">
                      Change detected{w.last_changed_at ? ` · ${fmtDate(w.last_changed_at)}` : ""} ·{" "}
                      {w.probe_url ? (
                        <a
                          href={w.probe_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="underline underline-offset-2 hover:text-amber-900"
                        >
                          {w.probe_url.replace(/^https?:\/\//, "").slice(0, 50)}
                        </a>
                      ) : "no URL"}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 ml-4 flex-shrink-0">
                  <Button
                    size="sm" variant="ghost"
                    className="h-7 px-2 text-xs text-amber-800 hover:bg-amber-200"
                    onClick={() => handleProbe(w.university_id)}
                    disabled={probing === w.university_id || !w.probe_url}
                  >
                    {probing === w.university_id ? <RefreshCw className="h-3 w-3 animate-spin" /> : <BarChart3 className="h-3 w-3" />}
                    <span className="ml-1">Re-probe</span>
                  </Button>
                  <button
                    onClick={() => dismissNotification(w.university_id)}
                    className="text-amber-600 hover:text-amber-900 transition-colors p-1 rounded hover:bg-amber-200"
                    title="Dismiss this notification"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* How it works */}
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
              Probe intervals adapt to each university's learned change frequency: fast-changing sites → every 3d; stable sites → every 60–90 days.
            </p>
          </div>
        </div>
      </div>

      {/* Filter tabs + search bar */}
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between">
        <div className="flex gap-1 bg-muted rounded-md p-1 w-fit">
          {(["all", "enabled", "disabled", "changed"] as const).map(f => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-sm rounded transition-colors ${
                filter === f ? "bg-white shadow-sm font-medium" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {f === "all" && `All (${watchers.length})`}
              {f === "enabled" && `Enabled (${stats?.enabled ?? 0})`}
              {f === "disabled" && `Disabled (${stats?.disabled ?? 0})`}
              {f === "changed" && (
                <span className="flex items-center gap-1.5">
                  Changed
                  {changedCount > 0 && (
                    <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] rounded-full bg-amber-500 text-white text-[10px] font-bold px-1">
                      {changedCount}
                    </span>
                  )}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2 w-full sm:w-auto">
          <div className="relative flex-1 sm:w-72">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
            <Input
              placeholder="Search university, country, or URL…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="pl-8 h-8 text-sm"
            />
            {search && (
              <button
                onClick={() => setSearch("")}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <select
            value={monPageSize}
            onChange={e => setMonPageSize(Number(e.target.value))}
            style={{ height: "2rem", fontSize: "0.875rem", border: "1px solid #e2e8f0", borderRadius: "0.375rem", padding: "0 0.5rem", color: "#374151", background: "white" }}
          >
            {[10, 25, 50, 100].map(n => <option key={n} value={n}>Show {n}</option>)}
          </select>
        </div>
      </div>

      {/* Watcher table */}
      {filtered.length === 0 ? (
        <div className="bg-white border rounded-lg p-12 text-center text-muted-foreground">
          <Radio className="h-12 w-12 mx-auto mb-4 text-muted-foreground/30" />
          {q ? (
            <>
              <p className="font-medium">No results for "{search}"</p>
              <p className="text-sm mt-1">Try a different name, country, or URL.</p>
            </>
          ) : (
            <>
              <p className="font-medium">No watchers yet</p>
              <p className="text-sm mt-1">Click <strong>Enable All</strong> to start monitoring universities that have a scrape URL configured.</p>
            </>
          )}
        </div>
      ) : (
        <div className="bg-white border rounded-lg overflow-hidden">
          {q && (
            <div className="px-4 py-2 bg-muted/30 border-b text-xs text-muted-foreground">
              Showing {filtered.length} of {watchers.length} watchers
            </div>
          )}
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
                {paginatedFiltered.map(w => (
                  <tr
                    key={w.id}
                    className={`hover:bg-muted/30 transition-colors ${!w.enabled ? "opacity-60" : ""} ${
                      w.last_probe_result === "changed" ? "border-l-2 border-l-amber-400" : ""
                    }`}
                  >
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
                        every {w.check_interval_hours < 24 ? `${w.check_interval_hours}h` : `${Math.round(w.check_interval_hours / 24)}d`}
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
          {/* Monitoring pagination footer */}
          {filtered.length > monPageSize && (
            <div className="px-4 py-3 border-t bg-muted/20 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                Showing {(safeMonPage - 1) * monPageSize + 1}–{Math.min(safeMonPage * monPageSize, filtered.length)} of {filtered.length}
                {q && ` (filtered from ${watchers.length})`}
              </span>
              <div className="flex items-center gap-1">
                <button onClick={() => setMonPage(1)} disabled={safeMonPage === 1} style={{ padding: "2px 6px", border: "1px solid #e2e8f0", borderRadius: "4px", background: "white", cursor: safeMonPage === 1 ? "not-allowed" : "pointer", color: safeMonPage === 1 ? "#cbd5e1" : "#374151" }}>«</button>
                <button onClick={() => setMonPage(p => Math.max(1, p - 1))} disabled={safeMonPage === 1} style={{ padding: "2px 6px", border: "1px solid #e2e8f0", borderRadius: "4px", background: "white", cursor: safeMonPage === 1 ? "not-allowed" : "pointer", color: safeMonPage === 1 ? "#cbd5e1" : "#374151" }}>‹</button>
                {Array.from({ length: Math.min(7, monTotalPages) }, (_, i) => {
                  const start = Math.max(1, Math.min(safeMonPage - 3, monTotalPages - 6));
                  const p = start + i;
                  return p <= monTotalPages ? (
                    <button key={p} onClick={() => setMonPage(p)} style={{ padding: "2px 8px", border: "1px solid #e2e8f0", borderRadius: "4px", background: p === safeMonPage ? "#4f46e5" : "white", color: p === safeMonPage ? "white" : "#374151", cursor: "pointer", fontWeight: p === safeMonPage ? 600 : 400 }}>{p}</button>
                  ) : null;
                })}
                <button onClick={() => setMonPage(p => Math.min(monTotalPages, p + 1))} disabled={safeMonPage === monTotalPages} style={{ padding: "2px 6px", border: "1px solid #e2e8f0", borderRadius: "4px", background: "white", cursor: safeMonPage === monTotalPages ? "not-allowed" : "pointer", color: safeMonPage === monTotalPages ? "#cbd5e1" : "#374151" }}>›</button>
                <button onClick={() => setMonPage(monTotalPages)} disabled={safeMonPage === monTotalPages} style={{ padding: "2px 6px", border: "1px solid #e2e8f0", borderRadius: "4px", background: "white", cursor: safeMonPage === monTotalPages ? "not-allowed" : "pointer", color: safeMonPage === monTotalPages ? "#cbd5e1" : "#374151" }}>»</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import {
  LineChart, Line, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, AreaChart, Area,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  TrendingUp, Zap, DollarSign, Bot, RefreshCw,
  CheckCircle2, AlertTriangle, BarChart2, PieChart as PieIcon,
} from "lucide-react";

const BASE = import.meta.env.BASE_URL?.replace(/\/$/, "") ?? "";

const PIE_COLORS: Record<string, string> = {
  "HTML Extraction": "#60a5fa",
  "Gemini Fallback":  "#f472b6",
  "PDF Extraction":   "#34d399",
  "AI Rules":         "#a78bfa",
  "Pattern Reuse":    "#fb923c",
  "API Extraction":   "#38bdf8",
};

const RECOVERY_COLORS: Record<string, string> = {
  "CASCADE":             "#ef4444",
  "Repair Extractor":    "#f97316",
  "PDF Quality Gate":    "#eab308",
  "Browser Retry":       "#8b5cf6",
  "Quality Optimizer":   "#06b6d4",
  "Human Intervention":  "#e11d48",
};

type Summary = {
  period_days: number;
  total_jobs: number;
  avg_first_completeness: number;
  avg_final_completeness: number;
  avg_completeness_gain: number;
  jobs_crossed_85: number;
  jobs_below_85_start: number;
  auto_publish_conversion_rate: number;
  total_courses_staged: number;
  total_courses_auto_published: number;
  total_gemini_calls: number;
  total_gemini_cost_usd: number;
  total_patterns_reused: number;
  total_p7_inline_improved: number;
  recovery_counts: Record<string, number>;
};

type TrendMonth = {
  month: string;
  total_jobs: number;
  avg_first_completeness: number;
  avg_final_completeness: number;
  jobs_crossed_85: number;
  auto_publish_rate: number;
  gemini_cost_usd: number;
  patterns_reused: number;
  avg_pct_html: number;
  avg_pct_gemini: number;
  avg_pct_pdf: number;
  avg_pct_ai_rules: number;
  avg_pct_pattern: number;
};

type SourceEntry = {
  source: string;
  key: string;
  value: number;
  pct: number;
};

type RecoveryAction = {
  action: string;
  count: number;
  rate: number;
};

async function fetchJSON<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { credentials: "include" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function pct(v: number) {
  return `${(v * 100).toFixed(1)}%`;
}

function money(v: number) {
  return `$${v.toFixed(4)}`;
}

function KpiCard({
  title, value, sub, icon: Icon, color = "text-foreground",
}: {
  title: string; value: string; sub?: string;
  icon: React.FC<{ className?: string }>; color?: string;
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
        <Icon className={`h-4 w-4 ${color}`} />
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-bold ${color}`}>{value}</div>
        {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
      </CardContent>
    </Card>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="h-48 flex items-center justify-center text-muted-foreground text-sm">
      {message}
    </div>
  );
}

const CustomTooltip = ({
  active, payload, label, formatter,
}: {
  active?: boolean; payload?: { name: string; value: number; color: string }[];
  label?: string; formatter?: (v: number, name: string) => string;
}) => {
  if (!active || !payload?.length) return null;
  return (
    <div className="bg-background border rounded-md shadow-md px-3 py-2 text-xs space-y-1">
      {label && <p className="font-medium mb-1">{label}</p>}
      {payload.map((p) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full" style={{ background: p.color }} />
          <span className="text-muted-foreground">{p.name}:</span>
          <span className="font-medium">
            {formatter ? formatter(p.value, p.name) : p.value}
          </span>
        </div>
      ))}
    </div>
  );
};

export default function PerformancePage() {
  const [days, setDays] = useState(30);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [trend, setTrend] = useState<TrendMonth[]>([]);
  const [sources, setSources] = useState<SourceEntry[]>([]);
  const [recovery, setRecovery] = useState<RecoveryAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = async (d: number) => {
    setLoading(true);
    setError(null);
    // months shown in trend chart: 1 for 7d, 2 for 30d, 3 for 90d (min 1)
    const trendMonths = Math.max(1, Math.ceil(d / 30));
    try {
      const [sum, trd, src, rec] = await Promise.all([
        fetchJSON<{ period_days: number } & Summary>(`/api/performance/summary?days=${d}`),
        fetchJSON<{ months: TrendMonth[] }>(`/api/performance/trend?months=${trendMonths}`),
        fetchJSON<{ sources: SourceEntry[] }>(`/api/performance/sources?days=${d}`),
        fetchJSON<{ actions: RecoveryAction[] }>(`/api/performance/recovery?days=${d}`),
      ]);
      setSummary(sum as Summary);
      setTrend(trd.months);
      setSources(src.sources.filter((s) => s.value > 0));
      setRecovery(rec.actions);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  // Period changes use the refreshing=true pattern so the full-page skeleton
  // is suppressed — the existing data stays visible while new data loads in.
  // Without this, setDays(d) → useEffect → setLoading(true) with refreshing=false
  // triggers the early-return skeleton that hides the period buttons entirely.
  const handlePeriodChange = (d: number) => {
    if (d === days) return;
    setRefreshing(true);
    setDays(d);
  };

  useEffect(() => { load(days); }, [days]);

  const handleRefresh = () => {
    setRefreshing(true);
    load(days);
  };

  if (loading && !refreshing) {
    return (
      <div className="space-y-6">
        <div>
          <Skeleton className="h-8 w-64 mb-2" />
          <Skeleton className="h-4 w-96" />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {[0,1,2,3].map(i => (
            <Card key={i}><CardContent className="pt-6"><Skeleton className="h-16 w-full" /></CardContent></Card>
          ))}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {[0,1,2,3].map(i => (
            <Card key={i}><CardContent className="pt-6"><Skeleton className="h-52 w-full" /></CardContent></Card>
          ))}
        </div>
      </div>
    );
  }

  const hasData = summary && summary.total_jobs > 0;

  const trendChartData = trend.map(m => ({
    month: m.month,
    "First Completeness": Math.round(m.avg_first_completeness * 100),
    "Final Completeness": Math.round(m.avg_final_completeness * 100),
    "Auto-publish Rate":  Math.round(m.auto_publish_rate * 100),
  }));

  const costChartData = trend.map(m => ({
    month: m.month,
    "Cost (USD)": Number(m.gemini_cost_usd.toFixed(4)),
    "Patterns Reused": m.patterns_reused,
  }));

  const sourceMix = trend.length > 0
    ? trend[trend.length - 1]
    : null;

  const sourceMixData = sourceMix
    ? [
        { name: "HTML",     value: Math.round(sourceMix.avg_pct_html * 100) },
        { name: "Gemini",   value: Math.round(sourceMix.avg_pct_gemini * 100) },
        { name: "PDF",      value: Math.round(sourceMix.avg_pct_pdf * 100) },
        { name: "AI Rules", value: Math.round(sourceMix.avg_pct_ai_rules * 100) },
        { name: "Patterns", value: Math.round(sourceMix.avg_pct_pattern * 100) },
      ].filter(d => d.value > 0)
    : sources.map(s => ({ name: s.source.replace(" Extraction", "").replace(" Fallback", ""), value: Math.round(s.pct) }));

  const recoveryChartData = recovery
    .filter(r => r.count > 0)
    .map(r => ({
      action: r.action,
      count: r.count,
      rate: Math.round(r.rate * 100),
    }));

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            <BarChart2 className="h-6 w-6 text-primary" />
            Performance Intelligence
          </h1>
          <p className="text-muted-foreground text-sm mt-1">
            Evidence that the autonomous pipeline is reducing manual work and improving quality over time.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {[7, 30, 90].map(d => (
            <Button
              key={d}
              variant={days === d ? "default" : "outline"}
              size="sm"
              onClick={() => handlePeriodChange(d)}
            >
              {d}d
            </Button>
          ))}
          <Button variant="ghost" size="sm" onClick={handleRefresh} disabled={refreshing}>
            <RefreshCw className={`h-4 w-4 mr-1 ${refreshing ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          Failed to load: {error}
        </div>
      )}

      {!hasData && !error && (
        <Card className="border-dashed">
          <CardContent className="pt-8 pb-8 text-center">
            <Bot className="h-10 w-10 text-muted-foreground mx-auto mb-3" />
            <p className="font-medium">No performance data yet</p>
            <p className="text-sm text-muted-foreground mt-1">
              Performance metrics are recorded automatically 30 seconds after each scrape job completes.
              Run a scrape job to start building the intelligence layer.
            </p>
          </CardContent>
        </Card>
      )}

      {/* KPI Cards */}
      {summary && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            title="Avg Completeness Gain"
            value={pct(summary.avg_completeness_gain)}
            sub={`${pct(summary.avg_first_completeness)} → ${pct(summary.avg_final_completeness)} avg`}
            icon={TrendingUp}
            color={summary.avg_completeness_gain > 0.05 ? "text-emerald-600" : "text-foreground"}
          />
          <KpiCard
            title="Auto-Publish Conversion"
            value={pct(summary.auto_publish_conversion_rate)}
            sub={`${summary.jobs_crossed_85} of ${summary.jobs_below_85_start} jobs crossed 85%`}
            icon={CheckCircle2}
            color="text-blue-600"
          />
          <KpiCard
            title="Gemini Cost (Period)"
            value={money(summary.total_gemini_cost_usd)}
            sub={`${summary.total_gemini_calls.toLocaleString()} calls · ${summary.total_jobs} jobs`}
            icon={DollarSign}
            color={summary.total_gemini_cost_usd < 1 ? "text-emerald-600" : "text-amber-600"}
          />
          <KpiCard
            title="Pattern Reuse Events"
            value={summary.total_patterns_reused.toLocaleString()}
            sub={`${summary.total_p7_inline_improved} courses improved inline by P7`}
            icon={Zap}
            color="text-violet-600"
          />
        </div>
      )}

      {/* Charts Row 1: Completeness Trend + Auto-Publish Rate */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Completeness Trend</CardTitle>
            <CardDescription>First vs final avg completeness per month — the gap shows automation value</CardDescription>
          </CardHeader>
          <CardContent>
            {trendChartData.length === 0 ? (
              <EmptyChart message="No trend data yet — run scrape jobs to build history" />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={trendChartData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                  <YAxis domain={[0, 100]} tickFormatter={v => `${v}%`} tick={{ fontSize: 11 }} />
                  <Tooltip content={<CustomTooltip formatter={(v) => `${v}%`} />} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line
                    type="monotone"
                    dataKey="First Completeness"
                    stroke="#94a3b8"
                    strokeWidth={2}
                    dot={{ r: 3 }}
                    strokeDasharray="4 2"
                  />
                  <Line
                    type="monotone"
                    dataKey="Final Completeness"
                    stroke="#22c55e"
                    strokeWidth={2.5}
                    dot={{ r: 4 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Auto-Publish Rate</CardTitle>
            <CardDescription>% of jobs that crossed the 85% completeness gate automatically</CardDescription>
          </CardHeader>
          <CardContent>
            {trendChartData.length === 0 ? (
              <EmptyChart message="Run scrape jobs to see trend data" />
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={trendChartData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                  <defs>
                    <linearGradient id="autoGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                  <YAxis domain={[0, 100]} tickFormatter={v => `${v}%`} tick={{ fontSize: 11 }} />
                  <Tooltip content={<CustomTooltip formatter={(v) => `${v}%`} />} />
                  <Area
                    type="monotone"
                    dataKey="Auto-publish Rate"
                    stroke="#3b82f6"
                    strokeWidth={2.5}
                    fill="url(#autoGrad)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Charts Row 2: Source Mix + Recovery Actions */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              <PieIcon className="h-4 w-4" />
              Data Source Mix
            </CardTitle>
            <CardDescription>Where extracted field values come from — less Gemini = cheaper + faster</CardDescription>
          </CardHeader>
          <CardContent>
            {sourceMixData.length === 0 ? (
              <EmptyChart message="No source data recorded yet" />
            ) : (
              <div className="flex items-center gap-4">
                <ResponsiveContainer width="55%" height={200}>
                  <PieChart>
                    <Pie
                      data={sourceMixData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={80}
                      paddingAngle={2}
                    >
                      {sourceMixData.map((entry, i) => {
                        const fullKey = sources.find(s =>
                          s.source.includes(entry.name.split(" ")[0])
                        )?.source ?? entry.name;
                        return (
                          <Cell
                            key={entry.name}
                            fill={PIE_COLORS[fullKey] ?? `hsl(${i * 60}, 70%, 60%)`}
                          />
                        );
                      })}
                    </Pie>
                    <Tooltip formatter={(v: number) => `${v}%`} />
                  </PieChart>
                </ResponsiveContainer>
                <div className="flex-1 space-y-2">
                  {sourceMixData.map((d, i) => {
                    const fullKey = sources.find(s =>
                      s.source.includes(d.name.split(" ")[0])
                    )?.source ?? d.name;
                    const color = PIE_COLORS[fullKey] ?? `hsl(${i * 60}, 70%, 60%)`;
                    return (
                      <div key={d.name} className="flex items-center gap-2 text-xs">
                        <span className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
                          style={{ background: color }} />
                        <span className="flex-1 text-muted-foreground truncate">{d.name}</span>
                        <Badge variant="secondary" className="text-xs px-1.5 py-0">{d.value}%</Badge>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              <AlertTriangle className="h-4 w-4" />
              Recovery Actions Fired
            </CardTitle>
            <CardDescription>How often each autonomous recovery mechanism was triggered</CardDescription>
          </CardHeader>
          <CardContent>
            {recoveryChartData.length === 0 ? (
              <EmptyChart message="No recovery actions recorded — all jobs completed at ≥85% first try!" />
            ) : (
              <ResponsiveContainer width="100%" height={200}>
                <BarChart
                  data={recoveryChartData}
                  layout="vertical"
                  margin={{ top: 0, right: 40, left: 10, bottom: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 10 }} />
                  <YAxis dataKey="action" type="category" tick={{ fontSize: 10 }} width={110} />
                  <Tooltip content={<CustomTooltip />} />
                  <Bar dataKey="count" radius={[0, 3, 3, 0]}>
                    {recoveryChartData.map((entry) => (
                      <Cell
                        key={entry.action}
                        fill={RECOVERY_COLORS[entry.action] ?? "#64748b"}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Charts Row 3: Gemini Cost + Cost per Job trend */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Gemini Cost Over Time</CardTitle>
            <CardDescription>Monthly AI spend — downward trend means patterns + rules are working</CardDescription>
          </CardHeader>
          <CardContent>
            {costChartData.length === 0 ? (
              <EmptyChart message="No cost data yet" />
            ) : (
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={costChartData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                  <YAxis tickFormatter={v => `$${v.toFixed(3)}`} tick={{ fontSize: 10 }} />
                  <Tooltip content={<CustomTooltip formatter={(v) => `$${v}`} />} />
                  <Bar dataKey="Cost (USD)" fill="#f472b6" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Pattern Reuse Growth</CardTitle>
            <CardDescription>Reused learned patterns per month — rising trend = system getting smarter</CardDescription>
          </CardHeader>
          <CardContent>
            {costChartData.length === 0 ? (
              <EmptyChart message="No pattern reuse data yet" />
            ) : (
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={costChartData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                  <defs>
                    <linearGradient id="patternGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#a78bfa" stopOpacity={0.4} />
                      <stop offset="95%" stopColor="#a78bfa" stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip content={<CustomTooltip />} />
                  <Area
                    type="monotone"
                    dataKey="Patterns Reused"
                    stroke="#a78bfa"
                    strokeWidth={2.5}
                    fill="url(#patternGrad)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Autonomy Score Summary */}
      {summary && hasData && (
        <Card className="border-primary/20 bg-primary/5">
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              <Bot className="h-4 w-4 text-primary" />
              Autonomy Score — Last {days} Days
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 text-center">
              {[
                { label: "Jobs Completed", value: summary.total_jobs.toString() },
                { label: "Courses Staged", value: summary.total_courses_staged.toLocaleString() },
                { label: "Auto-published", value: summary.total_courses_auto_published.toLocaleString() },
                { label: "Conversion Rate", value: pct(summary.auto_publish_conversion_rate) },
                { label: "Human Interventions", value: (summary.recovery_counts.human_intervention ?? 0).toString() },
                { label: "Avg Gain", value: pct(summary.avg_completeness_gain) },
              ].map(({ label, value }) => (
                <div key={label} className="space-y-1">
                  <p className="text-2xl font-bold text-primary">{value}</p>
                  <p className="text-xs text-muted-foreground leading-tight">{label}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

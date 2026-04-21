import { useState, useEffect, useCallback } from "react";
import { Shield, RefreshCw, CheckCircle2, AlertTriangle, Clock, Database } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface TableBackupInfo {
  table: string;
  source: string;
  totalBackedUpRows: number;
  lastBackedUp: string | null;
  snapshots: { snap_date: string; rows: string }[];
}

const TABLE_LABELS: Record<string, { label: string; fields: string[] }> = {
  courses_backup: {
    label: "Courses",
    fields: ["Duration", "Term", "Study Mode", "Course Location"],
  },
  fees_backup: {
    label: "Fees",
    fields: ["International Fee", "Fee Term", "Year"],
  },
  intakes_backup: {
    label: "Intakes",
    fields: ["Intake Month / Day / Year"],
  },
  english_requirements_backup: {
    label: "English Proficiency",
    fields: ["Test Type", "Listening", "Speaking", "Writing", "Reading", "Overall"],
  },
  academic_requirements_backup: {
    label: "Academic Requirements",
    fields: ["Academic Level", "Score", "Score Type", "Country"],
  },
  scholarships_backup: {
    label: "Scholarships",
    fields: ["Name", "Details", "Eligibility", "Amount", "Currency"],
  },
};

function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-AU", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

export default function BackupPage() {
  const { toast } = useToast();
  const [data, setData] = useState<TableBackupInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${BASE}/api/backup`);
      const json = await res.json();
      if (json.ok) setData(json.backups);
    } catch {
      toast({ title: "Failed to load backup info", variant: "destructive" });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { load(); }, [load]);

  const triggerBackup = async () => {
    setRunning(true);
    try {
      const res = await fetch(`${BASE}/api/backup`, { method: "POST" });
      const json = await res.json();
      if (json.ok) {
        const total = Object.values(json.inserted as Record<string, number>).reduce((a, b) => a + b, 0);
        toast({
          title: "Backup successful",
          description: `${total.toLocaleString()} rows backed up at ${fmt(json.backedUpAt)}`,
        });
        await load();
      } else {
        throw new Error(json.error);
      }
    } catch (err: unknown) {
      toast({ title: "Backup failed", description: String(err), variant: "destructive" });
    } finally {
      setRunning(false);
    }
  };

  const latestSnap = data.length > 0
    ? data.map(d => d.lastBackedUp).filter(Boolean).sort().pop()
    : null;

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Shield className="w-6 h-6 text-cyan-600" />
            Data Backup
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Protects manually entered data from being overwritten by future scraping imports.
            Each backup snapshot is stored inside the database and can be reused to map scraped records.
          </p>
        </div>
        <Button onClick={triggerBackup} disabled={running || loading} className="shrink-0 cursor-pointer">
          {running ? <RefreshCw className="w-4 h-4 mr-2 animate-spin" /> : <Database className="w-4 h-4 mr-2" />}
          {running ? "Backing up…" : "Create New Backup"}
        </Button>
      </div>

      {/* Last snapshot banner */}
      {latestSnap && (
        <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-lg px-4 py-3">
          <CheckCircle2 className="w-5 h-5 text-green-600 shrink-0" />
          <div>
            <p className="text-sm font-medium text-green-800">Last backup: {fmt(latestSnap)}</p>
            <p className="text-xs text-green-600">All 6 protected tables have a stored snapshot. Safe to import new scraping data.</p>
          </div>
        </div>
      )}

      {/* Protected fields notice */}
      <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 flex gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
        <div className="text-sm text-amber-800">
          <p className="font-semibold mb-1">Protected fields — never deleted by scraping</p>
          <p className="text-amber-700">
            Duration · Term · Study Mode · Intake · Course Location · International Fee ·
            Fee Term · Year · English Proficiency · Academic Requirements · Scholarships
          </p>
        </div>
      </div>

      {/* Table cards */}
      {loading ? (
        <div className="text-sm text-muted-foreground py-10 text-center">Loading backup status…</div>
      ) : (
        <div className="grid sm:grid-cols-2 gap-4">
          {data.map((t) => {
            const meta = TABLE_LABELS[t.table];
            return (
              <div key={t.table} className="border rounded-lg p-4 space-y-3 bg-white">
                <div className="flex items-center justify-between">
                  <div>
                    <p className="font-semibold text-sm">{meta?.label ?? t.table}</p>
                    <p className="text-xs text-muted-foreground">← {t.source}</p>
                  </div>
                  <span className="text-xs bg-cyan-50 text-cyan-700 border border-cyan-200 rounded-full px-2 py-0.5">
                    {t.totalBackedUpRows.toLocaleString()} rows
                  </span>
                </div>

                {meta?.fields && (
                  <div className="flex flex-wrap gap-1">
                    {meta.fields.map((f) => (
                      <span key={f} className="text-[11px] bg-gray-100 text-gray-600 rounded px-1.5 py-0.5">{f}</span>
                    ))}
                  </div>
                )}

                <div className="text-xs text-muted-foreground flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  Last backed up: {fmt(t.lastBackedUp)}
                </div>

                {t.snapshots.length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                      {t.snapshots.length} snapshot{t.snapshots.length > 1 ? "s" : ""}
                    </summary>
                    <ul className="mt-1 space-y-0.5 pl-2">
                      {t.snapshots.map((s) => (
                        <li key={s.snap_date} className="flex justify-between text-gray-500">
                          <span>{s.snap_date}</span>
                          <span>{Number(s.rows).toLocaleString()} rows</span>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

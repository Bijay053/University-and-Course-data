import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

const BASE = "";

interface SnapshotMeta {
  id: number;
  course_url: string;
  snapshot_type: string;
  storage_path: string | null;
  fetch_method: string | null;
  content_length: number | null;
  fetched_at: string | null;
}

interface FieldChange {
  old: unknown;
  new: unknown;
}

interface CourseDiff {
  url: string;
  snapshot_key: string | null;
  fetch_method: string | null;
  fetched_at: string | null;
  new_name: string;
  changes: Record<string, FieldChange>;
}

interface ReplayResult {
  job_id: string;
  replayed: number;
  changed: number;
  unchanged: number;
  errors: number;
  commit: boolean;
  message: string;
  diffs: CourseDiff[];
}

interface SnapshotListResponse {
  job_id: string;
  total: number;
  s3_enabled: boolean;
  db_records: SnapshotMeta[];
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.join(", ") || "—";
  return String(v);
}

function FieldDiffRow({ field, change }: { field: string; change: FieldChange }) {
  return (
    <div className="grid grid-cols-3 gap-2 py-1 border-b border-gray-100 text-xs">
      <span className="font-mono text-gray-500">{field}</span>
      <span className="text-red-600 line-through">{fmt(change.old)}</span>
      <span className="text-emerald-600">{fmt(change.new)}</span>
    </div>
  );
}

function CourseDiffCard({ diff }: { diff: CourseDiff }) {
  const [open, setOpen] = useState(false);
  const fieldCount = Object.keys(diff.changes).length;
  return (
    <div className="border border-gray-200 rounded-lg mb-2 overflow-hidden">
      <button
        className="w-full text-left px-3 py-2 flex items-center justify-between bg-gray-50 hover:bg-gray-100 transition-colors"
        onClick={() => setOpen(o => !o)}
      >
        <div className="flex-1 min-w-0">
          <span className="font-medium text-[13px] text-gray-800 truncate block">
            {diff.new_name || diff.url.split("/").pop()}
          </span>
          <span className="text-[10px] text-gray-400 truncate block">{diff.url}</span>
        </div>
        <div className="flex items-center gap-2 ml-3 flex-shrink-0">
          <Badge variant="outline" className="text-amber-700 border-amber-300 bg-amber-50">
            {fieldCount} field{fieldCount !== 1 ? "s" : ""} changed
          </Badge>
          <span className="text-gray-400">{open ? "▲" : "▼"}</span>
        </div>
      </button>
      {open && (
        <div className="px-3 py-2 bg-white">
          <div className="grid grid-cols-3 gap-2 py-1 mb-1">
            <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">Field</span>
            <span className="text-[10px] font-semibold text-red-500 uppercase tracking-wide">Before</span>
            <span className="text-[10px] font-semibold text-emerald-600 uppercase tracking-wide">After</span>
          </div>
          {Object.entries(diff.changes).map(([field, change]) => (
            <FieldDiffRow key={field} field={field} change={change} />
          ))}
          <div className="mt-2 text-[10px] text-gray-400 flex gap-4">
            {diff.fetch_method && <span>Method: <span className="font-mono">{diff.fetch_method}</span></span>}
            {diff.fetched_at && <span>Snapshot: {new Date(diff.fetched_at).toLocaleString()}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

export function SnapshotReplayPanel({ jobId }: { jobId: string }) {
  const [snapshots, setSnapshots] = useState<SnapshotListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [replayResult, setReplayResult] = useState<ReplayResult | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  async function loadSnapshots() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${BASE}/api/scrape/snapshots/${jobId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSnapshots(await res.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  async function runReplay(commit: boolean) {
    if (commit) setCommitting(true);
    else setReplaying(true);
    setError(null);
    setReplayResult(null);
    try {
      const endpoint = commit
        ? `${BASE}/api/scrape/replay/${jobId}/commit`
        : `${BASE}/api/scrape/replay/${jobId}`;
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_courses: 500 }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setReplayResult(await res.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setReplaying(false);
      setCommitting(false);
    }
  }

  const visibleDiffs = replayResult
    ? showAll
      ? replayResult.diffs
      : replayResult.diffs.slice(0, 20)
    : [];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-800">HTML Snapshot Replay</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Re-run extractors against saved S3 snapshots — no live fetch, no Scrape.do cost.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={loadSnapshots} disabled={loading}>
          {loading ? "Loading…" : "Load Snapshots"}
        </Button>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Snapshot summary */}
      {snapshots && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 space-y-2">
          <div className="flex items-center gap-4 flex-wrap">
            <span className="text-xs font-medium text-blue-800">
              {snapshots.total} snapshot{snapshots.total !== 1 ? "s" : ""} in DB
            </span>
            <Badge
              variant="outline"
              className={snapshots.s3_enabled
                ? "text-emerald-700 border-emerald-300 bg-emerald-50"
                : "text-red-700 border-red-300 bg-red-50"}
            >
              S3 {snapshots.s3_enabled ? "connected" : "not configured"}
            </Badge>
          </div>
          {snapshots.total === 0 && (
            <p className="text-xs text-blue-600">
              No snapshots yet — they are saved automatically during the next scrape.
            </p>
          )}
          {snapshots.db_records.length > 0 && (
            <div className="mt-2 max-h-40 overflow-y-auto border border-blue-100 rounded bg-white">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-blue-50">
                  <tr>
                    <th className="text-left px-2 py-1 text-blue-700 font-medium">URL</th>
                    <th className="text-left px-2 py-1 text-blue-700 font-medium">Method</th>
                    <th className="text-left px-2 py-1 text-blue-700 font-medium">Size</th>
                    <th className="text-left px-2 py-1 text-blue-700 font-medium">Saved</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.db_records.slice(0, 50).map(s => (
                    <tr key={s.id} className="border-t border-gray-50 hover:bg-gray-50">
                      <td className="px-2 py-1 max-w-[300px] truncate text-gray-700 font-mono">
                        {s.course_url.split("/").slice(-2).join("/")}
                      </td>
                      <td className="px-2 py-1 text-gray-500">{s.fetch_method || "—"}</td>
                      <td className="px-2 py-1 text-gray-500">
                        {s.content_length ? `${Math.round(s.content_length / 1024)}KB` : "—"}
                      </td>
                      <td className="px-2 py-1 text-gray-500">
                        {s.fetched_at ? new Date(s.fetched_at).toLocaleTimeString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Replay actions */}
      <div className="flex gap-3 flex-wrap">
        <Button
          size="sm"
          variant="outline"
          onClick={() => runReplay(false)}
          disabled={replaying || committing || !snapshots || snapshots.total === 0}
          className="border-blue-300 text-blue-700 hover:bg-blue-50"
        >
          {replaying ? "Replaying…" : "▶ Replay (diff only)"}
        </Button>
        <Button
          size="sm"
          onClick={() => runReplay(true)}
          disabled={replaying || committing || !replayResult || replayResult.changed === 0}
          className="bg-emerald-600 hover:bg-emerald-700 text-white"
        >
          {committing ? "Committing…" : "✓ Commit changes"}
        </Button>
        {replayResult && replayResult.changed === 0 && !committing && (
          <span className="text-xs text-gray-400 self-center">Nothing to commit</span>
        )}
      </div>

      {/* Replay result summary */}
      {replayResult && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <Badge variant="outline" className="text-blue-700 border-blue-200">
              {replayResult.replayed} replayed
            </Badge>
            <Badge variant="outline" className="text-amber-700 border-amber-300 bg-amber-50">
              {replayResult.changed} changed
            </Badge>
            <Badge variant="outline" className="text-emerald-700 border-emerald-300 bg-emerald-50">
              {replayResult.unchanged} unchanged
            </Badge>
            {replayResult.errors > 0 && (
              <Badge variant="outline" className="text-red-700 border-red-300 bg-red-50">
                {replayResult.errors} errors
              </Badge>
            )}
            {replayResult.commit && (
              <Badge className="bg-emerald-100 text-emerald-800 border-emerald-300">
                ✓ committed
              </Badge>
            )}
          </div>
          <p className="text-xs text-gray-600">{replayResult.message}</p>

          {/* Diff cards */}
          {replayResult.diffs.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-gray-700 mb-2">
                Changed courses ({replayResult.diffs.length})
              </h4>
              {visibleDiffs.map((diff, i) => (
                <CourseDiffCard key={i} diff={diff} />
              ))}
              {replayResult.diffs.length > 20 && (
                <button
                  className="text-xs text-blue-600 hover:underline mt-1"
                  onClick={() => setShowAll(a => !a)}
                >
                  {showAll
                    ? "Show fewer"
                    : `Show all ${replayResult.diffs.length} changed courses`}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

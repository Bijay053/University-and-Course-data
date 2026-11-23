import type { AssistantMessage } from "./scrape-prompt-assistant";

/** Public, non-executable summary of an action saved in history. Never carries an executable id. */
export type SavedActionSummary = {
  kind?: string; label?: string; state?: string; resultSummary?: string; error?: string;
  jobId?: string; courseCount?: number;
};
export type SavedMessage = {
  threadId: string; jobId: string; position: number;
  role: "user" | "assistant"; content: string; createdAt?: string; turnId?: string;
  attachment?: { filename: string; contentType: string };
  attachments?: AssistantMessage["attachments"];
  sources?: { url?: string; title?: string; excerpt?: string; page?: number }[];
  actions?: SavedActionSummary[];
};

export type TimelineEntry =
  | { kind: "live"; key: string; message: AssistantMessage; liveIndex: number; at: number | null }
  | { kind: "saved"; key: string; message: SavedMessage; at: number | null };

export function isSavedMessage(m: unknown): m is SavedMessage {
  if (!m || typeof m !== "object") return false;
  const r = m as Record<string, unknown>;
  return typeof r.threadId === "string" && !!r.threadId
    && typeof r.jobId === "string" && !!r.jobId
    && typeof r.position === "number" && Number.isFinite(r.position)
    && (r.role === "user" || r.role === "assistant")
    && typeof r.content === "string";
}

export const savedKey = (m: { threadId?: string; position?: number }) =>
  typeof m.threadId === "string" && m.threadId && typeof m.position === "number" && Number.isFinite(m.position)
    ? `${m.threadId}:${m.position}` : null;

const timeOf = (v?: string | null) => {
  if (!v) return null;
  const n = Date.parse(v);
  return Number.isFinite(n) ? n : null;
};

/** Oldest-first, deduplicated by threadId+position. Later arrivals win. */
export function mergeSaved(existing: SavedMessage[], incoming: SavedMessage[]): SavedMessage[] {
  const map = new Map<string, SavedMessage>();
  for (const m of [...existing, ...incoming]) map.set(savedKey(m)!, m);
  return [...map.values()].sort((a, b) => {
    const ta = timeOf(a.createdAt) ?? 0, tb = timeOf(b.createdAt) ?? 0;
    if (ta !== tb) return ta - tb;
    if (a.threadId === b.threadId) return a.position - b.position;
    return a.threadId < b.threadId ? -1 : 1;
  });
}

/**
 * One continuous transcript: saved history followed/interleaved chronologically with the
 * live window. Live versions always replace saved copies (threadId+position, else turnId+role).
 */
export function buildTimeline(saved: SavedMessage[], live: AssistantMessage[]): TimelineEntry[] {
  const liveKeys = new Set<string>();
  const liveTurns = new Set<string>();
  const liveEntries: TimelineEntry[] = live.map((m, i) => {
    const k = savedKey(m);
    if (k) liveKeys.add(k);
    // Legacy fallback only: stable identities keep every message of a turn distinct.
    else if (m.turnId) liveTurns.add(`${m.turnId}:${m.role}`);
    return { kind: "live", key: k ? `live:${k}` : `live:${m.turnId ?? ""}:${m.role}:${i}`, message: m, liveIndex: i, at: timeOf(m.createdAt) };
  });
  const savedEntries: TimelineEntry[] = saved
    .filter(m => !liveKeys.has(savedKey(m)!) && !(m.turnId && liveTurns.has(`${m.turnId}:${m.role}`)))
    .map(m => ({ kind: "saved", key: `saved:${savedKey(m)}`, message: m, at: timeOf(m.createdAt) }));
  if (!liveEntries.length) return savedEntries;
  const firstLive = liveEntries[0].at;
  // Saved messages that precede the live window go first; anything later than the live window's
  // start (e.g. another review) is interleaved by time without reordering live messages.
  const out: TimelineEntry[] = [];
  let s = 0;
  for (const l of liveEntries) {
    while (s < savedEntries.length && (l.at === null ? firstLive === null : (savedEntries[s].at ?? -Infinity) <= l.at)) out.push(savedEntries[s++]);
    out.push(l);
  }
  while (s < savedEntries.length) out.push(savedEntries[s++]);
  return out;
}

const SAVED_ACTION_KEYS = ["kind", "label", "state", "resultSummary", "error", "jobId", "courseCount"] as const;
/**
 * Read-only projection of a live message for retention after it leaves the live window.
 * Only allowlisted public fields are copied; executable action ids are never retained.
 */
export function projectLive(m: AssistantMessage, fallbackJobId: string): SavedMessage | null {
  if (typeof m.threadId !== "string" || !m.threadId || typeof m.position !== "number" || !Number.isFinite(m.position)) return null;
  if (m.role !== "user" && m.role !== "assistant") return null;
  const out: SavedMessage = {
    threadId: m.threadId, jobId: typeof m.jobId === "string" && m.jobId ? m.jobId : fallbackJobId,
    position: m.position, role: m.role, content: String(m.content ?? ""),
  };
  if (typeof m.createdAt === "string") out.createdAt = m.createdAt;
  if (typeof m.turnId === "string") out.turnId = m.turnId;
  if (Array.isArray(m.attachments)) out.attachments = m.attachments.map(a => ({
    index: a.index, filename: a.filename, contentType: a.contentType, size: a.size,
  }));
  if (m.attachment && typeof m.attachment.filename === "string" && typeof m.attachment.contentType === "string")
    out.attachment = { filename: m.attachment.filename, contentType: m.attachment.contentType };
  if (Array.isArray(m.sources)) out.sources = m.sources.filter(Boolean).map(s => ({ url: s.url, title: s.title, excerpt: s.excerpt, page: s.page }));
  if (Array.isArray(m.actions)) out.actions = m.actions.filter(Boolean).map(a => {
    const src = a as unknown as Record<string, unknown>;
    const o: Record<string, unknown> = {};
    for (const k of SAVED_ACTION_KEYS) if (src[k] !== undefined && src[k] !== null) o[k] = src[k];
    return o as SavedActionSummary;
  });
  return out;
}

export function dayLabel(at: number | null, now = Date.now()): string | null {
  if (at === null) return null;
  const d = new Date(at), n = new Date(now);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((startOf(n) - startOf(d)) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: d.getFullYear() === n.getFullYear() ? undefined : "numeric" });
}

export function clockTime(at: number | null): string {
  return at === null ? "" : new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

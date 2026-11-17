import { useCallback, useEffect, useRef, useState } from "react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";
import { isSavedMessage, mergeSaved, projectLive, type SavedMessage } from "./assistant-timeline";
import type { AssistantMessage } from "./scrape-prompt-assistant";

export const HISTORY_PAGE_SIZE = 20;

type HistoryPage = {
  messages: unknown[]; nextCursor?: string | null; retentionNotice?: string;
  universityId?: number | string | null; universityName?: string | null;
};
type State = {
  key: string | null; messages: SavedMessage[]; nextCursor: string | null;
  universityName: string | null; loaded: boolean; loading: boolean; error: string | null;
};
const blank = (key: string | null): State => ({
  key, messages: [], nextCursor: null, universityName: null, loaded: false, loading: !!key, error: null,
});

export type AssistantHistory = {
  messages: SavedMessage[]; universityName: string | null;
  loaded: boolean; loading: boolean; error: string | null; hasMore: boolean;
  loadOlder: () => void; retry: () => void;
  /** Retain read-only projections of an accepted live window for this owner+job only. */
  retainLive: (forKey: string | null, messages: AssistantMessage[]) => void;
};

/**
 * Loads the actor-private university conversation latest-first, one page at a time.
 * Only fetches on identity/job change and on explicit older-page requests (never per poll).
 */
export function useAssistantHistory(jobId: string, viewKey: string | null): AssistantHistory {
  const [state, setState] = useState<State>(() => blank(viewKey));
  const ctrlRef = useRef<AbortController | null>(null);
  const keyRef = useRef(viewKey);
  keyRef.current = viewKey;

  const fetchPage = useCallback(async (cursor: string | null) => {
    const forKey = viewKey;
    if (!forKey || keyRef.current !== forKey || ctrlRef.current) return;
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setState(s => s.key === forKey ? { ...s, loading: true, error: null } : { ...blank(forKey), loading: true });
    try {
      const q = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) });
      if (cursor) q.set("cursor", cursor);
      const res = await fetch(`/api/scrape/jobs/${encodeURIComponent(jobId)}/assistant/history?${q}`, {
        credentials: "include", signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(res.status === 401 || res.status === 403
        ? "You do not have permission to view this saved conversation."
        : await getFetchErrorMessage(res));
      const page = await readResponseJson<HistoryPage>(res);
      if (!page || !Array.isArray(page.messages)) throw new Error("Earlier messages returned an invalid response.");
      if (ctrl.signal.aborted || keyRef.current !== forKey) return;
      const incoming = page.messages.filter(isSavedMessage);
      const next = typeof page.nextCursor === "string" && page.nextCursor && page.nextCursor !== cursor ? page.nextCursor : null;
      setState(s => {
        const base = s.key === forKey ? s : blank(forKey);
        return {
          ...base, messages: mergeSaved(base.messages, incoming), nextCursor: next,
          universityName: typeof page.universityName === "string" && page.universityName ? page.universityName : base.universityName,
          loaded: true, loading: false, error: null,
        };
      });
    } catch (err) {
      if (ctrl.signal.aborted || keyRef.current !== forKey) return;
      // Already loaded pages are retained on failure.
      setState(s => s.key === forKey ? { ...s, loading: false, error: err instanceof Error ? err.message : String(err) } : s);
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null;
    }
  }, [jobId, viewKey]);

  useEffect(() => {
    ctrlRef.current?.abort();
    ctrlRef.current = null;
    setState(blank(viewKey));
    if (viewKey) void fetchPage(null);
    return () => { ctrlRef.current?.abort(); ctrlRef.current = null; };
  }, [viewKey, fetchPage]);

  // Render-time guard: never expose another identity's or job's pages.
  const view = state.key === viewKey ? state : blank(viewKey);
  const stateRef = useRef(view);
  stateRef.current = view;

  const loadOlder = useCallback(() => {
    const s = stateRef.current;
    if (!s.loaded || !s.nextCursor || s.loading || s.error) return;
    void fetchPage(s.nextCursor);
  }, [fetchPage]);
  const retry = useCallback(() => {
    const s = stateRef.current;
    if (s.loading) return;
    void fetchPage(s.loaded ? s.nextCursor : null);
  }, [fetchPage]);

  const retainLive = useCallback((forKey: string | null, live: AssistantMessage[]) => {
    if (!forKey || keyRef.current !== forKey || !Array.isArray(live)) return;
    const projected = live.map(m => projectLive(m, jobId)).filter((m): m is SavedMessage => !!m);
    if (!projected.length) return;
    // Cursor and loaded state are untouched: retention never stands in for server pages.
    setState(s => s.key === forKey ? { ...s, messages: mergeSaved(s.messages, projected) } : s);
  }, [jobId]);

  return {
    retainLive,
    messages: view.messages, universityName: view.universityName, loaded: view.loaded,
    loading: view.loading, error: view.error, hasMore: view.loaded && !!view.nextCursor, loadOlder, retry,
  };
}

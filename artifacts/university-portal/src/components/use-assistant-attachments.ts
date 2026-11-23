import { useEffect, useRef, useState } from "react";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";

export const MAX_ATTACHMENTS = 5;
export type QueuedAttachment = {
  key: string; seq: number; name: string; size: number; file: File;
  status: "uploading" | "ready" | "error"; token: string | null; error: string | null;
};

/** Each upload owns its cancellation/state; finishing one never replaces another. */
export function useAssistantAttachments(
  viewKey: string | null, base: string, validate: (file: File) => string | null,
) {
  const [rows, setRows] = useState<QueuedAttachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const entries = useRef<QueuedAttachment[]>([]);
  const currentKey = useRef(viewKey);
  currentKey.current = viewKey;
  const sequence = useRef(0);
  const controllers = useRef(new Map<number, AbortController>());
  const replace = (next: QueuedAttachment[]) => { entries.current = next; setRows(next); };
  useEffect(() => {
    replace([]); setError(null);
    return () => { controllers.current.forEach(c => c.abort()); controllers.current.clear(); };
  }, [viewKey]);
  const remove = (seq: number) => {
    controllers.current.get(seq)?.abort(); controllers.current.delete(seq);
    replace(entries.current.filter(a => a.seq !== seq)); setError(null);
  };
  const clearSent = (ids: number[]) => {
    replace(entries.current.filter(a => !ids.includes(a.seq)));
    setError(null);
  };
  const upload = async (row: QueuedAttachment) => {
    if (row.status === "error") return;
    const controller = new AbortController();
    controllers.current.set(row.seq, controller);
    const current = () => !controller.signal.aborted && currentKey.current === row.key
      && entries.current.some(a => a.seq === row.seq);
    const update = (patch: Partial<QueuedAttachment>) => {
      if (current()) replace(entries.current.map(a => a.seq === row.seq ? { ...a, ...patch } : a));
    };
    try {
      const res = await fetch(`${base}/documents/upload-url`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: row.name, size: row.size, contentType: row.file.type }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(await getFetchErrorMessage(res));
      const grant = await readResponseJson<{
        transport?: string; uploadUrl?: string; fields?: Record<string, string>; documentToken: string;
      }>(res);
      if (!grant?.documentToken || (grant.transport !== "portal" && !grant.uploadUrl))
        throw new Error("Upload could not be prepared.");
      if (!current()) return;
      let up: Response;
      if (grant.transport === "portal") {
        up = await fetch(`${base}/documents/content`, {
          method: "POST", credentials: "include", body: row.file,
          headers: { "Content-Type": row.file.type, "X-Document-Token": grant.documentToken },
          signal: controller.signal,
        });
      } else {
        const form = new FormData();
        Object.entries(grant.fields ?? {}).forEach(([k, v]) => form.append(k, String(v)));
        form.append("file", row.file);
        up = await fetch(grant.uploadUrl!, { method: "POST", body: form, signal: controller.signal });
      }
      if (!up.ok) throw new Error(await getFetchErrorMessage(up));
      update({ status: "ready", token: grant.documentToken, error: null });
    } catch (e) {
      update({ status: "error", error: e instanceof Error ? e.message : String(e) });
    } finally { controllers.current.delete(row.seq); }
  };
  const add = (files: File[]) => {
    if (!viewKey || !files.length) return;
    const existing = entries.current.filter(a => a.key === viewKey);
    if (existing.length + files.length > MAX_ATTACHMENTS) {
      setError("Attach up to 5 files per message. Remove a file or select fewer files.");
      return;
    }
    setError(null);
    const added = files.map(file => {
      const invalid = validate(file);
      return { key: viewKey, seq: ++sequence.current, name: file.name, size: file.size,
        file, status: invalid ? "error" as const : "uploading" as const,
        error: invalid, token: null };
    });
    replace([...existing, ...added]);
    added.forEach(row => { void upload(row); });
  };
  return { attachments: rows.filter(a => a.key === viewKey), error, add, remove, clearSent };
}

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { getFetchErrorMessage, readResponseJson } from "@/lib/readResponseJson";

type Status = { paused: boolean; canResume: boolean; universityName: string; reason: string | null };

/** Mounted with an identity/job key, so private provider state cannot leak across sessions. */
export function AssistantImportStatus({ base, onStatus }: {
  base: string; onStatus: (paused: boolean | null) => void;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    const load = async () => {
      const version = generation.current;
      try {
        const response = await fetch(`${base}/import-status`, { credentials: "include", signal: controller.signal });
        if (!response.ok) throw new Error(await getFetchErrorMessage(response));
        const value = await readResponseJson<Status>(response);
        if (!value) throw new Error("Import status was empty.");
        if (!controller.signal.aborted && version === generation.current) { setStatus(value); onStatus(value.paused); setError(""); }
      } catch (e) {
        if (!controller.signal.aborted && version === generation.current) { setError(String(e)); onStatus(null); }
      }
    };
    void load();
    const interval = setInterval(load, 10000);
    return () => { mounted.current = false; controller.abort(); clearInterval(interval); };
  }, [base, onStatus]);

  const resume = async () => {
    generation.current += 1;
    setBusy(true); setError("");
    try {
      const response = await fetch(`${base}/resume-imports`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ consolidationComplete: confirmed, reason }),
      });
      if (!response.ok) throw new Error(await getFetchErrorMessage(response));
      const value = await readResponseJson<Status>(response);
      if (!value) throw new Error("Resume status was empty.");
      if (!mounted.current) return;
      generation.current += 1;
      setStatus(value); onStatus(value.paused); setEditing(false);
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <section aria-label="Assistant import status" className="border-b px-4 py-3 text-sm">
    {error && <p role="alert">Import status could not be confirmed: {error}</p>}
    {!status && !error && <p>Checking provider import status…</p>}
    {status?.paused ? <>
      <p className="font-semibold">Assistant imports paused — {status.universityName}</p>
      <p>{status.reason} Import actions stay proposed; resuming does not run them or publish courses.</p>
      {!status.canResume && <p>Only an owner administrator can resume imports after consolidation.</p>}
      {status.canResume && !editing && <Button variant="outline" onClick={() => setEditing(true)}>Review resume requirements</Button>}
      {status.canResume && editing && <div className="mt-2 space-y-2">
        <label className="block"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> I confirm this provider’s consolidation has been reviewed and completed.</label>
        <label className="block">Consolidation review outcome
          <textarea className="block w-full rounded border p-2" value={reason} maxLength={1000} onChange={e => setReason(e.target.value)} />
        </label>
        <Button disabled={busy || !confirmed || reason.trim().length < 10} onClick={resume}>Resume imports</Button>
        <Button variant="ghost" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
      </div>}
    </> : status && <p>Assistant imports enabled. Existing proposals still require their normal checks and confirmation.</p>}
  </section>;
}

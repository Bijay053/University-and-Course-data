import { useEffect, useState } from "react";
import { BASE } from "./lib";

type Insight = { id: number; country: string; text: string; updated_at: string | null };

export function UniversityInsights({ universityId, universityName }: {
  universityId: number; universityName: string;
}) {
  const [state, setState] = useState<{ id: number; notes: Insight[]; error: boolean } | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState(null);
    async function load() {
      try {
        const response = await fetch(`${BASE}/api/search/universities/${universityId}/insights`, { signal: controller.signal });
        if (!response.ok) throw new Error("Could not load insights");
        const body = await response.json();
        if (!Array.isArray(body.insights)) throw new Error("Invalid insights response");
        if (!controller.signal.aborted) setState({ id: universityId, notes: body.insights, error: false });
      } catch {
        if (!controller.signal.aborted) setState({ id: universityId, notes: [], error: true });
      }
    }
    void load();
    return () => controller.abort();
  }, [universityId, attempt]);
  const current = state?.id === universityId ? state : null;
  return (
    <section aria-label={`Key Insights — ${universityName}`} className="my-5 rounded-xl border border-[hsl(var(--sic-line))] bg-white p-4">
      <h3 className="sic-display text-lg">Key Insights</h3>
      <p className="mb-3 text-sm text-[hsl(var(--sic-muted))]">{universityName}</p>
      {!current ? <p role="status" className="text-sm">Loading insights…</p> : current.error ? (
        <p role="alert" className="text-sm">Could not load Key Insights.{" "}
          <button type="button" className="text-primary underline" onClick={() => setAttempt((n) => n + 1)}>Retry</button>
        </p>
      ) : current.notes.length === 0 ? (
        <p className="text-sm text-[hsl(var(--sic-muted))]">No Key Insights have been added for this university yet.</p>
      ) : (
        <div className="space-y-3">
          {current.notes.map((note) => (
            <details key={note.id} className="rounded-lg border border-[hsl(var(--sic-line))] p-3" open={current.notes.length === 1}>
              <summary className="cursor-pointer text-sm font-semibold">{note.country || "General guidance"}</summary>
              <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-relaxed">{note.text}</p>
              {note.updated_at && !Number.isNaN(Date.parse(note.updated_at)) && (
                <p className="mt-3 text-xs text-[hsl(var(--sic-muted))]">Updated {new Date(note.updated_at).toLocaleDateString()}</p>
              )}
            </details>
          ))}
          <p className="text-xs text-[hsl(var(--sic-muted))]">Guidance may vary by applicant country and change over time. Confirm current requirements with the university.</p>
        </div>
      )}
    </section>
  );
}

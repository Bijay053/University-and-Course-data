import { useEffect, useMemo, useState } from "react";
import { ClipboardList } from "lucide-react";
import { BASE } from "./lib";
import { InsightCardsGrid, type InsightCard } from "@/components/insights/insight-cards";
import { ReportCourseErrorButton } from "@/components/report-course-error";

type Insight = { id: number; country: string; text: string; updated_at: string | null; parsed_data?: InsightCard[] | null };

/** Public, read-only Key Insights: same card design as the admin view, without edit controls. */
export function UniversityInsights({ universityId, universityName, compact = false, hideEmpty = false }: {
  universityId: number; universityName: string; compact?: boolean; hideEmpty?: boolean;
}) {
  const [state, setState] = useState<{ id: number; notes: Insight[]; error: boolean } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [country, setCountry] = useState("__all__");
  useEffect(() => { setCountry("__all__"); }, [universityId]);
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
  const countries = useMemo(() => Array.from(new Set((current?.notes ?? []).map((n) => n.country || "General guidance"))).sort(), [current]);
  const shown = (current?.notes ?? []).filter((n) => country === "__all__" || (n.country || "General guidance") === country);

  if (current && !current.error && current.notes.length === 0) return null;

  if (hideEmpty && current && !current.error && current.notes.length === 0) return null;
  return (
    <section aria-label={`Key Insights — ${universityName}`} className={compact ? "my-5 rounded-xl border border-[hsl(var(--sic-line))] bg-white p-4" : ""} data-testid="section-insights">
      {compact && <><h3 className="sic-display text-lg">Key Insights</h3><p className="mb-3 text-sm text-[hsl(var(--sic-muted))]">{universityName}</p></>}
      {!current ? (
        <div role="status" aria-label="Loading insights" className="space-y-3">
          <div className="h-8 w-64 animate-pulse rounded-full bg-[hsl(var(--sic-mist))]" />
          <div className="h-48 animate-pulse rounded-xl bg-[hsl(var(--sic-mist))]" />
        </div>
      ) : current.error ? (
        <div role="alert" className="rounded-xl border border-[hsl(var(--sic-line))] bg-white p-6 text-center text-sm">
          Could not load Key Insights.{" "}
          <button type="button" className="font-semibold text-primary underline" onClick={() => setAttempt((n) => n + 1)} data-testid="button-retry-insights">Retry</button>
        </div>
      ) : current.notes.length === 0 ? (
        <div className="rounded-xl border p-12 text-center text-[hsl(var(--sic-muted))]" data-testid="state-insights-empty">
          <ClipboardList className="mx-auto mb-3 h-10 w-10 opacity-30" aria-hidden />
          <p>No Key Insights have been added for this university yet.</p>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-[hsl(var(--sic-muted))]">
            {current.notes.length} insight{current.notes.length !== 1 ? "s" : ""} across {countries.length} countr{countries.length !== 1 ? "ies" : "y"}
          </p>
          {countries.length > 0 && (
            <div className="flex flex-wrap gap-2" role="group" aria-label="Filter by country">
              {["__all__", ...countries].map((c) => (
                <button key={c} type="button" aria-pressed={country === c} onClick={() => setCountry(c)} data-testid={`pill-country-${c}`}
                  className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${country === c ? "bg-red-600 text-white border-red-600" : "bg-white text-gray-600 border-gray-200 hover:border-red-300"}`}>
                  {c === "__all__" ? "All countries" : c}
                </button>
              ))}
            </div>
          )}
          {shown.map((note) => (
            <div key={note.id} className="border rounded-xl overflow-hidden bg-white" data-testid={`insight-note-${note.id}`}>
              <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 bg-red-50 border-b border-red-100">
                <div className="flex items-center gap-2">
                  <ClipboardList className="w-4 h-4 text-red-600" aria-hidden />
                  <span className="font-semibold text-sm text-red-800">{note.country || "General guidance"}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                {note.updated_at && !Number.isNaN(Date.parse(note.updated_at)) && (
                  <span className="text-xs text-red-700/70">Updated {new Date(note.updated_at).toLocaleDateString()}</span>
                )}
                <ReportCourseErrorButton
                  courseName={`Key Insights — ${note.country || "General guidance"}`}
                  universityName={universityName}
                  insight={{ universityId, id: note.id, country: note.country || "General guidance" }}
                  className="inline-flex min-h-9 items-center gap-1.5 text-xs font-semibold text-red-700 hover:text-red-900"
                />
                </div>
              </div>
              {note.parsed_data && note.parsed_data.length > 0 ? (
                <InsightCardsGrid cards={note.parsed_data} />
              ) : (
                <p className="p-4 whitespace-pre-wrap break-words text-sm leading-relaxed">{note.text}</p>
              )}
            </div>
          ))}
          <p className="text-xs text-[hsl(var(--sic-muted))]">Guidance may vary by applicant country and change over time. Confirm current requirements with the university.</p>
        </div>
      )}
    </section>
  );
}

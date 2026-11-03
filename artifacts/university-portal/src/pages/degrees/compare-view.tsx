import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import { BASE, type CompareCourse, NOT_PUBLISHED, feeLines, formatDuration, formatFeeLine, safeHttpUrl } from "./lib";

type Props = { ids: number[]; onClose: () => void };

export function CompareView({ ids, onClose }: Props) {
  const [courses, setCourses] = useState<CompareCourse[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setError(null); setCourses(null);
    try {
      const res = await fetch(`${BASE}/api/search/compare?ids=${ids.join(",")}`);
      if (!res.ok) throw new Error("Comparison is unavailable right now.");
      const json = await res.json() as { courses: CompareCourse[] };
      setCourses(json.courses ?? []);
    } catch (e) { setError(e instanceof Error ? e.message : "Comparison failed."); }
  }, [ids]);
  useEffect(() => { void load(); }, [load]);

  const rows: { label: string; render: (c: CompareCourse) => React.ReactNode }[] = [
    { label: "University", render: (c) => c.university?.name || NOT_PUBLISHED },
    { label: "Level", render: (c) => c.degree_level || NOT_PUBLISHED },
    { label: "Location", render: (c) => c.course_location || [c.university?.city, c.university?.country].filter(Boolean).join(", ") || NOT_PUBLISHED },
    { label: "Duration", render: (c) => formatDuration(c.duration, c.duration_term) },
    { label: "Study mode", render: (c) => c.study_mode || NOT_PUBLISHED },
    { label: "Intakes", render: (c) => c.intakes?.length ? c.intakes.join(", ") : NOT_PUBLISHED },
    { label: "Fees (as published)", render: (c) => (
      <ul className="space-y-1">{feeLines(c).map((f, i) => <li key={i}>{f.label && (c.offerings?.length ?? 0) > 1 ? `${f.label}: ` : ""}{formatFeeLine(f)}</li>)}</ul>
    ) },
    { label: "English", render: (c) => c.english_requirements?.length ? (
      <ul className="space-y-1">{c.english_requirements.map((e, i) => <li key={i}>{e.test_type || e.test_name || "Test"} {e.overall ?? NOT_PUBLISHED}</li>)}</ul>
    ) : NOT_PUBLISHED },
    { label: "Academic entry", render: (c) => c.academic_requirements?.length ? (
      <ul className="space-y-1">{c.academic_requirements.map((a, i) => <li key={i}>{[a.academic_level, a.academic_score != null ? `${a.academic_score}${a.score_type ? ` ${a.score_type}` : ""}` : null].filter(Boolean).join(", ") || NOT_PUBLISHED}</li>)}</ul>
    ) : NOT_PUBLISHED },
    { label: "Official page", render: (c) => { const u = safeHttpUrl(c.course_url); return u ? <a className="text-[hsl(var(--sic-teal))] underline" href={u} target="_blank" rel="noopener noreferrer">Visit</a> : NOT_PUBLISHED; } },
  ];

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-[hsl(213_60%_12%/0.5)] p-3 sm:p-8" role="dialog" aria-modal="true" aria-labelledby="compare-title" aria-describedby="compare-desc"
      onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <div className="sic-rise mx-auto max-w-6xl rounded-2xl bg-[hsl(var(--sic-paper))] shadow-2xl" data-testid="dialog-compare">
        <header className="flex items-center justify-between p-5 border-b border-[hsl(var(--sic-line))]">
          <div>
            <h2 id="compare-title" className="sic-display text-2xl">Side by side</h2>
            <p id="compare-desc" className="text-xs text-[hsl(var(--sic-muted))]">Fees appear in their published currency. They are not converted or ranked against each other.</p>
          </div>
          <button type="button" autoFocus onClick={onClose} aria-label="Close comparison" data-testid="button-close-compare" className="grid h-9 w-9 place-items-center rounded-full hover:bg-[hsl(var(--sic-teal-soft))]"><X className="h-4 w-4" aria-hidden /></button>
        </header>
        <div className="p-5">
          {error ? (
            <div role="alert" className="rounded-xl bg-white p-6 text-sm">{error} <button type="button" onClick={load} className="ml-2 font-semibold text-[hsl(var(--sic-teal))] underline" data-testid="button-retry-compare">Retry</button></div>
          ) : !courses ? (
            <div className="grid gap-3" aria-busy="true">{[0, 1, 2, 3].map((i) => <div key={i} className="sic-skel h-10" />)}</div>
          ) : courses.length === 0 ? (
            <p className="text-sm">These courses are no longer listed.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-separate border-spacing-0 text-sm">
                <thead>
                  <tr>
                    <th className="w-36 sticky left-0 bg-[hsl(var(--sic-paper))]" scope="col"><span className="sr-only">Attribute</span></th>
                    {courses.map((c) => <th key={c.id} scope="col" className="p-3 text-left align-bottom sic-display text-base font-semibold">{c.course_name}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.label}>
                      <th scope="row" className="sticky left-0 bg-[hsl(var(--sic-paper))] p-3 text-left align-top text-xs uppercase tracking-[0.1em] text-[hsl(var(--sic-muted))] border-t border-[hsl(var(--sic-line))]">{r.label}</th>
                      {courses.map((c) => <td key={c.id} className="p-3 align-top border-t border-[hsl(var(--sic-line))]">{r.render(c)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
              {courses.length < ids.length && <p className="mt-3 text-xs text-[hsl(var(--sic-muted))]">{ids.length - courses.length} shortlisted course(s) are no longer listed and were left out.</p>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

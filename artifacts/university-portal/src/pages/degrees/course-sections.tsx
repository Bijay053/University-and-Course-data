import type { ReactNode } from "react";
import { Award, MapPin } from "lucide-react";
import type { Scholarship } from "./lib";

export const hasText = (v: string | null | undefined) => !!v && !!v.trim() && v.trim().toLowerCase() !== "not published";

/** Split comma/semicolon/pipe/newline-separated location strings into trimmed, case-insensitively unique places. */
export function splitLocations(raw: (string | null | undefined)[]): string[] {
  const seen = new Map<string, string>();
  for (const r of raw) {
    if (!hasText(r)) continue;
    for (const part of (r as string).split(/[,;|\n]+/)) {
      const t = part.replace(/\s+/g, " ").trim();
      if (hasText(t) && !seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
    }
  }
  return Array.from(seen.values());
}

/** Calm numbered section used down the left column of the course page. */
export function CourseSection({ id, index, title, aside, children }: { id: string; index: number; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`h-${id}`} data-testid={`section-${id}`} className="scroll-mt-6 border-t border-[hsl(var(--sic-line))] pt-5 first:border-t-0 first:pt-0">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 id={`h-${id}`} className="flex items-baseline gap-2.5 text-base font-semibold text-[hsl(var(--sic-ink))]">
          <span className="text-xs font-semibold tabular-nums text-primary" aria-hidden>{String(index).padStart(2, "0")}</span>{title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function LocationList({ places }: { places: string[] }) {
  return (
    <ul className="grid gap-x-4 gap-y-1.5 sm:grid-cols-2" data-testid="list-course-locations">
      {places.map((p) => (
        <li key={p} className="flex items-center gap-2 text-sm text-[hsl(var(--sic-ink))]">
          <MapPin className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden /><span className="break-words">{p}</span>
        </li>
      ))}
    </ul>
  );
}

function valueBadges(s: Scholarship) {
  const out: string[] = [];
  if (s.amount != null) out.push(`${hasText(s.currency) ? `${s.currency!.trim()} ` : ""}${s.amount.toLocaleString()}`);
  if (s.percentage != null) out.push(`${s.percentage}%`);
  return out;
}

export function ScholarshipList({ items }: { items: Scholarship[] }) {
  return (
    <ul className="space-y-3" data-testid="list-scholarships">
      {items.map((s) => {
        const badges = valueBadges(s);
        return (
          <li key={s.id} className="rounded-xl border border-[hsl(var(--sic-line))] bg-white p-4" data-testid={`card-scholarship-${s.id}`}>
            <div className="flex flex-wrap items-start justify-between gap-2">
              <p className="flex min-w-0 items-start gap-2 font-semibold leading-snug text-[hsl(var(--sic-navy))]">
                <Award className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden /><span className="break-words">{s.name}</span>
              </p>
              {badges.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {badges.map((b) => <span key={b} className="rounded-full bg-[hsl(var(--sic-navy))] px-2.5 py-0.5 text-xs font-semibold tabular-nums text-white" data-testid={`text-scholarship-value-${s.id}`}>{b}</span>)}
                </div>
              )}
            </div>
            {hasText(s.details) && <p className="mt-2 whitespace-pre-line break-words text-sm leading-relaxed text-[hsl(var(--sic-ink))]">{s.details}</p>}
            {hasText(s.eligibility_criteria) && (
              <div className="mt-3 rounded-lg bg-[hsl(var(--sic-mist))] px-3 py-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">Eligibility</p>
                <p className="mt-0.5 whitespace-pre-line break-words text-sm leading-relaxed">{s.eligibility_criteria}</p>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

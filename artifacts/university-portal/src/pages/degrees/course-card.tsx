import { Bookmark, BookmarkCheck, MapPin, Clock, CalendarDays } from "lucide-react";
import { type CourseResult, NOT_PUBLISHED, feeLines, formatDuration, headlineFee, locationText } from "./lib";

type Props = {
  course: CourseResult; view: "grid" | "list"; selected: boolean; shortlisted: boolean;
  onSelect: () => void; onToggleShortlist: () => void; index: number;
};

export function CourseCard({ course, view, selected, shortlisted, onSelect, onToggleShortlist, index }: Props) {
  const hasFee = feeLines(course).some((fee) => fee.amount != null);
  // Keep an 8px gap beside the corner control, including the larger phone
  // button. In wide list cards with fees, that control is in the fee column.
  const controlClearance = view === "list" && hasFee ? "pr-11 sm:pr-0" : "pr-11 sm:pr-9";
  const meta = (
    <ul className={`min-w-0 break-words text-[13px] text-[hsl(var(--sic-muted))] ${view === "list" ? "flex flex-wrap gap-x-5 gap-y-1" : "space-y-1"}`}>
      {locationText(course) !== NOT_PUBLISHED && <li className="flex items-center gap-1.5"><MapPin className="h-3.5 w-3.5 shrink-0" aria-hidden />{locationText(course)}</li>}
      {course.duration != null && <li className="flex items-center gap-1.5"><Clock className="h-3.5 w-3.5 shrink-0" aria-hidden />{formatDuration(course.duration, course.duration_term)}</li>}
      {course.intakes.some((intake) => intake.trim()) && <li className="flex items-center gap-1.5"><CalendarDays className="h-3.5 w-3.5 shrink-0" aria-hidden />{course.intakes.filter((intake) => intake.trim()).join(", ")}</li>}
    </ul>
  );
  return (
    <article style={{ animationDelay: `${Math.min(index, 10) * 30}ms` }} data-testid={`card-course-${course.id}`}
      className={`sic-rise group relative min-w-0 rounded-2xl border bg-white/95 transition-[transform,box-shadow] hover:-translate-y-0.5 hover:shadow-[0_10px_30px_-18px_hsl(213_60%_22%/0.5)] ${selected ? "border-[hsl(var(--sic-teal))] ring-2 ring-[hsl(var(--sic-teal))]/25" : "border-[hsl(var(--sic-line))]"} ${view === "list" ? "p-4 sm:p-5 flex flex-col sm:flex-row sm:items-center gap-4" : "p-4 sm:p-5 flex flex-col gap-3"}`}>
      <div className="flex-1 min-w-0 space-y-2">
        <div className={`flex min-w-0 flex-wrap items-center gap-2 ${controlClearance}`}>
          {course.degree_level?.trim() && <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-teal))] [overflow-wrap:anywhere]">{course.degree_level}</span>}
        </div>
        <h3 className={`sic-display ${controlClearance} text-[17px] sm:text-[19px] leading-snug break-words [overflow-wrap:anywhere]`}>
          <button type="button" onClick={onSelect} aria-pressed={selected} data-testid={`button-select-course-${course.id}`}
            className="block w-full min-w-0 max-w-full whitespace-normal text-left [overflow-wrap:anywhere] after:absolute after:inset-0 after:content-[''] hover:text-[hsl(var(--sic-teal))]">
            {course.course_name}
          </button>
        </h3>
        {course.university.name?.trim() && <p className="text-sm font-medium text-[hsl(var(--sic-navy))]">{course.university.name}</p>}
        {meta}
      </div>
      {hasFee && <div className={`${view === "list" ? "sm:w-56 sm:text-right" : "border-t border-dashed border-[hsl(var(--sic-line))] pt-3"}`}>
        <p className="text-[11px] uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]">International fee</p>
        <p className="text-sm font-semibold" data-testid={`text-fee-${course.id}`}>{headlineFee(course)}</p>
      </div>}
      <button type="button" onClick={onToggleShortlist} aria-pressed={shortlisted} data-testid={`button-shortlist-${course.id}`}
        aria-label={shortlisted ? `Remove ${course.course_name} from shortlist` : `Add ${course.course_name} to shortlist`}
        className={`absolute right-2.5 top-2.5 sm:right-3 sm:top-3 z-10 grid h-10 w-10 sm:h-9 sm:w-9 place-items-center rounded-full border transition-colors ${shortlisted ? "bg-[hsl(var(--sic-teal))] border-transparent text-white" : "bg-white border-[hsl(var(--sic-line))] text-[hsl(var(--sic-navy))] hover:border-[hsl(var(--sic-teal))]"}`}>
        {shortlisted ? <BookmarkCheck className="h-4 w-4" aria-hidden /> : <Bookmark className="h-4 w-4" aria-hidden />}
      </button>
    </article>
  );
}

export function CourseCardSkeleton({ view }: { view: "grid" | "list" }) {
  return (
    <div className={`rounded-2xl border border-[hsl(var(--sic-line))] bg-white/80 p-5 space-y-3 ${view === "list" ? "" : "min-h-[220px]"}`} aria-hidden>
      <div className="sic-skel h-3 w-24" /><div className="sic-skel h-5 w-4/5" /><div className="sic-skel h-4 w-1/2" />
      <div className="sic-skel h-3 w-2/3" /><div className="sic-skel h-3 w-1/3" />
    </div>
  );
}

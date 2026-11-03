import { Link } from "wouter";
import { ArrowRight, X, Bookmark, BookmarkCheck } from "lucide-react";
import { ReportCourseErrorButton } from "@/components/report-course-error";
import { type CourseResult, NOT_PUBLISHED, englishList, feeLines, formatDuration, formatFeeLine, locationText } from "./lib";

type Props = { course: CourseResult | null; shortlisted: boolean; onToggleShortlist: () => void; onClose: () => void };

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[120px_1fr] gap-3 py-2.5 border-b border-[hsl(var(--sic-line))]/70 text-sm">
      <dt className="text-[hsl(var(--sic-muted))]">{k}</dt><dd className="font-medium">{children}</dd>
    </div>
  );
}

export function DetailPanel({ course, shortlisted, onToggleShortlist, onClose }: Props) {
  if (!course) {
    return (
      <div className="rounded-2xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-6 text-sm text-[hsl(var(--sic-muted))]" data-testid="panel-detail-empty">
        <p className="sic-display text-lg text-[hsl(var(--sic-ink))] mb-1">Select a course</p>
        Choose any card to read its fees, duration, intakes and English requirements here.
      </div>
    );
  }
  const english = englishList(course.english_requirements);
  const fees = feeLines(course).filter((fee) => fee.amount != null);
  const field = [course.category, course.sub_category].filter((value) => value?.trim()).join(" / ");
  return (
    <section aria-labelledby="detail-title" className="rounded-2xl border border-[hsl(var(--sic-line))] bg-white overflow-hidden sic-rise" data-testid="panel-detail">
      <div className="sic-hero sic-contour px-5 pt-5 pb-6 text-white relative">
        <button type="button" onClick={onClose} aria-label="Close course details" data-testid="button-close-detail"
          className="absolute right-3 top-3 grid h-8 w-8 place-items-center rounded-full bg-white/10 hover:bg-white/20"><X className="h-4 w-4" aria-hidden /></button>
        {course.degree_level?.trim() && <p className="text-[11px] uppercase tracking-[0.14em] text-white/90">{course.degree_level}</p>}
        <h2 id="detail-title" className="sic-display text-2xl leading-tight mt-1 pr-8">{course.course_name}</h2>
        <p className="text-sm text-white/80 mt-1">{course.university.name}</p>
      </div>
      <div className="p-5">
        <dl>
          {locationText(course) !== NOT_PUBLISHED && <Row k="Location">{locationText(course)}</Row>}
          {course.duration != null && <Row k="Duration">{formatDuration(course.duration, course.duration_term)}</Row>}
          {course.intakes.some((intake) => intake.trim()) && <Row k="Intakes">{course.intakes.filter((intake) => intake.trim()).join(", ")}</Row>}
          {fees.length > 0 && <Row k="Fees">
            <ul className="space-y-1" data-testid="list-detail-fees">
              {fees.map((f, i) => (
                <li key={i}>{f.label && fees.length > 1 && <span className="text-[hsl(var(--sic-muted))]">{f.label}: </span>}{formatFeeLine(f)}</li>
              ))}
            </ul>
          </Row>}
          {english.length > 0 && <Row k="English">
            {(
              <ul className="flex flex-wrap gap-1.5">
                {english.map((e) => <li key={e.exam} className="rounded-full bg-[hsl(var(--sic-teal-soft))] px-2.5 py-0.5 text-xs">{e.exam} {e.score}</li>)}
              </ul>
            )}
          </Row>}
          {field && <Row k="Field">{field}</Row>}
        </dl>
        <div className="mt-5 flex flex-col gap-2">
<Link href={`/toolkit/courses/${course.id}`} data-testid="link-view-details"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-[hsl(var(--sic-navy))] text-sm font-semibold text-white hover:bg-[hsl(var(--sic-teal))]">
            View details <ArrowRight className="h-4 w-4" aria-hidden />
          </Link>
          <button type="button" onClick={onToggleShortlist} aria-pressed={shortlisted} data-testid="button-detail-shortlist"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg border border-[hsl(var(--sic-line))] text-sm font-semibold hover:border-[hsl(var(--sic-teal))]">
            {shortlisted ? <BookmarkCheck className="h-4 w-4" aria-hidden /> : <Bookmark className="h-4 w-4" aria-hidden />}
            {shortlisted ? "On your shortlist" : "Add to shortlist"}
          </button>
          <ReportCourseErrorButton key={course.id} courseId={course.id} courseName={course.course_name} universityName={course.university.name} />
        </div>
        <p className="mt-4 text-xs text-[hsl(var(--sic-muted))]">Fees are shown exactly as published by the institution, in their original currency and period. Always confirm with the university.</p>
      </div>
    </section>
  );
}

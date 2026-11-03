import { useEffect, useState } from "react";
import { Link, useParams } from "wouter";
import { ArrowLeft, ArrowRight, Bookmark, BookmarkCheck, Building2, CalendarDays } from "lucide-react";
import "./degrees/degrees.css";
import { BASE, feeLines, formatDuration, formatFeeLine, type CompareCourse, type CourseResult } from "./degrees/lib";
import { useShortlist } from "./degrees/use-shortlist";
import { usePageMeta } from "./degrees/use-page-meta";
import { UniversityInsights } from "./degrees/university-insights";
import { ReportCourseErrorButton } from "@/components/report-course-error";
import { CourseSection, LocationList, ScholarshipList, hasText as has, splitLocations } from "./degrees/course-sections";

type State = { status: "loading" } | { status: "error" } | { status: "missing" } | { status: "ok"; course: CompareCourse };
const BANDS = ["listening", "reading", "writing", "speaking"] as const;
export default function PublicCoursePage() {
  const params = useParams<{ id: string }>();
  const valid = /^\d+$/.test(params.id ?? "") && Number(params.id) > 0;
  const id = valid ? Number(params.id) : null;
  const [state, setState] = useState<State>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const shortlist = useShortlist();

  useEffect(() => {
    if (id == null) { setState({ status: "missing" }); return; }
    const ac = new AbortController();
    setState({ status: "loading" });
    fetch(`${BASE}/api/search/compare?ids=${id}`, { signal: ac.signal })
      .then(async (r) => {
        if (r.status === 404) return null;
        if (!r.ok) throw new Error();
        const body = await r.json();
        return (Array.isArray(body?.courses) ? body.courses : []).find((c: CompareCourse) => c?.id === id) ?? null;
      })
      .then((c: CompareCourse | null) => setState(c ? { status: "ok", course: c } : { status: "missing" }))
      .catch((e: Error) => { if (e.name !== "AbortError") setState({ status: "error" }); });
    return () => ac.abort();
  }, [id, attempt]);

  const course = state.status === "ok" ? state.course : null;
  usePageMeta(`${course?.course_name ?? "Course"} | Study Info Centre`, course ? `${course.course_name} at ${course.university.name}: published fees, duration, intakes and entry requirements.` : "Course details");

  const fees = course ? feeLines(course).filter((f) => f.amount != null) : [];
  const locations = course ? splitLocations([...(course.offerings ?? []).map((o) => o.location), course.course_location]) : [];
  const scholarships = (course?.scholarships ?? []).filter((x) => x && has(x.name));
  const intakes = course?.intakes.filter(has) ?? [];
  const english = (course?.english_requirements ?? []).filter((e) => e.overall != null || BANDS.some((b) => e[b] != null));
  const academic = (course?.academic_requirements ?? []).filter((a) => has(a.academic_level) || a.academic_score != null);
  const field = course ? [course.category, course.sub_category].filter(has).join(" / ") : "";
  const facts = course ? ([
    ["Level", has(course.degree_level) ? course.degree_level : null],
    ["Duration", course.duration != null ? formatDuration(course.duration, course.duration_term) : null],
    ["Study mode", has(course.study_mode) ? course.study_mode : null],
    ["Field", field || null],
  ] as const).filter(([, v]) => v) : [];
  const shortlisted = course ? shortlist.has(course.id) : false;

  const place = course ? [course.university.city, course.university.country].filter(has).join(", ") : "";
  const label = "text-[11px] font-semibold uppercase tracking-[0.12em] text-[hsl(var(--sic-muted))]";
  const bandCols = BANDS.filter((b) => english.some((e) => e[b] != null));
  let n = 0;

  return (
    <div className="sic-degrees min-h-[100dvh] max-w-full overflow-x-hidden">
      <header className="sic-hero sic-contour text-white">
        <div className="mx-auto max-w-[1120px] px-4 sm:px-6 pt-4 pb-6">
          <nav className="flex flex-wrap items-center gap-x-1 gap-y-1 text-[13px]" aria-label="Breadcrumb">
            <Link href="/toolkit" className="inline-flex items-center gap-1.5 rounded-full px-2 py-1 -ml-2 text-white/75 hover:bg-white/10 hover:text-white" data-testid="link-back-toolkit"><ArrowLeft className="h-3.5 w-3.5" aria-hidden /> All courses</Link>
            {course && <><span className="text-white/35" aria-hidden>/</span>
              <Link href={`/toolkit/universities/${course.university.id}`} className="inline-flex min-w-0 items-center gap-1.5 rounded-full px-2 py-1 text-white/75 hover:bg-white/10 hover:text-white" data-testid="link-university-page"><Building2 className="h-3.5 w-3.5 shrink-0" aria-hidden /><span className="truncate max-w-[60vw] sm:max-w-none">{course.university.name}</span></Link></>}
          </nav>
          {course ? (
            <div className="mt-5 max-w-3xl sic-rise">
              {has(course.degree_level) && <p className="mb-2 inline-flex rounded-full bg-primary px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-primary-foreground">{course.degree_level}</p>}
              <h1 className="sic-display text-[1.375rem] leading-[1.2] sm:text-[1.75rem] lg:text-[2rem] font-semibold [text-wrap:balance] break-words" data-testid="text-course-name">{course.course_name}</h1>
              <p className="mt-1.5 text-sm text-white/75" data-testid="text-course-university">{course.university.name}{place ? `, ${place}` : ""}</p>
            </div>
          ) : state.status === "loading" ? (
            <div className="mt-5 space-y-2" aria-label="Loading course"><div className="h-4 w-20 animate-pulse rounded bg-white/15" /><div className="h-8 w-[28rem] max-w-full animate-pulse rounded-lg bg-white/15" /><div className="h-4 w-48 animate-pulse rounded bg-white/10" /></div>
          ) : null}
          {course && facts.length > 0 && (
            <dl className="mt-5 grid w-full gap-px overflow-hidden rounded-xl bg-white/10"
              style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 12rem), 1fr))" }}
              data-testid="list-course-facts">
              {facts.map(([k, v]) => (
                <div key={k} className="min-w-0 bg-[hsl(var(--sic-navy))]/40 px-4 py-2.5">
                  <dt className="text-[10px] font-semibold uppercase tracking-[0.14em] text-white/60">{k}</dt><dd className="mt-0.5 text-sm font-semibold break-words">{v}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-[1120px] px-4 sm:px-6 py-6 sm:py-8">
        {state.status === "loading" ? (
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px]" aria-busy="true">
            <div className="h-80 animate-pulse rounded-2xl bg-[hsl(var(--sic-mist))]" />
            <div className="h-48 animate-pulse rounded-2xl bg-[hsl(var(--sic-mist))]" />
          </div>
        ) : state.status === "error" ? (
          <div role="alert" className="rounded-xl border border-[hsl(var(--sic-line))] bg-white p-8 text-center">
            <p className="sic-display text-xl">This course could not be loaded</p>
            <button type="button" onClick={() => setAttempt((x) => x + 1)} className="mt-4 h-10 rounded-lg bg-[hsl(var(--sic-navy))] px-5 text-sm font-semibold text-white" data-testid="button-retry-course">Try again</button>
          </div>
        ) : state.status === "missing" ? (
          <div className="rounded-xl border border-dashed border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-10 text-center" data-testid="state-course-not-found">
            <p className="sic-display text-2xl">This course is not available</p>
            <p className="mt-2 text-sm text-[hsl(var(--sic-muted))]">It may have been withdrawn, or the link is incorrect.</p>
            <Link href="/toolkit" className="mt-4 inline-flex h-10 items-center rounded-lg border border-[hsl(var(--sic-navy))] px-5 text-sm font-semibold" data-testid="link-browse-courses">Browse courses</Link>
          </div>
        ) : course && (
          <div className="space-y-6">
            <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_300px]">
              <aside className="self-start lg:order-2 lg:sticky lg:top-6 space-y-3" aria-label="Course summary">
                <div className="overflow-hidden rounded-2xl border border-[hsl(var(--sic-line))] bg-white">
                  <div className="space-y-3 p-4">
                    {fees.length > 0 && <div><p className={label}>{fees.length > 1 ? "Fees from" : "Tuition fee"}</p>
                      <p className="mt-0.5 text-lg font-semibold tabular-nums leading-snug text-[hsl(var(--sic-navy))]" data-testid="text-summary-fee">{formatFeeLine(fees[0])}</p></div>}
                    {intakes.length > 0 && <div><p className={label}>Intakes</p>
                      <p className="mt-0.5 flex items-start gap-1.5 text-sm"><CalendarDays className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />{intakes.join(", ")}</p></div>}
                    {scholarships.length > 0 && <a href="#scholarships" className="block text-sm font-semibold text-primary hover:underline" data-testid="link-summary-scholarships">{scholarships.length} scholarship{scholarships.length === 1 ? "" : "s"} for this course</a>}
                  </div>
                  <div className="space-y-2 border-t border-[hsl(var(--sic-line))] bg-[hsl(var(--sic-mist))] p-4">
                    <button type="button" aria-pressed={shortlisted} onClick={() => shortlist.toggle(course as unknown as CourseResult)} data-testid="button-course-shortlist"
                      className={`inline-flex h-10 w-full items-center justify-center gap-2 rounded-lg text-sm font-semibold transition-colors ${shortlisted ? "border border-[hsl(var(--sic-navy))] bg-white text-[hsl(var(--sic-navy))]" : "bg-primary text-primary-foreground hover:opacity-90"}`}>
                      {shortlisted ? <BookmarkCheck className="h-4 w-4" aria-hidden /> : <Bookmark className="h-4 w-4" aria-hidden />}
                      {shortlisted ? "On your shortlist" : "Add to shortlist"}
                    </button>
                    <Link href={`/toolkit/universities/${course.university.id}`} className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-lg text-sm font-semibold text-[hsl(var(--sic-navy))] hover:bg-white" data-testid="link-summary-university">
                      More from this university <ArrowRight className="h-4 w-4" aria-hidden />
                    </Link>
                    <ReportCourseErrorButton key={course.id} courseId={course.id} courseName={course.course_name} universityName={course.university.name}
                      className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-lg text-xs font-semibold text-[hsl(var(--sic-muted))] hover:bg-white hover:text-[hsl(var(--sic-navy))]" />
                  </div>
                </div>
              </aside>

              <article className="min-w-0 space-y-5 rounded-2xl border border-[hsl(var(--sic-line))] bg-white p-4 sm:p-6 lg:order-1">
                {fees.length > 0 && (
                  <CourseSection id="fees" index={++n} title="Tuition fees">
                    <ul className="divide-y divide-[hsl(var(--sic-line))] rounded-xl border border-[hsl(var(--sic-line))]" data-testid="list-course-fees">
                      {fees.map((f, i) => (
                        <li key={i} className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 px-3.5 py-2.5">
                          {has(f.label) && fees.length > 1 && <span className="text-sm text-[hsl(var(--sic-muted))]">{f.label}</span>}
                          <span className="text-[15px] font-semibold tabular-nums text-[hsl(var(--sic-ink))]">{formatFeeLine(f)}</span>
                        </li>
                      ))}
                    </ul>
                    <p className="mt-2 text-xs text-[hsl(var(--sic-muted))]">International fees, in the currency and period published by the institution.</p>
                  </CourseSection>
                )}
                {(locations.length > 0 || intakes.length > 0) && (
                  <CourseSection id="where" index={++n} title="Where and when">
                    <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_auto]">
                      {locations.length > 0 && <div><p className={`${label} mb-2`}>Locations</p><LocationList places={locations} /></div>}
                      {intakes.length > 0 && <div><p className={`${label} mb-2`}>Intakes</p>
                        <ul className="flex flex-wrap gap-1.5" data-testid="list-course-intakes">{intakes.map((m) => <li key={m} className="rounded-md bg-[hsl(var(--sic-navy))] px-2.5 py-1 text-[13px] font-medium text-white">{m}</li>)}</ul></div>}
                    </div>
                  </CourseSection>
                )}
                {(english.length > 0 || academic.length > 0) && (
                  <CourseSection id="entry" index={++n} title="Entry requirements">
                    <div className="space-y-4">
                      {english.length > 0 && (
                        <div data-testid="section-english">
                          <p className={`${label} mb-1.5`}>English</p>
                          <div className="-mx-1 overflow-x-auto px-1">
                            <table className="w-full min-w-[320px] text-sm" data-testid="table-english">
                              <thead><tr className="text-left text-[11px] uppercase tracking-[0.1em] text-[hsl(var(--sic-muted))]">
                                <th className="py-1.5 pr-3 font-medium">Test</th><th className="py-1.5 pr-3 font-medium">Overall</th>
                                {bandCols.map((b) => <th key={b} className="py-1.5 pr-3 font-medium capitalize">{b}</th>)}
                              </tr></thead>
                              <tbody>
                                {english.map((e, i) => (
                                  <tr key={i} className="border-t border-[hsl(var(--sic-line))]" data-testid={`row-english-${i}`}>
                                    <td className="py-2.5 pr-3 font-semibold">{e.test_name || e.test_type || "English test"}</td>
                                    <td className="py-2.5 pr-3">{e.overall != null && <span className="inline-flex min-w-[2.5rem] justify-center rounded-md bg-[hsl(var(--sic-navy))] px-2 py-0.5 font-semibold tabular-nums text-white">{e.overall}</span>}</td>
                                    {bandCols.map((b) => <td key={b} className="py-2.5 pr-3 tabular-nums">{e[b] ?? ""}</td>)}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      )}
                      {academic.length > 0 && (
                        <div data-testid="section-academic">
                          <p className={`${label} mb-1.5`}>Academic</p>
                          <ul className="divide-y divide-[hsl(var(--sic-line))] rounded-xl border border-[hsl(var(--sic-line))]" data-testid="list-academic">
                            {academic.map((a, i) => (
                              <li key={i} className="flex flex-wrap items-baseline justify-between gap-2 px-3.5 py-2.5 text-sm">
                                {has(a.academic_level) && <span>{a.academic_level}</span>}
                                {a.academic_score != null && <span className="font-semibold tabular-nums">{a.academic_score}{has(a.score_type) ? ` ${a.score_type}` : ""}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </div>
                  </CourseSection>
                )}
                {scholarships.length > 0 && (
                  <CourseSection id="scholarships" index={++n} title="Scholarships">
                    <ScholarshipList items={scholarships} />
                  </CourseSection>
                )}
              </article>
            </div>
            <UniversityInsights compact hideEmpty key={course.university.id} universityId={course.university.id} universityName={course.university.name} />
            <p className="text-xs text-[hsl(var(--sic-muted))]">Details are reproduced from institution publications. Confirm fees and entry rules with the university before applying.</p>
          </div>
        )}
      </main>
    </div>
  );
}
